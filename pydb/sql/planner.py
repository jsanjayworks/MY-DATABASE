from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from pydb.catalog import Index, Table
from pydb.heap import RowId
from pydb.record import ColumnType
from pydb.sql import nodes
from pydb.sql.errors import PlanError, ValueTypeError

UNKNOWN = None


@dataclass
class BoundColumn:
    label: str
    name: str
    type: ColumnType
    position: int
    index: int


class Binding:
    def __init__(self) -> None:
        self.columns: list[BoundColumn] = []
        self.labels: list[str] = []
        self._offsets: list[int] = []

    def add_source(self, label: str, table: Table) -> int:
        if label in self.labels:
            raise PlanError(
                f"{label!r} is used twice in FROM; give one of them an alias"
            )
        position = len(self.labels)
        self._offsets.append(len(self.columns))
        self.labels.append(label)
        for column in table.schema:
            self.columns.append(
                BoundColumn(label, column.name, column.type, position,
                            len(self.columns))
            )
        return position

    @property
    def width(self) -> int:
        return len(self.columns)

    def offset_of(self, position: int) -> int:
        return self._offsets[position]

    def resolve(self, reference: nodes.ColumnRef) -> BoundColumn:
        if reference.qualifier is not None:
            if reference.qualifier not in self.labels:
                raise PlanError(
                    f"no table {reference.qualifier!r} in this query; it has "
                    f"{self.labels}"
                )
            matches = [
                column
                for column in self.columns
                if column.label == reference.qualifier
                and column.name == reference.name
            ]
            if not matches:
                available = [
                    c.name for c in self.columns if c.label == reference.qualifier
                ]
                raise PlanError(
                    f"no column {reference.name!r} in {reference.qualifier}; it has "
                    f"{available}"
                )
            return matches[0]

        matches = [column for column in self.columns if column.name == reference.name]
        if not matches:
            raise PlanError(
                f"no column {reference.name!r} in this query; it has "
                f"{[c.name for c in self.columns]}"
            )
        if len(matches) > 1:
            owners = sorted({column.label for column in matches})
            raise PlanError(
                f"{reference.name!r} is ambiguous: it is in {owners}. Qualify it, "
                f"as in {owners[0]}.{reference.name}"
            )
        return matches[0]

    def columns_of(self, position: int) -> list[BoundColumn]:
        return [column for column in self.columns if column.position == position]

    def positions_used(self, expression: nodes.Expression | None) -> set[int]:
        used = set()
        for node in nodes.walk(expression):
            if isinstance(node, nodes.ColumnRef):
                used.add(self.resolve(node).position)
        return used

    def output_label(self, column: BoundColumn) -> str:
        return column.name if len(self.labels) == 1 else f"{column.label}.{column.name}"


@dataclass
class SourcePlan:
    label: str
    table: Table
    position: int
    join_kind: str = "INNER"
    condition: nodes.Expression | None = None
    method: str = "scan"
    index: Index | None = None
    value: object = None
    low: bytes | None = None
    high: bytes | None = None
    probe: nodes.Expression | None = None
    reason: str = ""

    def describe(self) -> str:
        joined = "" if self.position == 0 else f"{self.join_kind} join "
        return f"{joined}{self.method} {self.label}{self.reason}"


@dataclass
class QueryPlan:
    binding: Binding
    sources: list[SourcePlan]
    items: list[nodes.SelectItem]
    output: list[str]
    where: nodes.Expression | None = None
    group_by: list[nodes.Expression] = field(default_factory=list)
    having: nodes.Expression | None = None
    order_by: list[nodes.OrderBy] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False
    aggregates: list[nodes.FunctionCall] = field(default_factory=list)
    grouped_columns: set[int] = field(default_factory=set)

    @property
    def grouped(self) -> bool:
        return bool(self.aggregates) or bool(self.group_by)

    @property
    def summary(self) -> str:
        return ", ".join(source.describe() for source in self.sources)

    def describe(self) -> list[str]:
        lines = [source.describe() for source in self.sources]
        if self.where is not None:
            lines.append(f"filter {self.where}")
        if self.group_by:
            lines.append(
                "group by " + ", ".join(str(key) for key in self.group_by)
            )
        elif self.aggregates:
            lines.append("aggregate over all rows")
        if self.having is not None:
            lines.append(f"having {self.having}")
        lines.append("project " + ", ".join(self.output))
        if self.distinct:
            lines.append("distinct")
        if self.order_by:
            lines.append(
                "sort by " + ", ".join(str(key) for key in self.order_by)
            )
        if self.limit is not None or self.offset is not None:
            lines.append(f"limit {self.limit} offset {self.offset or 0}")
        return lines


