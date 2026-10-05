from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pydb.database import Database
from pydb.repl import Repl, format_table
from pydb.sql import Engine, Result


class ReplTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")

    def session(self, script: str) -> tuple[str, int]:
        with Database(self.path) as db:
            output = io.StringIO()
            repl = Repl(
                Engine(db),
                stdin=io.StringIO(script),
                stdout=output,
                interactive=False,
            )
            failures = repl.run()
        return output.getvalue(), failures


class TestFormatting(unittest.TestCase):
    def test_columns_are_aligned_and_rows_are_counted(self):
        result = Result(columns=("id", "name"), rows=[(1, "ada"), (22, "b")])
        self.assertEqual(
            format_table(result).splitlines(),
            ["id  name", "--  ----", "1   ada", "22  b", "(2 rows)"],
        )

    def test_one_row_is_singular(self):
        result = Result(columns=("a",), rows=[(1,)])
        self.assertIn("(1 row)", format_table(result))

    def test_no_rows_still_shows_the_header(self):
        result = Result(columns=("a", "b"), rows=[])
        self.assertEqual(
            format_table(result).splitlines(), ["a  b", "-  -", "(0 rows)"]
        )

    def test_null_is_spelled_out(self):
        result = Result(columns=("a",), rows=[(None,)])
        self.assertIn("NULL", format_table(result))


