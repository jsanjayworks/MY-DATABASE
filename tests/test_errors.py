from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pydb
from pydb import Database, Engine, PydbError


class TestHierarchy(unittest.TestCase):
    def test_every_exported_error_is_a_pydb_error(self):
        errors = [
            value
            for value in vars(pydb).values()
            if inspect.isclass(value) and issubclass(value, Exception)
        ]
        self.assertGreater(len(errors), 20, "found too few to prove anything")
        stray = [error.__name__ for error in errors if not issubclass(error, PydbError)]
        self.assertEqual(stray, [])


class TestFailedStatements(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "test.db"))
        self.addCleanup(self.db.close)
        self.sql = Engine(self.db)
        self.sql.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
        self.sql.execute("INSERT INTO t VALUES (1, 'ada')")

    def test_whichever_layer_refuses_a_statement_it_is_a_pydb_error(self):
        refused = {
            "SELEC * FROM t": "the parser",
            "SELECT nope FROM t": "the planner",
            "SELECT * FROM nope": "the catalog",
            "CREATE TABLE t (a INT)": "the catalog",
            "INSERT INTO t VALUES (1, 'clash')": "the B+Tree",
            "INSERT INTO t VALUES (2, NULL)": "the record layer",
            f"INSERT INTO t VALUES (3, '{'x' * 5000}')": "the slotted page",
            "COMMIT": "the transaction layer",
        }
        for statement, layer in refused.items():
            with self.subTest(layer=layer):
                with self.assertRaises(PydbError):
                    self.sql.execute(statement)
        self.assertEqual(self.sql.execute("SELECT * FROM t").rows, [(1, "ada")])


if __name__ == "__main__":
    unittest.main()
