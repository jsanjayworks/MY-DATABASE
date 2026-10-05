"""Layer 3b: the slotted page.

A page is 4096 fixed bytes; a row is however long its text happens to be. The
slotted page is the standard answer to that mismatch, and it is worth
understanding because every real database uses some version of it.

    +--------+----------------+--------------------+---------------------+
    | header | slot array --> |     free space     | <-- rows (data)     |
    +--------+----------------+--------------------+---------------------+
    0        12                                                      4096

The slot array grows forward from the header, the rows grow backward from the
end of the page, and the hole in the middle is the free space. Two consequences,
both of which the rest of the database depends on:

* A row is addressed by its *slot index*, not by its offset. Rows can be shuffled
  around inside the page during compaction without any pointer to them changing.
* Deleting a row only clears its slot. The bytes stay where they are until the
  page needs the space, at which point `compact` squeezes them out.

This class is a view over a buffer -- almost always a live buffer-pool frame, so
every mutating call changes the cached page directly and the caller is
responsible for unpinning it `dirty=True`.

The byte layout is documented in NOTES.md.
"""

from __future__ import annotations

import struct

from pydb.errors import PydbError
from pydb.pager import PAGE_SIZE

# page_type, reserved, slot_count, free_end, live_count, next_page
HEADER_FORMAT = ">BBHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 12

SLOT_FORMAT = ">HH"  # (offset, length) of one row
SLOT_SIZE = struct.calcsize(SLOT_FORMAT)

PAGE_TYPE_HEAP = 1

# A slot with offset 0 is a tombstone: offset 0 is inside the header, so it can
# never be a real row.
DEAD_SLOT = (0, 0)

# The biggest row that can ever be stored: a page with one slot and nothing else.
MAX_ROW_SIZE = PAGE_SIZE - HEADER_SIZE - SLOT_SIZE


class SlottedPageError(PydbError):
    """The page's own bytes are inconsistent, or a slot index is wrong."""


class NoRoomError(SlottedPageError):
    """This page cannot hold the row. The caller should try another page."""


class RowTooLargeError(SlottedPageError):
    """No page could ever hold this row.

    There are no overflow pages in this database, so a single row is hard-capped
    at `MAX_ROW_SIZE` bytes. Growing past that would mean a chain of overflow
    pages per value, which is a layer of its own.
    """


