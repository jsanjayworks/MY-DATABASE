"""Tests for layer 3b, the slotted page.

These run on a bare `bytearray` rather than a real page from the pool -- the
slotted page does not know or care where its bytes came from, and testing it in
isolation means a failure here is never a buffer pool bug.

`verify()` is called after almost every mutation. That is the habit the roadmap
recommends for the B+Tree, and it is worth starting one layer early.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.pager import PAGE_SIZE  # noqa: E402
from pydb.slotted_page import (  # noqa: E402
    HEADER_SIZE,
    MAX_ROW_SIZE,
    SLOT_SIZE,
    NoRoomError,
    RowTooLargeError,
    SlottedPage,
    SlottedPageError,
)


class SlottedPageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.page = SlottedPage.initialize(bytearray(PAGE_SIZE))

    def tearDown(self) -> None:
        self.page.verify()  # every test leaves the page consistent

    def insert_all(self, *rows: bytes) -> list[int]:
        return [self.page.insert(row) for row in rows]


class TestEmptyPage(SlottedPageTestCase):
    def test_a_fresh_page_has_no_slots_and_all_the_space(self):
        self.assertEqual(len(self.page), 0)
        self.assertEqual(self.page.slot_count, 0)
        self.assertEqual(self.page.free_end, PAGE_SIZE)
        self.assertEqual(self.page.free_space, PAGE_SIZE - HEADER_SIZE)
        self.assertEqual(self.page.next_page, 0)
        self.assertEqual(self.page.rows(), [])

    def test_refuses_to_read_a_slot_that_does_not_exist(self):
        with self.assertRaises(KeyError):
            self.page.read(0)

    def test_refuses_a_buffer_that_is_not_page_sized(self):
        with self.assertRaises(SlottedPageError):
            SlottedPage(bytearray(100))

    def test_refuses_a_page_that_was_never_initialised(self):
        """Page type 0 is what a zeroed page looks like. Catch it, do not parse it."""
        with self.assertRaises(SlottedPageError):
            SlottedPage(bytearray(PAGE_SIZE))

    def test_next_page_is_a_settable_header_field(self):
        self.page.next_page = 42
        self.assertEqual(self.page.next_page, 42)


class TestInsertAndRead(SlottedPageTestCase):
    def test_slots_are_handed_out_in_order_from_zero(self):
        self.assertEqual(self.insert_all(b"a", b"bb", b"ccc"), [0, 1, 2])
        self.assertEqual(len(self.page), 3)

    def test_rows_read_back_exactly(self):
        rows = [b"", b"a", b"x" * 500, bytes(range(256))]
        slots = self.insert_all(*rows)
        for slot, row in zip(slots, rows):
            with self.subTest(slot=slot):
                self.assertEqual(self.page.read(slot), row)

    def test_rows_grow_down_from_the_end_of_the_page(self):
        self.page.insert(b"x" * 100)
        self.assertEqual(self.page.free_end, PAGE_SIZE - 100)
        self.page.insert(b"y" * 50)
        self.assertEqual(self.page.free_end, PAGE_SIZE - 150)

    def test_each_row_costs_its_length_plus_a_slot(self):
        before = self.page.free_space
        self.page.insert(b"x" * 40)
        self.assertEqual(before - self.page.free_space, 40 + SLOT_SIZE)

    def test_rows_returns_slot_and_bytes_in_slot_order(self):
        self.insert_all(b"one", b"two", b"three")
        self.assertEqual(
            self.page.rows(), [(0, b"one"), (1, b"two"), (2, b"three")]
        )

    def test_fills_a_page_to_the_last_byte(self):
        """The arithmetic has to be exact, so store the largest possible row."""
        slot = self.page.insert(b"z" * MAX_ROW_SIZE)
        self.assertEqual(self.page.free_space, 0)
        self.assertEqual(len(self.page.read(slot)), MAX_ROW_SIZE)

    def test_a_full_page_raises_no_room_and_is_unchanged(self):
        self.page.insert(b"z" * MAX_ROW_SIZE)
        with self.assertRaises(NoRoomError):
            self.page.insert(b"one more")
        self.assertEqual(len(self.page), 1)

    def test_a_row_too_big_for_any_page_is_a_different_error(self):
        with self.assertRaises(RowTooLargeError):
            self.page.insert(b"z" * (MAX_ROW_SIZE + 1))


class TestDelete(SlottedPageTestCase):
    def test_deleting_leaves_the_other_slots_alone(self):
        a, b, c = self.insert_all(b"aaa", b"bbb", b"ccc")
        self.page.delete(b)
        self.assertEqual(self.page.read(a), b"aaa")
        self.assertEqual(self.page.read(c), b"ccc")
        self.assertEqual(len(self.page), 2)
        self.assertEqual(self.page.slots(), [a, c])

    def test_reading_a_deleted_slot_raises(self):
        slot = self.page.insert(b"gone")
        self.page.delete(slot)
        self.assertTrue(self.page.is_deleted(slot))
        with self.assertRaises(KeyError):
            self.page.read(slot)

    def test_double_delete_raises(self):
        slot = self.page.insert(b"gone")
        self.page.delete(slot)
        with self.assertRaises(KeyError):
            self.page.delete(slot)

    def test_delete_does_not_reclaim_space_on_its_own(self):
        """The tombstone decision, made visible: deletes leave dead bytes behind."""
        slot = self.page.insert(b"x" * 100)
        free_before = self.page.free_space
        self.page.delete(slot)
        self.assertEqual(self.page.free_space, free_before)
        self.assertEqual(self.page.dead_space, 100)

    def test_a_freed_slot_is_reused_before_the_slot_array_grows(self):
        a, b = self.insert_all(b"aa", b"bb")
        self.page.delete(a)
        self.assertEqual(self.page.insert(b"cc"), a)
        self.assertEqual(self.page.slot_count, 2)
        self.assertEqual(self.page.read(a), b"cc")
        self.assertEqual(self.page.read(b), b"bb")


class TestCompaction(SlottedPageTestCase):
    def test_compaction_reclaims_dead_bytes(self):
        slots = self.insert_all(*[b"x" * 100 for _ in range(10)])
        for slot in slots[:5]:
            self.page.delete(slot)
        reclaimed = self.page.compact()
        self.assertEqual(reclaimed, 500)
        self.assertEqual(self.page.dead_space, 0)
        self.assertEqual(len(self.page), 5)

    def test_compaction_moves_bytes_but_never_slot_indices(self):
        """The whole point of slot addressing: row ids survive compaction."""
        a, b, c = self.insert_all(b"first", b"second", b"third")
        self.page.delete(b)
        offset_before = self.page._slot(c)[0]
        self.page.compact()
        self.assertEqual(self.page.read(a), b"first")
        self.assertEqual(self.page.read(c), b"third")
        self.assertNotEqual(self.page._slot(c)[0], offset_before)

    def test_compaction_zeroes_the_space_it_reclaims(self):
        """A deleted row's bytes should not survive in the file."""
        self.page.insert(b"keep")
        secret = self.page.insert(b"SECRET-VALUE")
        self.page.delete(secret)
        self.page.compact()
        self.assertNotIn(b"SECRET-VALUE", bytes(self.page.data))

    def test_insert_compacts_automatically_when_that_is_the_only_way(self):
        big = b"x" * 1000
        slots = self.insert_all(big, big, big, big)  # 4000 of 4084 bytes
        self.page.delete(slots[0])
        self.page.delete(slots[2])
        self.assertLess(self.page.free_space, len(big))
        slot = self.page.insert(b"y" * 1500)  # only fits after compaction
        self.assertEqual(self.page.read(slot), b"y" * 1500)
        self.assertEqual(len(self.page), 3)

    def test_compacting_an_empty_page_resets_it(self):
        slot = self.page.insert(b"x" * 200)
        self.page.delete(slot)
        self.page.compact()
        self.assertEqual(self.page.free_end, PAGE_SIZE)


