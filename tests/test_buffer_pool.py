from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.buffer_pool import (
    AllFramesPinnedError,
    BufferPool,
    BufferPoolError,
    PinnedPageError,
)
from pydb.pager import PAGE_SIZE, Pager


class PoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")

    def pool(self, capacity: int = 4) -> BufferPool:
        pool = BufferPool.open(self.path, capacity=capacity)
        self.addCleanup(pool.close)
        return pool

    def fill(self, pool: BufferPool, count: int) -> list[int]:
        ids = []
        for _ in range(count):
            page_id, page = pool.new_page()
            page[:STAMP_SIZE] = stamp(page_id)
            pool.unpin_page(page_id, dirty=True)
            ids.append(page_id)
        return ids


def stamp(page_id: int) -> bytes:
    return b"page-%011d" % page_id


STAMP_SIZE = len(stamp(0))


class TestFetchAndPin(PoolTestCase):
    def test_a_resident_page_is_a_hit_and_an_evicted_one_is_a_miss(self):
        pool = self.pool(capacity=4)
        page_id = self.fill(pool, 1)[0]
        pool.stats.hits = pool.stats.misses = 0

        with pool.pinned(page_id):
            pass
        self.assertEqual((pool.stats.hits, pool.stats.misses), (1, 0))

        self.fill(pool, 4)
        self.assertNotIn(page_id, pool.resident_pages())
        with pool.pinned(page_id):
            pass
        with pool.pinned(page_id):
            pass
        self.assertEqual((pool.stats.hits, pool.stats.misses), (2, 1))

    def test_fetch_returns_the_live_buffer_not_a_copy(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        first = pool.fetch_page(page_id)
        second = pool.fetch_page(page_id)
        first[:4] = b"edit"
        self.assertEqual(second[:4], b"edit")
        pool.unpin_page(page_id, dirty=True)
        pool.unpin_page(page_id)

    def test_pins_nest_and_are_counted(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.fetch_page(page_id)
        pool.fetch_page(page_id)
        self.assertEqual(pool.pin_count(page_id), 2)
        pool.unpin_page(page_id)
        self.assertEqual(pool.pin_count(page_id), 1)
        pool.unpin_page(page_id)
        self.assertEqual(pool.pin_count(page_id), 0)

    def test_unbalanced_unpin_is_an_error(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        with self.assertRaises(BufferPoolError):
            pool.unpin_page(page_id)

    def test_dirty_flag_is_sticky_across_unpins(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.flush_all()
        pool.fetch_page(page_id)
        pool.fetch_page(page_id)
        pool.unpin_page(page_id, dirty=True)
        pool.unpin_page(page_id, dirty=False)
        self.assertEqual(pool.dirty_pages(), [page_id])

    def test_pinned_block_marks_dirty_even_if_it_raises(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.flush_all()
        with self.assertRaises(ZeroDivisionError):
            with pool.pinned(page_id, dirty=True) as page:
                page[:4] = b"oops"
                raise ZeroDivisionError
        self.assertEqual(pool.pin_count(page_id), 0)
        self.assertEqual(pool.dirty_pages(), [page_id])

    def test_resizing_a_page_is_caught_at_unpin(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        page = pool.fetch_page(page_id)
        page[:3] = b"oops"
        with self.assertRaises(BufferPoolError):
            pool.unpin_page(page_id, dirty=True)
        del page[PAGE_SIZE:]
        pool.unpin_page(page_id)

    def test_meta_page_is_not_cacheable(self):
        pool = self.pool()
        with self.assertRaises(BufferPoolError):
            pool.fetch_page(0)

    def test_rejects_pages_past_the_end_of_the_file(self):
        pool = self.pool()
        with self.assertRaises(BufferPoolError):
            pool.fetch_page(99)

    def test_assert_no_pins_catches_a_leak(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.assert_no_pins()
        pool.fetch_page(page_id)
        with self.assertRaises(BufferPoolError):
            pool.assert_no_pins()
        pool.unpin_page(page_id)


class TestEviction(PoolTestCase):
    def test_a_pinned_page_is_never_evicted(self):
        pool = self.pool(capacity=3)
        ids = self.fill(pool, 3)
        pool.fetch_page(ids[0])
        self.fill(pool, 3)
        self.assertIn(ids[0], pool.resident_pages())
        self.assertEqual(pool.pin_count(ids[0]), 1)
        pool.unpin_page(ids[0])

    def test_all_frames_pinned_is_an_error_not_a_corruption(self):
        pool = self.pool(capacity=2)
        ids = self.fill(pool, 3)
        pool.fetch_page(ids[0])
        pool.fetch_page(ids[1])
        with self.assertRaises(AllFramesPinnedError):
            pool.fetch_page(ids[2])
        pool.unpin_page(ids[0])
        pool.unpin_page(ids[1])

    def test_evicting_a_dirty_page_writes_it_back(self):
        pool = self.pool(capacity=2)
        ids = self.fill(pool, 2)
        with pool.pinned(ids[0], dirty=True) as page:
            page[:5] = b"dirty"
        pool.flush_all()

        with pool.pinned(ids[0], dirty=True) as page:
            page[:5] = b"newer"
        writes_before = pool.stats.disk_writes
        self.fill(pool, 2)
        self.assertGreater(pool.stats.evictions, 0)
        self.assertGreater(pool.stats.disk_writes, writes_before)
        with pool.pinned(ids[0]) as page:
            self.assertEqual(page[:5], b"newer")

    def test_evicting_a_clean_page_does_not_write(self):
        pool = self.pool(capacity=2)
        ids = self.fill(pool, 2)
        pool.flush_all()
        for page_id in ids:
            with pool.pinned(page_id):
                pass
        writes_before = pool.stats.disk_writes
        self.fill(pool, 2)
        self.assertGreater(pool.stats.evictions, 0)
        self.assertEqual(pool.stats.disk_writes, writes_before)

    def test_clock_gives_a_recently_used_page_a_second_chance(self):
        pool = self.pool(capacity=3)
        self.fill(pool, 3)
        self.fill(pool, 1)

        b, c = pool.resident_pages()[:2]
        with pool.pinned(b):
            pass
        self.fill(pool, 1)

        self.assertIn(b, pool.resident_pages(), "the touched page kept its frame")
        self.assertNotIn(c, pool.resident_pages(), "the untouched page lost it")


class TestAllocation(PoolTestCase):
    def test_new_page_is_zeroed_pinned_and_already_dirty(self):
        pool = self.pool()
        page_id, page = pool.new_page()
        self.assertEqual(page, bytearray(PAGE_SIZE))
        self.assertEqual(pool.pin_count(page_id), 1)
        self.assertEqual(pool.dirty_pages(), [page_id])
        pool.unpin_page(page_id)

    def test_new_page_does_not_read_from_disk(self):
        pool = self.pool()
        reads_before = pool.stats.disk_reads
        page_id, _ = pool.new_page()
        pool.unpin_page(page_id)
        self.assertEqual(pool.stats.disk_reads, reads_before)

    def test_free_page_drops_the_cached_copy(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        self.assertIn(page_id, pool.resident_pages())
        pool.free_page(page_id)
        self.assertNotIn(page_id, pool.resident_pages())
        self.assertEqual(pool.pager.free_pages(), [page_id])

    def test_a_freed_page_gives_its_frame_back_to_the_pool(self):
        pool = self.pool(capacity=2)
        ids = self.fill(pool, 2)
        pool.free_page(ids[0])
        self.fill(pool, 2)
        self.assertEqual(len(pool.resident_pages()), 2)

    def test_cannot_free_a_pinned_page(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.fetch_page(page_id)
        with self.assertRaises(PinnedPageError):
            pool.free_page(page_id)
        pool.unpin_page(page_id)

    def test_freeing_discards_dirty_bytes_instead_of_flushing_them(self):
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        pool.free_page(page_id)
        self.assertEqual(pool.dirty_pages(), [])
        self.assertEqual(pool.pager.allocate_page(), page_id)


class TestPersistence(PoolTestCase):
    def test_close_flushes_dirty_frames(self):
        with BufferPool.open(self.path, capacity=4) as pool:
            page_id, page = pool.new_page()
            page[:7] = b"durable"
            pool.unpin_page(page_id, dirty=True)
        with Pager(self.path) as pager:
            self.assertEqual(pager.read_page(page_id)[:7], b"durable")

    def test_an_unreported_change_can_be_lost(self):
        with BufferPool.open(self.path, capacity=4) as pool:
            page_id, page = pool.new_page()
            page[:4] = b"orig"
            pool.unpin_page(page_id, dirty=True)

        with BufferPool.open(self.path, capacity=4) as pool:
            with pool.pinned(page_id) as page:
                page[:4] = b"lost"
        with Pager(self.path) as pager:
            self.assertEqual(pager.read_page(page_id)[:4], b"orig")

    def test_flush_page_leaves_the_frame_resident_and_pinned(self):
        pool = self.pool()
        page_id, page = pool.new_page()
        page[:4] = b"stay"
        pool.unpin_page(page_id, dirty=True)
        pool.fetch_page(page_id)
        self.assertTrue(pool.flush_page(page_id))
        self.assertFalse(pool.flush_page(page_id))
        self.assertEqual(pool.pin_count(page_id), 1)
        self.assertIn(page_id, pool.resident_pages())
        pool.unpin_page(page_id)

    def test_closed_pool_refuses_work(self):
        pool = BufferPool.open(self.path)
        pool.close()
        pool.close()
        with self.assertRaises(BufferPoolError):
            pool.new_page()


class TestMilestone(PoolTestCase):
    PAGES = 25600
    CAPACITY = 50

    def test_100mb_file_through_a_50_frame_pool(self):
        with BufferPool.open(self.path, capacity=self.CAPACITY) as pool:
            for _ in range(self.PAGES):
                page_id, page = pool.new_page()
                page[:STAMP_SIZE] = stamp(page_id)
                pool.unpin_page(page_id, dirty=True)
            self.assertGreater(
                pool.stats.evictions,
                self.PAGES - self.CAPACITY - 1,
                "a 50-frame pool must have evicted nearly every page it wrote",
            )

        self.assertEqual(
            os.path.getsize(self.path), (self.PAGES + 1) * PAGE_SIZE
        )

        with BufferPool.open(self.path, capacity=self.CAPACITY) as pool:
            for page_id in range(self.PAGES, 0, -1):
                with pool.pinned(page_id) as page:
                    self.assertEqual(bytes(page[:STAMP_SIZE]), stamp(page_id))
            self.assertEqual(pool.stats.disk_reads, self.PAGES)
            self.assertEqual(pool.stats.disk_writes, 0, "reads must not write")
            pool.assert_no_pins()

    def test_a_hot_page_stays_in_memory(self):
        with BufferPool.open(self.path, capacity=self.CAPACITY) as pool:
            root = self.fill(pool, 1)[0]
            others = self.fill(pool, 500)
            with pool.pinned(root):
                pass
            reads_before = pool.stats.disk_reads
            for page_id in others:
                with pool.pinned(root):
                    pass
                with pool.pinned(page_id):
                    pass
            self.assertEqual(
                pool.stats.disk_reads - reads_before,
                len(others),
                "only the cold pages should have been read from disk",
            )


if __name__ == "__main__":
    unittest.main()
