"""Tests for layer 7a, the catalog and its tables.

The catalog is a table of tables, stored in the database it describes, so the
question every test here is really asking is: does the database still know what it
contains after being closed and reopened?

`Table.verify()` is the counterpart to the B+Tree's invariant check: it asserts
the heap and the index agree about every row. An insert that updates one and not
the other produces a table that answers the same query differently depending on
the plan, which is a horrible bug to find any other way.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.btree import DuplicateKeyError  # noqa: E402
from pydb.catalog import (  # noqa: E402
    Catalog,
    CatalogError,
    TableExistsError,
    TableInfo,
    UnknownTableError,
)
from pydb.database import Database  # noqa: E402
from pydb.record import Schema, SchemaError  # noqa: E402

PEOPLE = Schema.of(("id", "INT", False), ("name", "TEXT"), ("age", "INT"))


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.db = self.open()
        self.catalog = Catalog(self.db)

    def open(self) -> Database:
        db = Database(self.path, capacity=32)
        self.addCleanup(db.close)
        return db

    def reopen(self) -> Catalog:
        self.db.close()
        self.db = self.open()
        self.catalog = Catalog(self.db)
        return self.catalog


class TestTableInfoEncoding(unittest.TestCase):
    def test_round_trips(self):
        info = TableInfo("people", PEOPLE, first_page_id=7, primary_key=0, index_root=9)
        decoded = TableInfo.decode("people", info.encode())
        self.assertEqual(decoded, info)

    def test_round_trips_without_a_primary_key(self):
        info = TableInfo("logs", PEOPLE, first_page_id=3)
        decoded = TableInfo.decode("logs", info.encode())
        self.assertIsNone(decoded.primary_key)
        self.assertEqual(decoded.schema, PEOPLE)

    def test_round_trips_unicode_column_names(self):
        schema = Schema.of(("año", "INT"), ("名前", "TEXT"))
        info = TableInfo("t", schema, first_page_id=1)
        self.assertEqual(TableInfo.decode("t", info.encode()).schema, schema)

    def test_primary_key_name_comes_from_the_column_index(self):
        info = TableInfo("people", PEOPLE, first_page_id=1, primary_key=1)
        self.assertEqual(info.primary_key_name, "name")
        self.assertIsNone(TableInfo("x", PEOPLE, 1).primary_key_name)


class TestDefinitions(CatalogTestCase):
    def test_a_created_table_can_be_found_again(self):
        self.catalog.create_table("people", PEOPLE, primary_key="id")
        self.assertIn("people", self.catalog)
        self.assertEqual(self.catalog.table_names(), ["people"])
        self.assertEqual(self.catalog.info("people").schema, PEOPLE)

    def test_definitions_survive_a_reopen(self):
        self.catalog.create_table("people", PEOPLE, primary_key="id")
        self.catalog.create_table("logs", Schema.of(("line", "TEXT")))
        self.reopen()
        self.assertEqual(self.catalog.table_names(), ["logs", "people"])
        info = self.catalog.info("people")
        self.assertEqual(info.schema, PEOPLE)
        self.assertEqual(info.primary_key_name, "id")

    def test_table_names_come_back_sorted(self):
        for name in ("zebra", "ant", "moose"):
            self.catalog.create_table(name, Schema.of(("x", "INT")))
        self.assertEqual(self.catalog.table_names(), ["ant", "moose", "zebra"])

    def test_creating_the_same_table_twice_is_an_error(self):
        self.catalog.create_table("people", PEOPLE)
        with self.assertRaises(TableExistsError):
            self.catalog.create_table("people", PEOPLE)

    def test_an_unknown_table_is_an_error(self):
        with self.assertRaises(UnknownTableError):
            self.catalog.info("nope")

    def test_a_primary_key_must_name_a_real_column(self):
        with self.assertRaises(SchemaError):
            self.catalog.create_table("people", PEOPLE, primary_key="nope")

    def test_a_nullable_primary_key_is_refused(self):
        with self.assertRaises(CatalogError):
            self.catalog.create_table("people", PEOPLE, primary_key="name")

    def test_dropping_a_table_removes_it(self):
        self.catalog.create_table("people", PEOPLE, primary_key="id")
        self.catalog.drop_table("people")
        self.assertNotIn("people", self.catalog)
        self.reopen()
        self.assertEqual(self.catalog.table_names(), [])

    def test_one_table_object_per_table(self):
        self.catalog.create_table("people", PEOPLE, primary_key="id")
        self.assertIs(self.catalog.open("people"), self.catalog.open("people"))


class TestRows(CatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.table = self.catalog.create_table("people", PEOPLE, primary_key="id")

    def tearDown(self) -> None:
        self.table.verify()

    def insert_many(self, count: int) -> None:
        with self.db.transaction():
            for i in range(count):
                self.table.insert((i, f"name-{i}", i * 2))

    def test_insert_then_look_up_by_key(self):
        with self.db.transaction():
            self.table.insert((1, "ada", 36))
        found = self.table.lookup(1)
        self.assertIsNotNone(found)
        self.assertEqual(found[1], (1, "ada", 36))

    def test_looking_up_a_missing_key_is_none(self):
        self.assertIsNone(self.table.lookup(99))

    def test_a_duplicate_primary_key_is_refused_and_changes_nothing(self):
        with self.db.transaction():
            self.table.insert((1, "ada", 36))
        with self.assertRaises(DuplicateKeyError):
            with self.db.transaction():
                self.table.insert((1, "imposter", 0))
        self.assertEqual(self.table.lookup(1)[1], (1, "ada", 36))
        self.assertEqual(len(list(self.table.scan())), 1)

    def test_the_index_and_heap_stay_in_step_over_many_rows(self):
        self.insert_many(500)
        self.table.verify()
        for i in range(500):
            with self.subTest(row=i):
                self.assertEqual(self.table.lookup(i)[1], (i, f"name-{i}", i * 2))

    def test_delete_removes_the_row_and_its_index_entry(self):
        self.insert_many(50)
        with self.db.transaction():
            rid, values = self.table.lookup(20)
            self.table.delete(rid, values)
        self.assertIsNone(self.table.lookup(20))
        self.assertEqual(len(list(self.table.scan())), 49)

    def test_update_keeping_the_key_rewrites_the_row(self):
        self.insert_many(20)
        with self.db.transaction():
            rid, values = self.table.lookup(5)
            self.table.update(rid, values, (5, "renamed", 99))
        self.assertEqual(self.table.lookup(5)[1], (5, "renamed", 99))

    def test_update_changing_the_key_moves_the_index_entry(self):
        self.insert_many(20)
        with self.db.transaction():
            rid, values = self.table.lookup(5)
            self.table.update(rid, values, (500, "moved", 1))
        self.assertIsNone(self.table.lookup(5))
        self.assertEqual(self.table.lookup(500)[1], (500, "moved", 1))

    def test_update_onto_an_existing_key_is_refused(self):
        self.insert_many(20)
        with self.assertRaises(DuplicateKeyError):
            with self.db.transaction():
                rid, values = self.table.lookup(5)
                self.table.update(rid, values, (6, "clash", 1))
        self.assertEqual(self.table.lookup(5)[1][1], "name-5")

    def test_an_index_range_reads_rows_in_key_order(self):
        self.insert_many(100)
        from pydb.record import ColumnType, encode_key

        low = encode_key(ColumnType.INT, 10)
        high = encode_key(ColumnType.INT, 20)
        keys = [values[0] for _rid, values in self.table.index_range(low, high)]
        self.assertEqual(keys, list(range(10, 20)))

    def test_a_table_without_a_primary_key_has_no_index(self):
        logs = self.catalog.create_table("logs", Schema.of(("line", "TEXT")))
        self.assertIsNone(logs.index)
        with self.db.transaction():
            logs.insert(("first",))
            logs.insert(("second",))
        self.assertEqual([row for _rid, row in logs.scan()], [("first",), ("second",)])
        with self.assertRaises(CatalogError):
            logs.lookup("first")
        logs.verify()


class TestRollback(CatalogTestCase):
    """Cached page ids are the trap here, and there are three of them: the index
    root, the heap's page chain, and the catalog record itself."""

    def test_a_rolled_back_create_table_leaves_no_table(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.catalog.create_table("people", PEOPLE, primary_key="id")
                raise RuntimeError
        self.assertNotIn("people", self.catalog)
        self.assertEqual(self.catalog.table_names(), [])

    def test_a_rolled_back_insert_leaves_a_usable_table(self):
        table = self.catalog.create_table("people", PEOPLE, primary_key="id")
        with self.db.transaction():
            table.insert((1, "kept", 1))

        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                for i in range(2, 400):  # enough to split the tree and grow the heap
                    table.insert((i, f"gone-{i}", i))
                raise RuntimeError

        table.verify()
        self.assertEqual(len(list(table.scan())), 1)
        self.assertIsNone(table.lookup(300))
        with self.db.transaction():
            table.insert((2, "after the rollback", 2))
        table.verify()
        self.assertEqual(table.lookup(2)[1][1], "after the rollback")

    def test_the_cached_index_root_still_agrees_with_the_catalog_after_a_rollback(self):
        """The index root is cached in two places -- the tree object and the
        catalog record -- and a rolled-back transaction that split the tree must
        not leave them disagreeing."""
        table = self.catalog.create_table("people", PEOPLE, primary_key="id")
        with self.db.transaction():
            for i in range(200):
                table.insert((i, f"name-{i}", i))
        root_before = table.index.root_page_id

        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                for i in range(200, 600):  # splits leaves, allocating index pages
                    table.insert((i, f"name-{i}", i))
                raise RuntimeError

        self.assertEqual(table.index.root_page_id, root_before)
        self.assertEqual(self.catalog.info("people").index_root, root_before)
        table.verify()
        self.assertEqual(len(list(table.scan())), 200)

    def test_a_transaction_larger_than_the_pool_is_refused_not_half_applied(self):
        """The no-steal limit from layer 5, reached through SQL-level machinery.
        The table has to be intact afterwards."""
        from pydb.buffer_pool import AllFramesPinnedError

        table = self.catalog.create_table("people", PEOPLE, primary_key="id")
        with self.assertRaises(AllFramesPinnedError):
            with self.db.transaction():
                for i in range(20000):
                    table.insert((i, f"name-{i}", i))
        table.verify()
        self.assertEqual(len(list(table.scan())), 0)


if __name__ == "__main__":
    unittest.main()
