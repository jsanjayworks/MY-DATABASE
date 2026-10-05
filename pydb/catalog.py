"""Layer 7a: the catalog, and the tables and indexes it describes.

A database has to be able to describe itself. `CREATE TABLE people (id INT, name
TEXT)` has to survive a restart, which means the schema is data like any other
data -- stored in the database, in a B+Tree whose root lives in the meta page slot
layer 5 added for exactly this.

Three classes:

* `TableInfo` is the durable description: columns, where the row heap starts, and
  every index on it. It encodes to bytes and back.
* `Index` is one B+Tree over one column, and knows how a value becomes a key.
* `Table` binds those to the storage layers and does the row work, keeping the heap
  and *every* index in step. That last part is the whole reason it exists: an
  insert that adds a row to the heap but not to an index leaves a table that
  answers the same query two different ways depending on the plan.

## How a value becomes an index key

A unique index stores `encode_key(value) -> row id`, and that is the whole story.

A **non-unique** index cannot, because two rows can share a value and a B+Tree
holds each key once. The fix is to append the row id to the key, which makes it
unique again -- but naively that breaks lookups on TEXT: `encode_key("ab")` is a
prefix of `encode_key("abc")`, so a range over "every key starting with ab" would
sweep up the rows for "abc" as well.

So the value part is escaped first: every `00` byte becomes `00 FF`, and the value
is terminated with `00 00`. No escaped value can then be a prefix of another, and
the escaping is order-preserving, because the terminator `00 00` compares below
both `00 FF` and any other byte. That makes a point lookup an exact prefix range
and a `>` bound exact rather than approximate.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

from pydb.btree import BTree, DuplicateKeyError
from pydb.btree_node import max_value_size
from pydb.database import Database
from pydb.errors import PydbError
from pydb.heap import HeapFile, RowId
from pydb.pager import META_SLOT_ROOT, NULL_PAGE_ID
from pydb.record import Column, ColumnType, Schema, encode_key

# first_page_id, column count
TABLE_HEADER_FORMAT = ">IH"
TABLE_HEADER_SIZE = struct.calcsize(TABLE_HEADER_FORMAT)

# A row id as stored in an index: page id and slot.
ROW_ID_FORMAT = ">IH"
ROW_ID_SIZE = struct.calcsize(ROW_ID_FORMAT)

INDEX_FLAG_UNIQUE = 1
INDEX_FLAG_PRIMARY = 2

# Catalog keys are prefixed, because two namespaces share one tree: table
# definitions and the index-name-to-table map.
KEY_TABLE = b"t"
KEY_INDEX = b"i"


class CatalogError(PydbError):
    """Base class for catalog errors."""


class UnknownTableError(CatalogError):
    """No such table."""


class UnknownIndexError(CatalogError):
    """No such index."""


class TableExistsError(CatalogError):
    """A table of that name is already there."""


class IndexExistsError(CatalogError):
    """An index of that name is already there."""


def pack_row_id(rid: RowId) -> bytes:
    return struct.pack(ROW_ID_FORMAT, rid.page_id, rid.slot)


def unpack_row_id(raw: bytes) -> RowId:
    return RowId(*struct.unpack(ROW_ID_FORMAT, raw))


def escape_key(key: bytes) -> bytes:
    """Make `key` unable to be a prefix of another escaped key, order intact."""
    return key.replace(b"\x00", b"\x00\xff") + b"\x00\x00"


def prefix_end(prefix: bytes) -> bytes | None:
    """The first byte string after every string starting with `prefix`.

    None when there is none -- `prefix` is all `FF` bytes, so the range runs to the
    end of the tree.
    """
    data = bytearray(prefix)
    while data:
        if data[-1] != 0xFF:
            data[-1] += 1
            return bytes(data)
        data.pop()
    return None


@dataclass
class IndexInfo:
    """The durable description of one index."""

    name: str
    column: int  # column index within the table's schema
    unique: bool = False
    primary: bool = False
    root: int = NULL_PAGE_ID

    @property
    def flags(self) -> int:
        return (INDEX_FLAG_UNIQUE if self.unique else 0) | (
            INDEX_FLAG_PRIMARY if self.primary else 0
        )


@dataclass
class TableInfo:
    """Everything about a table that has to outlive the process."""

    name: str
    schema: Schema
    first_page_id: int
    indexes: list[IndexInfo] = field(default_factory=list)

    @property
    def primary_index(self) -> IndexInfo | None:
        for index in self.indexes:
            if index.primary:
                return index
        return None

    @property
    def primary_key(self) -> int | None:
        index = self.primary_index
        return None if index is None else index.column

    @property
    def primary_key_name(self) -> str | None:
        key = self.primary_key
        return None if key is None else self.schema.columns[key].name

    def index_named(self, name: str) -> IndexInfo:
        for index in self.indexes:
            if index.name == name:
                return index
        raise UnknownIndexError(f"{self.name} has no index named {name}")

    def indexes_on(self, column: int) -> list[IndexInfo]:
        """Indexes over `column`, unique ones first: they are cheaper to probe."""
        matches = [index for index in self.indexes if index.column == column]
        return sorted(matches, key=lambda index: not index.unique)

    def encode(self) -> bytes:
        parts = [
            struct.pack(TABLE_HEADER_FORMAT, self.first_page_id, len(self.schema))
        ]
        for column in self.schema:
            name = column.name.encode("utf-8")
            parts.append(struct.pack(">H", len(name)))
            parts.append(name)
            parts.append(struct.pack(">BB", int(column.type), int(column.nullable)))
        parts.append(struct.pack(">H", len(self.indexes)))
        for index in self.indexes:
            name = index.name.encode("utf-8")
            parts.append(struct.pack(">H", len(name)))
            parts.append(name)
            parts.append(struct.pack(">HBI", index.column, index.flags, index.root))
        encoded = b"".join(parts)
        limit = max_value_size(_table_key(self.name))
        if len(encoded) > limit:
            raise CatalogError(
                f"{self.name}: its definition is {len(encoded)} bytes and a catalog "
                f"row holds {limit}. Fewer columns, shorter names, or fewer indexes "
                f"-- a real database spreads the definition over several rows"
            )
        return encoded

    @classmethod
    def decode(cls, name: str, raw: bytes) -> "TableInfo":
        first_page, count = struct.unpack_from(TABLE_HEADER_FORMAT, raw, 0)
        offset = TABLE_HEADER_SIZE
        columns = []
        for _ in range(count):
            (length,) = struct.unpack_from(">H", raw, offset)
            offset += 2
            column_name = str(raw[offset : offset + length], "utf-8")
            offset += length
            type_id, nullable = struct.unpack_from(">BB", raw, offset)
            offset += 2
            columns.append(Column(column_name, ColumnType(type_id), bool(nullable)))

        (index_count,) = struct.unpack_from(">H", raw, offset)
        offset += 2
        indexes = []
        for _ in range(index_count):
            (length,) = struct.unpack_from(">H", raw, offset)
            offset += 2
            index_name = str(raw[offset : offset + length], "utf-8")
            offset += length
            column, flags, root = struct.unpack_from(">HBI", raw, offset)
            offset += struct.calcsize(">HBI")
            indexes.append(
                IndexInfo(
                    name=index_name,
                    column=column,
                    unique=bool(flags & INDEX_FLAG_UNIQUE),
                    primary=bool(flags & INDEX_FLAG_PRIMARY),
                    root=root,
                )
            )
        return cls(name, Schema(columns), first_page, indexes)


class Index:
    """One B+Tree over one column of one table.

    Rows whose indexed column is NULL are **not in the index at all**, which is
    safe because no condition an index is used for can be true of NULL: SQL's
    three-valued logic already drops those rows.
    """

    def __init__(self, table: "Table", info: IndexInfo) -> None:
        self.table = table
        self.info = info
        self.tree = BTree(
            table.catalog.db.pool,
            info.root,
            on_root_change=lambda page_id: table.catalog.set_index_root(
                table.name, info.name, page_id
            ),
        )

    def __repr__(self) -> str:
        kind = "unique " if self.unique else ""
        return f"<{kind}index {self.name!r} on {self.table.name}.{self.column_name}>"

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def unique(self) -> bool:
        return self.info.unique

    @property
    def column(self) -> int:
        return self.info.column

    @property
    def column_name(self) -> str:
        return self.table.schema.columns[self.column].name

    @property
    def column_type(self) -> ColumnType:
        return self.table.schema.columns[self.column].type

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------

    def value_key(self, value: object) -> bytes:
        """The ordered byte form of one column value."""
        return encode_key(self.column_type, value)

    def entry_key(self, value: object, rid: RowId) -> bytes:
        """The key this row is stored under."""
        key = self.value_key(value)
        if self.unique:
            return key
        return escape_key(key) + pack_row_id(rid)

    def bounds_for_equal(self, value: object) -> tuple[bytes, bytes | None]:
        key = self.value_key(value)
        if self.unique:
            return key, key + b"\x00"
        escaped = escape_key(key)
        return escaped, prefix_end(escaped)

    def bound_above(self, value: object, inclusive: bool) -> bytes | None:
        """The `start` for `col >= value` (or `> value`)."""
        key = self.value_key(value)
        if self.unique:
            return key if inclusive else key + b"\x00"
        escaped = escape_key(key)
        return escaped if inclusive else prefix_end(escaped)

    def bound_below(self, value: object, inclusive: bool) -> bytes | None:
        """The exclusive `stop` for `col <= value` (or `< value`)."""
        key = self.value_key(value)
        if self.unique:
            return key + b"\x00" if inclusive else key
        escaped = escape_key(key)
        return prefix_end(escaped) if inclusive else escaped

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    def add(self, values: tuple, rid: RowId) -> None:
        value = values[self.column]
        if value is None:
            return  # NULLs are not indexed
        key = self.entry_key(value, rid)
        if self.unique and self.tree.get(key) is not None:
            raise DuplicateKeyError(
                f"{self.table.name}: duplicate value for "
                f"{'primary key' if self.info.primary else 'unique index'} "
                f"{self.column_name!r}: {value!r}"
            )
        self.tree.put(key, pack_row_id(rid))

    def remove(self, values: tuple, rid: RowId) -> None:
        value = values[self.column]
        if value is None:
            return
        self.tree.delete(self.entry_key(value, rid))

    def would_duplicate(self, value: object, rid: RowId) -> bool:
        """Whether adding `value` would collide with a different row."""
        if value is None or not self.unique:
            return False
        existing = self.tree.get(self.value_key(value))
        return existing is not None and unpack_row_id(existing) != rid

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def seek(self, value: object) -> Iterator[RowId]:
        """Row ids whose indexed column equals `value`."""
        if value is None:
            return
        if self.unique:
            raw = self.tree.get(self.value_key(value))
            if raw is not None:
                yield unpack_row_id(raw)
            return
        low, high = self.bounds_for_equal(value)
        for _key, raw in self.tree.items(low, high):
            yield unpack_row_id(raw)

    def scan(self, low: bytes | None, high: bytes | None) -> Iterator[RowId]:
        """Row ids in key order between two already-encoded bounds."""
        for _key, raw in self.tree.items(low, high):
            yield unpack_row_id(raw)

    def entries(self) -> Iterator[tuple[bytes, RowId]]:
        for key, raw in self.tree.items():
            yield key, unpack_row_id(raw)


class Table:
    """A table's rows, with every index kept in step.

    Each mutating method here does the heap *and* the indexes, and the reason they
    live behind one method rather than being called separately is that doing only
    one of them produces a table that gives different answers to the same question
    depending on which plan the query ends up using.
    """

    def __init__(self, catalog: "Catalog", info: TableInfo) -> None:
        self.catalog = catalog
        self.info = info
        self.heap = HeapFile(catalog.db.pool, info.schema, info.first_page_id)
        self.indexes = [Index(self, index) for index in info.indexes]

    def __repr__(self) -> str:
        return (
            f"<Table {self.name!r} {len(self.schema)} columns, "
            f"{len(self.indexes)} index(es)>"
        )

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def schema(self) -> Schema:
        return self.info.schema

    @property
    def primary_index(self) -> Index | None:
        for index in self.indexes:
            if index.info.primary:
                return index
        return None

    def index_named(self, name: str) -> Index:
        for index in self.indexes:
            if index.name == name:
                return index
        raise UnknownIndexError(f"{self.name} has no index named {name}")

    def indexes_on(self, column: int) -> list[Index]:
        """Usable indexes over `column`, unique ones first."""
        matches = [index for index in self.indexes if index.column == column]
        return sorted(matches, key=lambda index: not index.unique)

    def reload(self) -> None:
        """Re-read the cached page ids after a rollback may have moved them."""
        self.info = self.catalog.get(self.name)
        self.heap.reload()
        self.indexes = [Index(self, index) for index in self.info.indexes]

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------

    def insert(self, values: tuple) -> RowId:
        """Add a row to the heap and to every index."""
        self.schema.encode(values)  # validate before touching anything
        for index in self.indexes:
            if index.unique and index.would_duplicate(values[index.column], None):
                raise DuplicateKeyError(
                    f"{self.name}: duplicate value for "
                    f"{'primary key' if index.info.primary else 'unique index'} "
                    f"{index.column_name!r}: {values[index.column]!r}"
                )
        rid = self.heap.insert(values)
        for index in self.indexes:
            index.add(values, rid)
        return rid

    def delete(self, rid: RowId, values: tuple) -> None:
        """Remove a row. `values` is needed to find its index entries."""
        self.heap.delete(rid)
        for index in self.indexes:
            index.remove(values, rid)

    def update(self, rid: RowId, old: tuple, new: tuple) -> RowId:
        """Replace a row's values, returning where it ended up.

        The heap may move the row and an indexed value may itself have changed, so
        every index entry is removed and rewritten rather than patched.
        """
        self.schema.encode(new)
        for index in self.indexes:
            if index.unique and index.would_duplicate(new[index.column], rid):
                raise DuplicateKeyError(
                    f"{self.name}: duplicate value for "
                    f"{'primary key' if index.info.primary else 'unique index'} "
                    f"{index.column_name!r}: {new[index.column]!r}"
                )
        for index in self.indexes:
            index.remove(old, rid)
        new_rid = self.heap.update(rid, new)
        for index in self.indexes:
            index.add(new, new_rid)
        return new_rid

    def scan(self) -> Iterator[tuple[RowId, tuple]]:
        """Every row, in whatever order the heap holds them."""
        return self.heap.scan()

    def get(self, rid: RowId) -> tuple:
        return self.heap.get(rid)

    def rows_for(self, rids: Iterator[RowId]) -> Iterator[tuple[RowId, tuple]]:
        """Fetch rows for row ids coming out of an index."""
        for rid in rids:
            yield rid, self.heap.get(rid)

    def lookup(self, value: object) -> tuple[RowId, tuple] | None:
        """One row by primary key. None if there is no such row."""
        index = self.primary_index
        if index is None:
            raise CatalogError(f"{self.name} has no primary key to look up by")
        for rid in index.seek(value):
            return rid, self.heap.get(rid)
        return None

    # ------------------------------------------------------------------
    # invariants and maintenance
    # ------------------------------------------------------------------

    def verify(self) -> None:
        """Check the heap, every index, and that they all agree about every row."""
        self.heap.verify()
        rows = dict(self.heap.scan())
        for index in self.indexes:
            index.tree.verify_invariants()
            indexed = {rid: key for key, rid in index.entries()}
            expected = {
                rid: index.entry_key(values[index.column], rid)
                for rid, values in rows.items()
                if values[index.column] is not None
            }
            if set(indexed) != set(expected):
                raise CatalogError(
                    f"{self.name}.{index.name}: index and heap disagree; "
                    f"{len(set(expected) - set(indexed))} rows missing from the "
                    f"index, {len(set(indexed) - set(expected))} entries pointing "
                    f"nowhere"
                )
            for rid, key in expected.items():
                if indexed[rid] != key:
                    raise CatalogError(
                        f"{self.name}.{index.name}: row {rid} indexed under the "
                        f"wrong key"
                    )

    def compact(self) -> int:
        """Squeeze the dead space out of every heap page. Returns bytes reclaimed."""
        return self.heap.compact()

    def all_pages(self) -> list[int]:
        """Every page this table owns: its heap chain and all its index pages."""
        pages = list(self.heap.page_ids)
        for index in self.indexes:
            pages.extend(index.tree.all_pages())
        return pages


class Catalog:
    """The table of tables, stored in the database it describes.

        >>> with Database("my.db") as db:
        ...     catalog = Catalog(db)
        ...     with db.transaction():
        ...         people = catalog.create_table(
        ...             "people", Schema.of(("id", "INT", False)), primary_key="id"
        ...         )

    The tree holds two namespaces, distinguished by a one-byte key prefix: table
    definitions under `t`, and an index-name-to-table map under `i` so that
    `DROP INDEX by_name` can find which table to look in.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tree = db.open_tree(META_SLOT_ROOT)
        self._tables: dict[str, Table] = {}  # open tables, by name
        db.add_rollback_hook(self._reload_tables)

    def __repr__(self) -> str:
        return f"<Catalog {len(self.table_names())} tables>"

    def __contains__(self, name: str) -> bool:
        return self.tree.get(_table_key(name)) is not None

    # ------------------------------------------------------------------
    # definitions
    # ------------------------------------------------------------------

    def table_names(self) -> list[str]:
        """Every table name, sorted -- which is the order the tree stores them."""
        return [
            str(key[1:], "utf-8")
            for key in self.tree.keys(KEY_TABLE, _after(KEY_TABLE))
        ]

    def index_names(self) -> list[str]:
        """Every index name in the database, sorted."""
        return [
            str(key[1:], "utf-8")
            for key in self.tree.keys(KEY_INDEX, _after(KEY_INDEX))
        ]

    def info(self, name: str) -> TableInfo:
        raw = self.tree.get(_table_key(name))
        if raw is None:
            raise UnknownTableError(f"no such table: {name}")
        return TableInfo.decode(name, raw)

    get = info  # `Table.reload` reads better as catalog.get(name)

    def open(self, name: str) -> Table:
        """The `Table` for `name`, cached so there is one object per table."""
        if name not in self._tables:
            self._tables[name] = Table(self, self.info(name))
        return self._tables[name]

    def table_of_index(self, index_name: str) -> str:
        raw = self.tree.get(_index_key(index_name))
        if raw is None:
            raise UnknownIndexError(f"no such index: {index_name}")
        return str(raw, "utf-8")

    def has_index(self, index_name: str) -> bool:
        return self.tree.get(_index_key(index_name)) is not None

    def create_table(
        self, name: str, schema: Schema, primary_key: str | None = None
    ) -> Table:
        """Define a table: a heap for its rows, and an index if it has a key."""
        if not name:
            raise CatalogError("a table needs a name")
        if name in self:
            raise TableExistsError(f"table {name} already exists")

        indexes: list[IndexInfo] = []
        with self.db.autocommit():
            heap = HeapFile.create(self.db.pool, schema)
            if primary_key is not None:
                key_index = schema.index(primary_key)  # raises if there is no such column
                if schema.columns[key_index].nullable:
                    raise CatalogError(f"primary key {primary_key!r} must be NOT NULL")
                index_name = f"{name}_pkey"
                if self.has_index(index_name):
                    raise IndexExistsError(
                        f"cannot name the primary key index {index_name}: taken"
                    )
                tree = BTree.create(self.db.pool)
                indexes.append(
                    IndexInfo(index_name, key_index, unique=True, primary=True,
                              root=tree.root_page_id)
                )
                self.tree.put(_index_key(index_name), name.encode("utf-8"))
            info = TableInfo(name, schema, heap.first_page_id, indexes)
            self._write(info)
        return self.open(name)

    def create_index(
        self, index_name: str, table_name: str, column: str, unique: bool = False
    ) -> Index:
        """Build an index over an existing table, filling it from the rows there."""
        if self.has_index(index_name):
            raise IndexExistsError(f"index {index_name} already exists")
        table = self.open(table_name)
        column_index = table.schema.index(column)  # raises if there is no such column

        with self.db.autocommit():
            tree = BTree.create(self.db.pool)
            info = IndexInfo(index_name, column_index, unique=unique,
                             root=tree.root_page_id)
            table.info.indexes.append(info)
            self._write(table.info)
            self.tree.put(_index_key(index_name), table_name.encode("utf-8"))
            # Attach it, then fill it from the rows already in the heap. Building
            # an index is not a special case: it is inserting every existing row
            # into it.
            index = Index(table, info)
            table.indexes.append(index)
            for rid, values in table.heap.scan():
                index.add(values, rid)
        return index

    def drop_index(self, index_name: str) -> None:
        """Remove an index and free its pages. A primary key cannot be dropped."""
        table_name = self.table_of_index(index_name)
        table = self.open(table_name)
        index = table.index_named(index_name)
        if index.info.primary:
            raise CatalogError(
                f"{index_name} is the primary key of {table_name}; drop the table"
            )
        pages = index.tree.all_pages()
        with self.db.autocommit():
            table.info.indexes = [
                info for info in table.info.indexes if info.name != index_name
            ]
            table.indexes = [i for i in table.indexes if i.name != index_name]
            self._write(table.info)
            self.tree.delete(_index_key(index_name))
        self.db.free_pages(pages)

    def drop_table(self, name: str) -> None:
        """Forget a table and free every page it owned."""
        table = self.open(name)
        pages = table.all_pages()
        index_names = [index.name for index in table.indexes]
        with self.db.autocommit():
            for index_name in index_names:
                self.tree.delete(_index_key(index_name))
            self.tree.delete(_table_key(name))
        self._tables.pop(name, None)
        # Freeing happens after the definition is gone, and in batches, because a
        # large table has far more pages than one transaction may touch. A crash
        # part-way through leaks the rest, which costs space and not correctness.
        self.db.free_pages(pages)

    def set_index_root(self, table_name: str, index_name: str, page_id: int) -> None:
        """Record that an index root moved. Called by the tree itself."""
        info = self.info(table_name)
        info.index_named(index_name).root = page_id
        self._write(info)
        if table_name in self._tables:
            cached = self._tables[table_name]
            for index in cached.info.indexes:
                if index.name == index_name:
                    index.root = page_id

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write(self, info: TableInfo) -> None:
        self.tree.put(_table_key(info.name), info.encode())

    def _reload_tables(self) -> None:
        """After a rollback, every cached table's page ids may be stale."""
        for name, table in list(self._tables.items()):
            if name in self:
                table.reload()
            else:
                del self._tables[name]  # the CREATE that made it was rolled back


def _table_key(name: str) -> bytes:
    return KEY_TABLE + name.encode("utf-8")


def _index_key(name: str) -> bytes:
    return KEY_INDEX + name.encode("utf-8")


def _after(prefix: bytes) -> bytes:
    return prefix_end(prefix)
