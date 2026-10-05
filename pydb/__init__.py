"""pydb - a SQL database built from scratch, one layer at a time.

The short version::

    from pydb import Database, Engine

    with Database("my.db") as db:
        sql = Engine(db)
        sql.execute("CREATE TABLE people (id INT PRIMARY KEY, name TEXT)")
        sql.execute("INSERT INTO people VALUES (1, 'ada')")
        print(sql.execute("SELECT name FROM people WHERE id = 1").rows)

Or `python -m pydb my.db` for a shell.

Underneath that are seven layers, each usable on its own:

| Layer | Module | What it adds |
|-------|--------|--------------|
| 1 | `pager` | a file as an array of 4 KB pages |
| 2 | `buffer_pool` | those pages cached in memory, with pins and eviction |
| 3 | `record`, `slotted_page`, `heap` | typed rows, packed into pages, in tables |
| 4 | `btree_node`, `btree` | an ordered index with range scans |
| 5 | `wal` | a write-ahead log, so a crash is not data loss |
| 6 | `database` | transactions: begin, commit, rollback |
| 7 | `catalog`, `sql`, `repl` | tables and indexes that describe themselves, and SQL over them |
"""

from pydb.btree import BTree, BTreeError, CorruptTreeError, DuplicateKeyError
from pydb.btree_node import CellTooLargeError
from pydb.buffer_pool import (
    AllFramesPinnedError,
    BufferPool,
    BufferPoolError,
    PinnedPageError,
)
from pydb.catalog import (
    Catalog,
    CatalogError,
    Index,
    IndexInfo,
    Table,
    TableInfo,
    UnknownIndexError,
    UnknownTableError,
)
from pydb.database import Database, TransactionError
from pydb.errors import PydbError
from pydb.heap import HeapError, HeapFile, RowId, RowNotFoundError
from pydb.pager import PAGE_SIZE, CorruptFileError, FileInUseError, Pager, PagerError
from pydb.record import (
    Column,
    ColumnType,
    RecordError,
    Schema,
    SchemaError,
    decode_key,
    encode_key,
)
from pydb.slotted_page import NoRoomError, RowTooLargeError, SlottedPage
from pydb.sql import Engine, ParseError, PlanError, Result, SqlError, ValueTypeError
from pydb.wal import CorruptWalError, Wal, WalError

__all__ = [
    # every deliberate error, whichever layer raised it
    "PydbError",
    # layer 1
    "PAGE_SIZE",
    "Pager",
    "PagerError",
    "CorruptFileError",
    "FileInUseError",
    # layer 2
    "BufferPool",
    "BufferPoolError",
    "AllFramesPinnedError",
    "PinnedPageError",
    # layer 3
    "Column",
    "ColumnType",
    "Schema",
    "RecordError",
    "SchemaError",
    "SlottedPage",
    "NoRoomError",
    "RowTooLargeError",
    "HeapFile",
    "HeapError",
    "RowId",
    "RowNotFoundError",
    "encode_key",
    "decode_key",
    # layer 4
    "BTree",
    "BTreeError",
    "DuplicateKeyError",
    "CorruptTreeError",
    "CellTooLargeError",
    # layer 5
    "Wal",
    "WalError",
    "CorruptWalError",
    # layer 6
    "Database",
    "TransactionError",
    # layer 7
    "Catalog",
    "CatalogError",
    "UnknownTableError",
    "UnknownIndexError",
    "Table",
    "TableInfo",
    "Index",
    "IndexInfo",
    "Engine",
    "Result",
    "SqlError",
    "ParseError",
    "PlanError",
    "ValueTypeError",
]
