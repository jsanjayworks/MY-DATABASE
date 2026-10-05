from __future__ import annotations

from typing import Callable, Iterator

from pydb.btree_node import (
    MAX_KEY_SIZE,
    MIN_USED,
    POINTER_SIZE,
    CellTooLargeError,
    Node,
    cell_child,
    cell_key,
    internal_cell,
    leaf_cell,
    max_value_size,
)
from pydb.buffer_pool import BufferPool
from pydb.errors import PydbError
from pydb.pager import NULL_PAGE_ID


class BTreeError(PydbError):
    pass


class DuplicateKeyError(BTreeError):
    pass


class CorruptTreeError(BTreeError):
    pass


class BTree:
    def __init__(
        self,
        pool: BufferPool,
        root_page_id: int,
        on_root_change: Callable[[int], None] | None = None,
    ) -> None:
        self.pool = pool
        self.root_page_id = root_page_id
        self._on_root_change = on_root_change
        self._relaxed_fill = False
        self._shrank = False

    @classmethod
    def create(
        cls, pool: BufferPool, on_root_change: Callable[[int], None] | None = None
    ) -> "BTree":
        page_id, data = pool.new_page()
        try:
            Node.new_leaf(data, page_id)
        finally:
            pool.unpin_page(page_id, dirty=True)
        return cls(pool, page_id, on_root_change)

    def __repr__(self) -> str:
        return f"<BTree root={self.root_page_id} height={self.height()}>"

    def get(self, key: bytes, default: bytes | None = None) -> bytes | None:
        key = bytes(key)
        page_id = self._find_leaf(key)
        with self.pool.pinned(page_id) as data:
            node = Node(data, page_id)
            index, found = node.search(key)
            return node.value(index) if found else default

    def __contains__(self, key: bytes) -> bool:
        return self.get(key) is not None

    def __getitem__(self, key: bytes) -> bytes:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def items(
        self, start: bytes | None = None, stop: bytes | None = None
    ) -> Iterator[tuple[bytes, bytes]]:
        page_id = self._find_leaf(start) if start is not None else self._first_leaf()
        while page_id != NULL_PAGE_ID:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                pairs = node.items()
                page_id = node.next_leaf
            for key, value in pairs:
                if start is not None and key < start:
                    continue
                if stop is not None and key >= stop:
                    return
                yield key, value

    def keys(
        self, start: bytes | None = None, stop: bytes | None = None
    ) -> Iterator[bytes]:
        for key, _value in self.items(start, stop):
            yield key

    def __iter__(self) -> Iterator[bytes]:
        return self.keys()

    def count(self) -> int:
        total = 0
        page_id = self._first_leaf()
        while page_id != NULL_PAGE_ID:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                total += node.cell_count
                page_id = node.next_leaf
        return total

    def all_pages(self) -> list[int]:
        pages: list[int] = []
        pending = [self.root_page_id]
        while pending:
            page_id = pending.pop()
            pages.append(page_id)
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                if node.is_internal:
                    pending.extend(
                        node.child_at(index) for index in range(node.cell_count + 1)
                    )
        return pages

    def height(self) -> int:
        levels = 1
        page_id = self.root_page_id
        while True:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                if node.is_leaf:
                    return levels
                page_id = node.child_at(0)
            levels += 1

    def put(self, key: bytes, value: bytes, overwrite: bool = True) -> bool:
        key, value = bytes(key), bytes(value)
        if len(key) > MAX_KEY_SIZE:
            raise CellTooLargeError(
                f"key is {len(key)} bytes; the limit is {MAX_KEY_SIZE}"
            )
        if len(value) > max_value_size(key):
            raise CellTooLargeError(
                f"{len(key)}-byte key leaves room for {max_value_size(key)} bytes "
                f"of value, got {len(value)}; store the value in a heap and index "
                f"its row id instead"
            )
        self._shrank = False
        inserted, split = self._insert(self.root_page_id, key, value, overwrite)
        if split is not None:
            self._grow(*split)
        elif self._shrank:
            self._shrink()
        return inserted

    def insert(self, key: bytes, value: bytes) -> None:
        self.put(key, value, overwrite=False)

    def __setitem__(self, key: bytes, value: bytes) -> None:
        self.put(key, value)

    def delete(self, key: bytes) -> bool:
        removed = self._delete(self.root_page_id, bytes(key))
        if removed:
            self._shrink()
        return removed

    def __delitem__(self, key: bytes) -> None:
        if not self.delete(key):
            raise KeyError(key)

    def _find_leaf(self, key: bytes) -> int:
        page_id = self.root_page_id
        while True:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                if node.is_leaf:
                    return page_id
                page_id = node.child_at(node.find_child(key))

    def _first_leaf(self) -> int:
        page_id = self.root_page_id
        while True:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                if node.is_leaf:
                    return page_id
                page_id = node.child_at(0)

    def _insert(
        self, page_id: int, key: bytes, value: bytes, overwrite: bool
    ) -> tuple[bool, tuple[bytes, int] | None]:
        data = self.pool.fetch_page(page_id)
        dirty = False
        try:
            node = Node(data, page_id)
            if node.is_leaf:
                index, found = node.search(key)
                cell = leaf_cell(key, value)
                if found and not overwrite:
                    raise DuplicateKeyError(f"key already present: {key!r}")
                dirty = True
                if found:
                    before = node.used_bytes
                    if node.set_cell(index, cell):
                        self._shrank = node.used_bytes < before
                        return False, None
                    node.remove_cell(index)
                    if node.insert_cell(index, cell):
                        return False, None
                    return False, self._split(node, index, cell)
                if node.insert_cell(index, cell):
                    return True, None
                return True, self._split(node, index, cell)

            child_index = node.find_child(key)
            inserted, split = self._insert(
                node.child_at(child_index), key, value, overwrite
            )
            if split is None:
                if self._shrank and self._rebalance(node, child_index):
                    dirty = True
                return inserted, None
            separator, right_id = split
            cell = internal_cell(separator, right_id)
            dirty = True
            if node.insert_cell(child_index, cell):
                return inserted, None
            return inserted, self._split(node, child_index, cell)
        finally:
            self.pool.unpin_page(page_id, dirty=dirty)

    def _split(
        self, node: Node, index: int, cell: bytes
    ) -> tuple[bytes, int]:
        cells = node.cells()
        cells.insert(index, cell)
        at = node.split_index(cells)
        new_id, new_data = self.pool.new_page()
        try:
            if node.is_leaf:
                left, right = cells[:at], cells[at:]
                old_next = node.next_leaf
                Node.new_leaf(new_data, new_id).reset(right, extra=old_next)
                node.reset(left)
                node.next_leaf = new_id
                return cell_key(right[0]), new_id

            promoted = cells[at]
            left, right = cells[:at], cells[at + 1 :]
            Node.new_internal(new_data, new_id, cell_child(promoted)).reset(
                right, extra=cell_child(promoted)
            )
            node.reset(left)
            return cell_key(promoted), new_id
        finally:
            self.pool.unpin_page(new_id, dirty=True)

    def _grow(self, separator: bytes, right_id: int) -> None:
        new_root_id, data = self.pool.new_page()
        try:
            root = Node.new_internal(data, new_root_id, self.root_page_id)
            appended = root.append_cell(internal_cell(separator, right_id))
            assert appended, "a fresh internal node always fits one cell"
        finally:
            self.pool.unpin_page(new_root_id, dirty=True)
        self._set_root(new_root_id)

    def _delete(self, page_id: int, key: bytes) -> bool:
        data = self.pool.fetch_page(page_id)
        dirty = False
        try:
            node = Node(data, page_id)
            if node.is_leaf:
                index, found = node.search(key)
                if not found:
                    return False
                dirty = True
                node.remove_cell(index)
                return True

            child_index = node.find_child(key)
            removed = self._delete(node.child_at(child_index), key)
            if removed and self._rebalance(node, child_index):
                dirty = True
            return removed
        finally:
            self.pool.unpin_page(page_id, dirty=dirty)

    def _rebalance(self, parent: Node, child_index: int) -> bool:
        child_id = parent.child_at(child_index)
        with self.pool.pinned(child_id) as data:
            if not Node(data, child_id).is_underfull:
                return False

        left_index = child_index - 1 if child_index > 0 else child_index
        right_index = left_index + 1
        if right_index > parent.cell_count:
            return False
        separator = parent.key(left_index)
        left_id = parent.child_at(left_index)
        right_id = parent.child_at(right_index)

        with self.pool.pinned(left_id, dirty=True) as left_data:
            with self.pool.pinned(right_id, dirty=True) as right_data:
                left, right = Node(left_data, left_id), Node(right_data, right_id)
                cost = (
                    0
                    if left.is_leaf
                    else len(internal_cell(separator, 0)) + POINTER_SIZE
                )
                merged = left.can_absorb(right, cost)
                if merged:
                    self._merge(left, right, separator)
                else:
                    new_separator = self._borrow(left, right, separator)

        if merged:
            parent.remove_cell(left_index)
            self.pool.free_page(right_id)
            return True
        if new_separator == separator:
            return False
        cell = internal_cell(new_separator, right_id)
        if not parent.set_cell(left_index, cell):
            parent.defragment()
            if not parent.set_cell(left_index, cell):
                self._relaxed_fill = True
                return False
        return True

    def _merge(self, left: Node, right: Node, separator: bytes) -> None:
        if left.is_leaf:
            left.reset(left.cells() + right.cells(), extra=right.next_leaf)
            return
        cells = (
            left.cells()
            + [internal_cell(separator, right.leftmost_child)]
            + right.cells()
        )
        left.reset(cells)

    def _borrow(self, left: Node, right: Node, separator: bytes) -> bytes:
        if left.is_underfull:
            donor, recipient, rightward = right, left, False
        else:
            donor, recipient, rightward = left, right, True

        while recipient.is_underfull:
            if donor.cell_count == 0:
                break
            index = donor.cell_count - 1 if rightward else 0
            cost = len(donor.cell(index)) + POINTER_SIZE
            if donor.used_bytes - cost < MIN_USED:
                break
            separator = self._rotate(left, right, separator, rightward)
        return separator

    def _rotate(
        self, left: Node, right: Node, separator: bytes, rightward: bool
    ) -> bytes:
        if left.is_leaf:
            if rightward:
                index = left.cell_count - 1
                cell = left.cell(index)
                moved = right.insert_cell(0, cell)
                assert moved, "an underfull node always has room for one cell"
                left.remove_cell(index)
                return cell_key(cell)
            cell = right.cell(0)
            moved = left.append_cell(cell)
            assert moved, "an underfull node always has room for one cell"
            right.remove_cell(0)
            return right.key(0)

        if rightward:
            index = left.cell_count - 1
            rotated = left.cell(index)
            demoted = internal_cell(separator, right.leftmost_child)
            moved = right.insert_cell(0, demoted)
            assert moved, "an underfull node always has room for one cell"
            right.leftmost_child = cell_child(rotated)
            left.remove_cell(index)
            return cell_key(rotated)

        demoted = internal_cell(separator, right.leftmost_child)
        moved = left.append_cell(demoted)
        assert moved, "an underfull node always has room for one cell"
        promoted = right.key(0)
        right.leftmost_child = right.child(0)
        right.remove_cell(0)
        return promoted

    def _shrink(self) -> None:
        while True:
            with self.pool.pinned(self.root_page_id) as data:
                root = Node(data, self.root_page_id)
                if root.is_leaf or root.cell_count > 0:
                    return
                only_child = root.child_at(0)
            old_root = self.root_page_id
            self._set_root(only_child)
            self.pool.free_page(old_root)

    def _set_root(self, page_id: int) -> None:
        self.root_page_id = page_id
        if self._on_root_change is not None:
            self._on_root_change(page_id)

    def verify_invariants(self, check_fill: bool = True) -> None:
        leaves: list[int] = []
        seen: set[int] = set()
        self._verify(
            self.root_page_id,
            low=None,
            high=None,
            depth=0,
            leaf_depth=[None],
            leaves=leaves,
            seen=seen,
            check_fill=check_fill and not self._relaxed_fill,
            is_root=True,
        )

        walked: list[int] = []
        page_id = self._first_leaf()
        while page_id != NULL_PAGE_ID:
            if page_id in walked:
                raise CorruptTreeError(f"the leaf chain loops at page {page_id}")
            walked.append(page_id)
            with self.pool.pinned(page_id) as data:
                page_id = Node(data, page_id).next_leaf
        if walked != leaves:
            raise CorruptTreeError(
                f"the leaf chain visits {walked} but the tree's leaves, left to "
                f"right, are {leaves}"
            )

        previous: bytes | None = None
        for key in self.keys():
            if previous is not None and key <= previous:
                raise CorruptTreeError(
                    f"a scan returned {key!r} after {previous!r}, out of order"
                )
            previous = key

    def _verify(
        self,
        page_id: int,
        low: bytes | None,
        high: bytes | None,
        depth: int,
        leaf_depth: list[int | None],
        leaves: list[int],
        seen: set[int],
        check_fill: bool,
        is_root: bool = False,
    ) -> None:
        if page_id in seen:
            raise CorruptTreeError(f"page {page_id} appears twice in the tree")
        seen.add(page_id)
        with self.pool.pinned(page_id) as data:
            node = Node(data, page_id)
            node.verify()
            if not is_root and node.cell_count == 0:
                raise CorruptTreeError(f"page {page_id} is empty but is not the root")
            if not is_root and check_fill and node.is_underfull:
                raise CorruptTreeError(
                    f"page {page_id} holds {node.used_bytes} bytes, below the fill "
                    f"threshold, and is not the root"
                )
            for key in node.keys():
                if low is not None and key < low:
                    raise CorruptTreeError(
                        f"page {page_id} holds {key!r}, below its lower bound {low!r}"
                    )
                if high is not None and key >= high:
                    raise CorruptTreeError(
                        f"page {page_id} holds {key!r}, at or above its upper bound "
                        f"{high!r}"
                    )
            if node.is_leaf:
                if leaf_depth[0] is None:
                    leaf_depth[0] = depth
                elif leaf_depth[0] != depth:
                    raise CorruptTreeError(
                        f"leaf {page_id} is at depth {depth}, but another leaf is at "
                        f"depth {leaf_depth[0]}; the tree is not balanced"
                    )
                leaves.append(page_id)
                return
            children = [node.child_at(i) for i in range(node.cell_count + 1)]
            bounds = [low] + node.keys() + [high]
        for index, child in enumerate(children):
            self._verify(
                child,
                low=bounds[index],
                high=bounds[index + 1],
                depth=depth + 1,
                leaf_depth=leaf_depth,
                leaves=leaves,
                seen=seen,
                check_fill=check_fill,
            )
