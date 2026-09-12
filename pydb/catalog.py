"""Layer 7a: the catalog, and the tables it describes.

A database has to be able to describe itself. `CREATE TABLE people (id INT, name
TEXT)` has to survive a restart, which means the schema is data like any other
data -- stored in the database, in a B+Tree keyed by table name, whose root lives
in the meta page slot layer 5 added for exactly this.

Two classes:

* `TableInfo` is the durable description: columns, where the row heap starts, and
  where the primary key index is rooted. It encodes to bytes and back.
* `Table` binds one of those to the storage layers and does the row work, keeping
  the heap and the index in step. That pairing is the whole reason it exists: an
  insert that adds a row to the heap but not the index leaves a table that answers
  the same query two different ways depending on the plan.

The primary key index is where layer 4 earns its keep: `WHERE id = 42` on an
indexed column is three page reads instead of a scan of the entire table.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

from pydb.btree import BTree, DuplicateKeyError
from pydb.database import Database
from pydb.heap import HeapFile, RowId
from pydb.pager import META_SLOT_ROOT, NULL_PAGE_ID
from pydb.record import Column, ColumnType, Schema, encode_key

# first_page_id, index_root, primary key column (-1 for none), column count
TABLE_HEADER_FORMAT = ">IIhH"
TABLE_HEADER_SIZE = struct.calcsize(TABLE_HEADER_FORMAT)

# A row id as stored in an index: page id and slot.
ROW_ID_FORMAT = ">IH"
ROW_ID_SIZE = struct.calcsize(ROW_ID_FORMAT)


class CatalogError(Exception):
    """Base class for catalog errors."""


class UnknownTableError(CatalogError):
    """No such table."""


class TableExistsError(CatalogError):
    """A table of that name is already there."""


def pack_row_id(rid: RowId) -> bytes:
    return struct.pack(ROW_ID_FORMAT, rid.page_id, rid.slot)


def unpack_row_id(raw: bytes) -> RowId:
    return RowId(*struct.unpack(ROW_ID_FORMAT, raw))


@dataclass
class TableInfo:
    """Everything about a table that has to outlive the process."""

    name: str
    schema: Schema
    first_page_id: int
    primary_key: int | None = None  # column index, not name
    index_root: int = NULL_PAGE_ID

    @property
    def primary_key_name(self) -> str | None:
        if self.primary_key is None:
            return None
        return self.schema.columns[self.primary_key].name

    def encode(self) -> bytes:
        parts = [
            struct.pack(
                TABLE_HEADER_FORMAT,
                self.first_page_id,
                self.index_root,
                -1 if self.primary_key is None else self.primary_key,
                len(self.schema),
            )
        ]
        for column in self.schema:
            name = column.name.encode("utf-8")
            parts.append(struct.pack(">H", len(name)))
            parts.append(name)
            parts.append(struct.pack(">BB", int(column.type), int(column.nullable)))
        return b"".join(parts)

    @classmethod
    def decode(cls, name: str, raw: bytes) -> "TableInfo":
        first_page, index_root, primary_key, count = struct.unpack_from(
            TABLE_HEADER_FORMAT, raw, 0
        )
        offset = TABLE_HEADER_SIZE
        columns = []
        for _ in range(count):
            (length,) = struct.unpack_from(">H", raw, offset)
            offset += 2
            column_name = str(raw[offset : offset + length], "utf-8")
            offset += length
            type_id, nullable = struct.unpack_from(">BB", raw, offset)
            offset += 2
            columns.append(
                Column(column_name, ColumnType(type_id), bool(nullable))
            )
        return cls(
            name=name,
            schema=Schema(columns),
            first_page_id=first_page,
            primary_key=None if primary_key < 0 else primary_key,
            index_root=index_root,
        )


class Table:
    """A table's rows, with its primary key index kept in step.

    Every mutating method here does two things -- the heap and the index -- and the
    reason they live behind one method rather than being called separately is that
    doing only one of them produces a table that gives different answers to the
    same question depending on which plan the query ends up using.
    """

    def __init__(self, catalog: "Catalog", info: TableInfo) -> None:
        self.catalog = catalog
        self.info = info
        self.heap = HeapFile(catalog.db.pool, info.schema, info.first_page_id)
        self.index: BTree | None = None
        if info.primary_key is not None:
            self.index = BTree(
                catalog.db.pool,
                info.index_root,
                on_root_change=lambda page_id: catalog.set_index_root(
                    info.name, page_id
                ),
            )

    def __repr__(self) -> str:
        return f"<Table {self.name!r} {len(self.schema)} columns>"

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def schema(self) -> Schema:
        return self.info.schema

    def reload(self) -> None:
        """Re-read the cached page ids after a rollback may have moved them."""
        self.info = self.catalog.get(self.name)
        self.heap.reload()
        if self.index is not None:
            self.index.root_page_id = self.info.index_root

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------

    def key_of(self, values: tuple) -> bytes:
        """The index key for a row. Only meaningful with a primary key."""
        column = self.schema.columns[self.info.primary_key]
        return encode_key(column.type, values[self.info.primary_key])

    def insert(self, values: tuple) -> RowId:
        """Add a row, refusing a duplicate primary key."""
        self.schema.encode(values)  # validate before touching anything
        if self.index is not None:
            key = self.key_of(values)
            if self.index.get(key) is not None:
                raise DuplicateKeyError(
                    f"{self.name}: duplicate value for primary key "
                    f"{self.info.primary_key_name!r}: "
                    f"{values[self.info.primary_key]!r}"
                )
            rid = self.heap.insert(values)
            self.index.put(key, pack_row_id(rid))
            return rid
        return self.heap.insert(values)

    def delete(self, rid: RowId, values: tuple) -> None:
        """Remove a row. `values` is needed to find its index entry."""
        self.heap.delete(rid)
        if self.index is not None:
            self.index.delete(self.key_of(values))

    def update(self, rid: RowId, old: tuple, new: tuple) -> RowId:
        """Replace a row's values, returning where it ended up.

        The heap may move the row, and the primary key may itself have changed, so
        the index entry is rewritten rather than patched.
        """
        self.schema.encode(new)
        if self.index is not None:
            old_key, new_key = self.key_of(old), self.key_of(new)
            if new_key != old_key and self.index.get(new_key) is not None:
                raise DuplicateKeyError(
                    f"{self.name}: duplicate value for primary key "
                    f"{self.info.primary_key_name!r}: "
                    f"{new[self.info.primary_key]!r}"
                )
            new_rid = self.heap.update(rid, new)
            if new_key != old_key:
                self.index.delete(old_key)
            self.index.put(new_key, pack_row_id(new_rid))
            return new_rid
        return self.heap.update(rid, new)

    def scan(self) -> Iterator[tuple[RowId, tuple]]:
        """Every row, in whatever order the heap holds them."""
        return self.heap.scan()

    def lookup(self, value: object) -> tuple[RowId, tuple] | None:
        """One row by primary key, through the index. None if there is no such row."""
        if self.index is None:
            raise CatalogError(f"{self.name} has no primary key to look up by")
        column = self.schema.columns[self.info.primary_key]
        raw = self.index.get(encode_key(column.type, value))
        if raw is None:
            return None
        rid = unpack_row_id(raw)
        return rid, self.heap.get(rid)

    def index_range(
        self, low: bytes | None, high: bytes | None
    ) -> Iterator[tuple[RowId, tuple]]:
        """Rows whose key is in `[low, high)`, in key order, through the index."""
        if self.index is None:
            raise CatalogError(f"{self.name} has no primary key to range over")
        for _key, raw in self.index.items(low, high):
            rid = unpack_row_id(raw)
            yield rid, self.heap.get(rid)

    def verify(self) -> None:
        """Check the heap, the index, and that they agree about every row."""
        self.heap.verify()
        if self.index is None:
            return
        self.index.verify_invariants()
        rows = {rid: values for rid, values in self.heap.scan()}
        entries = {
            unpack_row_id(raw): key for key, raw in self.index.items()
        }
        if set(rows) != set(entries):
            raise CatalogError(
                f"{self.name}: the index and the heap disagree; "
                f"{len(set(rows) - set(entries))} rows are missing from the index "
                f"and {len(set(entries) - set(rows))} index entries point nowhere"
            )
        for rid, values in rows.items():
            if self.key_of(values) != entries[rid]:
                raise CatalogError(
                    f"{self.name}: row {rid} is indexed under the wrong key"
                )


class Catalog:
    """The table of tables, stored in the database it describes.

        >>> with Database("my.db") as db:
        ...     catalog = Catalog(db)
        ...     with db.transaction():
        ...         people = catalog.create_table(
        ...             "people", Schema.of(("id", "INT", False)), primary_key="id"
        ...         )
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tree = db.open_tree(META_SLOT_ROOT)
        self._tables: dict[str, Table] = {}  # open tables, by name
        db.add_rollback_hook(self._reload_tables)

    def __repr__(self) -> str:
        return f"<Catalog {len(self.table_names())} tables>"

    def __contains__(self, name: str) -> bool:
        return self.tree.get(_catalog_key(name)) is not None

    # ------------------------------------------------------------------
    # definitions
    # ------------------------------------------------------------------

    def table_names(self) -> list[str]:
        """Every table name, in sorted order -- which is how the tree stores them."""
        return [str(key, "utf-8") for key in self.tree.keys()]

    def info(self, name: str) -> TableInfo:
        raw = self.tree.get(_catalog_key(name))
        if raw is None:
            raise UnknownTableError(f"no such table: {name}")
        return TableInfo.decode(name, raw)

    get = info  # `Table.reload` reads better as catalog.get(name)

    def open(self, name: str) -> Table:
        """The `Table` for `name`, cached so one object per table per database."""
        if name not in self._tables:
            self._tables[name] = Table(self, self.info(name))
        return self._tables[name]

    def create_table(
        self, name: str, schema: Schema, primary_key: str | None = None
    ) -> Table:
        """Define a table: a heap for its rows, and an index if it has a key."""
        if not name:
            raise CatalogError("a table needs a name")
        if name in self:
            raise TableExistsError(f"table {name} already exists")
        key_index = None
        if primary_key is not None:
            key_index = schema.index(primary_key)  # raises if there is no such column
            if schema.columns[key_index].nullable:
                raise CatalogError(
                    f"primary key {primary_key!r} must be NOT NULL"
                )

        with self.db.autocommit():
            heap = HeapFile.create(self.db.pool, schema)
            info = TableInfo(
                name=name,
                schema=schema,
                first_page_id=heap.first_page_id,
                primary_key=key_index,
            )
            if key_index is not None:
                index = BTree.create(self.db.pool)
                info.index_root = index.root_page_id
            self._write(info)
        return self.open(name)

    def drop_table(self, name: str) -> None:
        """Forget a table. Its pages are leaked, deliberately -- see the comment."""
        info = self.info(name)
        with self.db.autocommit():
            self.tree.delete(_catalog_key(name))
        self._tables.pop(name, None)
        # The heap's pages and the index's pages are not freed. Walking both
        # structures to free every page is straightforward but it is a lot of
        # pages in one transaction, and layer 5 caps a transaction at the size of
        # the buffer pool. Doing it properly means freeing in batches across
        # several transactions, which is a vacuum, not a drop.
        _ = info

    def set_index_root(self, name: str, page_id: int) -> None:
        """Record that a table's index root moved. Called by the tree itself."""
        info = self.info(name)
        info.index_root = page_id
        self._write(info)
        if name in self._tables:
            self._tables[name].info.index_root = page_id

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write(self, info: TableInfo) -> None:
        self.tree.put(_catalog_key(info.name), info.encode())

    def _reload_tables(self) -> None:
        """After a rollback, every cached table's page ids may be stale."""
        for name, table in list(self._tables.items()):
            if name in self:
                table.reload()
            else:
                del self._tables[name]  # the CREATE that made it was rolled back


def _catalog_key(name: str) -> bytes:
    return encode_key(ColumnType.TEXT, name)
