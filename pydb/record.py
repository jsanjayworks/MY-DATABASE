"""Layer 3a: schemas and row encoding.

Layers 1 and 2 move anonymous 4 KB blocks around. This is where bytes start
meaning something: a `Schema` is an ordered list of typed columns, and it knows
how to turn a Python tuple into the exact bytes that go in a page and back.

Three types, which is enough to build a database on: INT, TEXT, and NULL -- NULL
being a property of a value rather than a type of its own, tracked in a bitmap at
the front of every row so a null costs one bit instead of a whole field.

The byte layout is documented in NOTES.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator, Sequence

INT_FORMAT = ">q"  # 8-byte signed, so it can hold any SQLite-ish INTEGER
INT_SIZE = struct.calcsize(INT_FORMAT)
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1

TEXT_LEN_FORMAT = ">H"  # 2-byte length prefix
TEXT_LEN_SIZE = struct.calcsize(TEXT_LEN_FORMAT)
TEXT_MAX_LEN = 2**16 - 1


class RecordError(Exception):
    """Base class for schema and encoding errors."""


class SchemaError(RecordError):
    """A value does not match the column it is being stored in."""


class ColumnType(IntEnum):
    """The type tags. The integer values are written to disk by the catalog."""

    INT = 1
    TEXT = 2

    def __str__(self) -> str:
        return self.name

    @classmethod
    def parse(cls, name: str) -> "ColumnType":
        """Accept the SQL spellings a user is likely to type."""
        key = name.strip().upper()
        aliases = {
            "INT": cls.INT,
            "INTEGER": cls.INT,
            "BIGINT": cls.INT,
            "TEXT": cls.TEXT,
            "STRING": cls.TEXT,
            "VARCHAR": cls.TEXT,
        }
        if key not in aliases:
            raise SchemaError(f"unknown column type {name!r}")
        return aliases[key]


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    nullable: bool = True

    def __str__(self) -> str:
        return f"{self.name} {self.type}{'' if self.nullable else ' NOT NULL'}"


class Schema:
    """An ordered list of named, typed columns, and the row codec for it.

        >>> schema = Schema([Column("id", ColumnType.INT, nullable=False),
        ...                  Column("name", ColumnType.TEXT)])
        >>> schema.decode(schema.encode((1, "ada")))
        (1, 'ada')
    """

    __slots__ = ("columns", "_index", "_null_bytes")

    def __init__(self, columns: Sequence[Column]) -> None:
        if not columns:
            raise SchemaError("a schema needs at least one column")
        names = [c.name for c in columns]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise SchemaError(f"duplicate column name(s): {sorted(duplicates)}")
        self.columns = tuple(columns)
        self._index = {c.name: i for i, c in enumerate(self.columns)}
        # One bit per column, rounded up to whole bytes.
        self._null_bytes = (len(self.columns) + 7) // 8

    @classmethod
    def of(cls, *specs: tuple) -> "Schema":
        """Terse constructor for tests: `Schema.of(("id", "INT", False), ...)`."""
        columns = []
        for spec in specs:
            name, type_name, *rest = spec
            nullable = rest[0] if rest else True
            columns.append(Column(name, ColumnType.parse(type_name), nullable))
        return cls(columns)

    # ------------------------------------------------------------------
    # column lookup
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    def __getitem__(self, key: int | str) -> Column:
        if isinstance(key, str):
            return self.columns[self.index(key)]
        return self.columns[key]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Schema) and self.columns == other.columns

    def __repr__(self) -> str:
        return f"Schema({', '.join(str(c) for c in self.columns)})"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def index(self, name: str) -> int:
        """Position of `name`, raising `SchemaError` rather than `KeyError`."""
        try:
            return self._index[name]
        except KeyError:
            raise SchemaError(
                f"no column {name!r}; this table has {list(self.names)}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._index

    # ------------------------------------------------------------------
    # encoding
    # ------------------------------------------------------------------

    def encode(self, values: Sequence[object]) -> bytes:
        """Pack one row. Raises `SchemaError` if the tuple does not fit the schema."""
        if len(values) != len(self.columns):
            raise SchemaError(
                f"expected {len(self.columns)} values {list(self.names)}, "
                f"got {len(values)}"
            )
        null_bits = bytearray(self._null_bytes)
        parts: list[bytes] = []
        for i, (column, value) in enumerate(zip(self.columns, values)):
            if value is None:
                if not column.nullable:
                    raise SchemaError(f"column {column.name!r} is NOT NULL")
                null_bits[i // 8] |= 1 << (i % 8)
                continue
            parts.append(self._encode_value(column, value))
        return bytes(null_bits) + b"".join(parts)

    def decode(self, data: bytes | bytearray | memoryview) -> tuple:
        """Unpack one row. `data` may be longer than the row; the tail is ignored."""
        view = memoryview(data)
        if len(view) < self._null_bytes:
            raise RecordError(
                f"row is {len(view)} bytes, too short for a "
                f"{self._null_bytes}-byte null bitmap"
            )
        null_bits = view[: self._null_bytes]
        offset = self._null_bytes
        values: list[object] = []
        for i, column in enumerate(self.columns):
            if null_bits[i // 8] >> (i % 8) & 1:
                values.append(None)
                continue
            value, offset = self._decode_value(column, view, offset)
            values.append(value)
        return tuple(values)

    def encoded_size(self, values: Sequence[object]) -> int:
        """Bytes `encode(values)` will produce, without building them."""
        size = self._null_bytes
        for column, value in zip(self.columns, values):
            if value is None:
                continue
            if column.type is ColumnType.INT:
                size += INT_SIZE
            else:
                size += TEXT_LEN_SIZE + len(str(value).encode("utf-8"))
        return size

    def _encode_value(self, column: Column, value: object) -> bytes:
        if column.type is ColumnType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaError(
                    f"column {column.name!r} is INT, got "
                    f"{type(value).__name__} {value!r}"
                )
            if not INT_MIN <= value <= INT_MAX:
                raise SchemaError(
                    f"column {column.name!r}: {value} does not fit in 8 bytes"
                )
            return struct.pack(INT_FORMAT, value)

        if not isinstance(value, str):
            raise SchemaError(
                f"column {column.name!r} is TEXT, got "
                f"{type(value).__name__} {value!r}"
            )
        encoded = value.encode("utf-8")
        if len(encoded) > TEXT_MAX_LEN:
            raise SchemaError(
                f"column {column.name!r}: {len(encoded)} bytes of text exceeds "
                f"the {TEXT_MAX_LEN}-byte limit"
            )
        return struct.pack(TEXT_LEN_FORMAT, len(encoded)) + encoded

    def _decode_value(
        self, column: Column, view: memoryview, offset: int
    ) -> tuple[object, int]:
        if column.type is ColumnType.INT:
            end = offset + INT_SIZE
            self._require(view, end, column)
            (value,) = struct.unpack_from(INT_FORMAT, view, offset)
            return value, end

        self._require(view, offset + TEXT_LEN_SIZE, column)
        (length,) = struct.unpack_from(TEXT_LEN_FORMAT, view, offset)
        start = offset + TEXT_LEN_SIZE
        end = start + length
        self._require(view, end, column)
        return str(view[start:end], "utf-8"), end

    @staticmethod
    def _require(view: memoryview, end: int, column: Column) -> None:
        if end > len(view):
            raise RecordError(
                f"row ends mid-value: column {column.name!r} needs bytes up to "
                f"{end} but the row is {len(view)} bytes"
            )
