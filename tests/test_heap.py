"""Tests for layer 3c, the heap file.

This is the first layer where all the pieces below are involved at once, so the
tests that matter most are the ones that close the database and reopen it: if the
pager, the pool, the page layout and the row codec disagree about a single byte,
a reopened scan is where it shows up.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.buffer_pool import BufferPool  # noqa: E402
from pydb.heap import HeapFile, RowId, RowNotFoundError  # noqa: E402
from pydb.record import Schema  # noqa: E402
from pydb.slotted_page import MAX_ROW_SIZE, NoRoomError  # noqa: E402

SCHEMA = Schema.of(("id", "INT", False), ("name", "TEXT"), ("age", "INT"))


class HeapTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.pool = BufferPool.open(self.path, capacity=16)
        self.addCleanup(self.pool.close)
        self.heap = HeapFile.create(self.pool, SCHEMA)

    def tearDown(self) -> None:
        self.heap.verify()
        self.pool.assert_no_pins()

    def reopen(self, capacity: int = 16) -> HeapFile:
        """Close everything and open the same heap again, as a new process would."""
        first_page_id = self.heap.first_page_id
        self.pool.close()
        self.pool = BufferPool.open(self.path, capacity=capacity)
        self.addCleanup(self.pool.close)
        self.heap = HeapFile(self.pool, SCHEMA, first_page_id)
        return self.heap


class TestInsertAndGet(HeapTestCase):
    def test_round_trips_one_row(self):
        rid = self.heap.insert((1, "ada", 36))
        self.assertEqual(self.heap.get(rid), (1, "ada", 36))
        self.assertEqual(len(self.heap), 1)

    def test_row_ids_are_distinct_and_start_on_the_first_page(self):
        rids = [self.heap.insert((i, f"row{i}", i)) for i in range(3)]
        self.assertEqual(len(set(rids)), 3)
        self.assertEqual(
            rids, [RowId(self.heap.first_page_id, slot) for slot in range(3)]
        )

    def test_round_trips_nulls(self):
        rid = self.heap.insert((1, None, None))
        self.assertEqual(self.heap.get(rid), (1, None, None))

    def test_get_on_an_unknown_page_raises(self):
        with self.assertRaises(RowNotFoundError):
            self.heap.get(RowId(999, 0))

    def test_get_on_an_unused_slot_raises(self):
        with self.assertRaises(RowNotFoundError):
            self.heap.get(RowId(self.heap.first_page_id, 7))

    def test_a_row_too_large_for_a_page_is_rejected(self):
        with self.assertRaises(NoRoomError):
            self.heap.insert((1, "x" * MAX_ROW_SIZE, 1))


class TestGrowth(HeapTestCase):
    def test_the_heap_chains_a_new_page_when_the_first_one_fills(self):
        self.assertEqual(len(self.heap.page_ids), 1)
        for i in range(200):  # ~30 bytes each, comfortably more than one page
            self.heap.insert((i, f"name-{i:04d}", i))
        self.assertGreater(len(self.heap.page_ids), 1)
        self.heap.verify()  # the chain in the file matches the one in memory

    def test_every_row_is_findable_after_the_heap_spans_pages(self):
        rids = {i: self.heap.insert((i, f"name-{i:04d}", i)) for i in range(500)}
        for i, rid in rids.items():
            with self.subTest(row=i):
                self.assertEqual(self.heap.get(rid), (i, f"name-{i:04d}", i))

    def test_variable_length_rows_pack_without_gaps(self):
        sizes = [10, 4000, 10, 4000, 10]
        for i, size in enumerate(sizes):
            self.heap.insert((i, "x" * size, None))
        self.assertEqual(len(self.heap), len(sizes))
        self.assertEqual(
            sorted(len(row[1]) for row in self.heap), sorted(sizes)
        )


class TestDelete(HeapTestCase):
    def test_deleted_rows_disappear_from_get_and_scan(self):
        rids = [self.heap.insert((i, f"row{i}", i)) for i in range(5)]
        self.heap.delete(rids[2])
        with self.assertRaises(RowNotFoundError):
            self.heap.get(rids[2])
        self.assertEqual(len(self.heap), 4)
        self.assertNotIn(2, [row[0] for row in self.heap])

    def test_double_delete_raises(self):
        rid = self.heap.insert((1, "a", 1))
        self.heap.delete(rid)
        with self.assertRaises(RowNotFoundError):
            self.heap.delete(rid)

    def test_space_from_deletes_is_reused_instead_of_growing_the_file(self):
        """A delete has to put its page back in play, or the heap grows forever."""
        filler = "x" * 500
        rids = [self.heap.insert((i, filler, i)) for i in range(100)]
        pages_before = len(self.heap.page_ids)
        for rid in rids:
            self.heap.delete(rid)
        for i in range(100):
            self.heap.insert((i, filler, i))
        self.assertEqual(len(self.heap.page_ids), pages_before)

    def test_deleting_everything_leaves_an_empty_but_valid_heap(self):
        rids = [self.heap.insert((i, "x", i)) for i in range(50)]
        for rid in rids:
            self.heap.delete(rid)
        self.assertEqual(len(self.heap), 0)
        self.assertEqual(list(self.heap), [])


class TestUpdate(HeapTestCase):
    def test_a_same_size_update_keeps_the_row_id(self):
        rid = self.heap.insert((1, "aaa", 1))
        self.assertEqual(self.heap.update(rid, (1, "bbb", 2)), rid)
        self.assertEqual(self.heap.get(rid), (1, "bbb", 2))

    def test_a_growing_update_keeps_the_row_id_while_the_page_has_room(self):
        rid = self.heap.insert((1, "aaa", 1))
        self.assertEqual(self.heap.update(rid, (1, "a" * 500, 1)), rid)
        self.assertEqual(self.heap.get(rid), (1, "a" * 500, 1))

    def test_an_update_that_no_longer_fits_moves_the_row_and_says_so(self):
        self.heap.insert((0, "x" * 2000, 0))
        rid = self.heap.insert((1, "y" * 1500, 1))
        self.heap.insert((2, "z" * 500, 2))
        new_rid = self.heap.update(rid, (1, "y" * 3000, 1))
        self.assertNotEqual(new_rid, rid)
        self.assertEqual(self.heap.get(new_rid), (1, "y" * 3000, 1))
        self.assertEqual(len(self.heap), 3)

    def test_update_rejects_a_row_that_breaks_the_schema(self):
        rid = self.heap.insert((1, "a", 1))
        with self.assertRaises(Exception):
            self.heap.update(rid, (None, "a", 1))
        self.assertEqual(self.heap.get(rid), (1, "a", 1))


class TestScan(HeapTestCase):
    def test_scan_yields_every_row_with_its_id(self):
        expected = {}
        for i in range(300):
            rid = self.heap.insert((i, f"name-{i}", i * 2))
            expected[rid] = (i, f"name-{i}", i * 2)
        self.assertEqual(dict(self.heap.scan()), expected)

    def test_scan_visits_pages_in_chain_order(self):
        for i in range(300):
            self.heap.insert((i, f"name-{i:04d}", i))
        seen = [rid.page_id for rid, _ in self.heap.scan()]
        order = list(dict.fromkeys(seen))
        self.assertEqual(order, list(self.heap.page_ids))

    def test_scan_holds_no_pins_while_the_consumer_is_slow(self):
        """A scan must not pin a page across a yield, or a slow consumer with a
        small pool would deadlock the whole database."""
        for i in range(300):
            self.heap.insert((i, f"name-{i}", i))
        for _rid, _row in self.heap.scan():
            self.pool.assert_no_pins()
            break


class TestPersistence(HeapTestCase):
    def test_rows_survive_a_reopen(self):
        rids = {i: self.heap.insert((i, f"name-{i}", i)) for i in range(100)}
        self.reopen()
        for i, rid in rids.items():
            with self.subTest(row=i):
                self.assertEqual(self.heap.get(rid), (i, f"name-{i}", i))

    def test_the_chain_and_free_space_map_are_rebuilt_on_reopen(self):
        for i in range(300):
            self.heap.insert((i, f"name-{i:04d}", i))
        pages = self.heap.page_ids
        self.reopen()
        self.assertEqual(self.heap.page_ids, pages)
        # The rebuilt free-space map must still find the room a delete freed.
        rid, _ = next(iter(self.heap.scan()))
        self.heap.delete(rid)
        self.heap.insert((999, "late arrival", 999))
        self.assertEqual(self.heap.page_ids, pages)

    def test_deletes_survive_a_reopen(self):
        rids = [self.heap.insert((i, f"row{i}", i)) for i in range(20)]
        for rid in rids[::2]:
            self.heap.delete(rid)
        self.reopen()
        self.assertEqual(len(self.heap), 10)
        self.assertEqual(sorted(row[0] for row in self.heap), list(range(1, 20, 2)))


class TestMilestone(HeapTestCase):
    """The layer 3 milestone from ROADMAP.md: insert 10 000 rows, reopen, scan
    them all back in order."""

    ROWS = 10_000

    def test_ten_thousand_rows_reopen_and_scan_in_order(self):
        random.seed(3)  # varied row sizes, same sizes on every run
        expected = [
            (i, "x" * random.randint(0, 200), None if i % 7 == 0 else i)
            for i in range(self.ROWS)
        ]
        for row in expected:
            self.heap.insert(row)

        self.assertEqual(len(self.heap), self.ROWS)
        self.reopen(capacity=8)  # a pool far too small to hold the table

        scanned = list(self.heap)
        self.assertEqual(len(scanned), self.ROWS)
        self.assertEqual(
            scanned, expected, "a heap scan returns rows in insertion order"
        )
        self.heap.verify()

    def test_deleting_half_of_a_large_heap_and_refilling_it(self):
        rids = [self.heap.insert((i, f"name-{i:05d}", i)) for i in range(2000)]
        for rid in rids[::2]:
            self.heap.delete(rid)
        self.assertEqual(len(self.heap), 1000)

        pages_before = len(self.heap.page_ids)
        for i in range(1000):  # same encoded size as the rows we deleted
            self.heap.insert((-i, f"name-{i:05d}", i))
        self.assertEqual(len(self.heap), 2000)
        self.assertLessEqual(
            len(self.heap.page_ids),
            pages_before + 1,
            "refilling a half-empty heap should reuse its pages",
        )
        self.reopen()
        self.assertEqual(len(list(self.heap)), 2000)


if __name__ == "__main__":
    unittest.main()
