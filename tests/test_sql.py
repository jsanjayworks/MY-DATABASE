"""Tests for layer 7b-7f: tokenizer, parser, planner and engine.

Three groups, matching where things can go wrong:

* the **tokenizer and parser**, tested on strings, where the interesting cases are
  the ones SQL does differently from other languages -- doubled quotes, keyword
  case, operator precedence;
* the **planner**, where the assertion is which access path was chosen, because
  "the right answer" is not enough: a query that returns correct rows by scanning a
  million of them is still broken;
* the **engine**, end to end, including what a statement does when it fails
  half-way.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.btree import DuplicateKeyError  # noqa: E402
from pydb.catalog import TableExistsError, UnknownTableError  # noqa: E402
from pydb.database import Database  # noqa: E402
from pydb.sql import nodes  # noqa: E402
from pydb.sql.engine import Engine  # noqa: E402
from pydb.sql.errors import ParseError, PlanError, ValueTypeError  # noqa: E402
from pydb.sql.parser import parse, parse_script  # noqa: E402
from pydb.sql.tokenizer import TokenType, tokenize  # noqa: E402


class TestTokenizer(unittest.TestCase):
    def kinds(self, sql: str) -> list[TokenType]:
        return [token.type for token in tokenize(sql)[:-1]]

    def values(self, sql: str) -> list[object]:
        return [token.value for token in tokenize(sql)[:-1]]

    def test_an_empty_statement_is_just_the_end_token(self):
        self.assertEqual(len(tokenize("")), 1)
        self.assertTrue(tokenize("   \n\t ")[0].is_end)

    def test_keywords_are_recognised_whatever_their_case(self):
        self.assertEqual(self.values("select SeLeCt SELECT"), ["SELECT"] * 3)
        self.assertEqual(self.kinds("select"), [TokenType.KEYWORD])

    def test_identifiers_are_folded_to_lower_case(self):
        self.assertEqual(self.values("People NAME"), ["people", "name"])

    def test_a_quoted_identifier_keeps_its_case_and_may_be_a_keyword(self):
        tokens = tokenize('"Order" "select"')
        self.assertEqual([t.type for t in tokens[:-1]], [TokenType.IDENTIFIER] * 2)
        self.assertEqual([t.value for t in tokens[:-1]], ["Order", "select"])

    def test_strings_use_single_quotes_and_double_them_to_escape(self):
        self.assertEqual(self.values("'it''s'"), ["it's"])
        self.assertEqual(self.values("''"), [""])

    def test_an_unterminated_string_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            tokenize("'never ends")

    def test_numbers_are_whole_and_anything_else_is_rejected(self):
        self.assertEqual(self.values("0 42 007"), [0, 42, 7])
        with self.assertRaises(ParseError):
            tokenize("3.14")
        with self.assertRaises(ParseError):
            tokenize("12abc")

    def test_two_character_operators_are_not_split(self):
        self.assertEqual(self.values("<= >= <> != = < >"), ["<=", ">=", "<>", "!=", "=", "<", ">"])

    def test_comments_run_to_the_end_of_the_line(self):
        self.assertEqual(self.values("1 -- two\n3"), [1, 3])
        self.assertEqual(self.values("-- all of it"), [])

    def test_an_unexpected_character_reports_its_position(self):
        with self.assertRaises(ParseError) as caught:
            tokenize("SELECT # FROM t")
        self.assertEqual(caught.exception.position, 7)


class TestParser(unittest.TestCase):
    def test_create_table_with_types_and_constraints(self):
        statement = parse(
            "CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INTEGER)"
        )
        self.assertEqual(statement.name, "people")
        self.assertEqual(statement.primary_key, "id")
        self.assertEqual([c.name for c in statement.columns], ["id", "name", "age"])
        self.assertEqual([c.nullable for c in statement.columns], [False, False, True])

    def test_a_primary_key_is_implicitly_not_null(self):
        statement = parse("CREATE TABLE t (id INT PRIMARY KEY)")
        self.assertFalse(statement.columns[0].nullable)

    def test_two_primary_keys_are_refused(self):
        with self.assertRaises(ParseError):
            parse("CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)")

    def test_insert_with_and_without_a_column_list(self):
        self.assertEqual(parse("INSERT INTO t VALUES (1)").columns, None)
        self.assertEqual(parse("INSERT INTO t (a, b) VALUES (1, 2)").columns, ["a", "b"])
        self.assertEqual(len(parse("INSERT INTO t VALUES (1), (2), (3)").rows), 3)

    def test_select_star_and_named_columns(self):
        star = parse("SELECT * FROM t").items
        self.assertEqual(len(star), 1)
        self.assertIsInstance(star[0].value, nodes.Star)
        named = parse("SELECT a, b FROM t").items
        self.assertEqual([item.label() for item in named], ["a", "b"])
        self.assertEqual(parse("SELECT * FROM t").source.name, "t")

    def test_column_references_keep_their_qualifier(self):
        item = parse("SELECT people.name FROM people").items[0]
        self.assertEqual(item.value, nodes.ColumnRef("name", "people"))
        self.assertEqual(item.label(), "name")

    def test_aliases_with_and_without_as(self):
        items = parse("SELECT a AS x, b y, c FROM t").items
        self.assertEqual([item.label() for item in items], ["x", "y", "c"])

    def test_joins_nest_to_the_left(self):
        source = parse("SELECT * FROM a JOIN b ON a.id = b.id JOIN c ON c.id = a.id").source
        self.assertIsInstance(source, nodes.Join)
        self.assertEqual(source.right.name, "c")
        self.assertIsInstance(source.left, nodes.Join)
        self.assertEqual(source.left.right.name, "b")
        self.assertEqual(source.left.left.name, "a")

    def test_join_kinds(self):
        for sql, kind in (
            ("SELECT * FROM a JOIN b ON a.x = b.x", "INNER"),
            ("SELECT * FROM a INNER JOIN b ON a.x = b.x", "INNER"),
            ("SELECT * FROM a LEFT JOIN b ON a.x = b.x", "LEFT"),
            ("SELECT * FROM a LEFT OUTER JOIN b ON a.x = b.x", "LEFT"),
            ("SELECT * FROM a CROSS JOIN b", "CROSS"),
            ("SELECT * FROM a, b", "CROSS"),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(parse(sql).source.kind, kind)

    def test_a_left_join_without_on_is_refused(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM a LEFT JOIN b")

    def test_table_aliases(self):
        source = parse("SELECT * FROM people AS p JOIN pets q ON p.id = q.owner").source
        self.assertEqual((source.left.name, source.left.alias), ("people", "p"))
        self.assertEqual((source.right.name, source.right.alias), ("pets", "q"))
        self.assertEqual(source.left.label, "p")

    def test_aggregates_parse_with_and_without_distinct(self):
        items = parse("SELECT COUNT(*), SUM(a), COUNT(DISTINCT b) FROM t").items
        self.assertTrue(items[0].value.is_count_star)
        self.assertEqual(items[1].value.name, "SUM")
        self.assertTrue(items[2].value.distinct)
        self.assertEqual([item.label() for item in items],
                         ["count(*)", "sum(a)", "count(DISTINCT b)"])

    def test_only_count_takes_a_star(self):
        with self.assertRaises(ParseError):
            parse("SELECT SUM(*) FROM t")

    def test_nested_aggregates_are_refused(self):
        with self.assertRaises(ParseError):
            parse("SELECT SUM(COUNT(a)) FROM t")

    def test_group_by_having_and_distinct(self):
        statement = parse(
            "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a, b HAVING COUNT(*) > 1"
        )
        self.assertTrue(statement.distinct)
        self.assertEqual(len(statement.group_by), 2)
        self.assertIsInstance(statement.having, nodes.Compare)

    def test_create_and_drop_index(self):
        statement = parse("CREATE UNIQUE INDEX by_name ON people (name)")
        self.assertEqual(
            (statement.name, statement.table, statement.column, statement.unique),
            ("by_name", "people", "name", True),
        )
        self.assertFalse(parse("CREATE INDEX i ON t (c)").unique)
        self.assertTrue(parse("DROP INDEX IF EXISTS i").if_exists)

    def test_a_composite_index_is_refused(self):
        with self.assertRaises(ParseError):
            parse("CREATE INDEX i ON t (a, b)")

    def test_vacuum_and_explain(self):
        self.assertIsInstance(parse("VACUUM"), nodes.Vacuum)
        explained = parse("EXPLAIN SELECT * FROM t")
        self.assertIsInstance(explained, nodes.Explain)
        self.assertIsInstance(explained.statement, nodes.Select)

    def test_and_binds_tighter_than_or(self):
        where = parse("SELECT * FROM t WHERE a = 1 AND b = 2 OR c = 3").where
        self.assertIsInstance(where, nodes.Or)
        self.assertIsInstance(where.left, nodes.And)
        self.assertIsInstance(where.right, nodes.Compare)

    def test_parentheses_override_precedence(self):
        where = parse("SELECT * FROM t WHERE a = 1 AND (b = 2 OR c = 3)").where
        self.assertIsInstance(where, nodes.And)
        self.assertIsInstance(where.right, nodes.Or)

    def test_not_and_is_null(self):
        self.assertIsInstance(parse("SELECT * FROM t WHERE NOT a = 1").where, nodes.Not)
        where = parse("SELECT * FROM t WHERE a IS NOT NULL").where
        self.assertIsInstance(where, nodes.IsNull)
        self.assertTrue(where.negated)
        self.assertFalse(parse("SELECT * FROM t WHERE a IS NULL").where.negated)

    def test_negative_and_null_literals(self):
        where = parse("SELECT * FROM t WHERE a = -5").where
        self.assertEqual(where.right, nodes.Literal(-5))
        self.assertEqual(parse("INSERT INTO t VALUES (NULL)").rows[0][0].value, None)

    def test_not_equals_spellings_are_the_same_node(self):
        for sql in ("a != 1", "a <> 1"):
            self.assertEqual(parse(f"SELECT * FROM t WHERE {sql}").where.operator, "!=")

    def test_order_by_limit_and_offset(self):
        statement = parse(
            "SELECT * FROM t ORDER BY a DESC, b ASC, c LIMIT 5 OFFSET 10"
        )
        self.assertEqual(
            [(str(k.value), k.descending) for k in statement.order_by],
            [("a", True), ("b", False), ("c", False)],
        )
        self.assertEqual((statement.limit, statement.offset), (5, 10))

    def test_order_by_an_output_position(self):
        statement = parse("SELECT a, b FROM t ORDER BY 2 DESC")
        self.assertEqual(statement.order_by[0].value, 2)
        self.assertTrue(statement.order_by[0].descending)

    def test_update_and_delete(self):
        statement = parse("UPDATE t SET a = 1, b = 'x' WHERE c = 2")
        self.assertEqual([name for name, _ in statement.assignments], ["a", "b"])
        self.assertIsNone(parse("DELETE FROM t").where)

    def test_transaction_statements_take_an_optional_keyword(self):
        self.assertIsInstance(parse("BEGIN"), nodes.Begin)
        self.assertIsInstance(parse("BEGIN TRANSACTION"), nodes.Begin)
        self.assertIsInstance(parse("COMMIT"), nodes.Commit)
        self.assertIsInstance(parse("ROLLBACK"), nodes.Rollback)

    def test_a_trailing_semicolon_is_allowed_but_trailing_junk_is_not(self):
        self.assertIsInstance(parse("SELECT * FROM t;"), nodes.Select)
        with self.assertRaises(ParseError):
            parse("SELECT * FROM t 99")
        # A bare name after a table *is* valid: it is an alias.
        self.assertEqual(parse("SELECT * FROM t x").source.alias, "x")

    def test_a_script_splits_on_semicolons(self):
        statements = parse_script(
            "CREATE TABLE t (a INT); INSERT INTO t VALUES (1); SELECT * FROM t;"
        )
        self.assertEqual(len(statements), 3)

    def test_a_keyword_used_as_a_name_says_how_to_fix_it(self):
        with self.assertRaises(ParseError) as caught:
            parse("CREATE TABLE order (a INT)")
        self.assertIn('"order"', str(caught.exception))

    def test_errors_carry_a_position(self):
        with self.assertRaises(ParseError) as caught:
            parse("SELECT * FROM")
        self.assertIsNotNone(caught.exception.position)


class EngineTestCase(unittest.TestCase):
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

    def people(self) -> None:
        self.sql.execute(
            "CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)"
        )
        self.sql.execute(
            "INSERT INTO people VALUES "
            "(1, 'ada', 36), (2, 'bob', 41), (3, 'cy', NULL), (4, 'dee', 22)"
        )


class TestDdl(EngineTestCase):
    def test_create_and_drop(self):
        self.assertEqual(
            self.sql.execute("CREATE TABLE t (a INT)").message, "CREATE TABLE t"
        )
        self.assertEqual(self.sql.catalog.table_names(), ["t"])
        self.sql.execute("DROP TABLE t")
        self.assertEqual(self.sql.catalog.table_names(), [])

    def test_creating_twice_is_an_error_unless_if_not_exists(self):
        self.sql.execute("CREATE TABLE t (a INT)")
        with self.assertRaises(TableExistsError):
            self.sql.execute("CREATE TABLE t (a INT)")
        self.sql.execute("CREATE TABLE IF NOT EXISTS t (a INT)")  # no error

    def test_dropping_a_missing_table_is_an_error_unless_if_exists(self):
        with self.assertRaises(UnknownTableError):
            self.sql.execute("DROP TABLE nope")
        self.sql.execute("DROP TABLE IF EXISTS nope")

    def test_an_unknown_table_error_lists_the_tables_there_are(self):
        self.sql.execute("CREATE TABLE people (a INT)")
        with self.assertRaises(UnknownTableError) as caught:
            self.sql.execute("SELECT * FROM peple")
        self.assertIn("people", str(caught.exception))


class TestInsert(EngineTestCase):
    def test_insert_all_columns_positionally(self):
        self.people()
        self.assertEqual(len(self.rows("SELECT * FROM people")), 4)

    def test_insert_by_column_name_in_any_order(self):
        self.sql.execute("CREATE TABLE t (a INT, b TEXT, c INT)")
        self.sql.execute("INSERT INTO t (c, a) VALUES (3, 1)")
        self.assertEqual(self.rows("SELECT * FROM t"), [(1, None, 3)])

    def test_omitting_a_not_null_column_is_refused(self):
        self.sql.execute("CREATE TABLE t (a INT NOT NULL, b INT)")
        with self.assertRaises(PlanError):
            self.sql.execute("INSERT INTO t (b) VALUES (1)")

    def test_the_wrong_number_of_values_is_refused(self):
        self.sql.execute("CREATE TABLE t (a INT, b INT)")
        with self.assertRaises(PlanError):
            self.sql.execute("INSERT INTO t VALUES (1)")
        with self.assertRaises(PlanError):
            self.sql.execute("INSERT INTO t VALUES (1, 2, 3)")

    def test_a_value_of_the_wrong_type_is_refused_not_coerced(self):
        self.sql.execute("CREATE TABLE t (a INT, b TEXT)")
        with self.assertRaises(ValueTypeError):
            self.sql.execute("INSERT INTO t VALUES ('1', 'x')")
        with self.assertRaises(ValueTypeError):
            self.sql.execute("INSERT INTO t VALUES (1, 2)")

    def test_null_in_a_not_null_column_is_refused(self):
        self.sql.execute("CREATE TABLE t (a INT NOT NULL)")
        with self.assertRaises(Exception):
            self.sql.execute("INSERT INTO t VALUES (NULL)")
        self.assertEqual(self.rows("SELECT * FROM t"), [])

    def test_a_duplicate_primary_key_is_refused(self):
        self.people()
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute("INSERT INTO people VALUES (1, 'imposter', 0)")

    def test_a_multi_row_insert_is_all_or_nothing(self):
        """One statement is one transaction, so a bad third row undoes the first two."""
        self.people()
        before = len(self.rows("SELECT * FROM people"))
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute(
                "INSERT INTO people VALUES (10, 'ok', 1), (11, 'ok', 2), (1, 'clash', 3)"
            )
        self.assertEqual(len(self.rows("SELECT * FROM people")), before)
        self.sql.catalog.open("people").verify()


class TestSelect(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.people()

    def test_select_star_returns_every_column_in_schema_order(self):
        result = self.sql.execute("SELECT * FROM people WHERE id = 1")
        self.assertEqual(result.columns, ("id", "name", "age"))
        self.assertEqual(result.rows, [(1, "ada", 36)])

    def test_projection_picks_and_reorders_columns(self):
        result = self.sql.execute("SELECT age, name FROM people WHERE id = 2")
        self.assertEqual(result.columns, ("age", "name"))
        self.assertEqual(result.rows, [(41, "bob")])

    def test_an_unknown_column_is_an_error(self):
        with self.assertRaises(PlanError):
            self.sql.execute("SELECT nope FROM people")
        with self.assertRaises(PlanError):
            self.sql.execute("SELECT * FROM people WHERE nope = 1")
        with self.assertRaises(PlanError):
            self.sql.execute("SELECT * FROM people ORDER BY nope")

    def test_comparisons(self):
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people WHERE age > 30")], [1, 2]
        )
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people WHERE name = 'cy'")], [3]
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people WHERE id != 1")),
            [2, 3, 4],
        )

    def test_and_or_not(self):
        self.assertEqual(
            [r[0] for r in self.rows(
                "SELECT id FROM people WHERE age > 30 AND name = 'bob'"
            )],
            [2],
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows(
                "SELECT id FROM people WHERE id = 1 OR id = 4"
            )),
            [1, 4],
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows(
                "SELECT id FROM people WHERE NOT age > 30"
            )),
            [4],
        )

    def test_null_comparisons_are_unknown_and_filtered_out(self):
        """Three-valued logic: row 3 has a NULL age, so it satisfies neither
        `age = 41` nor `age != 41`. That is SQL, not a bug."""
        self.assertNotIn(3, [r[0] for r in self.rows(
            "SELECT id FROM people WHERE age = 41"
        )])
        self.assertNotIn(3, [r[0] for r in self.rows(
            "SELECT id FROM people WHERE age != 41"
        )])
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people WHERE age IS NULL")], [3]
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows(
                "SELECT id FROM people WHERE age IS NOT NULL"
            )),
            [1, 2, 4],
        )

    def test_false_and_unknown_is_false_not_unknown(self):
        rows = self.rows("SELECT id FROM people WHERE id = 999 AND age = 1")
        self.assertEqual(rows, [])

    def test_comparing_text_with_a_number_is_an_error(self):
        with self.assertRaises(ValueTypeError):
            self.sql.execute("SELECT * FROM people WHERE name = 1")

    def test_order_by_ascending_and_descending(self):
        self.assertEqual(
            [r[0] for r in self.rows("SELECT name FROM people ORDER BY name DESC")],
            ["dee", "cy", "bob", "ada"],
        )
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people ORDER BY id")], [1, 2, 3, 4]
        )

    def test_order_by_puts_nulls_first(self):
        self.assertEqual(
            [r[0] for r in self.rows("SELECT age FROM people ORDER BY age")],
            [None, 22, 36, 41],
        )

    def test_order_by_several_keys_with_mixed_directions(self):
        self.sql.execute("CREATE TABLE t (a INT, b INT)")
        self.sql.execute("INSERT INTO t VALUES (1, 1), (1, 2), (2, 1), (2, 2)")
        self.assertEqual(
            self.rows("SELECT a, b FROM t ORDER BY a ASC, b DESC"),
            [(1, 2), (1, 1), (2, 2), (2, 1)],
        )

    def test_limit_and_offset(self):
        self.assertEqual(
            [r[0] for r in self.rows("SELECT id FROM people ORDER BY id LIMIT 2")],
            [1, 2],
        )
        self.assertEqual(
            [r[0] for r in self.rows(
                "SELECT id FROM people ORDER BY id LIMIT 2 OFFSET 2"
            )],
            [3, 4],
        )
        self.assertEqual(self.rows("SELECT id FROM people LIMIT 0"), [])

    def test_a_query_matching_nothing_returns_no_rows_not_an_error(self):
        result = self.sql.execute("SELECT * FROM people WHERE id = 999")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.columns, ("id", "name", "age"))


class TestAccessPaths(EngineTestCase):
    """Which plan was chosen, not just which rows came back.

    A query that returns the right answer the slow way is still a bug, and the
    only way to catch it is to assert on the plan.
    """

    def setUp(self) -> None:
        super().setUp()
        self.people()

    def plan(self, sql: str) -> str:
        return self.sql.execute(sql).plan

    def test_equality_on_the_primary_key_uses_an_index_lookup(self):
        self.assertIn("seek", self.plan("SELECT * FROM people WHERE id = 2"))

    def test_the_comparison_can_be_written_either_way_round(self):
        self.assertIn("seek", self.plan("SELECT * FROM people WHERE 2 = id"))

    def test_a_range_on_the_primary_key_uses_an_index_range(self):
        for sql in (
            "SELECT * FROM people WHERE id > 2",
            "SELECT * FROM people WHERE id >= 2",
            "SELECT * FROM people WHERE id < 3",
            "SELECT * FROM people WHERE id <= 3",
        ):
            with self.subTest(sql=sql):
                self.assertIn("range", self.plan(sql))

    def test_a_range_bound_is_exact_at_the_edges(self):
        """The tree's bounds and SQL's are not the same: `>` is exclusive and the
        tree's lower bound is not, so the filter has to catch the boundary row."""
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people WHERE id > 2")), [3, 4]
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people WHERE id >= 2")),
            [2, 3, 4],
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people WHERE id < 3")), [1, 2]
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people WHERE id <= 3")),
            [1, 2, 3],
        )

    def test_an_indexable_condition_inside_an_and_is_still_used(self):
        plan = self.plan("SELECT * FROM people WHERE name = 'bob' AND id = 2")
        self.assertIn("seek", plan)

    def test_an_or_cannot_use_the_index(self):
        """Both halves have to be considered, so the index would miss rows."""
        self.assertIn(
            "scan", self.plan("SELECT * FROM people WHERE id = 1 OR name = 'cy'")
        )

    def test_a_condition_on_an_unindexed_column_scans(self):
        self.assertIn("scan", self.plan("SELECT * FROM people WHERE age = 36"))

    def test_no_where_clause_scans(self):
        self.assertIn("scan", self.plan("SELECT * FROM people"))

    def test_a_table_without_a_primary_key_always_scans(self):
        self.sql.execute("CREATE TABLE logs (line TEXT)")
        self.sql.execute("INSERT INTO logs VALUES ('a')")
        self.assertIn("scan", self.plan("SELECT * FROM logs WHERE line = 'a'"))

    def test_an_index_lookup_reads_far_fewer_pages_than_a_scan(self):
        """The point of the whole exercise, measured in page reads.

        Needs its own database with a pool far smaller than the table: if
        everything is already cached, both plans read zero pages from disk and the
        measurement proves nothing.
        """
        path = os.path.join(self._tmp.name, "measured.db")
        db = Database(path, capacity=16)
        self.addCleanup(db.close)
        sql = Engine(db)
        sql.execute("CREATE TABLE big (id INT PRIMARY KEY, filler TEXT)")
        table = sql.catalog.open("big")
        for batch in range(20):  # committed in batches: one transaction would
            with db.transaction():  # outgrow a 16-frame pool
                for i in range(batch * 200, (batch + 1) * 200):
                    table.insert((i, f"filler-{i:06d}"))
        db.checkpoint()

        db.pool.stats.disk_reads = 0
        self.assertEqual(
            sql.execute("SELECT filler FROM big WHERE id = 3500").rows,
            [("filler-003500",)],
        )
        indexed = db.pool.stats.disk_reads

        db.pool.stats.disk_reads = 0
        self.assertEqual(
            sql.execute("SELECT id FROM big WHERE filler = 'filler-003500'").rows,
            [(3500,)],
        )
        scanned = db.pool.stats.disk_reads

        self.assertGreater(scanned, 20, "the scan should have read real pages")
        self.assertLess(indexed, scanned / 5, f"indexed {indexed}, scanned {scanned}")


