"""Layer 7c: the AST.

Plain dataclasses, no behaviour. The parser builds these, the planner reads them,
and keeping them dumb is what stops the two from growing into each other.

Two shapes here are worth understanding before reading the planner:

* A **column reference carries a qualifier**: `people.name` is
  `ColumnRef("name", "people")` and bare `name` is `ColumnRef("name", None)`. Once
  a query can name two tables, an unqualified name may be ambiguous, and the
  planner needs to be able to say so.
* The **FROM clause is a tree**, not a name. `a JOIN b ON ... JOIN c ON ...` nests
  to the left, which is the order the join will actually be executed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydb.record import Column

# The aggregate functions, and whether each accepts `*`.
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


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
        if isinstance(self.value, str):
            escaped = self.value.replace("'", "''")
            return f"'{escaped}'"
        return str(self.value)


@dataclass(frozen=True)
class ColumnRef(Expression):
    name: str
    qualifier: str | None = None

    def __str__(self) -> str:
        return f"{self.qualifier}.{self.name}" if self.qualifier else self.name


@dataclass(frozen=True)
class FunctionCall(Expression):
    """An aggregate call. `argument` is None for `COUNT(*)`.

    Frozen and hashable on purpose: the planner uses the node itself as the key
    for the value it computed, so an expression tree can be evaluated with the
    aggregates already filled in.
    """

    name: str  # upper case, one of AGGREGATES
    argument: Expression | None = None
    distinct: bool = False

    def __str__(self) -> str:
        inside = "*" if self.argument is None else str(self.argument)
        return f"{self.name.lower()}({'DISTINCT ' if self.distinct else ''}{inside})"

    @property
    def is_count_star(self) -> bool:
        return self.name == "COUNT" and self.argument is None


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
# select list and FROM clause
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Star:
    """`*`, or `people.*` when qualified."""

    qualifier: str | None = None

    def __str__(self) -> str:
        return f"{self.qualifier}.*" if self.qualifier else "*"


@dataclass
class SelectItem:
    value: Expression | Star
    alias: str | None = None

    def label(self) -> str:
        """The column name this item produces."""
        if self.alias:
            return self.alias
        if isinstance(self.value, ColumnRef):
            return self.value.name
        return str(self.value)


@dataclass
class TableRef:
    name: str
    alias: str | None = None

    @property
    def label(self) -> str:
        """How this table is named inside the query."""
        return self.alias or self.name

    def __str__(self) -> str:
        return self.name if self.alias is None else f"{self.name} AS {self.alias}"


@dataclass
class Join:
    left: "TableRef | Join"
    right: TableRef
    kind: str = "INNER"  # INNER, LEFT or CROSS
    condition: Expression | None = None

    def __str__(self) -> str:
        text = f"{self.left} {self.kind} JOIN {self.right}"
        return text if self.condition is None else f"{text} ON {self.condition}"


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
class CreateIndex(Statement):
    name: str
    table: str
    column: str
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class DropIndex(Statement):
    name: str
    if_exists: bool = False


@dataclass
class Insert(Statement):
    table: str
    columns: list[str] | None  # None means "every column, in schema order"
    rows: list[list[Expression]]


@dataclass
class OrderBy:
    value: Expression | int  # an expression, or a 1-based select-list position
    descending: bool = False

    def __str__(self) -> str:
        return f"{self.value}{' DESC' if self.descending else ''}"


@dataclass
class Select(Statement):
    source: TableRef | Join
    items: list[SelectItem]
    where: Expression | None = None
    group_by: list[Expression] = field(default_factory=list)
    having: Expression | None = None
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False


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
class Vacuum(Statement):
    pass


@dataclass
class Explain(Statement):
    statement: Statement


@dataclass
class Begin(Statement):
    pass


@dataclass
class Commit(Statement):
    pass


@dataclass
class Rollback(Statement):
    pass


# ----------------------------------------------------------------------
# walking expressions
# ----------------------------------------------------------------------


def walk(expression: Expression | Star | None):
    """Yield `expression` and every expression inside it, outermost first."""
    if expression is None or isinstance(expression, Star):
        return
    yield expression
    for child in (
        getattr(expression, "left", None),
        getattr(expression, "right", None),
        getattr(expression, "operand", None),
        getattr(expression, "argument", None),
    ):
        if isinstance(child, Expression):
            yield from walk(child)


def aggregates_in(expression: Expression | Star | None) -> list[FunctionCall]:
    """Every aggregate call inside `expression`, outermost first."""
    return [node for node in walk(expression) if isinstance(node, FunctionCall)]
