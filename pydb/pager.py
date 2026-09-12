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

# A freed page stores the next free page id in its first four bytes.
FREE_NEXT_FORMAT = ">I"
FREE_NEXT_SIZE = struct.calcsize(FREE_NEXT_FORMAT)

ZERO_PAGE = bytes(PAGE_SIZE)


class PagerError(Exception):
    """Base class for every error the pager raises."""


class CorruptFileError(PagerError):
    """The file on disk is not a pydb database, or its header is damaged."""


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
        self._file = None
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
            if is_new:
                self._write_page_raw(META_PAGE_ID, ZERO_PAGE)
                self._write_meta()
            else:
                self._read_meta()
        except Exception:
            # A rejected header must not leave the handle dangling: __init__ is
            # about to raise, so nothing will ever call close().
            self._file.close()
            self._file = None
            raise

    def close(self) -> None:
        """Flush to disk and release the file handle. Safe to call twice."""
        if self._file is None:
            return
        self.sync()
        self._file.close()
        self._file = None

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

    def _write_meta(self) -> None:
        header = struct.pack(
            META_FORMAT,
            MAGIC,
            FORMAT_VERSION,
            PAGE_SIZE,
            self.page_count,
            self.free_list_head,
        )
        # Preserve the reserved tail of the meta page; later layers store the
        # B+Tree root and the WAL checkpoint there.
        page = bytearray(self._read_page_raw(META_PAGE_ID))
        page[:META_SIZE] = header
        self._write_page_raw(META_PAGE_ID, page)

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
        else:
            page_id = self.page_count
            self.page_count += 1
        self._write_page_raw(page_id, ZERO_PAGE)
        self._write_meta()
        return page_id

    def free_page(self, page_id: int) -> None:
        """Return `page_id` to the free list so a later allocate can reuse it."""
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("cannot free the meta page")
        if page_id in self.free_pages():
            raise PagerError(f"page {page_id} is already free (double free)")
        # Push onto the head of the list: the freed page points at the old head.
        page = bytearray(PAGE_SIZE)
        struct.pack_into(FREE_NEXT_FORMAT, page, 0, self.free_list_head)
        self._write_page_raw(page_id, page)
        self.free_list_head = page_id
        self._write_meta()

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