class TestUpdateAndDelete(EngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.people()

    def tearDown(self) -> None:
        self.sql.catalog.open("people").verify()

    def test_update_changes_the_rows_it_matches_and_says_how_many(self):
        result = self.sql.execute("UPDATE people SET age = 100 WHERE id = 1")
        self.assertEqual(result.row_count, 1)
        self.assertEqual(self.rows("SELECT age FROM people WHERE id = 1"), [(100,)])

    def test_update_without_a_where_touches_every_row(self):
        self.assertEqual(self.sql.execute("UPDATE people SET age = 1").row_count, 4)
        self.assertEqual(
            {r[0] for r in self.rows("SELECT age FROM people")}, {1}
        )

    def test_update_can_set_null_and_text(self):
        self.sql.execute("UPDATE people SET age = NULL, name = 'renamed' WHERE id = 1")
        self.assertEqual(
            self.rows("SELECT name, age FROM people WHERE id = 1"), [("renamed", None)]
        )

    def test_update_refuses_null_in_a_not_null_column(self):
        with self.assertRaises(PlanError):
            self.sql.execute("UPDATE people SET name = NULL WHERE id = 1")

    def test_update_refuses_the_wrong_type(self):
        with self.assertRaises(ValueTypeError):
            self.sql.execute("UPDATE people SET age = 'old' WHERE id = 1")

    def test_updating_the_primary_key_moves_the_index_entry(self):
        self.sql.execute("UPDATE people SET id = 99 WHERE id = 1")
        self.assertEqual(self.rows("SELECT name FROM people WHERE id = 99"), [("ada",)])
        self.assertEqual(self.rows("SELECT name FROM people WHERE id = 1"), [])

    def test_updating_onto_an_existing_primary_key_is_refused(self):
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute("UPDATE people SET id = 2 WHERE id = 1")
        self.assertEqual(len(self.rows("SELECT * FROM people")), 4)

    def test_delete_removes_matching_rows(self):
        self.assertEqual(
            self.sql.execute("DELETE FROM people WHERE age > 30").row_count, 2
        )
        self.assertEqual(
            sorted(r[0] for r in self.rows("SELECT id FROM people")), [3, 4]
        )

    def test_delete_without_a_where_empties_the_table(self):
        self.assertEqual(self.sql.execute("DELETE FROM people").row_count, 4)
        self.assertEqual(self.rows("SELECT * FROM people"), [])

    def test_delete_matching_nothing_is_zero_rows(self):
        self.assertEqual(
            self.sql.execute("DELETE FROM people WHERE id = 999").row_count, 0
        )

    def test_deleted_keys_can_be_reused(self):
        self.sql.execute("DELETE FROM people WHERE id = 1")
        self.sql.execute("INSERT INTO people VALUES (1, 'new ada', 1)")
        self.assertEqual(
            self.rows("SELECT name FROM people WHERE id = 1"), [("new ada",)]
        )


class TestTransactions(EngineTestCase):
    def test_statements_outside_a_transaction_commit_on_their_own(self):
        self.people()
        self.reopen()
        self.assertEqual(len(self.rows("SELECT * FROM people")), 4)

    def test_begin_and_rollback_undo_every_statement_between_them(self):
        self.people()
        self.sql.execute("BEGIN")
        self.sql.execute("DELETE FROM people")
        self.sql.execute("INSERT INTO people VALUES (9, 'ghost', 1)")
        self.assertEqual(len(self.rows("SELECT * FROM people")), 1)
        self.sql.execute("ROLLBACK")
        self.assertEqual(len(self.rows("SELECT * FROM people")), 4)
        self.sql.catalog.open("people").verify()

    def test_begin_and_commit_keep_them(self):
        self.people()
        self.sql.execute("BEGIN")
        self.sql.execute("DELETE FROM people WHERE id = 1")
        self.sql.execute("COMMIT")
        self.reopen()
        self.assertEqual(len(self.rows("SELECT * FROM people")), 3)

    def test_a_rolled_back_create_table_leaves_no_table(self):
        self.sql.execute("BEGIN")
        self.sql.execute("CREATE TABLE t (a INT)")
        self.sql.execute("INSERT INTO t VALUES (1)")
        self.sql.execute("ROLLBACK")
        self.assertEqual(self.sql.catalog.table_names(), [])
        with self.assertRaises(UnknownTableError):
            self.sql.execute("SELECT * FROM t")

    def test_a_failed_statement_inside_a_transaction_does_not_end_it(self):
        self.people()
        self.sql.execute("BEGIN")
        self.sql.execute("DELETE FROM people WHERE id = 4")
        with self.assertRaises(DuplicateKeyError):
            self.sql.execute("INSERT INTO people VALUES (1, 'clash', 0)")
        self.assertTrue(self.db.in_transaction)
        self.sql.execute("COMMIT")
        self.assertEqual(len(self.rows("SELECT * FROM people")), 3)


class TestPersistence(EngineTestCase):
    def test_everything_survives_a_reopen(self):
        self.people()
        self.sql.execute("CREATE TABLE logs (line TEXT)")
        self.sql.execute("INSERT INTO logs VALUES ('one'), ('two')")
        self.reopen()
        self.assertEqual(self.sql.catalog.table_names(), ["logs", "people"])
        self.assertEqual(
            self.rows("SELECT name FROM people ORDER BY id"),
            [("ada",), ("bob",), ("cy",), ("dee",)],
        )
        self.assertEqual(len(self.rows("SELECT * FROM logs")), 2)
        self.sql.catalog.open("people").verify()

    def test_a_larger_table_survives_a_reopen_and_stays_indexed(self):
        self.sql.execute("CREATE TABLE big (id INT PRIMARY KEY, name TEXT)")
        with self.db.transaction():
            table = self.sql.catalog.open("big")
            for i in range(3000):
                table.insert((i, f"name-{i}"))
        self.reopen()
        self.sql.catalog.open("big").verify()
        self.assertIn("seek", self.sql.execute(
            "SELECT * FROM big WHERE id = 2999"
        ).plan)
        self.assertEqual(
            self.rows("SELECT name FROM big WHERE id = 2999"), [("name-2999",)]
        )
        self.assertEqual(
            [r[0] for r in self.rows(
                "SELECT id FROM big WHERE id >= 100 ORDER BY id LIMIT 3"
            )],
            [100, 101, 102],
        )


if __name__ == "__main__":
    unittest.main()
