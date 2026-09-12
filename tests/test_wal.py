"""Tests for layer 5, the write-ahead log.

Durability is the one property you cannot test by calling functions and checking
return values: it is about what survives a process that stops existing. So the
tests here come in three kinds.

* **Order** -- assertions about what has and has not reached the data file at each
  point in a commit, because the write-ahead rule *is* an ordering rule.
* **Recovery** -- a hand-built log file replayed into a database, including the
  torn and half-written shapes a real crash leaves behind.
* **Crashing** -- an actual child process, actually killed, at a moment it does
  not choose. That one is the milestone.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pydb.btree import BTree  # noqa: E402
from pydb.buffer_pool import AllFramesPinnedError, BufferPool  # noqa: E402
from pydb.pager import META_SLOT_ROOT, PAGE_SIZE, Pager  # noqa: E402
from pydb.record import ColumnType, encode_key  # noqa: E402
from pydb.wal import (  # noqa: E402
    FLAG_COMMIT,
    FRAME_HEADER_FORMAT,
    FRAME_HEADER_SIZE,
    FRAME_SIZE,
    WAL_HEADER_FORMAT,
    WAL_HEADER_SIZE,
    WAL_MAGIC,
    WAL_VERSION,
    CorruptWalError,
    Wal,
    WalError,
    _frame_crc,
)


def int_key(value: int) -> bytes:
    return encode_key(ColumnType.INT, value)


def handmade_log(path: str, frames: list[tuple[int, int, bytes, int]]) -> None:
    """Write a log file containing exactly `frames` as `(lsn, page_id, image, flags)`.

    Building the file by hand is the only way to test recovery against the shapes
    a crash leaves: a frame cut in half, a bad checksum, a transaction with no
    commit flag.
    """
    first_lsn = frames[0][0] if frames else 1
    with open(path, "wb") as f:
        f.write(
            struct.pack(
                WAL_HEADER_FORMAT, WAL_MAGIC, WAL_VERSION, PAGE_SIZE, 0, first_lsn
            )
        )
        for lsn, page_id, image, flags in frames:
            image = bytes(image).ljust(PAGE_SIZE, b"\x00")
            f.write(
                struct.pack(
                    FRAME_HEADER_FORMAT,
                    lsn,
                    page_id,
                    _frame_crc(lsn, page_id, flags, image),
                    flags,
                )
            )
            f.write(image)


class WalTestCase(unittest.TestCase):
    CAPACITY = 32

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.wal_path = self.path + ".wal"

    def open_db(self, capacity: int | None = None) -> tuple[BufferPool, Wal]:
        pool = BufferPool.open(self.path, capacity=capacity or self.CAPACITY)
        wal = Wal(pool)
        self.addCleanup(pool.close)
        self.addCleanup(wal.close)
        return pool, wal

    def raw_page(self, page_id: int) -> bytes:
        """A page straight out of the data file, bypassing pool, log and pager."""
        with open(self.path, "rb") as f:
            f.seek(page_id * PAGE_SIZE)
            return f.read(PAGE_SIZE)

    def log_frames(self) -> list[tuple[int, int, int, bytes]]:
        """Parse the log file: `(lsn, page_id, flags, image)` per frame."""
        with open(self.wal_path, "rb") as f:
            f.read(WAL_HEADER_SIZE)
            out = []
            while True:
                raw = f.read(FRAME_SIZE)
                if len(raw) < FRAME_SIZE:
                    return out
                lsn, page_id, _crc, flags = struct.unpack_from(
                    FRAME_HEADER_FORMAT, raw, 0
                )
                out.append((lsn, page_id, flags, raw[FRAME_HEADER_SIZE:]))


class TestCommit(WalTestCase):
    def test_a_new_log_is_just_a_header(self):
        self.open_db()
        self.assertEqual(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)

    def test_commit_logs_one_frame_per_modified_page_plus_the_meta_page(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:5] = b"hello"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()

        frames = self.log_frames()
        self.assertEqual([f[1] for f in frames], [page_id, 0])
        self.assertEqual(frames[-1][2] & FLAG_COMMIT, FLAG_COMMIT, "last frame commits")
        self.assertEqual(frames[0][2] & FLAG_COMMIT, 0, "earlier frames do not")
        self.assertEqual(frames[0][3][:5], b"hello")

    def test_lsns_start_at_one_and_increase(self):
        pool, wal = self.open_db()
        for _ in range(3):
            page_id, page = pool.new_page()
            page[:1] = b"x"
            pool.unpin_page(page_id, dirty=True)
            wal.commit()
        self.assertEqual([f[0] for f in self.log_frames()], [1, 2, 3, 4, 5, 6])

    def test_committing_nothing_is_a_no_op(self):
        _pool, wal = self.open_db()
        self.assertEqual(wal.commit(), 0)
        self.assertEqual(wal.commit(), 0)
        self.assertEqual(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)

    def test_a_page_modified_twice_in_one_transaction_is_logged_once(self):
        """The log records pages, not writes. This is what keeps it from being
        hundreds of megabytes for a B+Tree whose root is touched constantly."""
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        for i in range(20):
            page[:1] = bytes([i])
            pool.unpin_page(page_id, dirty=True)
            pool.fetch_page(page_id)
        pool.unpin_page(page_id, dirty=True)
        wal.commit()
        self.assertEqual([f[1] for f in self.log_frames()], [page_id, 0])

    def test_the_committed_image_is_the_final_one(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:4] = b"old!"
        pool.unpin_page(page_id, dirty=True)
        with pool.pinned(page_id, dirty=True) as page:
            page[:4] = b"new!"
        wal.commit()
        self.assertEqual(self.log_frames()[0][3][:4], b"new!")


class TestWriteAheadOrdering(WalTestCase):
    """The rule itself: nothing reaches the data file before its log record."""

    def test_an_uncommitted_page_never_reaches_the_data_file(self):
        """There is only ever one transaction here, so filling the pool with
        uncommitted pages is the strongest eviction pressure available: none of
        them may be written out, and none of them are."""
        pool, wal = self.open_db()
        pages = []
        for _ in range(self.CAPACITY - 1):
            page_id, page = pool.new_page()
            page[:9] = b"SECRETVAL"
            pool.unpin_page(page_id, dirty=True)
            pages.append(page_id)
        with open(self.path, "rb") as f:
            self.assertNotIn(b"SECRETVAL", f.read())
        self.assertTrue(all(wal.holds_uncommitted(p) for p in pages))

        wal.commit()
        with open(self.path, "rb") as f:
            self.assertIn(b"SECRETVAL", f.read())

    def test_a_committed_page_is_in_the_log_before_the_data_file_is_synced(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:6] = b"logged"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()
        self.assertEqual(self.log_frames()[0][3][:6], b"logged")
        self.assertEqual(self.raw_page(page_id)[:6], b"logged")

    def test_a_transaction_bigger_than_the_pool_is_refused_not_leaked(self):
        """No-steal has a price, and this is it: a transaction cannot outgrow the
        buffer pool, because no uncommitted page may be written out to make room.
        Refusing loudly beats silently putting uncommitted data in the file."""
        pool, wal = self.open_db(capacity=4)
        with self.assertRaises(AllFramesPinnedError):
            for _ in range(20):
                page_id, page = pool.new_page()
                page[:1] = b"x"
                pool.unpin_page(page_id, dirty=True)
        wal.rollback()

    def test_committing_frees_the_frames_again(self):
        pool, wal = self.open_db(capacity=4)
        for _ in range(20):
            page_id, page = pool.new_page()
            page[:1] = b"x"
            pool.unpin_page(page_id, dirty=True)
            wal.commit()  # each page becomes evictable as soon as it is durable
        self.assertEqual(pool.pager.page_count, 21)


class TestRollback(WalTestCase):
    def test_rollback_restores_the_last_committed_page_contents(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:9] = b"committed"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()

        with pool.pinned(page_id, dirty=True) as page:
            page[:9] = b"scribbled"
        wal.rollback()
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:9]), b"committed")

    def test_rollback_gives_back_pages_allocated_by_the_transaction(self):
        pool, wal = self.open_db()
        wal.commit()
        before = pool.pager.page_count
        for _ in range(5):
            page_id, page = pool.new_page()
            page[:1] = b"x"
            pool.unpin_page(page_id, dirty=True)
        self.assertEqual(pool.pager.page_count, before + 5)
        wal.rollback()
        self.assertEqual(pool.pager.page_count, before)

    def test_rollback_cancels_a_staged_free(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:4] = b"live"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()

        pool.free_page(page_id)
        wal.rollback()
        self.assertEqual(pool.pager.free_pages(), [])
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:4]), b"live", "the page was never clobbered")

    def test_rollback_undoes_a_meta_slot_write(self):
        """The B+Tree root lives in a meta slot, so "the root moved" has to be a
        change a rollback can take back like any other."""
        pool, wal = self.open_db()
        pool.pager.write_meta_slot(META_SLOT_ROOT, 7)
        wal.commit()
        pool.pager.write_meta_slot(META_SLOT_ROOT, 99)
        wal.rollback()
        self.assertEqual(pool.pager.read_meta_slot(META_SLOT_ROOT), 7)

    def test_rolling_back_nothing_is_a_no_op(self):
        _pool, wal = self.open_db()
        wal.rollback()
        self.assertFalse(wal.in_transaction)

    def test_writes_after_a_rollback_still_work(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:1] = b"a"
        pool.unpin_page(page_id, dirty=True)
        wal.rollback()
        page_id, page = pool.new_page()
        page[:1] = b"b"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()
        self.assertEqual(self.raw_page(page_id)[:1], b"b")


class TestCheckpoint(WalTestCase):
    def test_checkpoint_empties_the_log_and_completes_the_data_file(self):
        pool, wal = self.open_db()
        for i in range(10):
            page_id, page = pool.new_page()
            page[:1] = bytes([i])
            pool.unpin_page(page_id, dirty=True)
        wal.commit()
        self.assertGreater(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)

        wal.checkpoint()
        self.assertEqual(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)
        self.assertEqual(self.raw_page(9)[:1], bytes([8]))

    def test_lsns_keep_climbing_across_checkpoints(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:1] = b"a"
        pool.unpin_page(page_id, dirty=True)
        first = wal.commit()
        wal.checkpoint()

        with pool.pinned(page_id, dirty=True) as page:
            page[:1] = b"b"
        second = wal.commit()
        self.assertGreater(second, first)
        self.assertEqual(self.log_frames()[0][0], first + 1)

    def test_checkpointing_mid_transaction_is_refused(self):
        pool, wal = self.open_db()
        page_id, page = pool.new_page()
        page[:1] = b"x"
        pool.unpin_page(page_id, dirty=True)
        with self.assertRaises(WalError):
            wal.checkpoint()
        wal.commit()

    def test_closing_checkpoints(self):
        pool = BufferPool.open(self.path, capacity=8)
        wal = Wal(pool)
        page_id, page = pool.new_page()
        page[:4] = b"done"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()
        wal.close()
        pool.close()
        self.assertEqual(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)
        with Pager(self.path) as pager:
            self.assertEqual(pager.read_page(page_id)[:4], b"done")

    def test_closing_rolls_back_an_open_transaction(self):
        pool = BufferPool.open(self.path, capacity=8)
        wal = Wal(pool)
        page_id, page = pool.new_page()
        page[:4] = b"gone"
        pool.unpin_page(page_id, dirty=True)
        wal.close()
        pool.close()
        with open(self.path, "rb") as f:
            self.assertNotIn(b"gone", f.read())


class TestRecovery(WalTestCase):
    def existing_database(self) -> int:
        """A clean single-page database, closed properly. Returns the page id."""
        pool = BufferPool.open(self.path, capacity=8)
        wal = Wal(pool)
        page_id, page = pool.new_page()
        page[:8] = b"original"
        pool.unpin_page(page_id, dirty=True)
        wal.commit()
        wal.close()
        pool.close()
        return page_id

    def test_a_committed_frame_is_replayed(self):
        page_id = self.existing_database()
        handmade_log(
            self.wal_path, [(10, page_id, b"recovered", FLAG_COMMIT)]
        )
        pool, wal = self.open_db()
        self.assertEqual(wal.recovered_frames, 1)
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:9]), b"recovered")

    def test_frames_after_the_last_commit_are_discarded(self):
        """A transaction that never committed leaves no trace. This is atomicity:
        the log has the bytes, and recovery refuses to use them."""
        page_id = self.existing_database()
        handmade_log(
            self.wal_path,
            [
                (10, page_id, b"committed", FLAG_COMMIT),
                (11, page_id, b"half-done", 0),  # no commit flag ever arrived
            ],
        )
        pool, wal = self.open_db()
        self.assertEqual(wal.recovered_frames, 1)
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:9]), b"committed")

    def test_a_torn_final_frame_is_ignored(self):
        page_id = self.existing_database()
        handmade_log(self.wal_path, [(10, page_id, b"committed", FLAG_COMMIT)])
        with open(self.wal_path, "ab") as f:
            f.write(b"\x00" * (FRAME_SIZE // 3))  # the crash landed mid-append
        pool, wal = self.open_db()
        self.assertEqual(wal.recovered_frames, 1)
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:9]), b"committed")

    def test_a_bad_checksum_stops_recovery_there(self):
        page_id = self.existing_database()
        handmade_log(
            self.wal_path,
            [
                (10, page_id, b"good", FLAG_COMMIT),
                (11, page_id, b"corrupt", FLAG_COMMIT),
            ],
        )
        with open(self.wal_path, "r+b") as f:  # flip a byte in the second frame
            offset = WAL_HEADER_SIZE + FRAME_SIZE + FRAME_HEADER_SIZE
            f.seek(offset)
            f.write(b"X")
        pool, wal = self.open_db()
        self.assertEqual(wal.recovered_frames, 1)
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:4]), b"good")

    def test_an_out_of_sequence_lsn_stops_recovery_there(self):
        page_id = self.existing_database()
        handmade_log(
            self.wal_path,
            [
                (10, page_id, b"good", FLAG_COMMIT),
                (99, page_id, b"from another era", FLAG_COMMIT),
            ],
        )
        pool, wal = self.open_db()
        self.assertEqual(wal.recovered_frames, 1)
        with pool.pinned(page_id) as page:
            self.assertEqual(bytes(page[:4]), b"good")

    def test_recovery_empties_the_log_so_it_is_not_replayed_twice(self):
        page_id = self.existing_database()
        handmade_log(self.wal_path, [(10, page_id, b"recovered", FLAG_COMMIT)])
        _pool, _wal = self.open_db()
        self.assertEqual(os.path.getsize(self.wal_path), WAL_HEADER_SIZE)

    def test_replaying_the_meta_page_restores_the_page_count(self):
        """The nastiest thing a crash can break: page 0. If the meta page is lost
        or half-written the whole file is unreadable, so it is logged too."""
        self.existing_database()
        with Pager(self.path) as pager:
            real_count = pager.page_count
            image = bytearray(pager.meta_image())
        struct.pack_into(">I", image, 12, real_count + 5)  # a page count from later
        handmade_log(self.wal_path, [(10, 0, bytes(image), FLAG_COMMIT)])
        pool, _wal = self.open_db()
        self.assertEqual(pool.pager.page_count, real_count + 5)

    def test_a_log_from_a_different_build_is_refused(self):
        self.existing_database()
        with open(self.wal_path, "r+b") as f:
            f.seek(8)
            f.write(struct.pack(">H", 99))  # version from the future
        pool = BufferPool.open(self.path, capacity=8)
        self.addCleanup(pool.close)
        with self.assertRaises(CorruptWalError):
            Wal(pool)

    def test_a_log_cannot_be_attached_to_a_pool_that_already_read_pages(self):
        """Recovery writes pages behind the pool's back, so it must go first."""
        page_id = self.existing_database()
        pool = BufferPool.open(self.path, capacity=8)
        self.addCleanup(pool.close)
        with pool.pinned(page_id):
            pass
        with self.assertRaises(WalError):
            Wal(pool)