def build_plan(
    select: nodes.Select, open_table: Callable[[str], Table]
) -> QueryPlan:
    binding = Binding()
    references = _flatten_sources(select.source)
    sources: list[SourcePlan] = []
    for reference, kind, condition in references:
        table = open_table(reference.name)
        position = binding.add_source(reference.label, table)
        sources.append(
            SourcePlan(reference.label, table, position, kind, condition)
        )

    where_conjuncts = list(_conjuncts(select.where))
    for source in sources:
        candidates = list(where_conjuncts)
        if source.condition is not None:
            candidates += list(_conjuncts(source.condition))
        _choose_access_path(binding, source, candidates)

    items = _expand_stars(binding, select.items)
    output = _output_labels(binding, items)

    aggregate_calls: list[nodes.FunctionCall] = []
    for expression in [item.value for item in items] + [select.having] + [
        key.value for key in select.order_by if isinstance(key.value, nodes.Expression)
    ]:
        for call in nodes.aggregates_in(expression):
            if call not in aggregate_calls:
                aggregate_calls.append(call)

    plan = QueryPlan(
        binding=binding,
        sources=sources,
        items=items,
        output=output,
        where=select.where,
        group_by=list(select.group_by),
        having=select.having,
        order_by=list(select.order_by),
        limit=select.limit,
        offset=select.offset,
        distinct=select.distinct,
        aggregates=aggregate_calls,
    )
    _validate(plan)
    return plan


def _flatten_sources(
    source: nodes.TableRef | nodes.Join,
) -> list[tuple[nodes.TableRef, str, nodes.Expression | None]]:
    if isinstance(source, nodes.TableRef):
        return [(source, "INNER", None)]
    return _flatten_sources(source.left) + [
        (source.right, source.kind, source.condition)
    ]


def _expand_stars(
    binding: Binding, items: list[nodes.SelectItem]
) -> list[nodes.SelectItem]:
    expanded: list[nodes.SelectItem] = []
    for item in items:
        if not isinstance(item.value, nodes.Star):
            expanded.append(item)
            continue
        star = item.value
        if star.qualifier is not None and star.qualifier not in binding.labels:
            raise PlanError(
                f"no table {star.qualifier!r} in this query; it has {binding.labels}"
            )
        for column in binding.columns:
            if star.qualifier is not None and column.label != star.qualifier:
                continue
            expanded.append(
                nodes.SelectItem(
                    nodes.ColumnRef(column.name, column.label),
                    alias=binding.output_label(column),
                )
            )
    if not expanded:
        raise PlanError("the select list is empty")
    return expanded


def _output_labels(binding: Binding, items: list[nodes.SelectItem]) -> list[str]:
    labels = [item.label() for item in items]
    duplicated = {label for label in labels if labels.count(label) > 1}
    if not duplicated:
        return labels
    for position, (item, label) in enumerate(zip(items, labels)):
        if label not in duplicated or item.alias is not None:
            continue
        if isinstance(item.value, nodes.ColumnRef):
            column = binding.resolve(item.value)
            labels[position] = f"{column.label}.{column.name}"
    return labels


def _validate(plan: QueryPlan) -> None:
    for expression in [item.value for item in plan.items]:
        for node in nodes.walk(expression):
            if isinstance(node, nodes.ColumnRef):
                plan.binding.resolve(node)
    for key in plan.group_by:
        for node in nodes.walk(key):
            if isinstance(node, nodes.ColumnRef):
                plan.binding.resolve(node)
    if plan.having is not None and not plan.grouped:
        raise PlanError("HAVING needs GROUP BY or an aggregate")

    if not plan.grouped:
        return
    plan.grouped_columns = {
        plan.binding.resolve(node).index
        for key in plan.group_by
        for node in nodes.walk(key)
        if isinstance(node, nodes.ColumnRef)
    }
    for item in plan.items:
        for node in _outside_aggregates(item.value):
            if isinstance(node, nodes.ColumnRef):
                if plan.binding.resolve(node).index not in plan.grouped_columns:
                    raise PlanError(
                        f"{node} must appear in GROUP BY or inside an aggregate: "
                        f"with grouping it has no single value per group"
                    )


