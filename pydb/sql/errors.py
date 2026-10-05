"""Errors the SQL layer raises, in one place so nothing imports in a circle."""

from __future__ import annotations

from pydb.errors import PydbError


class SqlError(PydbError):
    """Base class for statements the SQL layer itself rejects.

    Not every failed statement raises one. A duplicate key or a NOT NULL column
    is found further down, and raises that layer's own error -- which cannot
    subclass this one without a lower layer importing from a higher one. Catch
    `PydbError` for "the statement failed, for whatever reason".
    """


class ParseError(SqlError):
    """The statement is not valid SQL -- or not the subset this database speaks.

    Carries the position in the input so the message can point at the offending
    token instead of just naming it.
    """

    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.position = position


class PlanError(SqlError):
    """The statement parses but cannot be run: unknown column, wrong value count."""


class ValueTypeError(SqlError):
    """A value does not fit the column or the comparison it is being used in."""
