"""Errors the SQL layer raises, in one place so nothing imports in a circle."""

from __future__ import annotations


class SqlError(Exception):
    """Base class for everything wrong with a statement."""


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
