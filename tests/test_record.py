"""Tests for layer 3a, schemas and row encoding.

Encoding is the one place where a bug is invisible until much later: a row that
encodes wrongly still writes, still reads back, and only surfaces as nonsense
three layers up. So these tests are mostly round-trips, plus the edges where
`struct` would happily do the wrong thing.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.record import (  # noqa: E402
    INT_MAX,
    INT_MIN,
    TEXT_MAX_LEN,
    Column,
    ColumnType,
    RecordError,
    Schema,
    SchemaError,
)


class TestSchema(unittest.TestCase):
    def test_of_builds_columns_from_terse_specs(self):
        schema = Schema.of(("id", "INTEGER", False), ("name", "text"))
        self.assertEqual(schema.names, ("id", "name"))
        self.assertEqual(schema[0], Column("id", ColumnType.INT, nullable=False))
        self.assertEqual(schema["name"], Column("name", ColumnType.TEXT, True))

    def test_rejects_an_empty_schema(self):
        with self.assertRaises(SchemaError):
            Schema([])

    def test_rejects_duplicate_column_names(self):
        with self.assertRaises(SchemaError):
            Schema.of(("a", "INT"), ("a", "TEXT"))

    def test_rejects_an_unknown_type_name(self):
        with self.assertRaises(SchemaError):
            Schema.of(("a", "BLOB"))

    def test_unknown_column_names_report_what_is_available(self):
        schema = Schema.of(("id", "INT"), ("name", "TEXT"))
        with self.assertRaises(SchemaError) as caught:
            schema.index("nmae")
        self.assertIn("'id', 'name'", str(caught.exception))

    def test_schemas_compare_by_columns(self):
        self.assertEqual(Schema.of(("a", "INT")), Schema.of(("a", "INT")))
        self.assertNotEqual(Schema.of(("a", "INT")), Schema.of(("a", "TEXT")))


class TestRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = Schema.of(("id", "INT", False), ("name", "TEXT"), ("age", "INT"))

    def assert_round_trips(self, values):
        encoded = self.schema.encode(values)
        self.assertEqual(len(encoded), self.schema.encoded_size(values))
        self.assertEqual(self.schema.decode(encoded), tuple(values))
        return encoded

    def test_round_trips_a_plain_row(self):
        self.assert_round_trips((1, "ada", 36))

    def test_round_trips_nulls(self):
        self.assert_round_trips((1, None, None))

    def test_round_trips_an_empty_string(self):
        """An empty string is not NULL, and the difference must survive a round trip."""
        encoded = self.assert_round_trips((1, "", 0))
        self.assertEqual(self.schema.decode(encoded)[1], "")
        self.assertIsNotNone(self.schema.decode(encoded)[1])

    def test_round_trips_negative_and_extreme_integers(self):
        for value in (-1, 0, 1, INT_MIN, INT_MAX):
            with self.subTest(value=value):
                self.assert_round_trips((value, "x", value))

    def test_round_trips_non_ascii_text(self):
        self.assert_round_trips((1, "π ≈ 3.14 — naïve café 日本語", 0))

    def test_multibyte_text_is_measured_in_bytes_not_characters(self):
        schema = Schema.of(("t", "TEXT"))
        self.assertEqual(len(schema.encode(("é",))), 1 + 2 + 2)  # bitmap, len, utf-8

    def test_decode_ignores_trailing_bytes(self):
        """A slot is exact, but a page buffer is not: decode must stop on its own."""
        encoded = self.schema.encode((7, "bob", 1))
        self.assertEqual(self.schema.decode(encoded + b"garbage"), (7, "bob", 1))

    def test_null_bitmap_is_one_byte_per_eight_columns(self):
        for count, expected in ((1, 1), (8, 1), (9, 2), (17, 3)):
            schema = Schema.of(*[(f"c{i}", "INT") for i in range(count)])
            with self.subTest(columns=count):
                self.assertEqual(len(schema.encode([None] * count)), expected)


class TestValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = Schema.of(("id", "INT", False), ("name", "TEXT"))

    def test_rejects_the_wrong_number_of_values(self):
        with self.assertRaises(SchemaError):
            self.schema.encode((1,))
        with self.assertRaises(SchemaError):
            self.schema.encode((1, "a", "extra"))

    def test_rejects_null_in_a_not_null_column(self):
        with self.assertRaises(SchemaError):
            self.schema.encode((None, "a"))

    def test_rejects_a_string_in_an_int_column(self):
        with self.assertRaises(SchemaError):
            self.schema.encode(("1", "a"))

    def test_rejects_an_int_in_a_text_column(self):
        with self.assertRaises(SchemaError):
            self.schema.encode((1, 2))

    def test_rejects_a_bool_in_an_int_column(self):
        """`True` is an `int` in Python. Storing it as 1 silently loses the type."""
        with self.assertRaises(SchemaError):
            self.schema.encode((True, "a"))

    def test_rejects_an_integer_too_big_for_eight_bytes(self):
        for value in (INT_MAX + 1, INT_MIN - 1):
            with self.subTest(value=value):
                with self.assertRaises(SchemaError):
                    self.schema.encode((value, "a"))

    def test_rejects_text_longer_than_the_length_prefix(self):
        with self.assertRaises(SchemaError):
            self.schema.encode((1, "x" * (TEXT_MAX_LEN + 1)))

    def test_a_truncated_row_is_a_record_error_not_a_struct_error(self):
        encoded = self.schema.encode((1, "ada"))
        for cut in range(1, len(encoded)):
            with self.subTest(cut=cut):
                with self.assertRaises(RecordError):
                    self.schema.decode(encoded[:cut])


if __name__ == "__main__":
    unittest.main()
