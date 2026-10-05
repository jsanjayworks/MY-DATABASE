from __future__ import annotations

from pydb.errors import PydbError


class SqlError(PydbError):
    pass


class ParseError(SqlError):
    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.position = position


class PlanError(SqlError):
    pass


class ValueTypeError(SqlError):
    pass
