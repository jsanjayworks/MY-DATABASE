"""pydb - a SQL database built from scratch, one layer at a time."""

from pydb.buffer_pool import (
    AllFramesPinnedError,
    BufferPool,
    BufferPoolError,
    PinnedPageError,
)
from pydb.heap import HeapError, HeapFile, RowId, RowNotFoundError
from pydb.pager import PAGE_SIZE, CorruptFileError, Pager, PagerError
from pydb.record import Column, ColumnType, RecordError, Schema, SchemaError
from pydb.slotted_page import NoRoomError, RowTooLargeError, SlottedPage

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
]
