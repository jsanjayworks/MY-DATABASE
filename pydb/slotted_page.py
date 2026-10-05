from __future__ import annotations

import struct

from pydb.errors import PydbError
from pydb.pager import PAGE_SIZE

HEADER_FORMAT = ">BBHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 12

SLOT_FORMAT = ">HH"
SLOT_SIZE = struct.calcsize(SLOT_FORMAT)

PAGE_TYPE_HEAP = 1

DEAD_SLOT = (0, 0)

MAX_ROW_SIZE = PAGE_SIZE - HEADER_SIZE - SLOT_SIZE


class SlottedPageError(PydbError):
    pass


class NoRoomError(SlottedPageError):
    pass


class RowTooLargeError(SlottedPageError):
    pass


class SlottedPage:
    __slots__ = ("data",)

    def __init__(self, data: bytearray, *, expect_type: int = PAGE_TYPE_HEAP) -> None:
        if len(data) != PAGE_SIZE:
            raise SlottedPageError(f"a page is {PAGE_SIZE} bytes, got {len(data)}")
        self.data = data
        if self.page_type != expect_type:
            raise SlottedPageError(
                f"page type is {self.page_type}, expected {expect_type}; this is "
                f"not a slotted page (or it was never initialised)"
            )

    @classmethod
    def initialize(
        cls, data: bytearray, *, page_type: int = PAGE_TYPE_HEAP, next_page: int = 0
    ) -> "SlottedPage":
        struct.pack_into(
            HEADER_FORMAT, data, 0, page_type, 0, 0, PAGE_SIZE, 0, next_page
        )
        return cls(data, expect_type=page_type)

    def __repr__(self) -> str:
        return (
            f"<SlottedPage slots={self.slot_count} live={self.live_count} "
            f"free={self.free_space} next={self.next_page}>"
        )

    def __len__(self) -> int:
        return self.live_count

    @property
    def page_type(self) -> int:
        return self.data[0]

    @property
    def slot_count(self) -> int:
        return struct.unpack_from(">H", self.data, 2)[0]

    @property
    def free_end(self) -> int:
        return struct.unpack_from(">H", self.data, 4)[0]

    @property
    def live_count(self) -> int:
        return struct.unpack_from(">H", self.data, 6)[0]

    @property
    def next_page(self) -> int:
        return struct.unpack_from(">I", self.data, 8)[0]

    @next_page.setter
    def next_page(self, page_id: int) -> None:
        struct.pack_into(">I", self.data, 8, page_id)

    @property
    def free_start(self) -> int:
        return HEADER_SIZE + self.slot_count * SLOT_SIZE

    @property
    def free_space(self) -> int:
        return self.free_end - self.free_start

    @property
    def dead_space(self) -> int:
        used = sum(length for _, length in self._live_slots())
        return PAGE_SIZE - self.free_end - used

    def _set(self, offset: int, value: int) -> None:
        struct.pack_into(">H", self.data, offset, value)

    def insert(self, row: bytes) -> int:
        if len(row) > MAX_ROW_SIZE:
            raise RowTooLargeError(
                f"row is {len(row)} bytes; the limit is {MAX_ROW_SIZE} and there "
                f"are no overflow pages"
            )
        slot = self._find_dead_slot()
        need = len(row) if slot is not None else len(row) + SLOT_SIZE
        if self.free_space < need:
            if self.free_space + self.dead_space < need:
                raise NoRoomError(
                    f"row needs {need} bytes, page has {self.free_space} free "
                    f"and {self.dead_space} reclaimable"
                )
            self.compact()
        if slot is None:
            slot = self.slot_count
            self._set(2, slot + 1)
        self._write_row(slot, row)
        self._set(6, self.live_count + 1)
        return slot

    def read(self, slot: int) -> bytes:
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is deleted")
        return bytes(self.data[offset : offset + length])

    def delete(self, slot: int) -> None:
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is already deleted")
        self._write_slot(slot, *DEAD_SLOT)
        self._set(6, self.live_count - 1)

    def replace(self, slot: int, row: bytes) -> bool:
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is deleted")
        if len(row) > MAX_ROW_SIZE:
            raise RowTooLargeError(f"row is {len(row)} bytes, limit is {MAX_ROW_SIZE}")
        if len(row) == length:
            self.data[offset : offset + length] = row
            return True
        if self.free_space < len(row):
            if self.free_space + self.dead_space + length < len(row):
                return False
            self.delete(slot)
            self.compact()
            self._set(6, self.live_count + 1)
        self._write_row(slot, row)
        return True

    def slots(self) -> list[int]:
        return [
            slot
            for slot in range(self.slot_count)
            if self._slot(slot) != DEAD_SLOT
        ]

    def rows(self) -> list[tuple[int, bytes]]:
        out = []
        for slot in range(self.slot_count):
            offset, length = self._slot(slot)
            if (offset, length) != DEAD_SLOT:
                out.append((slot, bytes(self.data[offset : offset + length])))
        return out

    def is_deleted(self, slot: int) -> bool:
        return self._slot(slot) == DEAD_SLOT

    def compact(self) -> int:
        before = self.free_space
        live = self.rows()
        cursor = PAGE_SIZE
        for slot, row in reversed(live):
            cursor -= len(row)
            self.data[cursor : cursor + len(row)] = row
            self._write_slot(slot, cursor, len(row))
        self.data[self.free_start : cursor] = bytes(cursor - self.free_start)
        self._set(4, cursor)
        return self.free_space - before

    def verify(self) -> None:
        if not HEADER_SIZE <= self.free_start <= self.free_end <= PAGE_SIZE:
            raise SlottedPageError(
                f"free region is inside out: free_start={self.free_start} "
                f"free_end={self.free_end}"
            )
        live = 0
        occupied: list[tuple[int, int]] = []
        for slot in range(self.slot_count):
            offset, length = self._slot(slot)
            if (offset, length) == DEAD_SLOT:
                continue
            live += 1
            if offset < self.free_end or offset + length > PAGE_SIZE:
                raise SlottedPageError(
                    f"slot {slot} points at [{offset}, {offset + length}), "
                    f"outside the data region [{self.free_end}, {PAGE_SIZE})"
                )
            occupied.append((offset, offset + length))
        if live != self.live_count:
            raise SlottedPageError(
                f"header says {self.live_count} live rows, slots say {live}"
            )
        occupied.sort()
        for (_, end), (start, _) in zip(occupied, occupied[1:]):
            if start < end:
                raise SlottedPageError(f"rows overlap at offset {start}")

    def _slot_offset(self, slot: int) -> int:
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise TypeError(f"slot must be an int, got {type(slot).__name__}")
        if slot < 0 or slot >= self.slot_count:
            raise KeyError(
                f"slot {slot} does not exist (page has {self.slot_count} slots)"
            )
        return HEADER_SIZE + slot * SLOT_SIZE

    def _slot(self, slot: int) -> tuple[int, int]:
        return struct.unpack_from(SLOT_FORMAT, self.data, self._slot_offset(slot))

    def _write_slot(self, slot: int, offset: int, length: int) -> None:
        struct.pack_into(SLOT_FORMAT, self.data, self._slot_offset(slot), offset, length)

    def _live_slots(self) -> list[tuple[int, int]]:
        entries = (self._slot(s) for s in range(self.slot_count))
        return [e for e in entries if e != DEAD_SLOT]

    def _find_dead_slot(self) -> int | None:
        for slot in range(self.slot_count):
            if self._slot(slot) == DEAD_SLOT:
                return slot
        return None

    def _write_row(self, slot: int, row: bytes) -> None:
        offset = self.free_end - len(row)
        self.data[offset : offset + len(row)] = row
        self._set(4, offset)
        self._write_slot(slot, offset, len(row))
