"""Tests for layer 2, the buffer pool.

Two things are worth testing here and they are easy to confuse:

* the *cache* behaves (hits, misses, evictions, dirty write-back), and
* the cache is *invisible* -- the data you read back is the same data you would
  have read with no cache at all.

The second is what the persistence tests check, by closing the pool and
reopening the file.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.buffer_pool import (  # noqa: E402
    AllFramesPinnedError,
    BufferPool,
    BufferPoolError,
    PinnedPageError,
)
from pydb.pager import PAGE_SIZE, Pager  # noqa: E402


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
        """Allocate `count` pages stamped with their own id, all unpinned."""
        ids = []
        for _ in range(count):
            page_id, page = pool.new_page()
            page[:STAMP_SIZE] = stamp(page_id)
            pool.unpin_page(page_id, dirty=True)
            ids.append(page_id)
        return ids


def stamp(page_id: int) -> bytes:
    """A fixed-width marker so a page can be identified by its own contents."""
    return b"page-%011d" % page_id


STAMP_SIZE = len(stamp(0))


class TestFetchAndPin(PoolTestCase):
    def test_a_resident_page_is_a_hit_and_an_evicted_one_is_a_miss(self):
        pool = self.pool(capacity=4)
        page_id = self.fill(pool, 1)[0]
        pool.stats.hits = pool.stats.misses = 0

        with pool.pinned(page_id):  # still resident from the allocation
            pass
        self.assertEqual((pool.stats.hits, pool.stats.misses), (1, 0))

        self.fill(pool, 4)  # push it out of every frame
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
        pool.unpin_page(page_id, dirty=False)  # must not un-dirty the frame
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
        """`page[:3] = b"oops"` grows a bytearray. It must not reach the disk."""
        pool = self.pool()
        page_id = self.fill(pool, 1)[0]
        page = pool.fetch_page(page_id)
        page[:3] = b"oops"
        with self.assertRaises(BufferPoolError):
            pool.unpin_page(page_id, dirty=True)
        del page[PAGE_SIZE:]  # repair the frame so closing the pool can flush
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
        """The trap the roadmap warns about: pin counts are the whole point."""
        pool = self.pool(capacity=3)
        ids = self.fill(pool, 3)
        pool.fetch_page(ids[0])  # hold one frame hostage
        self.fill(pool, 3)  # forces evictions among the other two frames
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
        self.fill(pool, 2)  # both original frames get reused
        self.assertGreater(pool.stats.evictions, 0)
        self.assertGreater(pool.stats.disk_writes, writes_before)
        with pool.pinned(ids[0]) as page:
            self.assertEqual(page[:5], b"newer")

    def test_evicting_a_clean_page_does_not_write(self):
        pool = self.pool(capacity=2)
        ids = self.fill(pool, 2)
        pool.flush_all()
        for page_id in ids:
            with pool.pinned(page_id):  # read only, never dirty
                pass
        writes_before = pool.stats.disk_writes
        self.fill(pool, 2)
        self.assertGreater(pool.stats.evictions, 0)
        self.assertEqual(pool.stats.disk_writes, writes_before)

    def test_clock_gives_a_recently_used_page_a_second_chance(self):
        """Second chance only means anything once the hand has cleared the bits.

        A freshly loaded page arrives with its reference bit set, so the first
        sweep across a full pool clears everything and evicts blindly. The
        interesting case is the sweep *after* that: of two pages with cleared
        bits, touching one should cost the other its frame.
        """
        pool = self.pool(capacity=3)
        self.fill(pool, 3)
        self.fill(pool, 1)  # first sweep: clears all three reference bits

        b, c = pool.resident_pages()[:2]
        with pool.pinned(b):  # re-set b's bit only
            pass
        self.fill(pool, 1)

        self.assertIn(b, pool.resident_pages(), "the touched page kept its frame")
        self.assertNotIn(c, pool.resident_pages(), "the untouched page lost it")


class TestAllocation(PoolTestCase):
    def test_new_page_is_zeroed_and_pinned(self):
        pool = self.pool()
        page_id, page = pool.new_page()
        self.assertEqual(page, bytearray(PAGE_SIZE))
        self.assertEqual(pool.pin_count(page_id), 1)
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
        # The page now holds a free-list link, not the old contents.
        self.assertEqual(pool.pager.allocate_page(), page_id)


class TestPersistence(PoolTestCase):
    def test_close_flushes_dirty_frames(self):
        with BufferPool.open(self.path, capacity=4) as pool:
            page_id, page = pool.new_page()
            page[:7] = b"durable"
            pool.unpin_page(page_id, dirty=True)
        with Pager(self.path) as pager:  # bypass the pool entirely
            self.assertEqual(pager.read_page(page_id)[:7], b"durable")

    def test_an_unreported_change_can_be_lost(self):
        """Documenting the sharp edge: `dirty=True` is the caller's job.

        This is not a bug to fix, it is the contract. A pool that guessed which
        pages changed would have to copy every page it handed out, which is
        exactly the cost layer 2 exists to avoid.
        """
        with BufferPool.open(self.path, capacity=4) as pool:
            page_id, page = pool.new_page()
            page[:4] = b"lost"
            pool.unpin_page(page_id, dirty=False)
        with Pager(self.path) as pager:
            self.assertEqual(pager.read_page(page_id)[:4], bytearray(4))

    def test_flush_page_leaves_the_frame_resident_and_pinned(self):
        pool = self.pool()
        page_id, page = pool.new_page()
        page[:4] = b"stay"
        pool.unpin_page(page_id, dirty=True)
        pool.fetch_page(page_id)  # dirty *and* pinned
        self.assertTrue(pool.flush_page(page_id))
        self.assertFalse(pool.flush_page(page_id))  # no longer dirty
        self.assertEqual(pool.pin_count(page_id), 1)
        self.assertIn(page_id, pool.resident_pages())
        pool.unpin_page(page_id)

    def test_closed_pool_refuses_work(self):
        pool = BufferPool.open(self.path)
        pool.close()
        pool.close()  # idempotent
        with self.assertRaises(BufferPoolError):
            pool.new_page()


class TestMilestone(PoolTestCase):
    """The layer 2 milestone from ROADMAP.md.

    A file far larger than the pool, a small pool, correct results, and an
    eviction counter proving pages really were written out and read back.
    """

    PAGES = 25600  # 25600 * 4 KB = 100 MB
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

        # Read every page back in an order the pool cannot have prefetched.
        with BufferPool.open(self.path, capacity=self.CAPACITY) as pool:
            for page_id in range(self.PAGES, 0, -1):
                with pool.pinned(page_id) as page:
                    self.assertEqual(bytes(page[:STAMP_SIZE]), stamp(page_id))
            self.assertEqual(pool.stats.disk_reads, self.PAGES)
            self.assertEqual(pool.stats.disk_writes, 0, "reads must not write")
            pool.assert_no_pins()

    def test_a_hot_page_stays_in_memory(self):
        """The reason this layer exists: the B+Tree root must not hit the disk."""
        with BufferPool.open(self.path, capacity=self.CAPACITY) as pool:
            root = self.fill(pool, 1)[0]
            others = self.fill(pool, 500)
            with pool.pinned(root):  # bring the root back in, then start counting
                pass
            reads_before = pool.stats.disk_reads
            for page_id in others:
                with pool.pinned(root):  # touch the root on every "descent"
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