class TestWithABTree(WalTestCase):
    """The log carrying a real workload, where a torn commit means a broken tree."""

    def open_tree(self, capacity: int = 32) -> tuple[BufferPool, Wal, BTree]:
        pool, wal = self.open_db(capacity)
        pager = pool.pager
        root = pager.read_meta_slot(META_SLOT_ROOT)
        save = lambda page_id: pager.write_meta_slot(META_SLOT_ROOT, page_id)  # noqa: E731
        if root == 0:
            tree = BTree.create(pool, on_root_change=save)
            save(tree.root_page_id)
        else:
            tree = BTree(pool, root, on_root_change=save)
        return pool, wal, tree

    def test_a_tree_survives_commits_and_a_reopen(self):
        pool, wal, tree = self.open_tree()
        for i in range(1000):
            tree.put(int_key(i), str(i).encode())
            if i % 100 == 0:
                wal.commit()
        wal.commit()
        wal.close()
        pool.close()

        _pool, _wal, tree = self.open_tree()
        tree.verify_invariants()
        self.assertEqual(tree.count(), 1000)

    def test_rolling_back_a_tree_insert_leaves_the_tree_valid(self):
        pool, wal, tree = self.open_tree()
        for i in range(600):
            tree.put(int_key(i), str(i).encode())
        wal.commit()

        for i in range(600, 900):
            tree.put(int_key(i), b"uncommitted")
        wal.rollback()
        tree.verify_invariants()
        self.assertEqual(tree.count(), 600)
        self.assertIsNone(tree.get(int_key(700)))

    def test_deletes_that_free_pages_go_through_the_log(self):
        pool, wal, tree = self.open_tree()
        for i in range(800):
            tree.put(int_key(i), str(i).encode())
        wal.commit()
        for i in range(800):
            tree.delete(int_key(i))
        wal.commit()
        tree.verify_invariants()
        self.assertGreater(len(pool.pager.free_pages()), 0)
        wal.close()
        pool.close()

        _pool, _wal, tree = self.open_tree()
        tree.verify_invariants()
        self.assertEqual(tree.count(), 0)


