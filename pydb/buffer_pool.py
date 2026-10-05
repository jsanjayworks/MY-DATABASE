from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from pydb.pager import NULL_PAGE_ID, PAGE_SIZE, Pager, PagerError

DEFAULT_CAPACITY = 64


class BufferPoolError(PagerError):
    pass


class AllFramesPinnedError(BufferPoolError):
    pass


class PinnedPageError(BufferPoolError):
    pass


class Frame:
    __slots__ = ("index", "page_id", "data", "pin_count", "dirty", "referenced")

    def __init__(self, index: int) -> None:
        self.index = index
        self.page_id = NULL_PAGE_ID
        self.data = bytearray(PAGE_SIZE)
        self.pin_count = 0
        self.dirty = False
        self.referenced = False

    def __repr__(self) -> str:
        return (
            f"<Frame {self.index} page={self.page_id} pins={self.pin_count}"
            f"{' dirty' if self.dirty else ''}>"
        )


class BufferPoolStats:
    __slots__ = ("hits", "misses", "evictions", "disk_reads", "disk_writes")

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.disk_reads = 0
        self.disk_writes = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def __repr__(self) -> str:
        return (
            f"<stats hits={self.hits} misses={self.misses} "
            f"evictions={self.evictions} reads={self.disk_reads} "
            f"writes={self.disk_writes} hit_rate={self.hit_rate:.2%}>"
        )


