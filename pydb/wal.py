"""Layer 5: the write-ahead log.

Everything below this layer is correct only if the process lives. A crash between
two page writes can leave a B+Tree with a separator pointing at a child that was
never written, or a meta page claiming pages that do not exist. This is where
that stops.

The rule, and it is the only rule: **nothing reaches the data file until the log
record describing it is durable on disk.** Then any crash is recoverable, because
the log always knows what the data file was supposed to look like.

What gets logged here is whole page images, the same choice SQLite's WAL makes.
It is fatter than logging "byte 91 of page 7 changed from 3 to 4", but replaying
it is *idempotent* -- applying the same frame twice is harmless -- so recovery is
a loop over the file with no undo pass and no subtle interactions. For a database
you are writing to understand databases, that trade is worth making twice.

How a commit actually goes, in order, because the order *is* the algorithm:

1. deferred page frees are applied in memory, so the freed pages and the meta
   page are part of what gets logged rather than sneaking out separately;
2. one frame per modified page is appended to the log, the last one flagged
   `commit`, and the log is **fsynced**. The transaction is durable at this
   instant and not one instruction earlier;
3. the modified pages are written to the data file -- unsynced, because the log
   already guarantees them;
4. the deferred meta page is written, for the same reason.

Two things make this safe that are easy to miss:

* The log is fsynced (step 2) *before* the data file is touched (step 3). Two
  separate fsyncs, in that order. One fsync of both would prove nothing.
* An uncommitted page may not be evicted from the buffer pool, or a rollback
  could no longer take it back. The pool asks `holds_uncommitted` before it
  writes anything out, which caps a transaction at roughly the pool size --
  a real limit, and the honest price of not writing an undo log too.

Recovery reads frames until one fails its checksum or breaks the LSN sequence
(that is where the crash was), applies every frame up to the last `commit` flag,
and throws the rest away. An interrupted transaction leaves no trace.

The log's byte layout is documented in NOTES.md.
"""

from __future__ import annotations

import os
import struct
import zlib

from pydb.buffer_pool import BufferPool
from pydb.pager import META_PAGE_ID, PAGE_SIZE

WAL_MAGIC = b"PYDBWAL\x00"
WAL_VERSION = 1

# magic, version, page size, reserved, first lsn in this generation
WAL_HEADER_FORMAT = ">8sHHIQ"
WAL_HEADER_SIZE = struct.calcsize(WAL_HEADER_FORMAT)

# lsn, page id, crc32, flags, padding
FRAME_HEADER_FORMAT = ">QIIB3x"
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)
FRAME_SIZE = FRAME_HEADER_SIZE + PAGE_SIZE

FLAG_COMMIT = 1  # this frame is the last of a committed transaction


class WalError(Exception):
    """Base class for log errors."""


class CorruptWalError(WalError):
    """The log file's header is not ours, or is for a different page size.

    A *torn* log is not corruption -- it is the expected shape of a crash, and
    recovery stops at the tear rather than complaining.
    """