class SlottedPage:
    """Slot-addressed storage for variable-length rows inside one page."""

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
        """Format a zeroed page: no slots, all space free, `free_end` at the top."""
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
        """The number of live rows."""
        return self.live_count

    # ------------------------------------------------------------------
    # header fields
    # ------------------------------------------------------------------

    @property
    def page_type(self) -> int:
        return self.data[0]

    @property
    def slot_count(self) -> int:
        """Slots that exist, including tombstones."""
        return struct.unpack_from(">H", self.data, 2)[0]

    @property
    def free_end(self) -> int:
        """Offset of the lowest row; free space runs from `free_start` to here."""
        return struct.unpack_from(">H", self.data, 4)[0]

    @property
    def live_count(self) -> int:
        return struct.unpack_from(">H", self.data, 6)[0]

    @property
    def next_page(self) -> int:
        """Next page in the heap chain, or 0 for the end."""
        return struct.unpack_from(">I", self.data, 8)[0]

    @next_page.setter
    def next_page(self, page_id: int) -> None:
        struct.pack_into(">I", self.data, 8, page_id)

    @property
    def free_start(self) -> int:
        """First byte past the slot array."""
        return HEADER_SIZE + self.slot_count * SLOT_SIZE

    @property
    def free_space(self) -> int:
        """Contiguous free bytes in the middle of the page."""
        return self.free_end - self.free_start

    @property
    def dead_space(self) -> int:
        """Bytes held by deleted rows, reclaimable by `compact`."""
        used = sum(length for _, length in self._live_slots())
        return PAGE_SIZE - self.free_end - used

    def _set(self, offset: int, value: int) -> None:
        struct.pack_into(">H", self.data, offset, value)

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------

    def insert(self, row: bytes) -> int:
        """Store `row` and return its slot index.

        Raises `NoRoomError` if this page is too full (try another page) or
        `RowTooLargeError` if no page could ever hold it.
        """
        if len(row) > MAX_ROW_SIZE:
            raise RowTooLargeError(
                f"row is {len(row)} bytes; the limit is {MAX_ROW_SIZE} and there "
                f"are no overflow pages"
            )
        slot = self._find_dead_slot()
        # A brand new slot costs four bytes of slot array on top of the row.
        need = len(row) if slot is not None else len(row) + SLOT_SIZE
        if self.free_space < need:
            if self.free_space + self.dead_space < need:
                raise NoRoomError(
                    f"row needs {need} bytes, page has {self.free_space} free "
                    f"and {self.dead_space} reclaimable"
                )
            self.compact()  # tombstoned slots survive compaction, so `slot` still holds
        if slot is None:
            slot = self.slot_count
            self._set(2, slot + 1)
        self._write_row(slot, row)
        self._set(6, self.live_count + 1)
        return slot

    def read(self, slot: int) -> bytes:
        """The bytes of the row in `slot`."""
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is deleted")
        return bytes(self.data[offset : offset + length])

    def delete(self, slot: int) -> None:
        """Tombstone `slot`. The row's bytes stay until the next `compact`."""
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is already deleted")
        self._write_slot(slot, *DEAD_SLOT)
        self._set(6, self.live_count - 1)

    def replace(self, slot: int, row: bytes) -> bool:
        """Overwrite the row in `slot`, keeping its slot index if possible.

        Returns False if the new row does not fit in this page, in which case the
        old row is left untouched and the caller must move the row to another
        page (which changes its row id).
        """
        offset, length = self._slot(slot)
        if (offset, length) == DEAD_SLOT:
            raise KeyError(f"slot {slot} is deleted")
        if len(row) > MAX_ROW_SIZE:
            raise RowTooLargeError(f"row is {len(row)} bytes, limit is {MAX_ROW_SIZE}")
        if len(row) == length:
            self.data[offset : offset + length] = row  # the easy case
            return True
        if self.free_space < len(row):
            # The row being replaced is itself reclaimable space.
            if self.free_space + self.dead_space + length < len(row):
                return False
            self.delete(slot)
            self.compact()
            self._set(6, self.live_count + 1)
        self._write_row(slot, row)
        return True

    def slots(self) -> list[int]:
        """Live slot indices, in slot order."""
        return [
            slot
            for slot in range(self.slot_count)
            if self._slot(slot) != DEAD_SLOT
        ]

    def rows(self) -> list[tuple[int, bytes]]:
        """Every live `(slot, row_bytes)` pair, in slot order."""
        out = []
        for slot in range(self.slot_count):
            offset, length = self._slot(slot)
            if (offset, length) != DEAD_SLOT:
                out.append((slot, bytes(self.data[offset : offset + length])))
        return out

    def is_deleted(self, slot: int) -> bool:
        return self._slot(slot) == DEAD_SLOT

    # ------------------------------------------------------------------
    # compaction
    # ------------------------------------------------------------------

    def compact(self) -> int:
        """Squeeze out deleted rows, returning the bytes reclaimed.

        Rows are rewritten packed against the end of the page. Slot indices do
        not change -- that is the whole reason rows are addressed by slot -- so
        no row id anywhere in the database is invalidated.
        """
        before = self.free_space
        live = self.rows()
        cursor = PAGE_SIZE
        for slot, row in reversed(live):
            # Walking backwards keeps rows in roughly slot order on the page,
            # which makes hex dumps readable and scans sequential.
            cursor -= len(row)
            self.data[cursor : cursor + len(row)] = row
            self._write_slot(slot, cursor, len(row))
        # Everything from free_start to the new cursor is now free; zero it so a
        # deleted row's bytes do not linger in the file.
        self.data[self.free_start : cursor] = bytes(cursor - self.free_start)
        self._set(4, cursor)
        return self.free_space - before

    # ------------------------------------------------------------------
    # invariants
    # ------------------------------------------------------------------

    def verify(self) -> None:
        """Raise `SlottedPageError` unless this page is internally consistent.

        Cheap enough to call after every mutation in tests, which is exactly
        where a corrupted page should be caught.
        """
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
        """The lowest tombstoned slot, so slot indices get reused before growing.

        A reused slot means a row id that was deleted can start resolving to a
        different row. A row id is only meaningful while its row is alive, the
        same way a pointer is only meaningful before the free.
        """
        for slot in range(self.slot_count):
            if self._slot(slot) == DEAD_SLOT:
                return slot
        return None

    def _write_row(self, slot: int, row: bytes) -> None:
        offset = self.free_end - len(row)
        self.data[offset : offset + len(row)] = row
        self._set(4, offset)
        self._write_slot(slot, offset, len(row))