class BufferPool:
    def __init__(self, pager: Pager, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"a pool needs at least one frame, got {capacity}")
        self.pager = pager
        self.capacity = capacity
        self.stats = BufferPoolStats()
        self._frames = [Frame(i) for i in range(capacity)]
        self._table: dict[int, Frame] = {}
        self._free: list[Frame] = list(reversed(self._frames))
        self._hand = 0
        self._owns_pager = False
        self._closed = False
        self.wal = None

    @classmethod
    def open(
        cls, path: str | os.PathLike[str], capacity: int = DEFAULT_CAPACITY
    ) -> "BufferPool":
        pager = Pager(path)
        try:
            pool = cls(pager, capacity)
        except Exception:
            pager.close()
            raise
        pool._owns_pager = True
        return pool

    def close(self) -> None:
        if self._closed:
            return
        self.flush_all()
        self._closed = True
        if self._owns_pager:
            self.pager.close()

    def __enter__(self) -> "BufferPool":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._table)

    def __contains__(self, page_id: int) -> bool:
        return page_id in self._table

    def __repr__(self) -> str:
        return (
            f"<BufferPool {len(self._table)}/{self.capacity} frames used, "
            f"{self.pinned_count()} pinned>"
        )

    def fetch_page(self, page_id: int) -> bytearray:
        self._require_open()
        self._check_cacheable(page_id)
        frame = self._table.get(page_id)
        if frame is not None:
            self.stats.hits += 1
            frame.pin_count += 1
            frame.referenced = True
            return frame.data
        self.stats.misses += 1
        frame = self._claim_frame()
        frame.data[:] = self.pager.read_page(page_id)
        self.stats.disk_reads += 1
        self._install(frame, page_id)
        return frame.data

    def unpin_page(self, page_id: int, dirty: bool = False) -> None:
        self._require_open()
        frame = self._table.get(page_id)
        if frame is None:
            raise BufferPoolError(f"page {page_id} is not in the pool")
        if frame.pin_count == 0:
            raise BufferPoolError(f"page {page_id} is not pinned (unbalanced unpin)")
        if dirty and len(frame.data) != PAGE_SIZE:
            raise BufferPoolError(
                f"page {page_id} was resized to {len(frame.data)} bytes; a page "
                f"must stay exactly {PAGE_SIZE} bytes"
            )
        frame.pin_count -= 1
        if dirty:
            self._mark_dirty(frame)

    @contextmanager
    def pinned(self, page_id: int, dirty: bool = False) -> Iterator[bytearray]:
        data = self.fetch_page(page_id)
        try:
            yield data
        finally:
            self.unpin_page(page_id, dirty=dirty)

    def new_page(self) -> tuple[int, bytearray]:
        self._require_open()
        page_id = self.pager.allocate_page()
        if page_id in self._table:
            raise BufferPoolError(
                f"pager allocated page {page_id}, which is still resident; it "
                f"was freed without going through BufferPool.free_page"
            )
        frame = self._claim_frame()
        frame.data[:] = bytes(PAGE_SIZE)
        self._install(frame, page_id)
        self._mark_dirty(frame)
        return page_id, frame.data

    def free_page(self, page_id: int) -> None:
        self._require_open()
        frame = self._table.get(page_id)
        if frame is not None and frame.pin_count > 0:
            raise PinnedPageError(
                f"cannot free page {page_id}: still pinned {frame.pin_count} time(s)"
            )
        if self.wal is not None:
            self.wal.stage_free(page_id)
            return
        if frame is not None:
            self._evict(frame, flush=False)
        self.pager.free_page(page_id)

    def flush_page(self, page_id: int) -> bool:
        self._require_open()
        frame = self._table.get(page_id)
        if frame is None or not frame.dirty:
            return False
        self._write_back(frame)
        return True

    def flush_all(self) -> int:
        written = 0
        for frame in self._frames:
            if frame.page_id != NULL_PAGE_ID and frame.dirty:
                self._write_back(frame)
                written += 1
        if written:
            self.pager.sync()
        return written

    def peek_page(self, page_id: int) -> bytearray | None:
        frame = self._table.get(page_id)
        return frame.data if frame is not None else None

    def discard_page(self, page_id: int) -> bool:
        self._require_open()
        frame = self._table.get(page_id)
        if frame is None:
            return False
        if frame.pin_count > 0:
            raise PinnedPageError(
                f"cannot discard page {page_id}: still pinned "
                f"{frame.pin_count} time(s)"
            )
        self._evict(frame, flush=False)
        return True

    def pinned_count(self) -> int:
        return sum(1 for f in self._frames if f.pin_count > 0)

    def dirty_pages(self) -> list[int]:
        return [f.page_id for f in self._frames if f.page_id != NULL_PAGE_ID and f.dirty]

    def resident_pages(self) -> list[int]:
        return sorted(self._table)

    def pin_count(self, page_id: int) -> int:
        frame = self._table.get(page_id)
        return frame.pin_count if frame is not None else 0

    def assert_no_pins(self) -> None:
        leaked = {f.page_id: f.pin_count for f in self._frames if f.pin_count > 0}
        if leaked:
            raise BufferPoolError(f"leaked pins: {leaked}")

    def _install(self, frame: Frame, page_id: int) -> None:
        frame.page_id = page_id
        frame.pin_count = 1
        frame.dirty = False
        frame.referenced = True
        self._table[page_id] = frame

    def _mark_dirty(self, frame: Frame) -> None:
        frame.dirty = True
        if self.wal is not None:
            self.wal.note_dirty(frame.page_id)

    def _claim_frame(self) -> Frame:
        if not self._free:
            victim = self._find_victim()
            self._evict(victim, flush=True)
            self.stats.evictions += 1
        return self._free.pop()

    def _find_victim(self) -> Frame:
        for _ in range(2 * self.capacity):
            frame = self._frames[self._hand]
            self._hand = (self._hand + 1) % self.capacity
            assert frame.page_id != NULL_PAGE_ID, (
                f"{frame!r} holds nothing but is not on the free list; every "
                f"path that empties a frame must return it there"
            )
            if frame.pin_count > 0:
                continue
            if self.wal is not None and self.wal.holds_uncommitted(frame.page_id):
                continue
            if frame.referenced:
                frame.referenced = False
                continue
            return frame
        raise AllFramesPinnedError(
            f"no frame can be evicted: all {self.capacity} are pinned or hold "
            f"uncommitted changes. Unpin pages, commit the transaction, or open "
            f"the pool with a larger capacity"
        )

    def _evict(self, frame: Frame, flush: bool) -> None:
        assert frame.pin_count == 0, f"evicting pinned {frame!r}"
        if flush and frame.dirty:
            self._write_back(frame)
        del self._table[frame.page_id]
        frame.page_id = NULL_PAGE_ID
        frame.dirty = False
        frame.referenced = False
        self._free.append(frame)

    def _write_back(self, frame: Frame) -> None:
        assert self.wal is None or not self.wal.holds_uncommitted(frame.page_id), (
            f"write-ahead rule violated: page {frame.page_id} would reach the "
            f"data file before its log record is durable"
        )
        self.pager.write_page(frame.page_id, frame.data)
        frame.dirty = False
        self.stats.disk_writes += 1

    def _require_open(self) -> None:
        if self._closed:
            raise BufferPoolError("buffer pool is closed")

    def _check_cacheable(self, page_id: int) -> None:
        if page_id == NULL_PAGE_ID:
            raise BufferPoolError(
                "page 0 is the pager's meta page and is not cacheable"
            )
        if page_id < 0 or page_id >= self.pager.page_count:
            raise BufferPoolError(
                f"page {page_id} is out of range (file has "
                f"{self.pager.page_count} pages)"
            )