CHILD_SCRIPT = r"""
import os, sys
sys.path.insert(0, {root!r})
from pydb.btree import BTree
from pydb.buffer_pool import BufferPool
from pydb.pager import META_SLOT_ROOT
from pydb.record import ColumnType, encode_key

BATCH = {batch}
pool = BufferPool.open({path!r}, capacity=48)
wal_module = __import__("pydb.wal", fromlist=["Wal"])
wal = wal_module.Wal(pool)
pager = pool.pager
root = pager.read_meta_slot(META_SLOT_ROOT)
save = lambda pid: pager.write_meta_slot(META_SLOT_ROOT, pid)
if root == 0:
    tree = BTree.create(pool, on_root_change=save)
    save(tree.root_page_id)
    wal.commit()
else:
    tree = BTree(pool, root, on_root_change=save)

batch = {first_batch}
while True:
    for i in range(batch * BATCH, batch * BATCH + BATCH):
        tree.put(encode_key(ColumnType.INT, i), b"value-%d" % i)
    wal.commit()
    # Only now is this batch durable, so only now may it be acknowledged.
    sys.stdout.write("%d\n" % batch)
    sys.stdout.flush()
    batch += 1
"""


class TestMilestone(WalTestCase):
    """The layer 5 milestone from ROADMAP.md.

    A child process writes in a loop and is killed at a moment it does not choose.
    On reopen the database must be consistent and every acknowledged write must be
    present -- and, just as importantly, a batch that was *not* acknowledged must
    be either entirely there or entirely absent. A half-applied transaction is the
    failure this whole layer exists to prevent.
    """

    BATCH = 12  # keys per transaction: several pages, so a torn commit is possible
    ROUNDS = 4

    def test_crash_torture(self):
        rng = random.Random(17)
        acknowledged = -1

        for round_number in range(self.ROUNDS):
            script = CHILD_SCRIPT.format(
                root=PROJECT_ROOT,
                path=self.path,
                batch=self.BATCH,
                first_batch=acknowledged + 1,
            )
            with subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as child:
                try:
                    wanted = rng.randint(1, 25)
                    seen = 0
                    while seen < wanted:
                        line = child.stdout.readline()
                        if not line:
                            self.fail(
                                f"child died on its own: {child.stderr.read().decode()}"
                            )
                        acknowledged = int(line)
                        seen += 1
                finally:
                    child.kill()  # no cleanup, no close, no checkpoint
                    child.wait()

            with self.subTest(round=round_number, acknowledged=acknowledged):
                self.check_database(acknowledged)

    def check_database(self, acknowledged: int) -> None:
        pool = BufferPool.open(self.path, capacity=48)
        try:
            wal = Wal(pool)
            try:
                root = pool.pager.read_meta_slot(META_SLOT_ROOT)
                self.assertNotEqual(root, 0, "the root page id must be durable")
                tree = BTree(pool, root)

                # 1. The database is internally consistent, whatever the crash hit.
                tree.verify_invariants()

                # 2. Everything acknowledged is present.
                for batch in range(acknowledged + 1):
                    for key in range(batch * self.BATCH, (batch + 1) * self.BATCH):
                        if tree.get(int_key(key)) != b"value-%d" % key:
                            self.fail(
                                f"acknowledged batch {batch} lost key {key} "
                                f"(last acknowledged batch was {acknowledged})"
                            )

                # 3. Anything past that is all-or-nothing, never half applied.
                for batch in range(acknowledged + 1, acknowledged + 40):
                    keys = range(batch * self.BATCH, (batch + 1) * self.BATCH)
                    present = [tree.get(int_key(key)) is not None for key in keys]
                    self.assertIn(
                        len(set(present)),
                        (0, 1),
                        f"batch {batch} was applied in part: {present}",
                    )
            finally:
                wal.close()
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
