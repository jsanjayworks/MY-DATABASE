"""Layer 3c: the heap file.

A table's rows live in a singly-linked chain of slotted pages, in no particular
order -- a heap. Rows are addressed by `RowId(page_id, slot)`, which is what an
index will eventually point at.

This is the first component that combines all three layers below it: it asks the
buffer pool for pages, reads and writes them through `SlottedPage`, and encodes
rows with a `Schema`. It never touches the `Pager` directly, which is the rule
layer 2 established.

Finding room for an insert is the one interesting problem. Walking the chain
every time is quadratic, so the heap keeps an in-memory map of pages that still
have useful free space and drops a page from it once it falls below
`FREE_SPACE_THRESHOLD`. That wastes the last few hundred bytes of a nearly full
page until a delete reopens it -- the same trade a real free-space map makes when
it quantises free space into a handful of buckets.
"""

from __future__ import annotations

from typing import Iterator, NamedTuple, Sequence

from pydb.buffer_pool import BufferPool
from pydb.pager import NULL_PAGE_ID, PAGE_SIZE
from pydb.record import Schema
from pydb.slotted_page import MAX_ROW_SIZE, SLOT_SIZE, NoRoomError, SlottedPage

# A page with less than this much free space is not worth trying for an insert.
FREE_SPACE_THRESHOLD = PAGE_SIZE // 16


class HeapError(Exception):
    """Base class for heap file errors."""


class RowNotFoundError(HeapError, KeyError):
    """No live row at that row id.

    Subclasses `KeyError` so `except KeyError` around a lookup still behaves, and
    so the SQL layer can treat a missing row the same as a missing dict entry.
    """


class RowId(NamedTuple):
    """Where a row physically lives. Stable while the row exists, and no longer.

    Compaction moves a row's bytes but never its slot, so a row id survives that.
    A *deleted* row's id is dangling: the slot can be handed to a new row, at
    which point the old id silently refers to the new row. Treat it like a
    pointer after a free.
    """

    page_id: int
    slot: int

    def __str__(self) -> str:
        return f"{self.page_id}:{self.slot}"


