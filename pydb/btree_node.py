from __future__ import annotations

import struct

from pydb.errors import PydbError
from pydb.pager import PAGE_SIZE

PAGE_TYPE_INTERNAL = 2
PAGE_TYPE_LEAF = 3

HEADER_FORMAT = ">BBHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 12

POINTER_FORMAT = ">HH"
POINTER_SIZE = struct.calcsize(POINTER_FORMAT)

KEY_LEN_FORMAT = ">H"
KEY_LEN_SIZE = struct.calcsize(KEY_LEN_FORMAT)
CHILD_FORMAT = ">I"
CHILD_SIZE = struct.calcsize(CHILD_FORMAT)

USABLE = PAGE_SIZE - HEADER_SIZE

MAX_CELL_SIZE = USABLE // 8 - POINTER_SIZE
MAX_KEY_SIZE = MAX_CELL_SIZE - KEY_LEN_SIZE - CHILD_SIZE
MIN_USED = USABLE // 5


class NodeError(PydbError):
    pass


class CellTooLargeError(NodeError):
    pass


def leaf_cell(key: bytes, value: bytes) -> bytes:
    return struct.pack(KEY_LEN_FORMAT, len(key)) + key + value


def internal_cell(key: bytes, child: int) -> bytes:
    return (
        struct.pack(KEY_LEN_FORMAT, len(key)) + key + struct.pack(CHILD_FORMAT, child)
    )


def cell_key(cell: bytes) -> bytes:
    (length,) = struct.unpack_from(KEY_LEN_FORMAT, cell, 0)
    return bytes(cell[KEY_LEN_SIZE : KEY_LEN_SIZE + length])


def cell_value(cell: bytes) -> bytes:
    (length,) = struct.unpack_from(KEY_LEN_FORMAT, cell, 0)
    return bytes(cell[KEY_LEN_SIZE + length :])


def cell_child(cell: bytes) -> int:
    (length,) = struct.unpack_from(KEY_LEN_FORMAT, cell, 0)
    return struct.unpack_from(CHILD_FORMAT, cell, KEY_LEN_SIZE + length)[0]


def max_value_size(key: bytes) -> int:
    return MAX_CELL_SIZE - KEY_LEN_SIZE - len(key)