def _outside_aggregates(expression) -> Iterator[nodes.Expression]:
    if expression is None or isinstance(expression, nodes.Star):
        return
    if isinstance(expression, nodes.FunctionCall):
        return
    yield expression
    for child in (
        getattr(expression, "left", None),
        getattr(expression, "right", None),
        getattr(expression, "operand", None),
    ):
        if isinstance(child, nodes.Expression):
            yield from _outside_aggregates(child)


def _choose_access_path(
    binding: Binding, source: SourcePlan, candidates: list[nodes.Expression]
) -> None:
    best_score = 0
    for condition in candidates:
        if not isinstance(condition, nodes.Compare):
            continue
        column, other, operator = _orient(binding, condition, source.position)
        if column is None:
            continue
        indexes = source.table.indexes_on(column.index - binding.offset_of(source.position))
        if not indexes:
            continue
        index = indexes[0]
        dependencies = binding.positions_used(other)

        if isinstance(other, nodes.Literal):
            if other.value is None:
                continue
            try:
                index.value_key(other.value)
            except Exception:
                continue
            if operator == "=":
                score = 5 if index.unique else 4
                if score > best_score:
                    best_score = score
                    source.method = "seek"
                    source.index = index
                    source.value = other.value
                    source.reason = f" using {index.name} ({column.name} = {other})"
            elif operator in (">", ">=", "<", "<="):
                if best_score < 2:
                    best_score = 2
                    source.method = "range"
                    source.index = index
                    source.low = source.high = None
                    if operator in (">", ">="):
                        source.low = index.bound_above(other.value, operator == ">=")
                    else:
                        source.high = index.bound_below(other.value, operator == "<=")
                    source.reason = (
                        f" using {index.name} ({column.name} {operator} {other})"
                    )
            continue

        if operator == "=" and dependencies and max(dependencies) < source.position:
            score = 5 if index.unique else 4
            if score > best_score:
                best_score = score
                source.method = "probe"
                source.index = index
                source.probe = other
                source.reason = f" using {index.name} ({column.name} = {other})"


def _orient(
    binding: Binding, condition: nodes.Compare, position: int
) -> tuple[BoundColumn | None, nodes.Expression, str]:
    flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
    for left, right, operator in (
        (condition.left, condition.right, condition.operator),
        (condition.right, condition.left, flipped.get(condition.operator, condition.operator)),
    ):
        if not isinstance(left, nodes.ColumnRef):
            continue
        try:
            column = binding.resolve(left)
        except PlanError:
            return None, condition.right, condition.operator
        if column.position != position:
            continue
        if position in binding.positions_used(right):
            continue
        return column, right, operator
    return None, condition.right, condition.operator


def run(plan: QueryPlan) -> Iterator[tuple]:
    rows = _join(plan, (), 0)
    rows = (values for values in rows if _keeps(plan.where, plan.binding, values))
    emitted = _group(plan, rows) if plan.grouped else _project_each(plan, rows)
    if plan.distinct:
        emitted = _distinct(emitted)
    if plan.order_by:
        emitted = _sort(plan, emitted)
    return _limit(plan, emitted)


def _join(plan: QueryPlan, prefix: tuple, position: int) -> Iterator[tuple]:
    source = plan.sources[position]
    last = position + 1 == len(plan.sources)
    matched = False
    for _rid, values in _source_rows(plan, source, prefix):
        combined = prefix + values
        if source.condition is not None and not _keeps(
            source.condition, plan.binding, combined
        ):
            continue
        matched = True
        if last:
            yield combined
        else:
            yield from _join(plan, combined, position + 1)
    if not matched and source.join_kind == "LEFT":
        combined = prefix + (None,) * len(source.table.schema)
        if last:
            yield combined
        else:
            yield from _join(plan, combined, position + 1)


def _source_rows(
    plan: QueryPlan, source: SourcePlan, prefix: tuple
) -> Iterator[tuple[RowId, tuple]]:
    table = source.table
    if source.method == "seek":
        return table.rows_for(source.index.seek(source.value))
    if source.method == "range":
        return table.rows_for(source.index.scan(source.low, source.high))
    if source.method == "probe":
        value = value_of(source.probe, plan.binding, prefix)
        if value is None:
            return iter(())
        try:
            return table.rows_for(source.index.seek(value))
        except Exception as error:
            raise ValueTypeError(
                f"cannot probe {source.label}.{source.index.column_name} with "
                f"{value!r}: {error}"
            ) from None
    return table.scan()


