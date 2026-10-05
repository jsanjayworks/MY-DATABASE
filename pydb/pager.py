"""Layer 1: the pager.

The pager is the only component that touches the filesystem. It turns a single
file into an array of fixed-size pages, addressed by integer page id, and hands
out and reclaims those pages. Every layer above it -- records, B+Tree, WAL --
only ever says "give me page 7" and "here is the new page 7".

The on-disk byte layout is documented in NOTES.md.
"""

from __future__ import annotations

import os
import struct
import sys

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from pydb.errors import PydbError

PAGE_SIZE = 4096

MAGIC = b"PYDBFILE"
FORMAT_VERSION = 1

# Meta page (page 0): magic, version, page size, page count, free list head.
META_FORMAT = ">8sHHII"
META_SIZE = struct.calcsize(META_FORMAT)

# Page id 0 is the meta page, so 0 is never a valid allocation and is reused as
# the NULL page id (end of the free list).
META_PAGE_ID = 0
NULL_PAGE_ID = 0

# Bytes 20.. of the meta page are eight 8-byte slots for the layers above, which
# need somewhere durable to write a page id that moves. This is the registry of
# who owns which slot, kept here so two layers cannot claim the same one.
META_SLOT_FORMAT = ">Q"
META_SLOT_SIZE = struct.calcsize(META_SLOT_FORMAT)
META_SLOT_COUNT = 8

META_SLOT_ROOT = 0  # root page id of the top-level B+Tree (layer 7's catalog)

# A freed page stores the next free page id in its first four bytes.
FREE_NEXT_FORMAT = ">I"
FREE_NEXT_SIZE = struct.calcsize(FREE_NEXT_FORMAT)

ZERO_PAGE = bytes(PAGE_SIZE)

# The byte locked on Windows to say "this file is open". Windows locks are
# mandatory -- a locked byte cannot be read through any other handle -- so it is
# one no page can ever occupy: page ids are four bytes, so no file is longer than
# 2**32 pages. SQLite locks bytes past the data for the same reason.
LOCK_OFFSET = 2**32 * PAGE_SIZE


class PagerError(PydbError):
    """Base class for every error the pager raises."""


class CorruptFileError(PagerError):
    """The file on disk is not a pydb database, or its header is damaged."""


class FileInUseError(PagerError):
    """Something else already has the file open: another pager, or another process.

    Two at once is not a race to be managed but a guaranteed loss. Each one
    caches its own copy of the meta page and its own pages, so whichever writes
    last silently overwrites the other's commits.
    """


