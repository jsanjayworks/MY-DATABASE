"""Layer 7c: the AST.

Plain dataclasses, no behaviour. The parser builds these, the planner reads them,
and keeping them dumb is what stops the two from growing into each other.

Expressions are a small tree of their own: comparisons and `AND`/`OR`/`NOT` over
column references and literals. That is all `WHERE` supports, and it is enough to
run real queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydb.record import Column

# ----------------------------------------------------------------------
# expressions
# ----------------------------------------------------------------------


class Expression:
    """Base class, so a type annotation can say "any expression"."""


@dataclass(frozen=True)
class Literal(Expression):
    value: object  # int, str, or None for NULL

    def __str__(self) -> str:
        if self.value is None:
            return "NULL"
        return repr(self.value) if isinstance(self.value, str) else str(self.value)


@dataclass(frozen=True)
class ColumnRef(Expression):
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Compare(Expression):
    operator: str  # one of = != < <= > >=
    left: Expression
    right: Expression

    def __str__(self) -> str:
        return f"{self.left} {self.operator} {self.right}"


@dataclass(frozen=True)
class IsNull(Expression):
    operand: Expression
    negated: bool = False

    def __str__(self) -> str:
        return f"{self.operand} IS {'NOT ' if self.negated else ''}NULL"


@dataclass(frozen=True)
class And(Expression):
    left: Expression
    right: Expression

    def __str__(self) -> str:
        return f"({self.left} AND {self.right})"


@dataclass(frozen=True)
class Or(Expression):
    left: Expression
    right: Expression

    def __str__(self) -> str:
        return f"({self.left} OR {self.right})"


@dataclass(frozen=True)
class Not(Expression):
    operand: Expression

    def __str__(self) -> str:
        return f"NOT {self.operand}"


# ----------------------------------------------------------------------
# statements
# ----------------------------------------------------------------------


class Statement:
    """Base class for the things `execute` accepts."""


@dataclass
class CreateTable(Statement):
    name: str
    columns: list[Column]
    primary_key: str | None = None
    if_not_exists: bool = False


@dataclass
class DropTable(Statement):
    name: str
    if_exists: bool = False


@dataclass
class Insert(Statement):
    table: str
    columns: list[str] | None  # None means "every column, in schema order"
    rows: list[list[Expression]]


@dataclass
class OrderBy:
    column: str
    descending: bool = False


@dataclass
class Select(Statement):
    table: str
    columns: list[str] | None  # None means SELECT *
    where: Expression | None = None
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None


@dataclass
class Delete(Statement):
    table: str
    where: Expression | None = None


@dataclass
class Update(Statement):
    table: str
    assignments: list[tuple[str, Expression]]
    where: Expression | None = None


@dataclass
class Begin(Statement):
    pass


@dataclass
class Commit(Statement):
    pass


@dataclass
class Rollback(Statement):
    pass