def _keeps(
    condition: nodes.Expression | None, binding: Binding, values: tuple
) -> bool:
    return condition is None or evaluate(condition, binding, values) is True


@dataclass
class Emitted:
    row: tuple
    values: tuple
    aggregates: dict


def _project_each(plan: QueryPlan, rows: Iterable[tuple]) -> Iterator[Emitted]:
    for values in rows:
        row = tuple(
            value_of(item.value, plan.binding, values) for item in plan.items
        )
        yield Emitted(row, values, {})


def _group(plan: QueryPlan, rows: Iterable[tuple]) -> Iterator[Emitted]:
    groups: dict[tuple, tuple[tuple, list]] = {}
    for values in rows:
        key = tuple(
            value_of(expression, plan.binding, values)
            for expression in plan.group_by
        )
        if key not in groups:
            groups[key] = (
                values,
                [_Accumulator(call, plan.binding) for call in plan.aggregates],
            )
        for accumulator in groups[key][1]:
            accumulator.add(values)

    if not groups and not plan.group_by:
        groups[()] = (
            (None,) * plan.binding.width,
            [_Accumulator(call, plan.binding) for call in plan.aggregates],
        )

    for values, accumulators in groups.values():
        computed = {
            accumulator.call: accumulator.result() for accumulator in accumulators
        }
        if plan.having is not None:
            if evaluate(plan.having, plan.binding, values, computed) is not True:
                continue
        row = tuple(
            value_of(item.value, plan.binding, values, computed)
            for item in plan.items
        )
        yield Emitted(row, values, computed)


class _Accumulator:
    def __init__(self, call: nodes.FunctionCall, binding: Binding) -> None:
        self.call = call
        self.binding = binding
        self.count = 0
        self.total = 0
        self.extreme: object = None
        self.seen: set = set() if call.distinct else set()

    def add(self, values: tuple) -> None:
        if self.call.is_count_star:
            self.count += 1
            return
        value = value_of(self.call.argument, self.binding, values)
        if value is None:
            return
        if self.call.distinct:
            if value in self.seen:
                return
            self.seen.add(value)
        self.count += 1
        name = self.call.name
        if name in ("SUM", "AVG"):
            if isinstance(value, str):
                raise ValueTypeError(f"{name} needs numbers, got TEXT {value!r}")
            self.total += value
        elif name == "MIN":
            if self.extreme is None or value < self.extreme:
                self.extreme = value
        elif name == "MAX":
            if self.extreme is None or value > self.extreme:
                self.extreme = value

    def result(self) -> object:
        name = self.call.name
        if name == "COUNT":
            return self.count
        if self.count == 0:
            return None
        if name == "SUM":
            return self.total
        if name == "AVG":
            return self.total / self.count
        return self.extreme


def _distinct(emitted: Iterable[Emitted]) -> Iterator[Emitted]:
    seen: set[tuple] = set()
    for item in emitted:
        if item.row in seen:
            continue
        seen.add(item.row)
        yield item


def _sort(plan: QueryPlan, emitted: Iterable[Emitted]) -> list[Emitted]:
    materialised = list(emitted)
    for key in reversed(plan.order_by):
        extract = _sort_extractor(plan, key)
        materialised.sort(key=extract, reverse=key.descending)
    return materialised


def _sort_extractor(plan: QueryPlan, key: nodes.OrderBy) -> Callable[[Emitted], tuple]:
    if isinstance(key.value, int):
        position = key.value
        if not 1 <= position <= len(plan.output):
            raise PlanError(
                f"ORDER BY {position} is out of range: there are "
                f"{len(plan.output)} output column(s)"
            )
        return lambda item, i=position - 1: _sort_key(item.row[i])

    expression = key.value

    for candidate in (
        expression.name if isinstance(expression, nodes.ColumnRef) else None,
        str(expression),
    ):
        if candidate is not None and candidate in plan.output:
            position = plan.output.index(candidate)
            return lambda item, i=position: _sort_key(item.row[i])

    if isinstance(expression, nodes.ColumnRef):
        try:
            target = plan.binding.resolve(expression).index
        except PlanError:
            target = None
        if target is not None:
            for position, item in enumerate(plan.items):
                if isinstance(item.value, nodes.ColumnRef):
                    if plan.binding.resolve(item.value).index == target:
                        return lambda item, i=position: _sort_key(item.row[i])

    if plan.grouped:
        for node in _outside_aggregates(expression):
            if isinstance(node, nodes.ColumnRef):
                if plan.binding.resolve(node).index not in plan.grouped_columns:
                    raise PlanError(
                        f"ORDER BY {expression} is not available here: with "
                        f"grouping, sort by an output column, a grouping key, or "
                        f"an aggregate"
                    )
    return lambda item: _sort_key(
        value_of(expression, plan.binding, item.values, item.aggregates)
    )