class Pager:
    """A fixed-size-page view over one database file.

    Open it directly or as a context manager::

        with Pager("my.db") as pager:
            pid = pager.allocate_page()
            pager.write_page(pid, b"hello")
            assert pager.read_page(pid)[:5] == b"hello"
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self.page_count = 1  # page 0 (meta) always exists
        self.free_list_head = NULL_PAGE_ID
        # The free list mirrored in memory. Walking the on-disk list to catch a
        # double free costs a page read per free page, which turns freeing a lot
        # of pages into a quadratic disk grind; a set makes the check O(1).
        self._free_set: set[int] = set()
        self._meta_slots = [0] * META_SLOT_COUNT
        # Layer 5 turns this on. While it is set the pager stops writing page 0
        # itself and only marks it dirty: the write-ahead log decides when the
        # meta page is safe to put on disk, because a torn meta page is one of
        # the few ways to lose a whole database at once.
        self.defer_meta = False
        self._meta_dirty = False
        self._file = None
        self._locked = False
        self._open()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> None:
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        # "r+b" needs the file to exist already; create it first if it does not.
        if is_new:
            open(self.path, "wb").close()
        self._file = open(self.path, "r+b", buffering=0)
        try:
            self._lock()
            if is_new:
                self._write_page_raw(META_PAGE_ID, ZERO_PAGE)
                self._write_meta()
            else:
                self._read_meta()
        except Exception:
            # A rejected header must not leave the handle dangling: __init__ is
            # about to raise, so nothing will ever call close().
            self._unlock()
            self._file.close()
            self._file = None
            raise

    def close(self) -> None:
        """Flush to disk and release the file handle. Safe to call twice."""
        if self._file is None:
            return
        self.sync()
        self._unlock()
        self._file.close()
        self._file = None

    def _lock(self) -> None:
        """Claim the file for this pager alone, or raise `FileInUseError`.

        On POSIX this has to be `flock`, not `fcntl`/`lockf`. Those locks belong
        to the *process*, so a second open in the same process would be granted
        the lock -- and closing either descriptor would drop it for both.
        `flock` belongs to the open file, so two pagers in one process conflict
        exactly as two in separate processes do. Either kind is released by the
        OS when a process dies, so a crash never leaves the file locked.
        """
        try:
            if sys.platform == "win32":
                self._file.seek(LOCK_OFFSET)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise FileInUseError(
                f"{self.path} is already open, in this process or another; "
                f"close it there first"
            ) from error
        self._locked = True

    def _unlock(self) -> None:
        if not self._locked:
            return
        if sys.platform == "win32":
            # Closing the handle would release it too, but Windows documents that
            # as happening "eventually"; a reopen right after close must not lose
            # that race.
            self._file.seek(LOCK_OFFSET)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._locked = False

    def sync(self) -> None:
        """Force everything written so far all the way down to the platter.

        Without this, a crash can lose writes the OS was still buffering. This
        is the single most important line in the whole storage layer.
        """
        self._require_open()
        self._file.flush()
        os.fsync(self._file.fileno())

    def __enter__(self) -> "Pager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self.page_count

    def __repr__(self) -> str:
        return (
            f"<Pager {self.path!r} pages={self.page_count} "
            f"free_head={self.free_list_head}>"
        )

    # ------------------------------------------------------------------
    # meta page
    # ------------------------------------------------------------------

    def _read_meta(self) -> None:
        header = self._read_page_raw(META_PAGE_ID)[:META_SIZE]
        magic, version, page_size, page_count, free_head = struct.unpack(
            META_FORMAT, header
        )
        if magic != MAGIC:
            raise CorruptFileError(f"{self.path}: not a pydb file (magic={magic!r})")
        if version != FORMAT_VERSION:
            raise CorruptFileError(
                f"{self.path}: format version {version}, this build expects "
                f"{FORMAT_VERSION}"
            )
        if page_size != PAGE_SIZE:
            raise CorruptFileError(
                f"{self.path}: written with page size {page_size}, this build uses "
                f"{PAGE_SIZE}"
            )
        if page_count < 1:
            raise CorruptFileError(f"{self.path}: impossible page count {page_count}")
        self.page_count = page_count
        self.free_list_head = free_head
        self._free_set = set(self.free_pages())  # one walk, at open
        self._meta_slots = list(
            struct.unpack_from(
                f">{META_SLOT_COUNT}Q", self._read_page_raw(META_PAGE_ID), META_SIZE
            )
        )

    def meta_image(self) -> bytes:
        """The meta page exactly as it should look on disk right now.

        Everything past the slots is read back from the file and preserved: the
        pager does not own those bytes and must not clobber them.
        """
        page = bytearray(self._read_page_raw(META_PAGE_ID))
        struct.pack_into(
            META_FORMAT,
            page,
            0,
            MAGIC,
            FORMAT_VERSION,
            PAGE_SIZE,
            self.page_count,
            self.free_list_head,
        )
        struct.pack_into(
            f">{META_SLOT_COUNT}Q", page, META_SIZE, *self._meta_slots
        )
        return bytes(page)

    def read_meta_slot(self, slot: int) -> int:
        """One of the meta page's durable 8-byte slots. 0 means "unset"."""
        self._check_slot(slot)
        return self._meta_slots[slot]

    def write_meta_slot(self, slot: int, value: int) -> None:
        """Set a meta slot. Deferred like every other meta change when logging."""
        self._check_slot(slot)
        if value < 0 or value >= 2 ** (8 * META_SLOT_SIZE):
            raise PagerError(f"meta slot value {value} does not fit in 8 bytes")
        self._meta_slots[slot] = value
        self._write_meta()

    def _check_slot(self, slot: int) -> None:
        if not isinstance(slot, int) or not 0 <= slot < META_SLOT_COUNT:
            raise PagerError(
                f"meta slot must be 0..{META_SLOT_COUNT - 1}, got {slot!r}"
            )

    def _write_meta(self) -> None:
        if self.defer_meta:
            self._meta_dirty = True
            return
        self._write_page_raw(META_PAGE_ID, self.meta_image())

    @property
    def flush_meta_pending(self) -> bool:
        """Whether a deferred meta-page write is waiting."""
        return self._meta_dirty

    def flush_meta(self) -> bool:
        """Write the deferred meta page. Returns whether there was one to write."""
        if not self._meta_dirty:
            return False
        self._write_page_raw(META_PAGE_ID, self.meta_image())
        self._meta_dirty = False
        return True

    def reload_meta(self) -> None:
        """Re-read page 0 from disk, discarding the in-memory copy.

        Recovery uses this: it rewrites page 0 from the log, so the pager's idea
        of the page count and the free list has to be thrown away and re-read.
        """
        self._meta_dirty = False
        self._read_meta()

    def meta_state(self) -> tuple:
        """A snapshot of everything `_write_meta` would persist, for rollback."""
        return (
            self.page_count,
            self.free_list_head,
            frozenset(self._free_set),
            tuple(self._meta_slots),
        )

    def restore_meta_state(self, state: tuple) -> None:
        """Put back a snapshot from `meta_state`.

        Undoes allocations, frees, and slot writes together -- which is what makes
        "the B+Tree root moved" a change a transaction can roll back.
        """
        page_count, free_head, free_set, slots = state
        self.page_count = page_count
        self.free_list_head = free_head
        self._free_set = set(free_set)
        self._meta_slots = list(slots)
        self._meta_dirty = True

    # ------------------------------------------------------------------
    # reading and writing pages
    # ------------------------------------------------------------------

    def read_page(self, page_id: int) -> bytearray:
        """Return a mutable copy of `page_id`.

        The copy is deliberate: mutating it does not touch the file until you
        pass it back to `write_page`. Layer 2 (the buffer pool) is where pages
        stop being copied and start being cached.
        """
        self._check_page_id(page_id)
        return bytearray(self._read_page_raw(page_id))

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        """Overwrite `page_id`. Data shorter than a page is zero-padded."""
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("page 0 is the meta page; it is managed by the pager")
        self._write_page_raw(page_id, data)

    def _read_page_raw(self, page_id: int) -> bytes:
        self._require_open()
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            # A short read means the page lies past the end of the file, which
            # is legitimate: allocate_page bumps page_count before the caller
            # has written anything. Unwritten pages read as zeros.
            data = data + bytes(PAGE_SIZE - len(data))
        return data

    def _write_page_raw(self, page_id: int, data: bytes | bytearray) -> None:
        self._require_open()
        if len(data) > PAGE_SIZE:
            raise PagerError(f"{len(data)} bytes will not fit in a {PAGE_SIZE}B page")
        if len(data) < PAGE_SIZE:
            data = bytes(data) + bytes(PAGE_SIZE - len(data))
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)

    # ------------------------------------------------------------------
    # allocation
    # ------------------------------------------------------------------

    def allocate_page(self) -> int:
        """Reserve a page and return its id. The page is zeroed."""
        self._require_open()
        if self.free_list_head != NULL_PAGE_ID:
            page_id = self.free_list_head
            self.free_list_head = self._next_free(page_id)
            self._free_set.discard(page_id)
        else:
            page_id = self.page_count
            self.page_count += 1
        if not self.defer_meta:
            # With a log attached, zeroing the page here would be an unlogged
            # write to the data file. The caller's first write covers it instead.
            self._write_page_raw(page_id, ZERO_PAGE)
        self._write_meta()
        return page_id

    def free_page(self, page_id: int) -> None:
        """Return `page_id` to the free list so a later allocate can reuse it."""
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("cannot free the meta page")
        if page_id in self._free_set:
            raise PagerError(f"page {page_id} is already free (double free)")
        # Push onto the head of the list: the freed page points at the old head.
        page = bytearray(PAGE_SIZE)
        struct.pack_into(FREE_NEXT_FORMAT, page, 0, self.free_list_head)
        self._write_page_raw(page_id, page)
        self.free_list_head = page_id
        self._free_set.add(page_id)
        self._write_meta()

    def stage_free(self, page_id: int, image: bytearray) -> None:
        """Free `page_id` by writing its free-list link into `image` in memory.

        `free_page` writes that link straight to the file, which is exactly what
        the write-ahead rule forbids: the link would reach the data file before
        the log record describing it. This version hands the bytes back to the
        caller instead, so a log can be written first and the page can go to disk
        with everything else at commit time.
        """
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("cannot free the meta page")
        if page_id in self._free_set:
            raise PagerError(f"page {page_id} is already free (double free)")
        if len(image) != PAGE_SIZE:
            raise PagerError(f"a page image is {PAGE_SIZE} bytes, got {len(image)}")
        image[:] = ZERO_PAGE
        struct.pack_into(FREE_NEXT_FORMAT, image, 0, self.free_list_head)
        self.free_list_head = page_id
        self._free_set.add(page_id)
        self._write_meta()

    def restore_page(self, page_id: int, image: bytes) -> None:
        """Overwrite any page, meta page included. **Recovery only.**

        `write_page` refuses page 0 on purpose, but replaying a log has to be able
        to put back the meta page -- that is the whole point of logging it.
        """
        if len(image) != PAGE_SIZE:
            raise PagerError(f"a page image is {PAGE_SIZE} bytes, got {len(image)}")
        self._write_page_raw(page_id, image)

    def free_pages(self) -> list[int]:
        """Walk the free list, head first. Useful in tests and debugging."""
        pages: list[int] = []
        seen: set[int] = set()
        page_id = self.free_list_head
        while page_id != NULL_PAGE_ID:
            if page_id in seen or page_id >= self.page_count:
                raise CorruptFileError(
                    f"free list is cyclic or out of range at page {page_id}"
                )
            seen.add(page_id)
            pages.append(page_id)
            page_id = self._next_free(page_id)
        return pages

    def _next_free(self, page_id: int) -> int:
        (next_id,) = struct.unpack_from(
            FREE_NEXT_FORMAT, self._read_page_raw(page_id), 0
        )
        return next_id

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._file is None:
            raise PagerError("pager is closed")

    def _check_page_id(self, page_id: int) -> None:
        if not isinstance(page_id, int) or isinstance(page_id, bool):
            raise TypeError(f"page id must be an int, got {type(page_id).__name__}")
        if page_id < 0 or page_id >= self.page_count:
            raise PagerError(
                f"page {page_id} is out of range (file has {self.page_count} pages)"
            )
