from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.btree import DuplicateKeyError
from pydb.catalog import (
    IndexExistsError,
    UnknownIndexError,
    escape_key,
    prefix_end,
)
from pydb.database import Database
from pydb.sql.engine import Engine
from pydb.sql.errors import PlanError, ValueTypeError


class QueryTestCase(unittest.TestCase):
    CAPACITY = 64

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.sql = self.open()

    def open(self) -> Engine:
        self.db = Database(self.path, capacity=self.CAPACITY)
        self.addCleanup(self.db.close)
        return Engine(self.db)

    def reopen(self) -> Engine:
        self.db.close()
        self.sql = self.open()
        return self.sql

    def rows(self, sql: str) -> list[tuple]:
        return self.sql.execute(sql).rows

    def plan(self, sql: str) -> str:
        return self.sql.execute(sql).plan

    def verify_all(self) -> None:
        for name in self.sql.catalog.table_names():
            self.sql.catalog.open(name).verify()

    def bookshop(self) -> None:
        self.sql.execute(
            "CREATE TABLE authors (id INT PRIMARY KEY, name TEXT NOT NULL, born INT)"
        )
        self.sql.execute(
            "CREATE TABLE books "
            "(id INT PRIMARY KEY, author INT, title TEXT NOT NULL, year INT)"
        )
        self.sql.execute(
            "INSERT INTO authors VALUES "
            "(1, 'ada', 1815), (2, 'bob', 1950), (3, 'cy', NULL), (4, 'dee', 1950)"
        )
        self.sql.execute(
            "INSERT INTO books VALUES "
            "(10, 1, 'notes on the engine', 1843), "
            "(11, 1, 'sketch', 1842), "
            "(12, 2, 'a novel', 1990), "
            "(13, 2, 'another novel', 1995), "
            "(14, NULL, 'anonymous', 1900)"
        )