class HeapFile:
    """An unordered collection of rows spread over a chain of pages.

        >>> pool = BufferPool.open("my.db")
        >>> schema = Schema.of(("id", "INT", False), ("name", "TEXT"))
        >>> heap = HeapFile.create(pool, schema)
        >>> rid = heap.insert((1, "ada"))
        >>> heap.get(rid)
        (1, 'ada')
    """

    def __init__(self, pool: BufferPool, schema: Schema, first_page_id: int) -> None:
        self.pool = pool
        self.schema = schema
        self.first_page_id = first_page_id
        self._pages: list[int] = []  # the chain, in order
        self._page_set: set[int] = set()  # the same pages, for O(1) membership
        self._room: dict[int, int] = {}  # page id -> usable bytes, roomy pages only
        self._load_chain()

    @classmethod
    def create(cls, pool: BufferPool, schema: Schema) -> "HeapFile":
        """Allocate an empty heap and return it. Remember `first_page_id`."""
        page_id, data = pool.new_page()
        try:
            SlottedPage.initialize(data)
        finally:
            pool.unpin_page(page_id, dirty=True)
        return cls(pool, schema, page_id)

    def __repr__(self) -> str:
        return (
            f"<HeapFile first_page={self.first_page_id} "
            f"pages={len(self._pages)} rows={len(self)}>"
        )

    def __len__(self) -> int:
        """Live row count. Walks the chain's headers, not its rows."""
        total = 0
        for page_id in self._pages:
            with self.pool.pinned(page_id) as data:
                total += SlottedPage(data).live_count
        return total

    def __iter__(self) -> Iterator[tuple]:
        for _rid, row in self.scan():
            yield row

    @property
    def page_ids(self) -> tuple[int, ...]:
        return tuple(self._pages)

    # ------------------------------------------------------------------
    # the row operations
    # ------------------------------------------------------------------

    def insert(self, values: Sequence[object]) -> RowId:
        """Encode and store a row, returning where it went."""
        row = self.schema.encode(values)
        page_id = self._find_room(len(row))
        with self.pool.pinned(page_id, dirty=True) as data:
            page = SlottedPage(data)
            slot = page.insert(row)
            self._note_room(page_id, page)
        return RowId(page_id, slot)

    def get(self, rid: RowId) -> tuple:
        """Decode the row at `rid`, or raise `RowNotFoundError`."""
        self._require_page(rid)
        with self.pool.pinned(rid.page_id) as data:
            page = SlottedPage(data)
            try:
                row = page.read(rid.slot)
            except KeyError:
                raise RowNotFoundError(f"no live row at {rid}") from None
        return self.schema.decode(row)

    def delete(self, rid: RowId) -> None:
        """Remove the row at `rid`, or raise `RowNotFoundError`."""
        self._require_page(rid)
        with self.pool.pinned(rid.page_id, dirty=True) as data:
            page = SlottedPage(data)
            try:
                page.delete(rid.slot)
            except KeyError:
                raise RowNotFoundError(f"no live row at {rid}") from None
            # Deleting is the only thing that can make a closed page interesting
            # again, so this is where a page rejoins the free-space map. The dead
            # bytes count: an insert will compact the page to get at them.
            self._note_room(rid.page_id, page)

    def update(self, rid: RowId, values: Sequence[object]) -> RowId:
        """Replace the row at `rid`, returning its (possibly new) row id.

        A row that no longer fits on its page is moved, which changes its row id.
        Callers holding the old id -- an index, say -- have to be told; that is
        why this returns one instead of updating in silence.
        """
        row = self.schema.encode(values)
        self._require_page(rid)
        with self.pool.pinned(rid.page_id, dirty=True) as data:
            page = SlottedPage(data)
            try:
                fitted = page.replace(rid.slot, row)
            except KeyError:
                raise RowNotFoundError(f"no live row at {rid}") from None
            if fitted:
                self._note_room(rid.page_id, page)
                return rid
        self.delete(rid)
        return self.insert(values)

    def scan(self) -> Iterator[tuple[RowId, tuple]]:
        """Every live row, page by page, in physical order.

        One page is decoded at a time and yielded with no page pinned, so a slow
        consumer cannot hold a frame hostage. The flip side is that the scan is
        not a snapshot: inserting or deleting while it runs can make it miss or
        repeat rows. Collect the row ids first if you mean to modify them.
        """
        for page_id in list(self._pages):
            with self.pool.pinned(page_id) as data:
                rows = SlottedPage(data).rows()
            for slot, row in rows:
                yield RowId(page_id, slot), self.schema.decode(row)

    # ------------------------------------------------------------------
    # maintenance and invariants
    # ------------------------------------------------------------------

    def compact(self) -> int:
        """Compact every page, returning the bytes reclaimed across the heap."""
        reclaimed = 0
        for page_id in self._pages:
            with self.pool.pinned(page_id, dirty=True) as data:
                page = SlottedPage(data)
                reclaimed += page.compact()
                self._note_room(page_id, page)
        return reclaimed

    def verify(self) -> None:
        """Check every page's invariants and that the chain matches what's cached."""
        chain: list[int] = []
        page_id = self.first_page_id
        seen: set[int] = set()
        while page_id != NULL_PAGE_ID:
            if page_id in seen:
                raise HeapError(f"page chain loops back to {page_id}")
            seen.add(page_id)
            chain.append(page_id)
            with self.pool.pinned(page_id) as data:
                page = SlottedPage(data)
                page.verify()
                page_id = page.next_page
        if chain != self._pages:
            raise HeapError(
                f"cached chain {self._pages} does not match the file's {chain}"
            )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_page(self, rid: RowId) -> None:
        """Reject a row id from another table before the pool ever sees it.

        The pool would raise for a page id past the end of the file, but a page
        id belonging to a *different* table would read fine and return somebody
        else's row.
        """
        if rid.page_id not in self._page_set:
            raise RowNotFoundError(f"page {rid.page_id} is not part of this heap")

    def _load_chain(self) -> None:
        """Walk the chain once, recording page order and where there is room."""
        page_id = self.first_page_id
        while page_id != NULL_PAGE_ID:
            self._pages.append(page_id)
            self._page_set.add(page_id)
            with self.pool.pinned(page_id) as data:
                page = SlottedPage(data)
                self._note_room(page_id, page)
                page_id = page.next_page

    def _note_room(self, page_id: int, page: SlottedPage) -> None:
        """Record how many bytes `page_id` could give an insert.

        Dead bytes count: `SlottedPage.insert` compacts the page when that is
        what it takes to make the row fit.
        """
        usable = page.free_space + page.dead_space
        if usable >= FREE_SPACE_THRESHOLD:
            self._room[page_id] = usable
        else:
            self._room.pop(page_id, None)

    def _find_room(self, row_size: int) -> int:
        """A page that can take `row_size` bytes, appending one if necessary."""
        if row_size > MAX_ROW_SIZE:
            raise NoRoomError(
                f"row is {row_size} bytes; the per-row limit is {MAX_ROW_SIZE}"
            )
        need = row_size + SLOT_SIZE
        for page_id, free in self._room.items():
            if free >= need:
                return page_id
        return self._append_page()

    def _append_page(self) -> int:
        """Link a fresh page onto the end of the chain."""
        new_id, data = self.pool.new_page()
        try:
            page = SlottedPage.initialize(data)
            self._note_room(new_id, page)
        finally:
            self.pool.unpin_page(new_id, dirty=True)
        with self.pool.pinned(self._pages[-1], dirty=True) as tail:
            SlottedPage(tail).next_page = new_id
        self._pages.append(new_id)
        self._page_set.add(new_id)
        return new_id
