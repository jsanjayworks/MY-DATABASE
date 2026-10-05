from __future__ import annotations

import os
import struct
import zlib

from pydb.buffer_pool import BufferPool
from pydb.errors import PydbError
from pydb.pager import META_PAGE_ID, PAGE_SIZE

WAL_MAGIC = b"PYDBWAL\x00"
WAL_VERSION = 1

WAL_HEADER_FORMAT = ">8sHHIQ"
WAL_HEADER_SIZE = struct.calcsize(WAL_HEADER_FORMAT)

FRAME_HEADER_FORMAT = ">QIIB3x"
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)
FRAME_SIZE = FRAME_HEADER_SIZE + PAGE_SIZE

FLAG_COMMIT = 1


class WalError(PydbError):
    pass


class CorruptWalError(WalError):
    pass


class Wal:
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

        self._dirty: dict[int, None] = {}
        self._frees: list[int] = []
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

    def close(self) -> None:
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
        return bool(self._dirty) or bool(self._frees) or self.pager.flush_meta_pending

    def note_dirty(self, page_id: int) -> None:
        self._dirty[page_id] = None

    def holds_uncommitted(self, page_id: int) -> bool:
        return page_id in self._dirty

    def stage_free(self, page_id: int) -> None:
        if page_id in self._frees:
            raise WalError(f"page {page_id} is already staged to be freed")
        self._frees.append(page_id)

    def commit(self) -> int:
        self._require_open()
        if not self.in_transaction:
            return self.last_commit_lsn

        freed = self._apply_frees()
        pages = list(self._dirty)
        if self.pager.flush_meta_pending:
            pages.append(META_PAGE_ID)

        commit_lsn = self._append_frames(pages)
        self._file.flush()
        os.fsync(self._file.fileno())

        self._dirty.clear()
        self._frees.clear()
        for page_id in pages:
            if page_id != META_PAGE_ID:
                self.pool.flush_page(page_id)
        self.pager.flush_meta()
        for page_id in freed:
            self.pool.discard_page(page_id)
        self._meta_before = self.pager.meta_state()

        self.last_commit_lsn = commit_lsn
        self.commits += 1
        return commit_lsn

    def rollback(self) -> None:
        self._require_open()
        if not self.in_transaction:
            return
        for page_id in self._dirty:
            self.pool.discard_page(page_id)
        self.pager.restore_meta_state(self._meta_before)
        self._dirty.clear()
        self._frees.clear()
        self.pager.flush_meta()

    def savepoint(self) -> tuple:
        self._require_open()
        images = {}
        for page_id in self._dirty:
            data = self.pool.peek_page(page_id)
            assert data is not None, f"uncommitted page {page_id} was evicted"
            images[page_id] = bytes(data)
        return images, len(self._frees), self.pager.meta_state()

    def rollback_to(self, savepoint: tuple) -> None:
        self._require_open()
        images, frees, meta = savepoint
        for page_id in self._dirty:
            if page_id not in images:
                self.pool.discard_page(page_id)
        for page_id, image in images.items():
            self.pool.peek_page(page_id)[:] = image
        self._dirty = dict.fromkeys(images)
        del self._frees[frees:]
        self.pager.restore_meta_state(meta)

    def checkpoint(self) -> int:
        self._require_open()
        if self.in_transaction:
            raise WalError(
                "cannot checkpoint mid-transaction: commit or roll back first"
            )
        written = self.pool.flush_all()
        if self.pager.flush_meta():
            written += 1
        self.pager.sync()
        self._reset_log()
        self.checkpoints += 1
        return written

    def _recover(self) -> int:
        header = self._file.read(WAL_HEADER_SIZE)
        if len(header) < WAL_HEADER_SIZE:
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
        committed = 0
        expected_lsn = first_lsn
        while True:
            raw = self._file.read(FRAME_SIZE)
            if len(raw) < FRAME_SIZE:
                break
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
            self.pager.reload_meta()
            self.next_lsn = frames[committed - 1][0] + 1
            self.last_commit_lsn = frames[committed - 1][0]
        else:
            self.next_lsn = first_lsn
        self._reset_log()
        return committed

    def _append_frames(self, page_ids: list[int]) -> int:
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
        self._file.seek(0)
        self._file.truncate(0)
        self._write_header()
        self._file.flush()
        os.fsync(self._file.fileno())

    def _apply_frees(self) -> list[int]:
        freed = list(self._frees)
        for page_id in freed:
            data = self.pool.fetch_page(page_id)
            try:
                self.pager.stage_free(page_id, data)
            finally:
                self.pool.unpin_page(page_id, dirty=True)
        return freed

    def _require_open(self) -> None:
        if self._file is None:
            raise WalError("the log is closed")


def _frame_crc(lsn: int, page_id: int, flags: int, image: bytes) -> int:
    return zlib.crc32(struct.pack(">QIB", lsn, page_id, flags) + image)
