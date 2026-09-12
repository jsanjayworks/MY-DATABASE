"""Layer 7e: the planner and the executor.

The planner's whole job is choosing an **access path**: how to get the rows out of
the table before anything is done to them. There are three, and the difference
between them is the difference between a database and a file:

* `index_lookup` -- `WHERE id = 42` on the primary key. One descent of the B+Tree,
  one heap read. Three or four pages, whatever the table's size.
* `index_range` -- `WHERE id > 100`, again on the primary key. Descend once, then
  walk the leaf chain. Reads the matching rows and nothing else.
* `scan` -- everything else. Every page of the table.

Everything after the access path is a chain of generators -- filter, sort,
project, limit -- each taking rows from the one below. That is the classic
iterator (or "volcano") model, and it has a property worth the structure: a
`LIMIT 5` stops pulling after five rows, so the scan underneath it stops too.
Only `ORDER BY` has to break the chain, because sorting cannot start until it has
seen everything.

Comparisons follow SQL's three-valued logic, where a comparison against NULL is
neither true nor false but *unknown*, and `WHERE` keeps only rows that are
definitely true. `NULL = NULL` is unknown; that is not a bug to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from pydb.catalog import Table
from pydb.heap import RowId
from pydb.record import ColumnType, encode_key
from pydb.sql import nodes
from pydb.sql.errors import PlanError, ValueTypeError

# A row as it moves through the pipeline: where it came from, and its values.
Row = tuple[RowId, tuple]

UNKNOWN = None  # the third truth value, kept explicit rather than implied


@dataclass
class AccessPath:
    """How the rows will be fetched, and a sentence explaining why."""

    kind: str  # "scan", "index_lookup" or "index_range"
    description: str
    low: bytes | None = None
    high: bytes | None = None
    value: object = None

    def __str__(self) -> str:
        return self.description


def choose_access_path(table: Table, where: nodes.Expression | None) -> AccessPath:
    """Pick the cheapest way to read the rows a `WHERE` clause could match.

    Only the primary key is indexed, and only conjunctions are considered: in
    `a AND b` either half can choose the path because both must hold anyway. An
    `OR` cannot, since rows matching its other half would be missed.
    """
    key_name = table.info.primary_key_name
    if where is None or key_name is None:
        return AccessPath("scan", f"scan {table.name}")

    key_type = table.schema[key_name].type
    for condition in _conjuncts(where):
        if not isinstance(condition, nodes.Compare):
            continue
        column, literal, operator = _normalise(condition)
        if column is None or column.name != key_name or literal.value is None:
            continue
        try:
            key = encode_key(key_type, literal.value)
        except Exception:
            continue  # a type mismatch: let the filter reject the rows instead

        if operator == "=":
            return AccessPath(
                "index_lookup",
                f"index lookup on {table.name}.{key_name} = {literal}",
                value=literal.value,
            )
        if operator in (">", ">="):
            # A `>` lower bound is inclusive in the tree, so the filter above still
            # has to drop the boundary row. Being slightly generous is safe; being
            # slightly narrow would lose rows.
            return AccessPath(
                "index_range",
                f"index range on {table.name}.{key_name} {operator} {literal}",
                low=key,
            )
        if operator in ("<", "<="):
            return AccessPath(
                "index_range",
                f"index range on {table.name}.{key_name} {operator} {literal}",
                high=key if operator == "<" else _successor(key),
            )
    return AccessPath("scan", f"scan {table.name}")


def read_rows(table: Table, path: AccessPath) -> Iterator[Row]:
    """Open the access path and produce rows."""
    if path.kind == "index_lookup":
        found = table.lookup(path.value)
        if found is not None:
            yield found
        return
    if path.kind == "index_range":
        yield from table.index_range(path.low, path.high)
        return
    yield from table.scan()


# ----------------------------------------------------------------------
# the pipeline
# ----------------------------------------------------------------------


def filter_rows(
    rows: Iterable[Row], table: Table, where: nodes.Expression | None
) -> Iterator[Row]:
    if where is None:
        yield from rows
        return
    for rid, values in rows:
        if evaluate(where, table, values) is True:
            yield rid, values


def sort_rows(rows: Iterable[Row], table: Table, order: list[nodes.OrderBy]) -> list[Row]:
    """Sort by each key in turn. Materialises, because sorting has to.

    Sorted last key first, relying on Python's stable sort, which is the standard
    trick for a multi-key sort with mixed directions.
    """
    materialised = list(rows)
    for key in reversed(order):
        index = _column_index(table, key.column)
        materialised.sort(
            key=lambda row, index=index: _sort_key(row[1][index]),
            reverse=key.descending,
        )
    return materialised


def project(rows: Iterable[Row], table: Table, columns: list[str] | None) -> Iterator[tuple]:
    if columns is None:
        for _rid, values in rows:
            yield values
        return
    indexes = [_column_index(table, name) for name in columns]
    for _rid, values in rows:
        yield tuple(values[index] for index in indexes)


def apply_limit(
    rows: Iterable[tuple], limit: int | None, offset: int | None
) -> Iterator[tuple]:
    """Skip `offset` rows and stop after `limit`.

    Stopping matters: this generator is what lets a `LIMIT` keep the scan
    underneath it from reading the rest of the table.
    """
    remaining = limit
    skip = offset or 0
    for row in rows:
        if skip > 0:
            skip -= 1
            continue
        if remaining is not None:
            if remaining == 0:
                return
            remaining -= 1
        yield row


# ----------------------------------------------------------------------
# expression evaluation
# ----------------------------------------------------------------------


def evaluate(
    expression: nodes.Expression, table: Table, values: tuple
) -> bool | None:
    """Evaluate a `WHERE` expression against one row, in three-valued logic.

    Returns True, False, or None for unknown. `WHERE` keeps only True, which is
    why a row whose column is NULL is dropped by both `= 5` and `!= 5`.
    """
    if isinstance(expression, nodes.And):
        left = evaluate(expression.left, table, values)
        right = evaluate(expression.right, table, values)
        if left is False or right is False:
            return False  # false AND unknown is false, not unknown
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return True

    if isinstance(expression, nodes.Or):
        left = evaluate(expression.left, table, values)
        right = evaluate(expression.right, table, values)
        if left is True or right is True:
            return True
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return False

    if isinstance(expression, nodes.Not):
        inner = evaluate(expression.operand, table, values)
        return UNKNOWN if inner is UNKNOWN else not inner

    if isinstance(expression, nodes.IsNull):
        # The one construct that gives a definite answer about NULL.
        is_null = value_of(expression.operand, table, values) is None
        return is_null != expression.negated

    if isinstance(expression, nodes.Compare):
        left = value_of(expression.left, table, values)
        right = value_of(expression.right, table, values)
        if left is None or right is None:
            return UNKNOWN
        return _compare(expression.operator, left, right, expression)

    # A bare column or literal used as a condition: true when it is a truthy value.
    value = value_of(expression, table, values)
    return UNKNOWN if value is None else bool(value)


def value_of(expression: nodes.Expression, table: Table, values: tuple) -> object:
    if isinstance(expression, nodes.Literal):
        return expression.value
    if isinstance(expression, nodes.ColumnRef):
        return values[_column_index(table, expression.name)]
    raise PlanError(f"{expression} cannot be used as a value")


def _compare(
    operator: str, left: object, right: object, expression: nodes.Compare
) -> bool:
    if isinstance(left, str) != isinstance(right, str):
        raise ValueTypeError(
            f"cannot compare {_type_name(left)} with {_type_name(right)} in "
            f"{expression}"
        )
    if operator == "=":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "<":
        return left < right  # type: ignore[operator]
    if operator == "<=":
        return left <= right  # type: ignore[operator]
    if operator == ">":
        return left > right  # type: ignore[operator]
    return left >= right  # type: ignore[operator]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _conjuncts(expression: nodes.Expression) -> Iterator[nodes.Expression]:
    """Flatten a chain of ANDs. Everything else yields itself."""
    if isinstance(expression, nodes.And):
        yield from _conjuncts(expression.left)
        yield from _conjuncts(expression.right)
    else:
        yield expression


def _normalise(
    condition: nodes.Compare,
) -> tuple[nodes.ColumnRef | None, nodes.Literal, str]:
    """Rewrite a comparison as `column op literal`, flipping it if need be.

    `42 = id` and `id = 42` mean the same thing, and the planner should not have to
    care which way round it was written.
    """
    flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
    if isinstance(condition.left, nodes.ColumnRef) and isinstance(
        condition.right, nodes.Literal
    ):
        return condition.left, condition.right, condition.operator
    if isinstance(condition.right, nodes.ColumnRef) and isinstance(
        condition.left, nodes.Literal
    ):
        return (
            condition.right,
            condition.left,
            flipped.get(condition.operator, condition.operator),
        )
    return None, nodes.Literal(None), condition.operator


def _successor(key: bytes) -> bytes:
    """The next byte string after `key`, to turn `<=` into an exclusive bound."""
    return key + b"\x00"


def _column_index(table: Table, name: str) -> int:
    if not table.schema.has(name):
        raise PlanError(
            f"no column {name!r} in {table.name}; it has {list(table.schema.names)}"
        )
    return table.schema.index(name)


def _sort_key(value: object) -> tuple:
    """A sort key that puts NULL first and never compares a str with an int.

    Python refuses to order `None` or to compare `1 < 'a'`, so each value becomes
    `(group, value)` where the group makes the types sort in blocks.
    """
    if value is None:
        return (0, 0)
    if isinstance(value, str):
        return (2, value)
    return (1, value)


def _type_name(value: object) -> str:
    if value is None:
        return "NULL"
    return "TEXT" if isinstance(value, str) else "INT"


def coerce_value(column_type: ColumnType, value: object, where: str) -> object:
    """Check a literal against the column it is going into.

    SQL engines vary wildly in how much they coerce here. This one does not
    coerce at all: storing `'42'` in an INT column is a mistake worth reporting,
    not something to quietly convert.
    """
    if value is None:
        return None
    if column_type is ColumnType.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeError(f"{where} expects INT, got {_type_name(value)}")
        return value
    if not isinstance(value, str):
        raise ValueTypeError(f"{where} expects TEXT, got {_type_name(value)}")
    return value