class TestJoins(QueryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bookshop()

    def test_an_inner_join_pairs_matching_rows(self):
        rows = self.rows(
            "SELECT a.name, b.title FROM authors a "
            "JOIN books b ON b.author = a.id ORDER BY b.id"
        )
        self.assertEqual(
            rows,
            [
                ("ada", "notes on the engine"),
                ("ada", "sketch"),
                ("bob", "a novel"),
                ("bob", "another novel"),
            ],
        )

    def test_an_inner_join_drops_rows_with_no_partner(self):
        names = [r[0] for r in self.rows(
            "SELECT a.name FROM authors a JOIN books b ON b.author = a.id"
        )]
        self.assertNotIn("cy", names)
        self.assertNotIn("dee", names)

    def test_a_left_join_keeps_them_with_nulls(self):
        rows = self.rows(
            "SELECT a.name, b.title FROM authors a "
            "LEFT JOIN books b ON b.author = a.id ORDER BY a.id, b.id"
        )
        self.assertIn(("cy", None), rows)
        self.assertIn(("dee", None), rows)
        self.assertEqual(len(rows), 6)

    def test_a_null_join_column_matches_nothing(self):
        titles = [r[0] for r in self.rows(
            "SELECT b.title FROM books b JOIN authors a ON a.id = b.author"
        )]
        self.assertNotIn("anonymous", titles)
        left = [r[0] for r in self.rows(
            "SELECT b.title FROM books b LEFT JOIN authors a ON a.id = b.author"
        )]
        self.assertIn("anonymous", left)

    def test_a_cross_join_is_every_combination(self):
        rows = self.rows("SELECT a.id, b.id FROM authors a CROSS JOIN books b")
        self.assertEqual(len(rows), 4 * 5)
        comma = self.rows("SELECT a.id, b.id FROM authors a, books b")
        self.assertEqual(sorted(rows), sorted(comma))

    def test_three_tables_join_left_to_right(self):
        self.sql.execute("CREATE TABLE tags (book INT, tag TEXT)")
        self.sql.execute("INSERT INTO tags VALUES (10, 'maths'), (12, 'fiction')")
        rows = self.rows(
            "SELECT a.name, b.title, t.tag FROM authors a "
            "JOIN books b ON b.author = a.id "
            "JOIN tags t ON t.book = b.id ORDER BY b.id"
        )
        self.assertEqual(
            rows, [("ada", "notes on the engine", "maths"), ("bob", "a novel", "fiction")]
        )

    def test_where_filters_after_the_join(self):
        rows = self.rows(
            "SELECT a.name, b.title FROM authors a JOIN books b ON b.author = a.id "
            "WHERE b.year > 1900 ORDER BY b.id"
        )
        self.assertEqual(rows, [("bob", "a novel"), ("bob", "another novel")])

    def test_aliases_are_required_to_be_unambiguous(self):
        with self.assertRaises(PlanError) as caught:
            self.rows("SELECT id FROM authors a JOIN books b ON b.author = a.id")
        self.assertIn("ambiguous", str(caught.exception))

    def test_qualifying_resolves_the_ambiguity(self):
        rows = self.rows(
            "SELECT a.id FROM authors a JOIN books b ON b.author = a.id ORDER BY a.id"
        )
        self.assertEqual([r[0] for r in rows], [1, 1, 2, 2])

    def test_select_star_across_a_join_labels_columns_by_table(self):
        result = self.sql.execute(
            "SELECT * FROM authors a JOIN books b ON b.author = a.id"
        )
        self.assertEqual(
            result.columns,
            ("a.id", "a.name", "a.born", "b.id", "b.author", "b.title", "b.year"),
        )

    def test_a_qualified_star_takes_one_table_only(self):
        result = self.sql.execute(
            "SELECT b.* FROM authors a JOIN books b ON b.author = a.id"
        )
        self.assertEqual(result.columns, ("b.id", "b.author", "b.title", "b.year"))

    def test_colliding_column_names_are_qualified_in_the_output(self):
        result = self.sql.execute(
            "SELECT a.name, b.title, a.id, b.id FROM authors a "
            "JOIN books b ON b.author = a.id"
        )
        self.assertEqual(result.columns, ("name", "title", "a.id", "b.id"))

    def test_an_unknown_table_in_a_qualifier_is_an_error(self):
        with self.assertRaises(PlanError):
            self.rows("SELECT q.name FROM authors a")

    def test_the_same_table_twice_needs_an_alias(self):
        with self.assertRaises(PlanError):
            self.rows("SELECT * FROM authors JOIN authors ON 1 = 1")


class TestJoinPlans(QueryTestCase):
    def build(self, rows: int = 400) -> None:
        self.sql.execute("CREATE TABLE parent (id INT PRIMARY KEY, label TEXT)")
        self.sql.execute("CREATE TABLE child (id INT PRIMARY KEY, parent INT)")
        for batch in range(rows // 100):
            with self.db.transaction():
                parent = self.sql.catalog.open("parent")
                child = self.sql.catalog.open("child")
                for i in range(batch * 100, (batch + 1) * 100):
                    parent.insert((i, f"label-{i}"))
                    child.insert((i, i))
        self.db.checkpoint()

    def pages_visited(self, sql: str) -> int:
        stats = self.db.pool.stats
        before = stats.hits + stats.misses
        self.sql.execute(sql)
        return stats.hits + stats.misses - before

    def test_an_equality_on_the_inner_primary_key_becomes_a_probe(self):
        self.build(100)
        plan = self.plan(
            "SELECT * FROM child c JOIN parent p ON p.id = c.parent"
        )
        self.assertIn("scan c", plan)
        self.assertIn("probe p", plan)
        self.assertIn("parent_pkey", plan)

    def test_an_unindexed_join_column_falls_back_to_a_scan(self):
        self.build(100)
        plan = self.plan(
            "SELECT * FROM parent p JOIN child c ON c.parent = p.id"
        )
        self.assertIn("scan c", plan, "child.parent is not indexed")

    def test_the_probe_visits_far_fewer_pages_than_the_scan(self):
        self.sql.execute("CREATE TABLE small (id INT PRIMARY KEY, ref INT)")
        self.sql.execute(
            "CREATE TABLE large (id INT PRIMARY KEY, other INT, filler TEXT)"
        )
        with self.db.transaction():
            small = self.sql.catalog.open("small")
            for i in range(40):
                small.insert((i, i * 100))
        for batch in range(20):
            with self.db.transaction():
                large = self.sql.catalog.open("large")
                for i in range(batch * 200, (batch + 1) * 200):
                    large.insert((i, i, f"filler-{i:030d}"))
        self.db.checkpoint()

        probing = self.pages_visited(
            "SELECT COUNT(*) FROM small s JOIN large l ON l.id = s.ref"
        )
        scanning = self.pages_visited(
            "SELECT COUNT(*) FROM small s JOIN large l ON l.other = s.ref"
        )
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM small s JOIN large l ON l.id = s.ref"),
            self.rows("SELECT COUNT(*) FROM small s JOIN large l ON l.other = s.ref"),
            "both join orders must agree on the answer",
        )
        self.assertLess(
            probing,
            scanning / 10,
            f"probe visited {probing} pages, scan visited {scanning}",
        )

    def test_indexing_the_join_column_turns_the_scan_into_a_probe(self):
        self.build(200)
        before = self.plan("SELECT * FROM parent p JOIN child c ON c.parent = p.id")
        self.assertIn("scan c", before)
        self.sql.execute("CREATE INDEX child_parent ON child (parent)")
        after = self.plan("SELECT * FROM parent p JOIN child c ON c.parent = p.id")
        self.assertIn("probe c", after)
        self.assertIn("child_parent", after)

    def test_both_join_orders_return_the_same_rows(self):
        self.build(100)
        one = sorted(self.rows(
            "SELECT p.id, c.id FROM child c JOIN parent p ON p.id = c.parent"
        ))
        other = sorted(self.rows(
            "SELECT p.id, c.id FROM parent p JOIN child c ON c.parent = p.id"
        ))
        self.assertEqual(one, other)
        self.assertEqual(len(one), 100)


class TestAggregates(QueryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bookshop()

    def test_count_star_counts_rows_including_those_with_nulls(self):
        self.assertEqual(self.rows("SELECT COUNT(*) FROM authors"), [(4,)])

    def test_count_of_a_column_skips_nulls(self):
        self.assertEqual(self.rows("SELECT COUNT(born) FROM authors"), [(3,)])

    def test_count_distinct(self):
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT born) FROM authors"), [(2,)])

    def test_sum_min_max_and_avg(self):
        self.assertEqual(
            self.rows("SELECT SUM(year), MIN(year), MAX(year) FROM books"),
            [(1843 + 1842 + 1990 + 1995 + 1900, 1842, 1995)],
        )
        (average,) = self.rows("SELECT AVG(year) FROM books")[0]
        self.assertAlmostEqual(average, (1843 + 1842 + 1990 + 1995 + 1900) / 5)

    def test_aggregates_of_no_rows(self):
        self.assertEqual(
            self.rows(
                "SELECT COUNT(*), SUM(born), AVG(born), MIN(born), MAX(born) "
                "FROM authors WHERE id = 999"
            ),
            [(0, None, None, None, None)],
        )

    def test_aggregates_of_only_nulls(self):
        self.assertEqual(
            self.rows("SELECT COUNT(born), SUM(born) FROM authors WHERE id = 3"),
            [(0, None)],
        )

    def test_min_and_max_work_on_text(self):
        self.assertEqual(
            self.rows("SELECT MIN(name), MAX(name) FROM authors"), [("ada", "dee")]
        )

    def test_sum_of_text_is_an_error(self):
        with self.assertRaises(ValueTypeError):
            self.rows("SELECT SUM(name) FROM authors")

    def test_the_output_column_is_named_after_the_call(self):
        result = self.sql.execute("SELECT COUNT(*), SUM(born) FROM authors")
        self.assertEqual(result.columns, ("count(*)", "sum(born)"))

    def test_an_alias_names_it_instead(self):
        result = self.sql.execute("SELECT COUNT(*) AS how_many FROM authors")
        self.assertEqual(result.columns, ("how_many",))

    def test_an_aggregate_with_a_where_clause(self):
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM authors WHERE born = 1950"), [(2,)]
        )

    def test_an_aggregate_over_a_join(self):
        self.assertEqual(
            self.rows(
                "SELECT COUNT(*) FROM authors a JOIN books b ON b.author = a.id"
            ),
            [(4,)],
        )

    def test_an_aggregate_cannot_be_used_in_where(self):
        with self.assertRaises(PlanError):
            self.rows("SELECT name FROM authors WHERE COUNT(*) > 1")


