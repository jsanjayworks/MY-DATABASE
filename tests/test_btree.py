"""Tests for layer 4b, the B+Tree.

`verify_invariants()` runs after nearly every mutation here, which is the single
most useful habit in this whole project: a tree can return correct answers for a
long time while quietly rotting one level down, and the invariant check is what
turns "wrong answer, somewhere, eventually" into "this page, this operation".

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.btree import BTree, CorruptTreeError, DuplicateKeyError  # noqa: E402
from pydb.btree_node import (  # noqa: E402
    MAX_KEY_SIZE,
    CellTooLargeError,
    Node,
    cell_child,
    internal_cell,
    max_value_size,
)
from pydb.buffer_pool import BufferPool  # noqa: E402
from pydb.record import ColumnType, encode_key  # noqa: E402


def int_key(value: int) -> bytes:
    return encode_key(ColumnType.INT, value)


class BTreeTestCase(unittest.TestCase):
    CAPACITY = 32

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.roots: list[int] = []  # every root this tree has ever had
        self.pool = BufferPool.open(self.path, capacity=self.CAPACITY)
        self.addCleanup(self.pool.close)
        self.tree = BTree.create(self.pool, on_root_change=self.roots.append)
        self.first_root = self.tree.root_page_id  # the original, single-leaf root

    def tearDown(self) -> None:
        self.tree.verify_invariants()
        self.pool.assert_no_pins()

    def reopen(self) -> BTree:
        """Reopen the file, finding the root the same way a catalog would."""
        root = self.tree.root_page_id
        self.pool.close()
        self.pool = BufferPool.open(self.path, capacity=self.CAPACITY)
        self.addCleanup(self.pool.close)
        self.tree = BTree(self.pool, root, on_root_change=self.roots.append)
        return self.tree

    def put_ints(self, values, verify: bool = False) -> None:
        for value in values:
            self.tree.put(int_key(value), str(value).encode())
            if verify:
                self.tree.verify_invariants()

    def stored_ints(self) -> list[int]:
        return [int(value) for _key, value in self.tree.items()]


class TestEmptyTree(BTreeTestCase):
    def test_a_new_tree_is_a_single_empty_leaf(self):
        self.assertEqual(self.tree.height(), 1)
        self.assertEqual(self.tree.count(), 0)
        self.assertEqual(list(self.tree.items()), [])
        self.assertIsNone(self.tree.get(b"nothing"))

    def test_deleting_from_an_empty_tree_is_false_not_an_error(self):
        self.assertFalse(self.tree.delete(b"nothing"))

    def test_an_empty_root_leaf_is_allowed(self):
        self.tree.put(b"k", b"v")
        self.assertTrue(self.tree.delete(b"k"))
        self.assertEqual(self.tree.count(), 0)
        self.assertEqual(self.tree.height(), 1)


class TestBasicOperations(BTreeTestCase):
    def test_round_trips_one_entry(self):
        self.assertTrue(self.tree.put(b"key", b"value"))
        self.assertEqual(self.tree.get(b"key"), b"value")
        self.assertIn(b"key", self.tree)

    def test_put_returns_whether_the_key_was_new(self):
        self.assertTrue(self.tree.put(b"k", b"1"))
        self.assertFalse(self.tree.put(b"k", b"2"))
        self.assertEqual(self.tree.get(b"k"), b"2")
        self.assertEqual(self.tree.count(), 1)

    def test_overwriting_with_a_much_longer_value_works(self):
        self.tree.put(b"k", b"1")
        self.tree.put(b"k", b"x" * 400)
        self.assertEqual(self.tree.get(b"k"), b"x" * 400)
        self.assertEqual(self.tree.count(), 1)

    def test_insert_refuses_to_replace(self):
        self.tree.insert(b"k", b"1")
        with self.assertRaises(DuplicateKeyError):
            self.tree.insert(b"k", b"2")
        self.assertEqual(self.tree.get(b"k"), b"1")

    def test_empty_key_and_empty_value_are_storable(self):
        self.tree.put(b"", b"")
        self.assertEqual(self.tree.get(b""), b"")
        self.assertEqual(list(self.tree.items()), [(b"", b"")])

    def test_getitem_and_setitem_and_delitem(self):
        self.tree[b"k"] = b"v"
        self.assertEqual(self.tree[b"k"], b"v")
        del self.tree[b"k"]
        with self.assertRaises(KeyError):
            self.tree[b"k"]
        with self.assertRaises(KeyError):
            del self.tree[b"k"]

    def test_keys_and_values_are_returned_as_stored(self):
        payload = bytes(range(200))
        self.tree.put(payload, payload)
        self.assertEqual(self.tree.get(payload), payload)

    def test_a_key_too_long_for_a_node_is_rejected(self):
        with self.assertRaises(CellTooLargeError):
            self.tree.put(b"k" * (MAX_KEY_SIZE + 1), b"v")

    def test_a_value_too_long_for_a_node_is_rejected(self):
        key = b"key"
        with self.assertRaises(CellTooLargeError):
            self.tree.put(key, b"v" * (max_value_size(key) + 1))


class TestOrdering(BTreeTestCase):
    def test_iteration_is_sorted_whatever_order_keys_arrive_in(self):
        values = list(range(200))
        shuffled = values[:]
        random.Random(1).shuffle(shuffled)
        self.put_ints(shuffled)
        self.assertEqual(self.stored_ints(), values)

    def test_negative_integers_sort_below_positive_ones(self):
        self.put_ints([5, -5, 0, -1, 1, -(2**63), 2**63 - 1])
        self.assertEqual(
            self.stored_ints(), [-(2**63), -5, -1, 0, 1, 5, 2**63 - 1]
        )

    def test_text_keys_sort_lexicographically(self):
        words = ["pear", "apple", "Apple", "banana", "apple pie", ""]
        for word in words:
            self.tree.put(encode_key(ColumnType.TEXT, word), word.encode())
        self.assertEqual(
            [v.decode() for _k, v in self.tree.items()],
            ["", "Apple", "apple", "apple pie", "banana", "pear"],
        )


class TestSplits(BTreeTestCase):
    def test_the_tree_grows_taller_and_the_root_moves(self):
        self.assertEqual(self.roots, [])
        self.put_ints(range(500))
        self.assertGreater(self.tree.height(), 1)
        self.assertTrue(self.roots, "a split must report the new root page id")
        self.assertEqual(self.tree.root_page_id, self.roots[-1])

    def test_every_key_is_still_findable_after_many_splits(self):
        keys = list(range(2000))
        random.Random(2).shuffle(keys)
        self.put_ints(keys)
        for value in keys:
            with self.subTest(key=value):
                self.assertEqual(self.tree.get(int_key(value)), str(value).encode())

    def test_invariants_hold_after_every_single_insert(self):
        """Slow and worth it: this is what localises a split bug to one insert."""
        keys = list(range(120))
        random.Random(3).shuffle(keys)
        self.put_ints(keys, verify=True)
        self.assertEqual(self.tree.count(), 120)

    def test_ascending_inserts_split_correctly(self):
        """The pathological pattern for a B+Tree: every insert lands on the last
        leaf, so every split happens at the right edge."""
        self.put_ints(range(1000))
        self.assertEqual(self.stored_ints(), list(range(1000)))

    def test_descending_inserts_split_correctly(self):
        self.put_ints(range(1000, 0, -1))
        self.assertEqual(self.stored_ints(), list(range(1, 1001)))

    def test_big_values_build_a_three_level_tree(self):
        """Only ~3 cells fit in a leaf at this value size, so a few thousand keys
        are enough to overflow the root and split an *internal* node -- the path
        small keys would need tens of thousands of rows to reach."""
        for i in range(2500):
            self.tree.put(int_key(i), b"x" * 400)
        self.assertGreaterEqual(self.tree.height(), 3)
        self.tree.verify_invariants()
        self.assertEqual(self.tree.count(), 2500)
        self.assertEqual(self.tree.get(int_key(1249)), b"x" * 400)

    def test_the_leaf_chain_is_rebuilt_correctly_by_splits(self):
        self.put_ints(range(600))
        leaves = []
        page_id = self.tree._first_leaf()
        while page_id:
            with self.pool.pinned(page_id) as data:
                node = Node(data, page_id)
                leaves.append(node.cell_count)
                page_id = node.next_leaf
        self.assertGreater(len(leaves), 1)
        self.assertEqual(sum(leaves), 600)


class TestRangeScans(BTreeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.put_ints(range(0, 1000, 2))  # every even number below 1000

    def test_a_bounded_range_returns_exactly_the_keys_inside_it(self):
        got = [int(v) for _k, v in self.tree.items(int_key(100), int_key(200))]
        self.assertEqual(got, list(range(100, 200, 2)))

    def test_range_bounds_do_not_have_to_exist(self):
        got = [int(v) for _k, v in self.tree.items(int_key(101), int_key(201))]
        self.assertEqual(got, list(range(102, 201, 2)))

    def test_the_lower_bound_is_inclusive_and_the_upper_exclusive(self):
        got = [int(v) for _k, v in self.tree.items(int_key(10), int_key(20))]
        self.assertEqual(got, [10, 12, 14, 16, 18])

    def test_an_open_ended_range_runs_to_the_end(self):
        got = [int(v) for _k, v in self.tree.items(int_key(990))]
        self.assertEqual(got, [990, 992, 994, 996, 998])

    def test_an_empty_range_yields_nothing(self):
        self.assertEqual(list(self.tree.items(int_key(11), int_key(12))), [])
        self.assertEqual(list(self.tree.items(int_key(500), int_key(400))), [])

    def test_a_range_scan_holds_no_pins_between_rows(self):
        for _key, _value in self.tree.items():
            self.pool.assert_no_pins()
            break


class TestDeletes(BTreeTestCase):
    def test_deleting_a_missing_key_is_false(self):
        self.put_ints(range(100))
        self.assertFalse(self.tree.delete(int_key(1000)))
        self.assertEqual(self.tree.count(), 100)

    def test_deleting_every_key_empties_the_tree_and_shortens_it(self):
        keys = list(range(500))
        self.put_ints(keys)
        self.assertGreater(self.tree.height(), 1)
        random.Random(4).shuffle(keys)
        for value in keys:
            self.assertTrue(self.tree.delete(int_key(value)))
        self.assertEqual(self.tree.count(), 0)
        self.assertEqual(self.tree.height(), 1, "the tree should have collapsed")
        self.assertEqual(list(self.tree.items()), [])

    def test_invariants_hold_after_every_single_delete(self):
        keys = list(range(300))
        self.put_ints(keys)
        random.Random(5).shuffle(keys)
        for value in keys:
            self.tree.delete(int_key(value))
            self.tree.verify_invariants()

    def test_deleting_in_ascending_order_keeps_the_tree_valid(self):
        self.put_ints(range(400))
        for value in range(400):
            self.tree.delete(int_key(value))
            if value % 37 == 0:
                self.tree.verify_invariants()
        self.assertEqual(self.tree.count(), 0)

    def test_deleting_in_descending_order_keeps_the_tree_valid(self):
        self.put_ints(range(400))
        for value in range(399, -1, -1):
            self.tree.delete(int_key(value))
            if value % 37 == 0:
                self.tree.verify_invariants()
        self.assertEqual(self.tree.count(), 0)

    def test_the_root_collapses_and_the_new_root_is_reported(self):
        self.put_ints(range(400))
        tall = self.tree.height()
        roots_before = len(self.roots)
        for value in range(400):
            self.tree.delete(int_key(value))
        self.assertLess(self.tree.height(), tall)
        self.assertGreater(
            len(self.roots), roots_before, "collapsing must report the new root"
        )
        self.assertEqual(self.tree.root_page_id, self.roots[-1])

    def test_freed_pages_come_back_from_the_pager(self):
        """Merging frees pages; the pager should hand them out again."""
        self.put_ints(range(600))
        pages_before = self.pool.pager.page_count
        for value in range(600):
            self.tree.delete(int_key(value))
        self.assertGreater(len(self.pool.pager.free_pages()), 0)
        self.put_ints(range(600))
        self.assertEqual(
            self.pool.pager.page_count,
            pages_before,
            "refilling the tree should reuse the pages the merges freed",
        )

    def test_deletes_and_inserts_interleaved(self):
        rng = random.Random(6)
        model: dict[int, bytes] = {}
        for step in range(4000):
            key = rng.randrange(300)
            if rng.random() < 0.55:
                value = str(step).encode()
                self.tree.put(int_key(key), value)
                model[key] = value
            else:
                expected = key in model
                self.assertEqual(self.tree.delete(int_key(key)), expected)
                model.pop(key, None)
            if step % 500 == 0:
                self.tree.verify_invariants()
        self.assertEqual(
            [(int_key(k), v) for k, v in sorted(model.items())],
            list(self.tree.items()),
        )


class TestBorrowAndMerge(BTreeTestCase):
    """The two rebalancing paths, forced deliberately.

    Equal-sized keys merge; big values make merging impossible and force a
    borrow, which is the path that has to rotate a separator through the parent.
    """

    def test_merging_is_preferred_when_it_fits(self):
        self.put_ints(range(300))
        before = len(self.pool.pager.free_pages())
        for value in range(150):
            self.tree.delete(int_key(value))
        self.tree.verify_invariants()
        self.assertGreater(
            len(self.pool.pager.free_pages()), before, "merges should free pages"
        )

    def test_borrowing_keeps_the_tree_valid_with_large_cells(self):
        for i in range(60):
            self.tree.put(int_key(i), b"x" * 400)  # a handful of cells per leaf
        self.tree.verify_invariants()
        for i in range(0, 60, 2):
            self.tree.delete(int_key(i))
            self.tree.verify_invariants()
        self.assertEqual(self.tree.count(), 30)
        self.assertEqual(
            [int(k[-1]) for k, _v in self.tree.items()][:3],
            [int(int_key(1)[-1]), int(int_key(3)[-1]), int(int_key(5)[-1])],
        )

    def test_mixed_key_and_value_sizes_survive_churn(self):
        rng = random.Random(7)
        model: dict[bytes, bytes] = {}
        for step in range(1500):
            key = int_key(rng.randrange(200))
            if rng.random() < 0.6:
                value = b"x" * rng.choice([1, 10, 200, 480])
                self.tree.put(key, value)
                model[key] = value
            else:
                self.tree.delete(key)
                model.pop(key, None)
            if step % 250 == 0:
                self.tree.verify_invariants()
        self.assertEqual(sorted(model.items()), list(self.tree.items()))


class TestPersistence(BTreeTestCase):
    def test_the_tree_survives_a_reopen(self):
        self.put_ints(range(1000))
        self.reopen()
        self.assertEqual(self.tree.count(), 1000)
        self.assertEqual(self.stored_ints(), list(range(1000)))
        self.tree.verify_invariants()

    def test_a_reopened_tree_can_still_be_modified(self):
        self.put_ints(range(500))
        self.reopen()
        self.put_ints(range(500, 1000))
        for value in range(0, 1000, 3):
            self.tree.delete(int_key(value))
        self.tree.verify_invariants()
        self.assertEqual(
            self.stored_ints(), [v for v in range(1000) if v % 3 != 0]
        )

    def test_the_root_page_id_is_what_must_be_remembered(self):
        """Documenting the trap: reopening with a stale root loses the tree."""
        self.put_ints(range(500))
        stale_root = self.first_root
        self.assertNotEqual(stale_root, self.tree.root_page_id)
        root = self.tree.root_page_id
        self.pool.close()
        self.pool = BufferPool.open(self.path, capacity=self.CAPACITY)
        self.addCleanup(self.pool.close)
        stale = BTree(self.pool, stale_root)
        # The stale root is now just a leaf somewhere in the middle of the tree,
        # so a lookup that should descend past it finds nothing at all.
        self.assertEqual(stale.height(), 1)
        self.assertIsNone(stale.get(int_key(499)))
        self.tree = BTree(self.pool, root)  # the real root still sees everything
        self.assertEqual(self.tree.count(), 500)
        self.assertEqual(self.tree.get(int_key(499)), b"499")


class TestCorruptionDetection(BTreeTestCase):
    def test_verify_catches_a_broken_leaf_chain(self):
        self.put_ints(range(600))
        leaf = self.tree._first_leaf()
        with self.pool.pinned(leaf, dirty=True) as data:
            original = Node(data, leaf).next_leaf
            Node(data, leaf).next_leaf = 0  # snip the chain
        with self.assertRaises(CorruptTreeError):
            self.tree.verify_invariants()
        with self.pool.pinned(leaf, dirty=True) as data:
            Node(data, leaf).next_leaf = original

    def test_verify_catches_keys_outside_the_bounds_their_parent_implies(self):
        """Move a separator without moving the keys, and the leaves below it are
        suddenly on the wrong side of it. Every node still looks fine on its own,
        which is exactly why the check has to be about parents and children."""
        self.put_ints(range(600))
        root = self.tree.root_page_id
        with self.pool.pinned(root, dirty=True) as data:
            node = Node(data, root)
            original = node.cell(0)
            self.assertTrue(
                node.set_cell(0, internal_cell(int_key(-1), cell_child(original)))
            )
        with self.assertRaises(CorruptTreeError):
            self.tree.verify_invariants()
        with self.pool.pinned(root, dirty=True) as data:
            Node(data, root).set_cell(0, original)


class TestMilestone(BTreeTestCase):
    """The layer 4 milestone from ROADMAP.md.

    Insert 100 000 random keys, verify every one by lookup, range-scan and confirm
    sorted order, then delete half and re-verify.
    """

    KEYS = 100_000
    CAPACITY = 64

    def test_one_hundred_thousand_random_keys(self):
        rng = random.Random(11)
        keys = rng.sample(range(10 * self.KEYS), self.KEYS)

        for key in keys:
            self.tree.put(int_key(key), str(key).encode())
        self.assertEqual(self.tree.count(), self.KEYS)
        self.tree.verify_invariants()

        for key in keys:
            if self.tree.get(int_key(key)) != str(key).encode():
                self.fail(f"key {key} did not read back")

        scanned = [int(value) for _key, value in self.tree.items()]
        self.assertEqual(len(scanned), self.KEYS)
        self.assertEqual(scanned, sorted(keys), "a range scan must be sorted")

        self.reopen()
        self.assertEqual(self.tree.count(), self.KEYS)

        doomed = keys[: self.KEYS // 2]
        survivors = keys[self.KEYS // 2 :]
        for key in doomed:
            self.assertTrue(self.tree.delete(int_key(key)))
        self.tree.verify_invariants()

        self.assertEqual(self.tree.count(), len(survivors))
        for key in doomed:
            if self.tree.get(int_key(key)) is not None:
                self.fail(f"deleted key {key} is still there")
        for key in survivors:
            if self.tree.get(int_key(key)) != str(key).encode():
                self.fail(f"surviving key {key} was lost")
        self.assertEqual(
            [int(v) for _k, v in self.tree.items()], sorted(survivors)
        )


if __name__ == "__main__":
    unittest.main()
