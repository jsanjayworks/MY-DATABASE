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

TABLE_HEADER_FORMAT = ">IH"
TABLE_HEADER_SIZE = struct.calcsize(TABLE_HEADER_FORMAT)

ROW_ID_FORMAT = ">IH"
ROW_ID_SIZE = struct.calcsize(ROW_ID_FORMAT)

INDEX_FLAG_UNIQUE = 1
INDEX_FLAG_PRIMARY = 2

KEY_TABLE = b"t"
KEY_INDEX = b"i"


class CatalogError(PydbError):
    pass


class UnknownTableError(CatalogError):
    pass


class UnknownIndexError(CatalogError):
    pass


class TableExistsError(CatalogError):
    pass


class IndexExistsError(CatalogError):
    pass


def pack_row_id(rid: RowId) -> bytes:
    return struct.pack(ROW_ID_FORMAT, rid.page_id, rid.slot)


def unpack_row_id(raw: bytes) -> RowId:
    return RowId(*struct.unpack(ROW_ID_FORMAT, raw))


def escape_key(key: bytes) -> bytes:
    return key.replace(b"\x00", b"\x00\xff") + b"\x00\x00"


def prefix_end(prefix: bytes) -> bytes | None:
    data = bytearray(prefix)
    while data:
        if data[-1] != 0xFF:
            data[-1] += 1
            return bytes(data)
        data.pop()
    return None


@dataclass
class IndexInfo:
    name: str
    column: int
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

    def value_key(self, value: object) -> bytes:
        return encode_key(self.column_type, value)

    def entry_key(self, value: object, rid: RowId) -> bytes:
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
        key = self.value_key(value)
        if self.unique:
            return key if inclusive else key + b"\x00"
        escaped = escape_key(key)
        return escaped if inclusive else prefix_end(escaped)

    def bound_below(self, value: object, inclusive: bool) -> bytes | None:
        key = self.value_key(value)
        if self.unique:
            return key + b"\x00" if inclusive else key
        escaped = escape_key(key)
        return prefix_end(escaped) if inclusive else escaped

    def add(self, values: tuple, rid: RowId) -> None:
        value = values[self.column]
        if value is None:
            return
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
        if value is None or not self.unique:
            return False
        existing = self.tree.get(self.value_key(value))
        return existing is not None and unpack_row_id(existing) != rid

    def seek(self, value: object) -> Iterator[RowId]:
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
        for _key, raw in self.tree.items(low, high):
            yield unpack_row_id(raw)

    def entries(self) -> Iterator[tuple[bytes, RowId]]:
        for key, raw in self.tree.items():
            yield key, unpack_row_id(raw)


class Table:
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
        matches = [index for index in self.indexes if index.column == column]
        return sorted(matches, key=lambda index: not index.unique)

    def reload(self) -> None:
        self.info = self.catalog.get(self.name)
        self.heap.reload()
        self.indexes = [Index(self, index) for index in self.info.indexes]

    def insert(self, values: tuple) -> RowId:
        self.schema.encode(values)
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
        self.heap.delete(rid)
        for index in self.indexes:
            index.remove(values, rid)

    def update(self, rid: RowId, old: tuple, new: tuple) -> RowId:
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
        return self.heap.scan()

    def get(self, rid: RowId) -> tuple:
        return self.heap.get(rid)

    def rows_for(self, rids: Iterator[RowId]) -> Iterator[tuple[RowId, tuple]]:
        for rid in rids:
            yield rid, self.heap.get(rid)

    def lookup(self, value: object) -> tuple[RowId, tuple] | None:
        index = self.primary_index
        if index is None:
            raise CatalogError(f"{self.name} has no primary key to look up by")
        for rid in index.seek(value):
            return rid, self.heap.get(rid)
        return None

    def verify(self) -> None:
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
        return self.heap.compact()

    def all_pages(self) -> list[int]:
        pages = list(self.heap.page_ids)
        for index in self.indexes:
            pages.extend(index.tree.all_pages())
        return pages


class Catalog:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.tree = db.open_tree(META_SLOT_ROOT)
        self._tables: dict[str, Table] = {}
        db.add_rollback_hook(self._reload_tables)

    def __repr__(self) -> str:
        return f"<Catalog {len(self.table_names())} tables>"

    def __contains__(self, name: str) -> bool:
        return self.tree.get(_table_key(name)) is not None

    def table_names(self) -> list[str]:
        return [
            str(key[1:], "utf-8")
            for key in self.tree.keys(KEY_TABLE, _after(KEY_TABLE))
        ]

    def index_names(self) -> list[str]:
        return [
            str(key[1:], "utf-8")
            for key in self.tree.keys(KEY_INDEX, _after(KEY_INDEX))
        ]

    def info(self, name: str) -> TableInfo:
        raw = self.tree.get(_table_key(name))
        if raw is None:
            raise UnknownTableError(f"no such table: {name}")
        return TableInfo.decode(name, raw)

    get = info

    def open(self, name: str) -> Table:
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
        if not name:
            raise CatalogError("a table needs a name")
        if name in self:
            raise TableExistsError(f"table {name} already exists")

        indexes: list[IndexInfo] = []
        with self.db.autocommit():
            heap = HeapFile.create(self.db.pool, schema)
            if primary_key is not None:
                key_index = schema.index(primary_key)
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
        if self.has_index(index_name):
            raise IndexExistsError(f"index {index_name} already exists")
        table = self.open(table_name)
        column_index = table.schema.index(column)

        with self.db.autocommit():
            tree = BTree.create(self.db.pool)
            info = IndexInfo(index_name, column_index, unique=unique,
                             root=tree.root_page_id)
            table.info.indexes.append(info)
            self._write(table.info)
            self.tree.put(_index_key(index_name), table_name.encode("utf-8"))
            index = Index(table, info)
            table.indexes.append(index)
            for rid, values in table.heap.scan():
                index.add(values, rid)
        return index

    def drop_index(self, index_name: str) -> None:
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
        table = self.open(name)
        pages = table.all_pages()
        index_names = [index.name for index in table.indexes]
        with self.db.autocommit():
            for index_name in index_names:
                self.tree.delete(_index_key(index_name))
            self.tree.delete(_table_key(name))
        self._tables.pop(name, None)
        self.db.free_pages(pages)

    def set_index_root(self, table_name: str, index_name: str, page_id: int) -> None:
        info = self.info(table_name)
        info.index_named(index_name).root = page_id
        self._write(info)
        if table_name in self._tables:
            cached = self._tables[table_name]
            for index in cached.info.indexes:
                if index.name == index_name:
                    index.root = page_id

    def _write(self, info: TableInfo) -> None:
        self.tree.put(_table_key(info.name), info.encode())

    def _reload_tables(self) -> None:
        for name, table in list(self._tables.items()):
            if name in self:
                table.reload()
            else:
                del self._tables[name]


def _table_key(name: str) -> bytes:
    return KEY_TABLE + name.encode("utf-8")


def _index_key(name: str) -> bytes:
    return KEY_INDEX + name.encode("utf-8")


def _after(prefix: bytes) -> bytes:
    return prefix_end(prefix)
