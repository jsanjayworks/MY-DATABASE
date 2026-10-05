from __future__ import annotations

from typing import Iterator, NamedTuple, Sequence

from pydb.buffer_pool import BufferPool
from pydb.errors import PydbError
from pydb.pager import NULL_PAGE_ID, PAGE_SIZE
from pydb.record import Schema
from pydb.slotted_page import MAX_ROW_SIZE, SLOT_SIZE, NoRoomError, SlottedPage

FREE_SPACE_THRESHOLD = PAGE_SIZE // 16


class HeapError(PydbError):
    pass


class RowNotFoundError(HeapError, KeyError):
    pass


class RowId(NamedTuple):
    page_id: int
    slot: int

    def __str__(self) -> str:
        return f"{self.page_id}:{self.slot}"


class HeapFile:
    def __init__(self, pool: BufferPool, schema: Schema, first_page_id: int) -> None:
        self.pool = pool
        self.schema = schema
        self.first_page_id = first_page_id
        self._pages: list[int] = []
        self._page_set: set[int] = set()
        self._room: dict[int, int] = {}
        self._load_chain()

    @classmethod
    def create(cls, pool: BufferPool, schema: Schema) -> "HeapFile":
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

    def insert(self, values: Sequence[object]) -> RowId:
        row = self.schema.encode(values)
        page_id = self._find_room(len(row))
        with self.pool.pinned(page_id, dirty=True) as data:
            page = SlottedPage(data)
            slot = page.insert(row)
            self._note_room(page_id, page)
        return RowId(page_id, slot)

    def get(self, rid: RowId) -> tuple:
        self._require_page(rid)
        with self.pool.pinned(rid.page_id) as data:
            page = SlottedPage(data)
            try:
                row = page.read(rid.slot)
            except KeyError:
                raise RowNotFoundError(f"no live row at {rid}") from None
        return self.schema.decode(row)

    def delete(self, rid: RowId) -> None:
        self._require_page(rid)
        with self.pool.pinned(rid.page_id, dirty=True) as data:
            page = SlottedPage(data)
            try:
                page.delete(rid.slot)
            except KeyError:
                raise RowNotFoundError(f"no live row at {rid}") from None
            self._note_room(rid.page_id, page)

    def update(self, rid: RowId, values: Sequence[object]) -> RowId:
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
        for page_id in list(self._pages):
            with self.pool.pinned(page_id) as data:
                rows = SlottedPage(data).rows()
            for slot, row in rows:
                yield RowId(page_id, slot), self.schema.decode(row)

    def compact(self) -> int:
        reclaimed = 0
        for page_id in self._pages:
            with self.pool.pinned(page_id, dirty=True) as data:
                page = SlottedPage(data)
                reclaimed += page.compact()
                self._note_room(page_id, page)
        return reclaimed

    def reload(self) -> None:
        self._pages.clear()
        self._page_set.clear()
        self._room.clear()
        self._load_chain()

    def verify(self) -> None:
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

    def _require_page(self, rid: RowId) -> None:
        if rid.page_id not in self._page_set:
            raise RowNotFoundError(f"page {rid.page_id} is not part of this heap")

    def _load_chain(self) -> None:
        page_id = self.first_page_id
        while page_id != NULL_PAGE_ID:
            self._pages.append(page_id)
            self._page_set.add(page_id)
            with self.pool.pinned(page_id) as data:
                page = SlottedPage(data)
                self._note_room(page_id, page)
                page_id = page.next_page

    def _note_room(self, page_id: int, page: SlottedPage) -> None:
        usable = page.free_space + page.dead_space
        if usable >= FREE_SPACE_THRESHOLD:
            self._room[page_id] = usable
        else:
            self._room.pop(page_id, None)

    def _find_room(self, row_size: int) -> int:
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
