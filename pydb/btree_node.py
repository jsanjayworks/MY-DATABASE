"""Layer 4a: B+Tree node layout.

A node is one page holding a *sorted* run of variable-length cells. The physical
arrangement is the same trick layer 3 uses -- a pointer array growing forward
from the header, cell bytes growing backward from the end of the page -- but the
semantics are different in the one way that matters: **the pointer array is in
key order**. Inserting in the middle shifts a few bytes of pointer array rather
than kilobytes of cell data, and nothing else moves.

Two node types, distinguished by the page type byte:

* **Leaf** (type 3): cells are `(key, value)`. The header's spare 4 bytes hold
  `next_leaf`, which chains all the leaves left to right so a range scan never
  has to walk back up the tree.
* **Internal** (type 2): cells are `(key, child_page_id)`. The header's spare 4
  bytes hold the *leftmost* child, the one holding keys smaller than every
  separator in this node.

So an internal node with *n* cells has *n + 1* children, and the invariant is::

    child_at(0) < cells[0].key <= child_at(1) < cells[1].key <= child_at(2) ...

Keys are opaque byte strings compared with plain `<`, i.e. memcmp order. Making
integers sort correctly is the caller's job, and `pydb.record.encode_key` is how
it's done.

This module knows nothing about trees, the buffer pool, or page allocation: it is
one page's worth of bytes and the operations that keep them consistent. The tree
itself is in `pydb/btree.py`.

The byte layout is documented in NOTES.md.
"""

from __future__ import annotations

import struct

from pydb.pager import PAGE_SIZE

PAGE_TYPE_INTERNAL = 2
PAGE_TYPE_LEAF = 3

# page_type, reserved, cell_count, cell_start, frag_bytes, extra
HEADER_FORMAT = ">BBHHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 12

POINTER_FORMAT = ">HH"  # (offset, length) of one cell
POINTER_SIZE = struct.calcsize(POINTER_FORMAT)

KEY_LEN_FORMAT = ">H"
KEY_LEN_SIZE = struct.calcsize(KEY_LEN_FORMAT)
CHILD_FORMAT = ">I"
CHILD_SIZE = struct.calcsize(CHILD_FORMAT)

USABLE = PAGE_SIZE - HEADER_SIZE

# These two constants are a pair, and the arithmetic relating them is the whole
# reason the tree's fill invariant is checkable. Working backwards:
#
# A node splits when a cell will not fit, so the cells being divided total
# T > USABLE. The cut lands on a cell boundary at or just past T/2, which puts
# the smaller half above T/2 - (one cell) -- and an internal split also gives a
# cell away to the parent. So with a cell capped at an eighth of a page:
#
#     smaller half > 4085/2 - 510 - 510 = 1022 bytes
#
# Anything at or below that is a fill threshold a fresh split cannot violate,
# and a fifth of the page (816) clears it comfortably. The cap does double duty:
# `USABLE - MIN_USED` is far more than one cell, so a node below the threshold
# always has room for a whole cell from its sibling, which is what makes
# rebalancing always possible.
MAX_CELL_SIZE = USABLE // 8 - POINTER_SIZE
MAX_KEY_SIZE = MAX_CELL_SIZE - KEY_LEN_SIZE - CHILD_SIZE
MIN_USED = USABLE // 5


class NodeError(Exception):
    """A node's bytes are inconsistent, or an index is out of range."""


class CellTooLargeError(NodeError):
    """This key/value pair cannot fit in a node, so it cannot be stored at all.

    There are no overflow pages. A big value belongs in the heap with the tree
    holding its row id.
    """


# ----------------------------------------------------------------------
# cell encoding
#
# Both cell types start with a length-prefixed key, so `cell_key` works on
# either one and the tree can compare keys without caring which kind of node it
# is looking at.
# ----------------------------------------------------------------------


def leaf_cell(key: bytes, value: bytes) -> bytes:
    """`key_len | key | value` -- the value's length is the rest of the cell."""
    return struct.pack(KEY_LEN_FORMAT, len(key)) + key + value


def internal_cell(key: bytes, child: int) -> bytes:
    """`key_len | key | child_page_id`."""
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
    """How much value can accompany `key` in one leaf cell."""
    return MAX_CELL_SIZE - KEY_LEN_SIZE - len(key)


