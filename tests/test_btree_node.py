"""Tests for layer 4a, the B+Tree node layout.

Node-level tests on a bare buffer, so a failure here is never a tree bug. The
thing worth hammering is the pointer array: it is kept in key order, so every
insert and remove shifts it, and an off-by-one there corrupts a page silently.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.btree_node import (  # noqa: E402
    MAX_CELL_SIZE,
    MIN_USED,
    PAGE_TYPE_INTERNAL,
    PAGE_TYPE_LEAF,
    USABLE,
    CellTooLargeError,
    Node,
    NodeError,
    cell_child,
    cell_key,
    cell_value,
    internal_cell,
    leaf_cell,
    max_value_size,
)
from pydb.pager import PAGE_SIZE  # noqa: E402


class NodeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.leaf = Node.new_leaf(bytearray(PAGE_SIZE), page_id=1)
        self.internal = Node.new_internal(bytearray(PAGE_SIZE), page_id=2, leftmost=9)

    def tearDown(self) -> None:
        self.leaf.verify()
        self.internal.verify()

    def fill_leaf(self, keys: list[bytes], value: bytes = b"v") -> None:
        for key in keys:
            index, found = self.leaf.search(key)
            self.assertFalse(found)
            self.assertTrue(self.leaf.insert_cell(index, leaf_cell(key, value)))


class TestCellEncoding(unittest.TestCase):
    def test_leaf_cell_round_trips(self):
        cell = leaf_cell(b"key", b"value")
        self.assertEqual(cell_key(cell), b"key")
        self.assertEqual(cell_value(cell), b"value")

    def test_internal_cell_round_trips(self):
        cell = internal_cell(b"sep", 12345)
        self.assertEqual(cell_key(cell), b"sep")
        self.assertEqual(cell_child(cell), 12345)

    def test_empty_key_and_value_are_legal(self):
        cell = leaf_cell(b"", b"")
        self.assertEqual(cell_key(cell), b"")
        self.assertEqual(cell_value(cell), b"")

    def test_a_cell_is_capped_so_eight_always_fit_in_a_page(self):
        self.assertLessEqual(8 * (MAX_CELL_SIZE + 4), USABLE)


class TestEmptyNodes(NodeTestCase):
    def test_a_new_leaf_is_empty_with_the_whole_page_free(self):
        self.assertEqual(self.leaf.page_type, PAGE_TYPE_LEAF)
        self.assertEqual(self.leaf.cell_count, 0)
        self.assertEqual(self.leaf.free_space, USABLE)
        self.assertEqual(self.leaf.used_bytes, 0)
        self.assertEqual(self.leaf.next_leaf, 0)

    def test_a_new_internal_node_remembers_its_leftmost_child(self):
        self.assertEqual(self.internal.page_type, PAGE_TYPE_INTERNAL)
        self.assertEqual(self.internal.leftmost_child, 9)
        self.assertEqual(self.internal.child_at(0), 9)

    def test_an_uninitialised_page_is_not_a_node(self):
        with self.assertRaises(NodeError):
            Node(bytearray(PAGE_SIZE))

    def test_leaf_and_internal_accessors_do_not_mix(self):
        with self.assertRaises(NodeError):
            self.leaf.leftmost_child
        with self.assertRaises(NodeError):
            self.internal.next_leaf
        with self.assertRaises(NodeError):
            self.leaf.child(0)

    def test_searching_an_empty_node_says_insert_at_zero(self):
        self.assertEqual(self.leaf.search(b"anything"), (0, False))


class TestSearch(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fill_leaf([b"b", b"d", b"f", b"h"])

    def test_finds_every_key_it_holds(self):
        for index, key in enumerate([b"b", b"d", b"f", b"h"]):
            with self.subTest(key=key):
                self.assertEqual(self.leaf.search(key), (index, True))

    def test_reports_where_a_missing_key_would_go(self):
        for key, index in ((b"a", 0), (b"c", 1), (b"e", 2), (b"g", 3), (b"z", 4)):
            with self.subTest(key=key):
                self.assertEqual(self.leaf.search(key), (index, False))

    def test_a_key_equal_to_a_separator_descends_right(self):
        """The separator is the smallest key of the subtree it points at, so an
        exact match must go right, not left."""
        for key, child in ((b"b", 2), (b"d", 3), (b"f", 4), (b"h", 5)):
            self.internal.append_cell(internal_cell(key, child))
        self.assertEqual(self.internal.find_child(b"a"), 0)  # leftmost
        self.assertEqual(self.internal.find_child(b"b"), 1)
        self.assertEqual(self.internal.find_child(b"c"), 1)
        self.assertEqual(self.internal.find_child(b"h"), 4)
        self.assertEqual(self.internal.find_child(b"z"), 4)
        self.assertEqual(self.internal.child_at(1), 2)


class TestInsertAndRemove(NodeTestCase):
    def test_cells_stay_in_key_order_whatever_order_they_arrive_in(self):
        self.fill_leaf([b"m", b"a", b"z", b"c", b"q"])
        self.assertEqual(self.leaf.keys(), [b"a", b"c", b"m", b"q", b"z"])

    def test_values_survive_the_pointer_shuffling(self):
        for key in (b"m", b"a", b"z", b"c"):
            index, _ = self.leaf.search(key)
            self.leaf.insert_cell(index, leaf_cell(key, b"value-" + key))
        self.assertEqual(
            self.leaf.items(),
            [
                (b"a", b"value-a"),
                (b"c", b"value-c"),
                (b"m", b"value-m"),
                (b"z", b"value-z"),
            ],
        )

    def test_each_cell_costs_its_length_plus_a_pointer(self):
        cell = leaf_cell(b"k", b"v")
        self.leaf.insert_cell(0, cell)
        self.assertEqual(self.leaf.used_bytes, len(cell) + 4)
        self.assertEqual(self.leaf.free_space, USABLE - len(cell) - 4)

    def test_removing_shifts_the_pointers_left(self):
        self.fill_leaf([b"a", b"b", b"c", b"d"])
        self.leaf.remove_cell(1)
        self.assertEqual(self.leaf.keys(), [b"a", b"c", b"d"])
        self.leaf.remove_cell(0)
        self.assertEqual(self.leaf.keys(), [b"c", b"d"])

    def test_removing_the_lowest_cell_reclaims_space_without_fragmenting(self):
        self.fill_leaf([b"a", b"b"])
        self.leaf.remove_cell(1)  # b was written last, so it is the lowest cell
        self.assertEqual(self.leaf.frag_bytes, 0)

    def test_removing_a_middle_cell_leaves_a_hole_until_defragmented(self):
        self.fill_leaf([b"a", b"b", b"c"])
        self.leaf.remove_cell(1)
        self.assertGreater(self.leaf.frag_bytes, 0)
        reclaimed = self.leaf.defragment()
        self.assertEqual(self.leaf.frag_bytes, 0)
        self.assertGreater(reclaimed, 0)
        self.assertEqual(self.leaf.keys(), [b"a", b"c"])

    def test_insert_defragments_rather_than_failing(self):
        """Enough dead space for the cell, but not in one piece. Insert has to
        compact the page rather than report no room."""
        big = b"x" * (MAX_CELL_SIZE - 3)
        count = 0
        while self.leaf.append_cell(leaf_cell(bytes([65 + count]), big)):
            count += 1
        self.leaf.remove_cell(1)
        self.leaf.remove_cell(1)
        self.assertLess(self.leaf.free_space, len(big))
        self.assertGreater(self.leaf.frag_bytes, len(big))
        self.assertTrue(self.leaf.append_cell(leaf_cell(b"z", big)))
        self.assertEqual(self.leaf.cell_count, count - 1)
        self.assertEqual(self.leaf.value(0), big)

    def test_a_full_node_refuses_an_insert_without_changing_anything(self):
        payload = b"x" * (MAX_CELL_SIZE - 3)
        count = 0
        while self.leaf.append_cell(leaf_cell(bytes([65 + count]), payload)):
            count += 1
        self.assertGreaterEqual(count, 8, "eight max-size cells must always fit")
        self.assertFalse(self.leaf.insert_cell(0, leaf_cell(b"!", payload)))
        self.assertEqual(self.leaf.cell_count, count)

    def test_a_cell_bigger_than_the_cap_is_an_error_not_a_refusal(self):
        with self.assertRaises(CellTooLargeError):
            self.leaf.insert_cell(0, b"x" * (MAX_CELL_SIZE + 1))

    def test_max_value_size_is_exactly_what_fits(self):
        key = b"key"
        value = b"v" * max_value_size(key)
        self.assertTrue(self.leaf.insert_cell(0, leaf_cell(key, value)))
        with self.assertRaises(CellTooLargeError):
            self.leaf.insert_cell(0, leaf_cell(key, value + b"!"))

    def test_inserting_out_of_range_raises(self):
        with self.assertRaises(NodeError):
            self.leaf.insert_cell(3, leaf_cell(b"k", b"v"))


class TestSetCell(NodeTestCase):
    def test_same_length_replacement_is_in_place(self):
        self.fill_leaf([b"a", b"b"])
        start = self.leaf.cell_start
        self.assertTrue(self.leaf.set_cell(0, leaf_cell(b"a", b"w")))
        self.assertEqual(self.leaf.value(0), b"w")
        self.assertEqual(self.leaf.cell_start, start)

    def test_a_longer_replacement_moves_the_cell(self):
        self.fill_leaf([b"a", b"b"])
        self.assertTrue(self.leaf.set_cell(0, leaf_cell(b"a", b"w" * 500)))
        self.assertEqual(self.leaf.items(), [(b"a", b"w" * 500), (b"b", b"v")])

    def test_set_cell_returns_false_and_changes_nothing_when_it_cannot_fit(self):
        """Pack the page with big cells, top it off with small ones, then try to
        grow one of the small cells into a big one."""
        big = b"x" * (MAX_CELL_SIZE - 3)
        for i in range(7):  # not the full eight: leave room for the small ones
            self.assertTrue(self.leaf.append_cell(leaf_cell(bytes([65 + i]), big)))
        index = 0
        while self.leaf.append_cell(leaf_cell(b"H" + bytes([index]), b"sm")):
            index += 1
        small = self.leaf.cell_count - 1
        before = self.leaf.items()
        key = self.leaf.key(small)
        grown = leaf_cell(key, b"x" * max_value_size(key))
        self.assertFalse(self.leaf.set_cell(small, grown))
        self.assertEqual(self.leaf.items(), before)


class TestSplitIndex(NodeTestCase):
    def test_splits_equal_cells_down_the_middle(self):
        cells = [leaf_cell(bytes([65 + i]), b"v") for i in range(10)]
        self.assertEqual(self.leaf.split_index(cells), 5)

    def test_splits_by_bytes_not_by_count(self):
        """One huge cell and many tiny ones must not split into 'all' and 'none'."""
        cells = [leaf_cell(b"a", b"x" * (MAX_CELL_SIZE - 3))] + [
            leaf_cell(bytes([66 + i]), b"v") for i in range(20)
        ]
        at = self.leaf.split_index(cells)
        self.assertEqual(at, 1)

    def test_never_puts_everything_on_one_side(self):
        for count in range(2, 30):
            cells = [leaf_cell(bytes([65 + i]), b"v") for i in range(count)]
            with self.subTest(count=count):
                at = self.leaf.split_index(cells)
                self.assertGreaterEqual(at, 1)
                self.assertLessEqual(at, count - 1)


class TestReset(NodeTestCase):
    def test_reset_rebuilds_a_node_from_a_list_of_cells(self):
        self.fill_leaf([b"a", b"b", b"c", b"d"])
        cells = self.leaf.cells()
        self.leaf.reset(cells[:2])
        self.assertEqual(self.leaf.keys(), [b"a", b"b"])
        self.assertEqual(self.leaf.frag_bytes, 0)

    def test_reset_keeps_the_extra_field_unless_told_otherwise(self):
        self.leaf.next_leaf = 77
        self.leaf.reset([leaf_cell(b"a", b"v")])
        self.assertEqual(self.leaf.next_leaf, 77)
        self.leaf.reset([leaf_cell(b"a", b"v")], extra=88)
        self.assertEqual(self.leaf.next_leaf, 88)

    def test_reset_wipes_the_bytes_it_no_longer_needs(self):
        self.fill_leaf([b"a"], value=b"SECRET")
        self.leaf.reset([])
        self.assertNotIn(b"SECRET", bytes(self.leaf.data))

    def test_reset_refuses_more_cells_than_a_page_holds(self):
        cell = leaf_cell(b"k", b"x" * 500)
        with self.assertRaises(NodeError):
            self.leaf.reset([cell] * 20)


class TestFillAccounting(NodeTestCase):
    def test_an_empty_node_is_underfull_and_a_full_one_is_not(self):
        self.assertTrue(self.leaf.is_underfull)
        while self.leaf.append_cell(leaf_cell(b"k" + bytes([self.leaf.cell_count]), b"v" * 40)):
            pass
        self.assertFalse(self.leaf.is_underfull)

    def test_the_fill_threshold_leaves_room_for_a_whole_cell(self):
        """The bound that makes rebalancing always possible."""
        self.assertGreaterEqual(USABLE - MIN_USED, MAX_CELL_SIZE + 4)

    def test_can_absorb_compares_real_usage(self):
        other = Node.new_leaf(bytearray(PAGE_SIZE), page_id=3)
        self.fill_leaf([b"a", b"b"])
        other.append_cell(leaf_cell(b"c", b"v"))
        self.assertTrue(self.leaf.can_absorb(other))
        index = 0
        while other.append_cell(leaf_cell(b"d" + bytes([index]), b"x" * 500)):
            index += 1  # fill `other` to the brim
        self.assertFalse(self.leaf.can_absorb(other))


class TestVerify(NodeTestCase):
    def test_verify_catches_keys_out_of_order(self):
        self.leaf.append_cell(leaf_cell(b"b", b"v"))
        self.leaf.append_cell(leaf_cell(b"a", b"v"))  # appended blindly, unsorted
        with self.assertRaises(NodeError):
            self.leaf.verify()
        self.leaf.remove_cell(1)

    def test_verify_catches_a_wrong_fragment_count(self):
        self.fill_leaf([b"a"])
        self.leaf._set(6, 99)
        with self.assertRaises(NodeError):
            self.leaf.verify()
        self.leaf._set(6, 0)


if __name__ == "__main__":
    unittest.main()
