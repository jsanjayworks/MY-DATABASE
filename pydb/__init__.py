"""pydb - a SQL database built from scratch, one layer at a time."""

from pydb.btree import BTree, BTreeError, CorruptTreeError, DuplicateKeyError
from pydb.btree_node import CellTooLargeError
from pydb.database import Database, TransactionError
from pydb.buffer_pool import (
    AllFramesPinnedError,
    BufferPool,
    BufferPoolError,
    PinnedPageError,
)
from pydb.heap import HeapError, HeapFile, RowId, RowNotFoundError
from pydb.pager import PAGE_SIZE, CorruptFileError, Pager, PagerError
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
from pydb.wal import CorruptWalError, Wal, WalError

__all__ = [
    # layer 1
    "PAGE_SIZE",
    "Pager",
    "PagerError",
    "CorruptFileError",
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
]
