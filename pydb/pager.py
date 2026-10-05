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

META_FORMAT = ">8sHHII"
META_SIZE = struct.calcsize(META_FORMAT)

META_PAGE_ID = 0
NULL_PAGE_ID = 0

META_SLOT_FORMAT = ">Q"
META_SLOT_SIZE = struct.calcsize(META_SLOT_FORMAT)
META_SLOT_COUNT = 8

META_SLOT_ROOT = 0

FREE_NEXT_FORMAT = ">I"
FREE_NEXT_SIZE = struct.calcsize(FREE_NEXT_FORMAT)

ZERO_PAGE = bytes(PAGE_SIZE)

LOCK_OFFSET = 2**32 * PAGE_SIZE


class PagerError(PydbError):
    pass


class CorruptFileError(PagerError):
    pass


class FileInUseError(PagerError):
    pass


class Pager:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self.page_count = 1
        self.free_list_head = NULL_PAGE_ID
        self._free_set: set[int] = set()
        self._meta_slots = [0] * META_SLOT_COUNT
        self.defer_meta = False
        self._meta_dirty = False
        self._file = None
        self._locked = False
        self._open()

    def _open(self) -> None:
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
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
            self._unlock()
            self._file.close()
            self._file = None
            raise

    def close(self) -> None:
        if self._file is None:
            return
        self.sync()
        self._unlock()
        self._file.close()
        self._file = None

    def _lock(self) -> None:
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
            self._file.seek(LOCK_OFFSET)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._locked = False

    def sync(self) -> None:
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
        self._free_set = set(self.free_pages())
        self._meta_slots = list(
            struct.unpack_from(
                f">{META_SLOT_COUNT}Q", self._read_page_raw(META_PAGE_ID), META_SIZE
            )
        )

    def meta_image(self) -> bytes:
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
        self._check_slot(slot)
        return self._meta_slots[slot]

    def write_meta_slot(self, slot: int, value: int) -> None:
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
        return self._meta_dirty

    def flush_meta(self) -> bool:
        if not self._meta_dirty:
            return False
        self._write_page_raw(META_PAGE_ID, self.meta_image())
        self._meta_dirty = False
        return True

    def reload_meta(self) -> None:
        self._meta_dirty = False
        self._read_meta()

    def meta_state(self) -> tuple:
        return (
            self.page_count,
            self.free_list_head,
            frozenset(self._free_set),
            tuple(self._meta_slots),
        )

    def restore_meta_state(self, state: tuple) -> None:
        page_count, free_head, free_set, slots = state
        self.page_count = page_count
        self.free_list_head = free_head
        self._free_set = set(free_set)
        self._meta_slots = list(slots)
        self._meta_dirty = True

    def read_page(self, page_id: int) -> bytearray:
        self._check_page_id(page_id)
        return bytearray(self._read_page_raw(page_id))

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("page 0 is the meta page; it is managed by the pager")
        self._write_page_raw(page_id, data)

    def _read_page_raw(self, page_id: int) -> bytes:
        self._require_open()
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
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

    def allocate_page(self) -> int:
        self._require_open()
        if self.free_list_head != NULL_PAGE_ID:
            page_id = self.free_list_head
            self.free_list_head = self._next_free(page_id)
            self._free_set.discard(page_id)
        else:
            page_id = self.page_count
            self.page_count += 1
        if not self.defer_meta:
            self._write_page_raw(page_id, ZERO_PAGE)
        self._write_meta()
        return page_id

    def free_page(self, page_id: int) -> None:
        self._check_page_id(page_id)
        if page_id == META_PAGE_ID:
            raise PagerError("cannot free the meta page")
        if page_id in self._free_set:
            raise PagerError(f"page {page_id} is already free (double free)")
        page = bytearray(PAGE_SIZE)
        struct.pack_into(FREE_NEXT_FORMAT, page, 0, self.free_list_head)
        self._write_page_raw(page_id, page)
        self.free_list_head = page_id
        self._free_set.add(page_id)
        self._write_meta()

    def stage_free(self, page_id: int, image: bytearray) -> None:
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
        if len(image) != PAGE_SIZE:
            raise PagerError(f"a page image is {PAGE_SIZE} bytes, got {len(image)}")
        self._write_page_raw(page_id, image)

    def free_pages(self) -> list[int]:
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