class Node:
    __slots__ = ("data", "page_id")

    def __init__(self, data: bytearray, page_id: int = 0) -> None:
        if len(data) != PAGE_SIZE:
            raise NodeError(f"a node is {PAGE_SIZE} bytes, got {len(data)}")
        self.data = data
        self.page_id = page_id
        if self.page_type not in (PAGE_TYPE_INTERNAL, PAGE_TYPE_LEAF):
            raise NodeError(
                f"page {page_id} has type {self.page_type}, which is not a B+Tree "
                f"node (an uninitialised page reads as 0)"
            )

    @classmethod
    def new_leaf(cls, data: bytearray, page_id: int, next_leaf: int = 0) -> "Node":
        struct.pack_into(
            HEADER_FORMAT, data, 0, PAGE_TYPE_LEAF, 0, 0, PAGE_SIZE, 0, next_leaf
        )
        return cls(data, page_id)

    @classmethod
    def new_internal(cls, data: bytearray, page_id: int, leftmost: int) -> "Node":
        struct.pack_into(
            HEADER_FORMAT, data, 0, PAGE_TYPE_INTERNAL, 0, 0, PAGE_SIZE, 0, leftmost
        )
        return cls(data, page_id)

    def __repr__(self) -> str:
        kind = "leaf" if self.is_leaf else "internal"
        return (
            f"<{kind} page {self.page_id}: {self.cell_count} cells, "
            f"{self.used_bytes}/{USABLE} bytes used>"
        )

    def __len__(self) -> int:
        return self.cell_count

    @property
    def page_type(self) -> int:
        return self.data[0]

    @property
    def is_leaf(self) -> bool:
        return self.data[0] == PAGE_TYPE_LEAF

    @property
    def is_internal(self) -> bool:
        return self.data[0] == PAGE_TYPE_INTERNAL

    @property
    def cell_count(self) -> int:
        return struct.unpack_from(">H", self.data, 2)[0]

    @property
    def cell_start(self) -> int:
        return struct.unpack_from(">H", self.data, 4)[0]

    @property
    def frag_bytes(self) -> int:
        return struct.unpack_from(">H", self.data, 6)[0]

    @property
    def extra(self) -> int:
        return struct.unpack_from(">I", self.data, 8)[0]

    @extra.setter
    def extra(self, value: int) -> None:
        struct.pack_into(">I", self.data, 8, value)

    @property
    def next_leaf(self) -> int:
        self._require_leaf()
        return self.extra

    @next_leaf.setter
    def next_leaf(self, page_id: int) -> None:
        self._require_leaf()
        self.extra = page_id

    @property
    def leftmost_child(self) -> int:
        self._require_internal()
        return self.extra

    @leftmost_child.setter
    def leftmost_child(self, page_id: int) -> None:
        self._require_internal()
        self.extra = page_id

    @property
    def pointer_end(self) -> int:
        return HEADER_SIZE + self.cell_count * POINTER_SIZE

    @property
    def free_space(self) -> int:
        return self.cell_start - self.pointer_end

    @property
    def used_bytes(self) -> int:
        return USABLE - self.free_space - self.frag_bytes

    @property
    def is_underfull(self) -> bool:
        return self.used_bytes < MIN_USED

    def has_room_for(self, cell_size: int) -> bool:
        return self.free_space + self.frag_bytes >= cell_size + POINTER_SIZE

    def can_absorb(self, other: "Node", extra: int = 0) -> bool:
        return self.used_bytes + other.used_bytes + extra <= USABLE

    def cell(self, index: int) -> bytes:
        offset, length = self._pointer(index)
        return bytes(self.data[offset : offset + length])

    def key(self, index: int) -> bytes:
        offset, _ = self._pointer(index)
        (length,) = struct.unpack_from(KEY_LEN_FORMAT, self.data, offset)
        start = offset + KEY_LEN_SIZE
        return bytes(self.data[start : start + length])

    def value(self, index: int) -> bytes:
        self._require_leaf()
        return cell_value(self.cell(index))

    def child(self, index: int) -> int:
        self._require_internal()
        return cell_child(self.cell(index))

    def child_at(self, index: int) -> int:
        self._require_internal()
        if index < 0 or index > self.cell_count:
            raise NodeError(
                f"child {index} out of range (node has {self.cell_count + 1})"
            )
        return self.leftmost_child if index == 0 else self.child(index - 1)

    def keys(self) -> list[bytes]:
        return [self.key(i) for i in range(self.cell_count)]

    def cells(self) -> list[bytes]:
        return [self.cell(i) for i in range(self.cell_count)]

    def items(self) -> list[tuple[bytes, bytes]]:
        self._require_leaf()
        return [(self.key(i), self.value(i)) for i in range(self.cell_count)]

    def search(self, key: bytes) -> tuple[int, bool]:
        low, high = 0, self.cell_count
        while low < high:
            mid = (low + high) // 2
            probe = self.key(mid)
            if probe == key:
                return mid, True
            if probe < key:
                low = mid + 1
            else:
                high = mid
        return low, False

    def find_child(self, key: bytes) -> int:
        index, found = self.search(key)
        return index + 1 if found else index

    def insert_cell(self, index: int, cell: bytes) -> bool:
        if len(cell) > MAX_CELL_SIZE:
            raise CellTooLargeError(
                f"cell is {len(cell)} bytes; a node caps a cell at {MAX_CELL_SIZE} "
                f"so that eight always fit"
            )
        if index < 0 or index > self.cell_count:
            raise NodeError(f"cannot insert at {index}: node has {self.cell_count}")
        if not self.has_room_for(len(cell)):
            return False
        if self.free_space < len(cell) + POINTER_SIZE:
            self.defragment()
        offset = self.cell_start - len(cell)
        self.data[offset : offset + len(cell)] = cell
        start = HEADER_SIZE + index * POINTER_SIZE
        end = self.pointer_end
        self.data[start + POINTER_SIZE : end + POINTER_SIZE] = self.data[start:end]
        struct.pack_into(POINTER_FORMAT, self.data, start, offset, len(cell))
        self._set(2, self.cell_count + 1)
        self._set(4, offset)
        return True

    def append_cell(self, cell: bytes) -> bool:
        return self.insert_cell(self.cell_count, cell)

    def remove_cell(self, index: int) -> None:
        offset, length = self._pointer(index)
        start = HEADER_SIZE + index * POINTER_SIZE
        end = self.pointer_end
        self.data[start : end - POINTER_SIZE] = self.data[start + POINTER_SIZE : end]
        self._set(2, self.cell_count - 1)
        if offset == self.cell_start:
            self._set(4, offset + length)
        else:
            self._set(6, self.frag_bytes + length)

    def set_cell(self, index: int, cell: bytes) -> bool:
        offset, length = self._pointer(index)
        if len(cell) == length:
            self.data[offset : offset + length] = cell
            return True
        if len(cell) > MAX_CELL_SIZE:
            raise CellTooLargeError(f"cell is {len(cell)} bytes, max {MAX_CELL_SIZE}")
        if self.free_space + self.frag_bytes + length < len(cell):
            return False
        self.remove_cell(index)
        inserted = self.insert_cell(index, cell)
        assert inserted, "the space check above guarantees this fits"
        return True

    def reset(self, cells: list[bytes], extra: int | None = None) -> None:
        total = sum(len(c) + POINTER_SIZE for c in cells)
        if total > USABLE:
            raise NodeError(f"{len(cells)} cells need {total} bytes, page has {USABLE}")
        keep_extra = self.extra if extra is None else extra
        struct.pack_into(
            HEADER_FORMAT, self.data, 0, self.page_type, 0, 0, PAGE_SIZE, 0, keep_extra
        )
        self.data[HEADER_SIZE:] = bytes(PAGE_SIZE - HEADER_SIZE)
        for cell in cells:
            appended = self.append_cell(cell)
            assert appended, "space was checked up front"

    def defragment(self) -> int:
        before = self.free_space
        cells = self.cells()
        cursor = PAGE_SIZE
        for index, cell in reversed(list(enumerate(cells))):
            cursor -= len(cell)
            self.data[cursor : cursor + len(cell)] = cell
            struct.pack_into(
                POINTER_FORMAT,
                self.data,
                HEADER_SIZE + index * POINTER_SIZE,
                cursor,
                len(cell),
            )
        self.data[self.pointer_end : cursor] = bytes(cursor - self.pointer_end)
        self._set(4, cursor)
        self._set(6, 0)
        return self.free_space - before

    def split_index(self, cells: list[bytes]) -> int:
        total = sum(len(c) + POINTER_SIZE for c in cells)
        half = total // 2
        running = 0
        for index, cell in enumerate(cells):
            running += len(cell) + POINTER_SIZE
            if running >= half:
                return min(max(index + 1, 1), len(cells) - 1)
        return len(cells) - 1

    def verify(self) -> None:
        if not HEADER_SIZE <= self.pointer_end <= self.cell_start <= PAGE_SIZE:
            raise NodeError(
                f"page {self.page_id}: pointer array ends at {self.pointer_end} "
                f"but cells start at {self.cell_start}"
            )
        spans: list[tuple[int, int]] = []
        for index in range(self.cell_count):
            offset, length = self._pointer(index)
            if offset < self.cell_start or offset + length > PAGE_SIZE:
                raise NodeError(
                    f"page {self.page_id} cell {index} spans "
                    f"[{offset}, {offset + length}), outside the cell area"
                )
            spans.append((offset, offset + length))
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:]):
            if start < end:
                raise NodeError(f"page {self.page_id}: cells overlap at {start}")
        occupied = sum(length for _, length in map(self._pointer, range(self.cell_count)))
        expected_frag = PAGE_SIZE - self.cell_start - occupied
        if expected_frag != self.frag_bytes:
            raise NodeError(
                f"page {self.page_id}: header claims {self.frag_bytes} dead bytes, "
                f"the cells account for {expected_frag}"
            )
        keys = self.keys()
        for left, right in zip(keys, keys[1:]):
            if left >= right:
                raise NodeError(
                    f"page {self.page_id}: keys out of order, {left!r} >= {right!r}"
                )

    def _set(self, offset: int, value: int) -> None:
        struct.pack_into(">H", self.data, offset, value)

    def _pointer(self, index: int) -> tuple[int, int]:
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(f"cell index must be an int, got {type(index).__name__}")
        if index < 0 or index >= self.cell_count:
            raise NodeError(
                f"cell {index} does not exist (page {self.page_id} has "
                f"{self.cell_count})"
            )
        return struct.unpack_from(
            POINTER_FORMAT, self.data, HEADER_SIZE + index * POINTER_SIZE
        )

    def _require_leaf(self) -> None:
        if not self.is_leaf:
            raise NodeError(f"page {self.page_id} is an internal node, not a leaf")

    def _require_internal(self) -> None:
        if not self.is_internal:
            raise NodeError(f"page {self.page_id} is a leaf, not an internal node")