class Node:
    """One B+Tree node: a sorted array of cells inside a single page buffer.

    Instances are views over a live buffer-pool frame. Every mutating method
    changes the cached page, so the caller unpins `dirty=True`.
    """

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

    # ------------------------------------------------------------------
    # header
    # ------------------------------------------------------------------

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
        """Offset of the lowest cell byte; cells occupy `cell_start`..4096."""
        return struct.unpack_from(">H", self.data, 4)[0]

    @property
    def frag_bytes(self) -> int:
        """Dead bytes stranded inside the cell area, reclaimable by `defragment`."""
        return struct.unpack_from(">H", self.data, 6)[0]

    @property
    def extra(self) -> int:
        """`next_leaf` on a leaf, leftmost child on an internal node."""
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

    # ------------------------------------------------------------------
    # space accounting
    # ------------------------------------------------------------------

    @property
    def pointer_end(self) -> int:
        return HEADER_SIZE + self.cell_count * POINTER_SIZE

    @property
    def free_space(self) -> int:
        """Contiguous free bytes between the pointer array and the cell area."""
        return self.cell_start - self.pointer_end

    @property
    def used_bytes(self) -> int:
        """Bytes this node's cells and pointers really occupy."""
        return USABLE - self.free_space - self.frag_bytes

    @property
    def is_underfull(self) -> bool:
        """Below the fill threshold. Meaningless for a root, which may be nearly
        empty and has no sibling to borrow from."""
        return self.used_bytes < MIN_USED

    def has_room_for(self, cell_size: int) -> bool:
        return self.free_space + self.frag_bytes >= cell_size + POINTER_SIZE

    def can_absorb(self, other: "Node", extra: int = 0) -> bool:
        """Whether every cell of `other` (plus `extra` bytes) would fit in here."""
        return self.used_bytes + other.used_bytes + extra <= USABLE

    # ------------------------------------------------------------------
    # reading cells
    # ------------------------------------------------------------------

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
        """The child of cell `index`, i.e. the subtree holding keys >= its key."""
        self._require_internal()
        return cell_child(self.cell(index))

    def child_at(self, index: int) -> int:
        """Child number `index`, from 0 (leftmost) to `cell_count` inclusive."""
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

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def search(self, key: bytes) -> tuple[int, bool]:
        """Binary search. Returns `(index, found)`.

        When found, `index` is the cell holding `key`. When not, `index` is where
        the cell would go to keep the node sorted -- which is exactly what
        `insert_cell` wants.
        """
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
        """Which child of this internal node `key` belongs in.

        A key equal to a separator goes *right*, which is what makes the
        separator the smallest key of the subtree it points at.
        """
        index, found = self.search(key)
        return index + 1 if found else index

    # ------------------------------------------------------------------
    # mutating cells
    # ------------------------------------------------------------------

    def insert_cell(self, index: int, cell: bytes) -> bool:
        """Insert `cell` at `index`, shifting later pointers right.

        Returns False if the node is too full, in which case nothing changed and
        the caller has to split.
        """
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
        """Delete cell `index`, shifting later pointers left."""
        offset, length = self._pointer(index)
        start = HEADER_SIZE + index * POINTER_SIZE
        end = self.pointer_end
        self.data[start : end - POINTER_SIZE] = self.data[start + POINTER_SIZE : end]
        self._set(2, self.cell_count - 1)
        if offset == self.cell_start:
            self._set(4, offset + length)  # it was the lowest cell: reclaim directly
        else:
            self._set(6, self.frag_bytes + length)

    def set_cell(self, index: int, cell: bytes) -> bool:
        """Replace cell `index`. Returns False (changing nothing) if it won't fit.

        Same length is the common case -- an updated value of the same size, or a
        separator key being rewritten -- and costs one memcpy.
        """
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
        """Rewrite this node from scratch to hold exactly `cells`.

        Splits and merges use this: rebuilding a page from a list is far harder
        to get subtly wrong than shuffling cells in place, and it happens once
        per split rather than once per insert.
        """
        total = sum(len(c) + POINTER_SIZE for c in cells)
        if total > USABLE:
            raise NodeError(f"{len(cells)} cells need {total} bytes, page has {USABLE}")
        keep_extra = self.extra if extra is None else extra
        struct.pack_into(
            HEADER_FORMAT, self.data, 0, self.page_type, 0, 0, PAGE_SIZE, 0, keep_extra
        )
        # Zero the body so no stale cell bytes linger in the file.
        self.data[HEADER_SIZE:] = bytes(PAGE_SIZE - HEADER_SIZE)
        for cell in cells:
            appended = self.append_cell(cell)
            assert appended, "space was checked up front"

    def defragment(self) -> int:
        """Pack the cells against the end of the page, reclaiming dead bytes."""
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

    # ------------------------------------------------------------------
    # splitting
    # ------------------------------------------------------------------

    def split_index(self, cells: list[bytes]) -> int:
        """Where to cut `cells` so both halves are about half a page.

        By bytes, not by count: with variable-length cells, splitting down the
        middle of the *list* can leave one half nearly empty.
        """
        total = sum(len(c) + POINTER_SIZE for c in cells)
        half = total // 2
        running = 0
        for index, cell in enumerate(cells):
            running += len(cell) + POINTER_SIZE
            if running >= half:
                # Never return 0 or len(cells): both halves need a cell, and an
                # internal split also has to promote one.
                return min(max(index + 1, 1), len(cells) - 1)
        return len(cells) - 1

    # ------------------------------------------------------------------
    # invariants
    # ------------------------------------------------------------------

    def verify(self) -> None:
        """Raise `NodeError` unless this page is internally consistent."""
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