class TestGrouping(QueryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bookshop()

    def test_group_by_one_column(self):
        self.assertEqual(
            self.rows(
                "SELECT born, COUNT(*) FROM authors GROUP BY born ORDER BY born"
            ),
            [(None, 1), (1815, 1), (1950, 2)],
        )

    def test_nulls_form_their_own_group(self):
        rows = self.rows("SELECT born, COUNT(*) FROM authors GROUP BY born")
        self.assertIn((None, 1), rows)

    def test_group_by_two_columns(self):
        rows = self.rows(
            "SELECT author, year, COUNT(*) FROM books GROUP BY author, year "
            "ORDER BY author, year"
        )
        self.assertEqual(len(rows), 5)

    def test_having_filters_groups_not_rows(self):
        self.assertEqual(
            self.rows(
                "SELECT born, COUNT(*) FROM authors GROUP BY born HAVING COUNT(*) > 1"
            ),
            [(1950, 2)],
        )

    def test_having_can_use_an_aggregate_that_is_not_selected(self):
        self.assertEqual(
            self.rows(
                "SELECT author FROM books GROUP BY author HAVING COUNT(*) = 2 "
                "ORDER BY author"
            ),
            [(1,), (2,)],
        )

    def test_having_without_grouping_is_refused(self):
        with self.assertRaises(PlanError):
            self.rows("SELECT name FROM authors HAVING COUNT(*) > 1")

    def test_a_bare_column_must_be_grouped(self):
        with self.assertRaises(PlanError) as caught:
            self.rows("SELECT name, COUNT(*) FROM authors GROUP BY born")
        self.assertIn("GROUP BY", str(caught.exception))

    def test_group_by_over_a_join(self):
        self.assertEqual(
            self.rows(
                "SELECT a.name, COUNT(b.id) FROM authors a "
                "LEFT JOIN books b ON b.author = a.id "
                "GROUP BY a.name ORDER BY a.name"
            ),
            [("ada", 2), ("bob", 2), ("cy", 0), ("dee", 0)],
        )

    def test_grouping_with_no_matching_rows_produces_no_groups(self):
        self.assertEqual(
            self.rows("SELECT born, COUNT(*) FROM authors WHERE id = 99 GROUP BY born"),
            [],
        )

    def test_order_by_an_aggregate(self):
        self.assertEqual(
            self.rows(
                "SELECT born, COUNT(*) AS n FROM authors GROUP BY born "
                "ORDER BY n DESC, born"
            ),
            [(1950, 2), (None, 1), (1815, 1)],
        )

    def test_order_by_an_output_position(self):
        self.assertEqual(
            self.rows(
                "SELECT born, COUNT(*) FROM authors GROUP BY born ORDER BY 2 DESC "
                "LIMIT 1"
            ),
            [(1950, 2)],
        )

    def test_order_by_a_grouping_key_however_it_is_spelled(self):
        for order in ("born", "authors.born"):
            with self.subTest(order=order):
                self.assertEqual(
                    self.rows(
                        f"SELECT born FROM authors GROUP BY born ORDER BY {order} DESC"
                    ),
                    [(1950,), (1815,), (None,)],
                )

    def test_order_by_something_ungrouped_is_refused(self):
        with self.assertRaises(PlanError):
            self.rows("SELECT born FROM authors GROUP BY born ORDER BY name")

    def test_distinct_removes_duplicate_output_rows(self):
        self.assertEqual(
            self.rows("SELECT DISTINCT born FROM authors ORDER BY born"),
            [(None,), (1815,), (1950,)],
        )
        self.assertEqual(
            self.rows("SELECT DISTINCT author FROM books ORDER BY author"),
            [(None,), (1,), (2,)],
        )

    def test_distinct_applies_to_the_whole_row(self):
        self.assertEqual(
            len(self.rows("SELECT DISTINCT born, name FROM authors")), 4
        )

    def test_limit_applies_after_grouping(self):
        self.assertEqual(
            len(self.rows("SELECT born FROM authors GROUP BY born LIMIT 2")), 2
        )


class TestSecondaryIndexes(QueryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sql.execute(
            "CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)"
        )
        self.sql.execute(
            "INSERT INTO people VALUES "
            "(1, 'ada', 36), (2, 'bob', 41), (3, 'cy', NULL), (4, 'dee', 36)"
        )

    def tearDown(self) -> None:
        self.verify_all()

    def test_creating_an_index_makes_a_scan_into_a_seek(self):
        self.assertIn("scan", self.plan("SELECT * FROM people WHERE age = 36"))
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        plan = self.plan("SELECT * FROM people WHERE age = 36")
        self.assertIn("seek", plan)
        self.assertIn("by_age", plan)

    def test_an_index_finds_every_matching_row(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.assertEqual(
            sorted(r[0] for r in self.rows(
                "SELECT name FROM people WHERE age = 36"
            )),
            ["ada", "dee"],
        )

    def test_an_index_is_built_from_the_rows_already_there(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        index = self.sql.catalog.open("people").index_named("by_age")
        self.assertEqual(len(list(index.entries())), 3, "the NULL age is not indexed")

    def test_rows_inserted_after_the_index_are_indexed_too(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.sql.execute("INSERT INTO people VALUES (5, 'eve', 36)")
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT name FROM people WHERE age = 36")),
            ["ada", "dee", "eve"],
        )

    def test_updates_and_deletes_keep_the_index_in_step(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.sql.execute("UPDATE people SET age = 50 WHERE name = 'ada'")
        self.assertEqual(
            [r[0] for r in self.rows("SELECT name FROM people WHERE age = 50")], ["ada"]
        )
        self.assertEqual(
            [r[0] for r in self.rows("SELECT name FROM people WHERE age = 36")], ["dee"]
        )
        self.sql.execute("DELETE FROM people WHERE age = 50")
        self.assertEqual(self.rows("SELECT name FROM people WHERE age = 50"), [])

    def test_a_text_index_and_the_prefix_problem(self):
        self.sql.execute("INSERT INTO people VALUES (6, 'ab', 1), (7, 'abc', 2)")
        self.sql.execute("CREATE INDEX by_name ON people (name)")
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people WHERE name = 'ab'")], [6]
        )
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people WHERE name = 'abc'")], [7]
        )

    def test_a_non_unique_index_allows_repeats_and_a_unique_one_does_not(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.sql.execute("INSERT INTO people VALUES (8, 'eve', 36)")
        self.sql.execute("CREATE UNIQUE INDEX by_name ON people (name)")
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute("INSERT INTO people VALUES (9, 'ada', 1)")

    def test_a_unique_index_ignores_nulls(self):
        self.sql.execute("DELETE FROM people WHERE name = 'dee'")
        self.sql.execute("CREATE UNIQUE INDEX by_age ON people (age)")
        self.sql.execute("INSERT INTO people VALUES (10, 'eve', NULL)")
        self.assertEqual(
            len(self.rows("SELECT id FROM people WHERE age IS NULL")),
            2,
            "two NULL ages coexist under a unique index",
        )

    def test_a_unique_index_cannot_be_created_over_duplicate_data(self):
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute("CREATE UNIQUE INDEX by_age ON people (age)")
        self.assertNotIn("by_age", self.sql.catalog.index_names())

    def test_a_range_condition_uses_the_index(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        plan = self.plan("SELECT name FROM people WHERE age > 36")
        self.assertIn("range", plan)
        self.assertEqual(
            [r[0] for r in self.rows("SELECT name FROM people WHERE age > 36")], ["bob"]
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT name FROM people WHERE age >= 36")),
            ["ada", "bob", "dee"],
        )

    def test_index_names_are_database_wide(self):
        self.sql.execute("CREATE TABLE other (x INT)")
        self.sql.execute("CREATE INDEX shared_name ON people (age)")
        with self.assertRaises(IndexExistsError):
            self.sql.execute("CREATE INDEX shared_name ON other (x)")

    def test_dropping_an_index_removes_it_and_the_plan_changes_back(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.assertIn("seek", self.plan("SELECT * FROM people WHERE age = 36"))
        self.sql.execute("DROP INDEX by_age")
        self.assertIn("scan", self.plan("SELECT * FROM people WHERE age = 36"))
        self.assertNotIn("by_age", self.sql.catalog.index_names())
        self.assertEqual(len(self.rows("SELECT * FROM people")), 4)

    def test_dropping_a_primary_key_index_is_refused(self):
        with self.assertRaises(Exception):
            self.sql.execute("DROP INDEX people_pkey")

    def test_dropping_a_missing_index_is_an_error_unless_if_exists(self):
        with self.assertRaises(UnknownIndexError):
            self.sql.execute("DROP INDEX nope")
        self.sql.execute("DROP INDEX IF EXISTS nope")

    def test_indexes_survive_a_reopen(self):
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.sql.execute("CREATE UNIQUE INDEX by_name ON people (name)")
        self.reopen()
        self.assertEqual(
            self.sql.catalog.index_names(), ["by_age", "by_name", "people_pkey"]
        )
        self.assertIn("seek", self.plan("SELECT * FROM people WHERE age = 36"))
        self.verify_all()

    def test_a_rolled_back_create_index_leaves_nothing(self):
        self.sql.execute("BEGIN")
        self.sql.execute("CREATE INDEX by_age ON people (age)")
        self.sql.execute("ROLLBACK")
        self.assertNotIn("by_age", self.sql.catalog.index_names())
        self.assertIn("scan", self.plan("SELECT * FROM people WHERE age = 36"))
        self.verify_all()

    def test_many_rows_through_a_secondary_index(self):
        self.sql.execute("CREATE TABLE big (id INT PRIMARY KEY, bucket INT)")
        self.sql.execute("CREATE INDEX by_bucket ON big (bucket)")
        for batch in range(10):
            with self.db.transaction():
                table = self.sql.catalog.open("big")
                for i in range(batch * 100, (batch + 1) * 100):
                    table.insert((i, i % 7))
        self.sql.catalog.open("big").verify()
        rows = self.rows("SELECT id FROM big WHERE bucket = 3")
        self.assertEqual(len(rows), len([i for i in range(1000) if i % 7 == 3]))
        self.assertIn("seek", self.plan("SELECT id FROM big WHERE bucket = 3"))


class TestKeyEscaping(unittest.TestCase):
    def test_escaping_preserves_order(self):
        values = [b"", b"a", b"a\x00", b"a\x00b", b"ab", b"b", b"\x00", b"\xff"]
        for left in values:
            for right in values:
                with self.subTest(left=left, right=right):
                    self.assertEqual(
                        escape_key(left) < escape_key(right), left < right
                    )

    def test_no_escaped_key_is_a_prefix_of_another(self):
        for left in (b"ab", b"a", b"", b"a\x00"):
            for right in (b"abc", b"ab", b"a", b"\x00"):
                if left == right:
                    continue
                with self.subTest(left=left, right=right):
                    self.assertFalse(escape_key(right).startswith(escape_key(left)))

    def test_prefix_end_bounds_the_prefix(self):
        self.assertEqual(prefix_end(b"ab"), b"ac")
        self.assertEqual(prefix_end(b"a\xff"), b"b")
        self.assertIsNone(prefix_end(b"\xff\xff"))


class TestVacuumAndSpace(QueryTestCase):
    def test_dropping_a_table_frees_its_pages(self):
        self.sql.execute("CREATE TABLE keep (id INT PRIMARY KEY, filler TEXT)")
        self.sql.execute("CREATE TABLE gone (id INT PRIMARY KEY, filler TEXT)")
        for batch in range(5):
            with self.db.transaction():
                table = self.sql.catalog.open("gone")
                for i in range(batch * 100, (batch + 1) * 100):
                    table.insert((i, f"filler-{i:040d}"))
        pages_before = self.db.pager.page_count
        free_before = len(self.db.pager.free_pages())

        self.sql.execute("DROP TABLE gone")
        freed = len(self.db.pager.free_pages()) - free_before
        self.assertGreater(freed, 10, "a dropped table's pages should come back")
        self.assertEqual(self.db.pager.page_count, pages_before, "file did not grow")

        self.sql.execute("CREATE TABLE reused (id INT PRIMARY KEY, filler TEXT)")
        for batch in range(5):
            with self.db.transaction():
                table = self.sql.catalog.open("reused")
                for i in range(batch * 100, (batch + 1) * 100):
                    table.insert((i, f"filler-{i:040d}"))
        self.assertLessEqual(self.db.pager.page_count, pages_before + 2)
        self.verify_all()

    def test_dropping_a_table_frees_its_index_pages_too(self):
        self.sql.execute("CREATE TABLE gone (id INT PRIMARY KEY, v INT)")
        self.sql.execute("CREATE INDEX by_v ON gone (v)")
        for batch in range(4):
            with self.db.transaction():
                table = self.sql.catalog.open("gone")
                for i in range(batch * 100, (batch + 1) * 100):
                    table.insert((i, i))
        free_before = len(self.db.pager.free_pages())
        self.sql.execute("DROP TABLE gone")
        self.assertGreater(len(self.db.pager.free_pages()) - free_before, 10)
        self.assertEqual(self.sql.catalog.index_names(), [])

    def test_vacuum_reclaims_space_left_by_deletes(self):
        self.sql.execute("CREATE TABLE t (id INT PRIMARY KEY, filler TEXT)")
        with self.db.transaction():
            table = self.sql.catalog.open("t")
            for i in range(300):
                table.insert((i, f"filler-{i:040d}"))
        self.sql.execute("DELETE FROM t WHERE id < 200")
        result = self.sql.execute("VACUUM")
        self.assertIn("reclaimed", result.message)
        self.assertGreater(
            int(result.message.split()[2]), 0, "there was dead space to reclaim"
        )
        self.assertEqual(len(self.rows("SELECT * FROM t")), 100)
        self.verify_all()

    def test_vacuum_on_an_empty_database_is_harmless(self):
        self.assertIn("reclaimed 0", self.sql.execute("VACUUM").message)


class TestExplain(QueryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bookshop()

    def lines(self, sql: str) -> list[str]:
        return [row[0] for row in self.rows(sql)]

    def test_explain_shows_the_access_path_and_the_operations(self):
        lines = self.lines(
            "EXPLAIN SELECT a.name, COUNT(*) FROM books b "
            "JOIN authors a ON a.id = b.author WHERE b.year > 1900 "
            "GROUP BY a.name HAVING COUNT(*) > 1 ORDER BY 2 LIMIT 3"
        )
        joined = "\n".join(lines)
        for expected in ("scan b", "probe a", "filter", "group by", "having",
                         "project", "sort by", "limit"):
            self.assertIn(expected, joined)

    def test_explain_does_not_run_the_statement(self):
        before = len(self.rows("SELECT * FROM authors"))
        self.sql.execute("EXPLAIN DELETE FROM authors")
        self.assertEqual(len(self.rows("SELECT * FROM authors")), before)

    def test_explain_covers_update_and_delete(self):
        self.assertIn(
            "delete from", "\n".join(self.lines("EXPLAIN DELETE FROM books WHERE id = 1"))
        )
        self.assertIn(
            "update", "\n".join(
                self.lines("EXPLAIN UPDATE books SET year = 1 WHERE id = 1")
            )
        )

    def test_explain_reports_the_index_it_would_use(self):
        self.assertIn(
            "authors_pkey",
            "\n".join(self.lines("EXPLAIN SELECT * FROM authors WHERE id = 2")),
        )


if __name__ == "__main__":
    unittest.main()