def _limit(plan: QueryPlan, emitted: Iterable[Emitted]) -> Iterator[tuple]:
    remaining = plan.limit
    skip = plan.offset or 0
    for item in emitted:
        if skip > 0:
            skip -= 1
            continue
        if remaining is not None:
            if remaining == 0:
                return
            remaining -= 1
        yield item.row


def evaluate(
    expression: nodes.Expression,
    binding: Binding,
    values: tuple,
    aggregates: dict | None = None,
) -> bool | None:
    if isinstance(expression, nodes.And):
        left = evaluate(expression.left, binding, values, aggregates)
        right = evaluate(expression.right, binding, values, aggregates)
        if left is False or right is False:
            return False
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return True

    if isinstance(expression, nodes.Or):
        left = evaluate(expression.left, binding, values, aggregates)
        right = evaluate(expression.right, binding, values, aggregates)
        if left is True or right is True:
            return True
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return False

    if isinstance(expression, nodes.Not):
        inner = evaluate(expression.operand, binding, values, aggregates)
        return UNKNOWN if inner is UNKNOWN else not inner

    if isinstance(expression, nodes.IsNull):
        is_null = value_of(expression.operand, binding, values, aggregates) is None
        return is_null != expression.negated

    if isinstance(expression, nodes.Compare):
        left = value_of(expression.left, binding, values, aggregates)
        right = value_of(expression.right, binding, values, aggregates)
        if left is None or right is None:
            return UNKNOWN
        return _compare(expression.operator, left, right, expression)

    value = value_of(expression, binding, values, aggregates)
    return UNKNOWN if value is None else bool(value)


def value_of(
    expression: nodes.Expression,
    binding: Binding,
    values: tuple,
    aggregates: dict | None = None,
) -> object:
    if isinstance(expression, nodes.Literal):
        return expression.value
    if isinstance(expression, nodes.ColumnRef):
        return values[binding.resolve(expression).index]
    if isinstance(expression, nodes.FunctionCall):
        if aggregates is None or expression not in aggregates:
            raise PlanError(
                f"{expression} can only be used where aggregates are computed: "
                f"the select list, HAVING, or ORDER BY"
            )
        return aggregates[expression]
    if isinstance(
        expression, (nodes.Compare, nodes.And, nodes.Or, nodes.Not, nodes.IsNull)
    ):
        truth = evaluate(expression, binding, values, aggregates)
        return None if truth is UNKNOWN else int(truth)
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
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    return left >= right


def _conjuncts(expression: nodes.Expression | None) -> Iterator[nodes.Expression]:
    if expression is None:
        return
    if isinstance(expression, nodes.And):
        yield from _conjuncts(expression.left)
        yield from _conjuncts(expression.right)
    else:
        yield expression


def _sort_key(value: object) -> tuple:
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
    if value is None:
        return None
    if column_type is ColumnType.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeError(f"{where} expects INT, got {_type_name(value)}")
        return value
    if not isinstance(value, str):
        raise ValueTypeError(f"{where} expects TEXT, got {_type_name(value)}")
    return value


def single_source_binding(label: str, table: Table) -> Binding:
    binding = Binding()
    binding.add_source(label, table)
    return binding


def single_source_plan(
    binding: Binding, table: Table, where: nodes.Expression | None
) -> SourcePlan:
    source = SourcePlan(table.name, table, 0)
    _choose_access_path(binding, source, list(_conjuncts(where)))
    return source


def scan_single(
    binding: Binding, source: SourcePlan, where: nodes.Expression | None
) -> Iterator[tuple[RowId, tuple]]:
    table = source.table
    if source.method == "seek":
        rows = table.rows_for(source.index.seek(source.value))
    elif source.method == "range":
        rows = table.rows_for(source.index.scan(source.low, source.high))
    else:
        rows = table.scan()
    for rid, values in rows:
        if _keeps(where, binding, values):
            yield rid, values