class TestStatementSplitting(ReplTestCase):
    def test_a_statement_can_span_several_lines(self):
        output, failures = self.session(
            "CREATE TABLE people (\n  id INT PRIMARY KEY,\n  name TEXT\n);\n"
            "INSERT INTO people\nVALUES (1, 'ada');\n"
            "SELECT name FROM people;\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("ada", output)

    def test_two_statements_on_one_line_both_run(self):
        output, failures = self.session(
            "CREATE TABLE t (a INT); INSERT INTO t VALUES (1);\nSELECT * FROM t;\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("(1 row)", output)

    def test_a_semicolon_inside_a_string_does_not_end_the_statement(self):
        output, failures = self.session(
            "CREATE TABLE t (a TEXT);\n"
            "INSERT INTO t VALUES ('a;b');\n"
            "SELECT * FROM t;\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("a;b", output)

    def test_blank_lines_and_comments_are_skipped(self):
        output, failures = self.session(
            "\n-- a comment\n\nCREATE TABLE t (a INT);\n-- another\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("CREATE TABLE t", output)

    def test_a_statement_without_its_semicolon_still_runs_at_end_of_input(self):
        output, failures = self.session("CREATE TABLE t (a INT)")
        self.assertEqual(failures, 0, output)
        self.assertIn("CREATE TABLE t", output)


class TestDotCommands(ReplTestCase):
    def test_tables_lists_them(self):
        output, failures = self.session(
            "CREATE TABLE b (x INT);\nCREATE TABLE a (y INT);\n.tables\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("a\nb", output)

    def test_tables_says_so_when_there_are_none(self):
        output, _ = self.session(".tables\n")
        self.assertIn("no tables", output)

    def test_schema_prints_a_create_table_statement(self):
        output, failures = self.session(
            "CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);\n"
            ".schema people\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("id INT PRIMARY KEY", output)
        self.assertIn("name TEXT NOT NULL", output)
        self.assertIn("age INT", output)

    def test_schema_with_no_argument_covers_every_table(self):
        output, _ = self.session(
            "CREATE TABLE a (x INT);\nCREATE TABLE b (y TEXT);\n.schema\n"
        )
        self.assertIn("CREATE TABLE a", output)
        self.assertIn("CREATE TABLE b", output)

    def test_plan_toggles_the_access_path_display(self):
        output, _ = self.session(
            "CREATE TABLE t (id INT PRIMARY KEY);\n"
            "INSERT INTO t VALUES (1);\n"
            ".plan on\n"
            "SELECT * FROM t WHERE id = 1;\n"
            ".plan off\n"
            "SELECT * FROM t WHERE id = 1;\n"
        )
        self.assertEqual(output.count("seek t using t_pkey"), 1)

    def test_help_mentions_the_commands(self):
        output, _ = self.session(".help\n")
        for command in (".tables", ".schema", ".quit"):
            self.assertIn(command, output)

    def test_quit_stops_reading(self):
        output, failures = self.session(".quit\nCREATE TABLE never (a INT);\n")
        self.assertEqual(failures, 0)
        self.assertNotIn("CREATE TABLE never", output)

    def test_an_unknown_command_is_reported_and_counted(self):
        output, failures = self.session(".nonsense\n")
        self.assertEqual(failures, 1)
        self.assertIn("unknown command", output)


class TestErrorHandling(ReplTestCase):
    def test_a_parse_error_is_a_message_not_a_traceback(self):
        output, failures = self.session("SELECT FROM;\n")
        self.assertEqual(failures, 1)
        self.assertIn("error:", output)
        self.assertNotIn("Traceback", output)

    def test_the_session_continues_after_an_error(self):
        output, failures = self.session(
            "SELECT * FROM nope;\nCREATE TABLE t (a INT);\nSELECT * FROM t;\n"
        )
        self.assertEqual(failures, 1)
        self.assertIn("no such table", output)
        self.assertIn("(0 rows)", output)

    def test_a_bug_in_pydb_is_labelled_as_one(self):
        with Database(self.path) as db:
            engine = Engine(db)
            engine.execute("CREATE TABLE t (id INT PRIMARY KEY)")
            real_execute = engine.execute

            def execute(sql: str):
                if sql.startswith("SELECT"):
                    raise TypeError("something pydb got wrong")
                return real_execute(sql)

            engine.execute = execute
            output = io.StringIO()
            repl = Repl(
                engine,
                stdin=io.StringIO("SELECT * FROM t;\nINSERT INTO t VALUES (1), (1);\n"),
                stdout=output,
                interactive=False,
            )
            failures = repl.run()
        lines = output.getvalue().splitlines()
        self.assertEqual(failures, 2)
        self.assertTrue(lines[0].startswith("internal error (TypeError):"), lines)
        self.assertTrue(lines[1].startswith("error: "), lines)

    def test_a_type_error_names_the_column(self):
        output, failures = self.session(
            "CREATE TABLE t (a INT);\nINSERT INTO t VALUES ('x');\n"
        )
        self.assertEqual(failures, 1)
        self.assertIn("t.a", output)

    def test_a_rolled_back_transaction_in_a_session(self):
        output, failures = self.session(
            "CREATE TABLE t (a INT);\n"
            "INSERT INTO t VALUES (1);\n"
            "BEGIN;\n"
            "DELETE FROM t;\n"
            "ROLLBACK;\n"
            "SELECT * FROM t;\n"
        )
        self.assertEqual(failures, 0, output)
        self.assertIn("(1 row)", output)


class TestMilestone(ReplTestCase):
    def repl_process(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pydb", self.path],
            input=script,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=120,
        )

    def test_a_session_survives_a_restart(self):
        first = self.repl_process(
            "CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);\n"
            "INSERT INTO people VALUES (1, 'ada', 36), (2, 'bob', 41);\n"
            "INSERT INTO people VALUES (3, 'cy', NULL);\n"
            "SELECT * FROM people;\n"
            ".quit\n"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("(3 rows)", first.stdout)

        second = self.repl_process(
            ".tables\n"
            ".schema people\n"
            "SELECT name, age FROM people ORDER BY id;\n"
            "SELECT name FROM people WHERE id = 2;\n"
            "SELECT name FROM people WHERE age IS NULL;\n"
            ".quit\n"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("people", second.stdout)
        self.assertIn("id INT PRIMARY KEY", second.stdout)
        self.assertIn("ada", second.stdout)
        self.assertIn("bob", second.stdout)
        self.assertIn("NULL", second.stdout)
        self.assertIn("(3 rows)", second.stdout)

        third = self.repl_process(
            "INSERT INTO people VALUES (4, 'dee', 22);\n"
            "UPDATE people SET age = 37 WHERE id = 1;\n"
            "DELETE FROM people WHERE id = 3;\n"
            "SELECT id, name, age FROM people ORDER BY id;\n"
            ".quit\n"
        )
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("(3 rows)", third.stdout)
        self.assertIn("37", third.stdout)
        self.assertIn("dee", third.stdout)
        self.assertNotIn("cy", third.stdout)

    def test_a_thousand_rows_through_the_repl_and_back(self):
        script = "CREATE TABLE nums (n INT PRIMARY KEY, label TEXT);\n"
        script += "BEGIN;\n"
        script += "".join(
            f"INSERT INTO nums VALUES ({i}, 'row-{i}');\n" for i in range(1000)
        )
        script += "COMMIT;\n.quit\n"
        first = self.repl_process(script)
        self.assertEqual(first.returncode, 0, first.stderr[-2000:])

        second = self.repl_process(
            "SELECT label FROM nums WHERE n = 999;\n"
            "SELECT n FROM nums ORDER BY n DESC LIMIT 3;\n"
            "SELECT n FROM nums WHERE n >= 500 AND n < 503 ORDER BY n;\n"
            ".quit\n"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("row-999", second.stdout)
        self.assertIn("999\n998\n997", second.stdout.replace("  \n", "\n"))
        self.assertIn("500", second.stdout)

    def test_the_usage_message_when_given_no_database(self):
        result = subprocess.run(
            [sys.executable, "-m", "pydb"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stdout)


if __name__ == "__main__":
    unittest.main()
