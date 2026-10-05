from __future__ import annotations

from dataclasses import dataclass, field

from pydb.btree import DuplicateKeyError
from pydb.catalog import (
    Catalog,
    IndexExistsError,
    Table,
    TableExistsError,
    UnknownIndexError,
    UnknownTableError,
)
from pydb.database import Database
from pydb.record import Schema
from pydb.sql import nodes, planner
from pydb.sql.errors import ParseError, PlanError, SqlError, ValueTypeError
from pydb.sql.parser import parse, parse_script

__all__ = [
    "Engine",
    "Result",
    "SqlError",
    "ParseError",
    "PlanError",
    "ValueTypeError",
    "DuplicateKeyError",
]


@dataclass
class Result:
    columns: tuple[str, ...] = ()
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    message: str = ""
    plan: str = ""

    @property
    def is_query(self) -> bool:
        return bool(self.columns)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __str__(self) -> str:
        if not self.is_query:
            return self.message
        return f"{len(self.rows)} row{'' if len(self.rows) == 1 else 's'}"


class Engine:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.catalog = Catalog(db)

    def __repr__(self) -> str:
        return f"<Engine on {self.db.path!r}>"

    def execute(self, sql: str) -> Result:
        return self.run(parse(sql))

    def execute_script(self, sql: str) -> list[Result]:
        return [self.run(statement) for statement in parse_script(sql)]

    def run(self, statement: nodes.Statement) -> Result:
        if isinstance(statement, nodes.Begin):
            self.db.begin()
            return Result(message="BEGIN")
        if isinstance(statement, nodes.Commit):
            self.db.commit()
            return Result(message="COMMIT")
        if isinstance(statement, nodes.Rollback):
            self.db.rollback()
            return Result(message="ROLLBACK")
        if isinstance(statement, nodes.Explain):
            return self._explain(statement.statement)

        handlers = {
            nodes.CreateTable: self._create_table,
            nodes.DropTable: self._drop_table,
            nodes.CreateIndex: self._create_index,
            nodes.DropIndex: self._drop_index,
            nodes.Insert: self._insert,
            nodes.Select: self._select,
            nodes.Delete: self._delete,
            nodes.Update: self._update,
            nodes.Vacuum: self._vacuum,
        }
        handler = handlers.get(type(statement))
        if handler is None:
            raise PlanError(f"cannot run {type(statement).__name__}")
        with self.db.autocommit():
            return handler(statement)

    def _explain(self, statement: nodes.Statement) -> Result:
        if isinstance(statement, nodes.Select):
            lines = planner.build_plan(statement, self._table).describe()
        elif isinstance(statement, (nodes.Delete, nodes.Update)):
            table = self._table(statement.table)
            binding = planner.single_source_binding(table.name, table)
            source = planner.single_source_plan(binding, table, statement.where)
            verb = "delete from" if isinstance(statement, nodes.Delete) else "update"
            lines = [source.describe(), f"{verb} {table.name}"]
        else:
            lines = [f"{type(statement).__name__}: nothing to plan"]
        return Result(columns=("plan",), rows=[(line,) for line in lines])

    def _create_table(self, statement: nodes.CreateTable) -> Result:
        if statement.name in self.catalog:
            if statement.if_not_exists:
                return Result(message=f"table {statement.name} already exists")
            raise TableExistsError(f"table {statement.name} already exists")
        self.catalog.create_table(
            statement.name, Schema(statement.columns), statement.primary_key
        )
        return Result(message=f"CREATE TABLE {statement.name}")

    def _drop_table(self, statement: nodes.DropTable) -> Result:
        if statement.name not in self.catalog:
            if statement.if_exists:
                return Result(message=f"no table {statement.name}")
            raise UnknownTableError(f"no such table: {statement.name}")
        self.catalog.drop_table(statement.name)
        return Result(message=f"DROP TABLE {statement.name}")

    def _create_index(self, statement: nodes.CreateIndex) -> Result:
        if self.catalog.has_index(statement.name):
            if statement.if_not_exists:
                return Result(message=f"index {statement.name} already exists")
            raise IndexExistsError(f"index {statement.name} already exists")
        index = self.catalog.create_index(
            statement.name, statement.table, statement.column, statement.unique
        )
        return Result(
            message=f"CREATE INDEX {index.name} ON "
            f"{statement.table}({index.column_name})"
        )

    def _drop_index(self, statement: nodes.DropIndex) -> Result:
        if not self.catalog.has_index(statement.name):
            if statement.if_exists:
                return Result(message=f"no index {statement.name}")
            raise UnknownIndexError(f"no such index: {statement.name}")
        self.catalog.drop_index(statement.name)
        return Result(message=f"DROP INDEX {statement.name}")

    def _vacuum(self, _statement: nodes.Vacuum) -> Result:
        reclaimed = 0
        for name in self.catalog.table_names():
            reclaimed += self.catalog.open(name).compact()
        return Result(message=f"VACUUM reclaimed {reclaimed} bytes")

    def _insert(self, statement: nodes.Insert) -> Result:
        table = self._table(statement.table)
        schema = table.schema
        if statement.columns is None:
            order = list(range(len(schema)))
        else:
            order = [self._column(table, name) for name in statement.columns]
            missing = [
                column.name
                for index, column in enumerate(schema)
                if index not in order and not column.nullable
            ]
            if missing:
                raise PlanError(
                    f"{table.name}: no value given for NOT NULL column(s) {missing}"
                )

        inserted = 0
        for row_expressions in statement.rows:
            if len(row_expressions) != len(order):
                raise PlanError(
                    f"{table.name}: expected {len(order)} value(s), got "
                    f"{len(row_expressions)}"
                )
            values: list[object] = [None] * len(schema)
            for index, expression in zip(order, row_expressions):
                column = schema.columns[index]
                if not isinstance(expression, nodes.Literal):
                    raise PlanError(
                        f"only literal values can be inserted, got {expression}"
                    )
                values[index] = planner.coerce_value(
                    column.type, expression.value, f"{table.name}.{column.name}"
                )
            table.insert(tuple(values))
            inserted += 1
        return Result(row_count=inserted, message=f"INSERT {inserted}")

    def _select(self, statement: nodes.Select) -> Result:
        plan = planner.build_plan(statement, self._table)
        return Result(
            columns=tuple(plan.output),
            rows=list(planner.run(plan)),
            plan=plan.summary,
        )

    def _delete(self, statement: nodes.Delete) -> Result:
        table = self._table(statement.table)
        binding = planner.single_source_binding(table.name, table)
        source = planner.single_source_plan(binding, table, statement.where)
        doomed = list(planner.scan_single(binding, source, statement.where))
        for rid, values in doomed:
            table.delete(rid, values)
        return Result(
            row_count=len(doomed),
            message=f"DELETE {len(doomed)}",
            plan=source.describe(),
        )

    def _update(self, statement: nodes.Update) -> Result:
        table = self._table(statement.table)
        schema = table.schema
        assignments = []
        for name, expression in statement.assignments:
            index = self._column(table, name)
            if not isinstance(expression, nodes.Literal):
                raise PlanError(
                    f"only literal values can be assigned, got {expression}"
                )
            value = planner.coerce_value(
                schema.columns[index].type, expression.value, f"{table.name}.{name}"
            )
            if value is None and not schema.columns[index].nullable:
                raise PlanError(f"{table.name}.{name} is NOT NULL")
            assignments.append((index, value))

        binding = planner.single_source_binding(table.name, table)
        source = planner.single_source_plan(binding, table, statement.where)
        targets = list(planner.scan_single(binding, source, statement.where))
        for rid, values in targets:
            updated = list(values)
            for index, value in assignments:
                updated[index] = value
            table.update(rid, values, tuple(updated))
        return Result(
            row_count=len(targets),
            message=f"UPDATE {len(targets)}",
            plan=source.describe(),
        )

    def _table(self, name: str) -> Table:
        if name not in self.catalog:
            known = self.catalog.table_names()
            hint = f"; this database has {known}" if known else "; it has no tables"
            raise UnknownTableError(f"no such table: {name}{hint}")
        return self.catalog.open(name)

    def _column(self, table: Table, name: str) -> int:
        if not table.schema.has(name):
            raise PlanError(
                f"no column {name!r} in {table.name}; it has "
                f"{list(table.schema.names)}"
            )
        return table.schema.index(name)
