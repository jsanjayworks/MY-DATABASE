"""Tests for layer 1, the pager.

The rule for this project: a storage test that never closes the file has not
tested storage. Most of these reopen the database and check what survived.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.pager import (  # noqa: E402
    MAGIC,
    META_FORMAT,
    PAGE_SIZE,
    CorruptFileError,
    Pager,
    PagerError,
)


class PagerTestCase(unittest.TestCase):
    """Gives each test a fresh database path in a temp directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")


class TestNewFile(PagerTestCase):
    def test_creates_file_with_only_a_meta_page(self):
        with Pager(self.path) as pager:
            self.assertEqual(pager.page_count, 1)
            self.assertEqual(pager.free_list_head, 0)
        self.assertEqual(os.path.getsize(self.path), PAGE_SIZE)

    def test_writes_the_magic_number(self):
        with Pager(self.path):
            pass
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(8), MAGIC)

    def test_treats_an_existing_empty_file_as_new(self):
        open(self.path, "wb").close()
        with Pager(self.path) as pager:
            self.assertEqual(pager.page_count, 1)


class TestAllocation(PagerTestCase):
    def test_hands_out_consecutive_ids_starting_at_one(self):
        with Pager(self.path) as pager:
            self.assertEqual(
                [pager.allocate_page() for _ in range(3)], [1, 2, 3]
            )
            self.assertEqual(pager.page_count, 4)

    def test_allocated_pages_are_zeroed(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            self.assertEqual(pager.read_page(page_id), bytearray(PAGE_SIZE))

    def test_file_grows_by_one_page_per_allocation(self):
        with Pager(self.path) as pager:
            for _ in range(5):
                pager.allocate_page()
        self.assertEqual(os.path.getsize(self.path), 6 * PAGE_SIZE)


class TestReadWrite(PagerTestCase):
    def test_round_trips_a_full_page(self):
        payload = bytes(range(256)) * (PAGE_SIZE // 256)
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.write_page(page_id, payload)
            self.assertEqual(pager.read_page(page_id), bytearray(payload))

    def test_pads_short_writes_with_zeros(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.write_page(page_id, b"hi")
            page = pager.read_page(page_id)
            self.assertEqual(page[:2], b"hi")
            self.assertEqual(page[2:], bytearray(PAGE_SIZE - 2))

    def test_read_returns_a_copy_not_a_view(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.write_page(page_id, b"original")
            page = pager.read_page(page_id)
            page[:8] = b"MUTATED!"
            self.assertEqual(pager.read_page(page_id)[:8], b"original")

    def test_rejects_oversized_writes(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            with self.assertRaises(PagerError):
                pager.write_page(page_id, bytes(PAGE_SIZE + 1))

    def test_rejects_ids_past_the_end_of_the_file(self):
        with Pager(self.path) as pager:
            pager.allocate_page()
            with self.assertRaises(PagerError):
                pager.read_page(99)

    def test_refuses_to_let_callers_scribble_on_the_meta_page(self):
        with Pager(self.path) as pager:
            with self.assertRaises(PagerError):
                pager.write_page(0, b"nope")


class TestFreeList(PagerTestCase):
    def test_reuses_freed_pages_before_growing_the_file(self):
        with Pager(self.path) as pager:
            first, second = pager.allocate_page(), pager.allocate_page()
            pager.free_page(first)
            self.assertEqual(pager.allocate_page(), first)
            self.assertEqual(pager.page_count, 3)  # meta + the two originals
            self.assertNotEqual(first, second)

    def test_free_list_is_lifo(self):
        with Pager(self.path) as pager:
            pages = [pager.allocate_page() for _ in range(3)]
            for page_id in pages:
                pager.free_page(page_id)
            self.assertEqual(pager.free_pages(), list(reversed(pages)))
            reused = [pager.allocate_page() for _ in range(3)]
            self.assertEqual(reused, list(reversed(pages)))

    def test_reallocated_pages_do_not_leak_old_data(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.write_page(page_id, b"secret")
            pager.free_page(page_id)
            self.assertEqual(pager.allocate_page(), page_id)
            self.assertEqual(pager.read_page(page_id), bytearray(PAGE_SIZE))

    def test_double_free_is_an_error(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.free_page(page_id)
            with self.assertRaises(PagerError):
                pager.free_page(page_id)

    def test_cannot_free_the_meta_page(self):
        with Pager(self.path) as pager:
            with self.assertRaises(PagerError):
                pager.free_page(0)


class TestPersistence(PagerTestCase):
    """The tests that actually matter: close the file, reopen, is it right?"""

    def test_page_contents_survive_a_reopen(self):
        with Pager(self.path) as pager:
            page_id = pager.allocate_page()
            pager.write_page(page_id, b"durable")
        with Pager(self.path) as pager:
            self.assertEqual(pager.read_page(page_id)[:7], b"durable")

    def test_page_count_survives_a_reopen(self):
        with Pager(self.path) as pager:
            for _ in range(4):
                pager.allocate_page()
        with Pager(self.path) as pager:
            self.assertEqual(pager.page_count, 5)
            self.assertEqual(pager.allocate_page(), 5)

    def test_free_list_survives_a_reopen(self):
        with Pager(self.path) as pager:
            pages = [pager.allocate_page() for _ in range(3)]
            pager.free_page(pages[0])
            pager.free_page(pages[2])
        with Pager(self.path) as pager:
            self.assertEqual(pager.free_pages(), [pages[2], pages[0]])
            self.assertEqual(pager.allocate_page(), pages[2])

    def test_survives_a_process_that_never_closes_the_file(self):
        """A hard kill after sync() must not lose the write.

        This is the closest thing to a real crash test we can run portably: a
        child process writes, syncs, then exits via os._exit, which skips every
        cleanup handler Python would normally run.
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {project_root!r})\n"
            "from pydb.pager import Pager\n"
            f"p = Pager({self.path!r})\n"
            "pid = p.allocate_page()\n"
            "p.write_page(pid, b'survived the crash')\n"
            "p.sync()\n"
            "os._exit(1)\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr.decode())

        with Pager(self.path) as pager:
            self.assertEqual(pager.page_count, 2)
            self.assertEqual(pager.read_page(1)[:18], b"survived the crash")


class TestCorruption(PagerTestCase):
    def test_rejects_a_file_that_is_not_a_database(self):
        with open(self.path, "wb") as f:
            f.write(b"this is a text file" + bytes(PAGE_SIZE))
        with self.assertRaises(CorruptFileError):
            Pager(self.path)

    def test_rejects_a_future_format_version(self):
        with Pager(self.path):
            pass
        with open(self.path, "r+b") as f:
            f.seek(0)
            f.write(struct.pack(META_FORMAT, MAGIC, 99, PAGE_SIZE, 1, 0))
        with self.assertRaises(CorruptFileError):
            Pager(self.path)

    def test_rejects_a_different_page_size(self):
        with Pager(self.path):
            pass
        with open(self.path, "r+b") as f:
            f.seek(0)
            f.write(struct.pack(META_FORMAT, MAGIC, 1, 8192, 1, 0))
        with self.assertRaises(CorruptFileError):
            Pager(self.path)


if __name__ == "__main__":
    unittest.main()