class TestReplace(SlottedPageTestCase):
    def test_same_length_replacement_is_in_place(self):
        slot = self.page.insert(b"aaaa")
        offset = self.page._slot(slot)[0]
        self.assertTrue(self.page.replace(slot, b"bbbb"))
        self.assertEqual(self.page.read(slot), b"bbbb")
        self.assertEqual(self.page._slot(slot)[0], offset)

    def test_a_longer_replacement_keeps_the_slot_and_moves_the_bytes(self):
        a, b = self.insert_all(b"short", b"neighbour")
        self.assertTrue(self.page.replace(a, b"much longer value"))
        self.assertEqual(self.page.read(a), b"much longer value")
        self.assertEqual(self.page.read(b), b"neighbour")
        self.assertEqual(len(self.page), 2)

    def test_a_shorter_replacement_works_too(self):
        slot = self.page.insert(b"x" * 100)
        self.assertTrue(self.page.replace(slot, b"tiny"))
        self.assertEqual(self.page.read(slot), b"tiny")

    def test_replace_reuses_the_row_it_is_replacing_as_free_space(self):
        """Growing a row on an otherwise full page still works: the old row's
        own bytes are reclaimable."""
        slot = self.page.insert(b"x" * (MAX_ROW_SIZE - 10))
        self.assertTrue(self.page.replace(slot, b"y" * MAX_ROW_SIZE))
        self.assertEqual(self.page.read(slot), b"y" * MAX_ROW_SIZE)

    def test_replace_returns_false_and_changes_nothing_when_it_cannot_fit(self):
        keep = self.page.insert(b"k" * 2000)
        slot = self.page.insert(b"s" * 1000)
        self.assertFalse(self.page.replace(slot, b"t" * 3000))
        self.assertEqual(self.page.read(slot), b"s" * 1000)
        self.assertEqual(self.page.read(keep), b"k" * 2000)

    def test_replacing_a_deleted_slot_raises(self):
        slot = self.page.insert(b"gone")
        self.page.delete(slot)
        with self.assertRaises(KeyError):
            self.page.replace(slot, b"back")


class TestVerify(SlottedPageTestCase):
    def test_verify_catches_a_wrong_live_count(self):
        self.page.insert(b"row")
        self.page._set(6, 5)
        with self.assertRaises(SlottedPageError):
            self.page.verify()
        self.page._set(6, 1)  # repair, so tearDown's verify passes

    def test_verify_catches_a_slot_pointing_into_free_space(self):
        slot = self.page.insert(b"row")
        self.page._write_slot(slot, HEADER_SIZE, 3)
        with self.assertRaises(SlottedPageError):
            self.page.verify()
        self.page._write_slot(slot, PAGE_SIZE - 3, 3)

    def test_verify_catches_overlapping_rows(self):
        a, b = self.insert_all(b"aaaa", b"bbbb")
        offset, length = self.page._slot(b)
        self.page._write_slot(a, offset + 1, length)
        with self.assertRaises(SlottedPageError):
            self.page.verify()
        self.page.delete(a)


if __name__ == "__main__":
    unittest.main()