class Wal:
    """A write-ahead log over a buffer pool, plus the transaction it is logging.

        >>> pool = BufferPool.open("my.db")
        >>> wal = Wal(pool)          # replays anything a previous crash left
        >>> page_id, page = pool.new_page()
        >>> page[:5] = b"hello"
        >>> pool.unpin_page(page_id, dirty=True)
        >>> wal.commit()             # durable here, and not before
        1

    Constructing a `Wal` recovers the database, so it has to happen before
    anything reads a page through the pool.
    """

    def __init__(self, pool: BufferPool, path: str | os.PathLike[str] | None = None):
        if len(pool) != 0:
            raise WalError(
                "attach the log to a fresh pool: recovery rewrites pages directly "
                f"and the pool already has {len(pool)} of them cached"
            )
        self.pool = pool
        self.pager = pool.pager
        self.path = os.fspath(path) if path is not None else self.pager.path + ".wal"
        self.next_lsn = 1
        self.last_commit_lsn = 0
        self.frames_written = 0
        self.commits = 0
        self.checkpoints = 0
        self.recovered_frames = 0

        self._dirty: dict[int, None] = {}  # pages this transaction touched, in order
        self._frees: list[int] = []  # frees waiting for commit
        # The pager's meta state as of the last commit. Captured when a
        # transaction *ends* rather than when one begins, which sidesteps the
        # question of exactly which call starts a transaction -- a page allocation
        # changes the meta page before the pool says a word about it.
        self._meta_before: tuple = ()
        self._file = None

        exists = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        self._file = open(self.path, "r+b" if exists else "w+b", buffering=0)
        try:
            if exists:
                self.recovered_frames = self._recover()
            else:
                self._write_header()
            self.pager.defer_meta = True
            self.pool.wal = self
            self._meta_before = self.pager.meta_state()
        except Exception:
            self._file.close()
            self._file = None
            raise

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Roll back anything open, checkpoint, and close the log. Safe twice."""
        if self._file is None:
            return
        if self.in_transaction:
            self.rollback()
        self.checkpoint()
        self._file.close()
        self._file = None
        self.pool.wal = None
        self.pager.defer_meta = False

    def __enter__(self) -> "Wal":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = (
            f"{len(self._dirty)} pages uncommitted" if self.in_transaction else "idle"
        )
        return f"<Wal {self.path!r} lsn={self.next_lsn} {state}>"

    @property
    def in_transaction(self) -> bool:
        """Whether anything is waiting to be committed.

        A deferred meta-page write counts: allocating a page or moving the B+Tree
        root changes the database even when no page in the pool is dirty.
        """
        return bool(self._dirty) or bool(self._frees) or self.pager.flush_meta_pending

    # ------------------------------------------------------------------
    # what the buffer pool calls
    # ------------------------------------------------------------------

    def note_dirty(self, page_id: int) -> None:
        """The pool telling us a page changed, which opens a transaction."""
        self._dirty[page_id] = None

    def holds_uncommitted(self, page_id: int) -> bool:
        """Whether writing `page_id` to the data file would break the log's rule."""
        return page_id in self._dirty

    def stage_free(self, page_id: int) -> None:
        """Remember that `page_id` should be freed when this transaction commits."""
        if page_id in self._frees:
            raise WalError(f"page {page_id} is already staged to be freed")
        self._frees.append(page_id)

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------

    def commit(self) -> int:
        """Make this transaction durable. Returns its commit LSN.

        A no-op with nothing to commit, so callers can commit unconditionally.
        """
        self._require_open()
        if not self.in_transaction:
            return self.last_commit_lsn

        self._apply_frees()
        pages = list(self._dirty)
        if self.pager.flush_meta_pending:
            # The meta page holds the page count and the free list, so an
            # allocation or a free has to be in the same commit group as the
            # pages that used it. A torn meta page loses the whole file.
            pages.append(META_PAGE_ID)

        commit_lsn = self._append_frames(pages)
        self._file.flush()
        os.fsync(self._file.fileno())  # fsync #1: the log. Everything rests on this.

        # Only now may any of it reach the data file.
        self._dirty.clear()
        self._frees.clear()
        for page_id in pages:
            if page_id != META_PAGE_ID:
                self.pool.flush_page(page_id)
        self.pager.flush_meta()
        self._meta_before = self.pager.meta_state()

        self.last_commit_lsn = commit_lsn
        self.commits += 1
        return commit_lsn

    def rollback(self) -> None:
        """Throw this transaction away, leaving the database as it was committed.

        There is no undo log: the last committed image of every page is already in
        the data file, because a page is written there only after it is committed
        and is never evicted before that. So undoing is simply forgetting -- drop
        the modified frames and let them be read again.
        """
        self._require_open()
        if not self.in_transaction:
            return
        for page_id in self._dirty:
            self.pool.discard_page(page_id)
        self.pager.restore_meta_state(self._meta_before)
        self._dirty.clear()
        self._frees.clear()
        # The restored state is what the data file already holds, so this write
        # changes nothing. It is here to clear the deferred-write flag honestly
        # rather than by poking at it.
        self.pager.flush_meta()

    # ------------------------------------------------------------------
    # checkpointing
    # ------------------------------------------------------------------

    def checkpoint(self) -> int:
        """Get the data file fully up to date, then start the log over.

        Until this runs, the log keeps growing and recovery keeps replaying from
        the beginning of it. Afterwards the data file stands on its own and the
        log is empty. Returns the number of pages written.
        """
        self._require_open()
        if self.in_transaction:
            raise WalError(
                "cannot checkpoint mid-transaction: commit or roll back first"
            )
        written = self.pool.flush_all()
        if self.pager.flush_meta():
            written += 1
        self.pager.sync()  # fsync #2: the data file, only ever after the log's
        self._reset_log()
        self.checkpoints += 1
        return written

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------

    def _recover(self) -> int:
        """Replay committed frames onto the data file. Returns how many.

        Reads forward until a frame is short, fails its checksum, or breaks the
        LSN sequence -- all three mean "the crash was here". Frames after the last
        `commit` flag belong to a transaction that never finished and are dropped.
        """
        header = self._file.read(WAL_HEADER_SIZE)
        if len(header) < WAL_HEADER_SIZE:
            # Nothing but a partial header: there is nothing to replay.
            self._write_header()
            return 0
        magic, version, page_size, _reserved, first_lsn = struct.unpack(
            WAL_HEADER_FORMAT, header
        )
        if magic != WAL_MAGIC:
            raise CorruptWalError(f"{self.path}: not a pydb log (magic={magic!r})")
        if version != WAL_VERSION:
            raise CorruptWalError(
                f"{self.path}: log version {version}, this build expects {WAL_VERSION}"
            )
        if page_size != PAGE_SIZE:
            raise CorruptWalError(
                f"{self.path}: log written with page size {page_size}, this build "
                f"uses {PAGE_SIZE}"
            )

        frames: list[tuple[int, int, bytes]] = []
        committed = 0  # how many frames are covered by a commit flag
        expected_lsn = first_lsn
        while True:
            raw = self._file.read(FRAME_SIZE)
            if len(raw) < FRAME_SIZE:
                break  # torn tail: the crash landed mid-append
            lsn, page_id, crc, flags = struct.unpack_from(FRAME_HEADER_FORMAT, raw, 0)
            image = raw[FRAME_HEADER_SIZE:]
            if lsn != expected_lsn or crc != _frame_crc(lsn, page_id, flags, image):
                break
            frames.append((lsn, page_id, image))
            expected_lsn += 1
            if flags & FLAG_COMMIT:
                committed = len(frames)

        for _lsn, page_id, image in frames[:committed]:
            self.pager.restore_page(page_id, image)
        if committed:
            self.pager.sync()
            self.pager.reload_meta()  # page 0 may have just been rewritten
            self.next_lsn = frames[committed - 1][0] + 1
            self.last_commit_lsn = frames[committed - 1][0]
        else:
            self.next_lsn = first_lsn
        self._reset_log()
        return committed

    # ------------------------------------------------------------------
    # the log file
    # ------------------------------------------------------------------

    def _append_frames(self, page_ids: list[int]) -> int:
        """Append one frame per page, flagging the last as the commit. Returns its LSN."""
        if not page_ids:
            raise WalError("a commit group needs at least one frame")
        buffer = bytearray()
        lsn = self.next_lsn
        for index, page_id in enumerate(page_ids):
            lsn = self.next_lsn + index
            flags = FLAG_COMMIT if index == len(page_ids) - 1 else 0
            image = self._image_of(page_id)
            buffer += struct.pack(
                FRAME_HEADER_FORMAT,
                lsn,
                page_id,
                _frame_crc(lsn, page_id, flags, image),
                flags,
            )
            buffer += image
        self._file.seek(0, os.SEEK_END)
        self._file.write(buffer)
        self.next_lsn = lsn + 1
        self.frames_written += len(page_ids)
        return lsn

    def _image_of(self, page_id: int) -> bytes:
        """The bytes that *should* be on disk for `page_id` once this commits."""
        if page_id == META_PAGE_ID:
            return self.pager.meta_image()
        cached = self.pool.peek_page(page_id)
        return bytes(cached) if cached is not None else bytes(
            self.pager.read_page(page_id)
        )

    def _write_header(self) -> None:
        self._file.seek(0)
        self._file.write(
            struct.pack(
                WAL_HEADER_FORMAT, WAL_MAGIC, WAL_VERSION, PAGE_SIZE, 0, self.next_lsn
            )
        )

    def _reset_log(self) -> None:
        """Empty the log, keeping LSNs moving forward across generations."""
        self._file.seek(0)
        self._file.truncate(0)
        self._write_header()
        self._file.flush()
        os.fsync(self._file.fileno())

    def _apply_frees(self) -> None:
        """Turn staged frees into page images, in memory, before anything is logged."""
        for page_id in self._frees:
            data = self.pool.fetch_page(page_id)
            try:
                self.pager.stage_free(page_id, data)
            finally:
                self.pool.unpin_page(page_id, dirty=True)

    def _require_open(self) -> None:
        if self._file is None:
            raise WalError("the log is closed")


def _frame_crc(lsn: int, page_id: int, flags: int, image: bytes) -> int:
    """Checksum the header fields *and* the image, so a torn header is caught too."""
    return zlib.crc32(struct.pack(">QIB", lsn, page_id, flags) + image)
