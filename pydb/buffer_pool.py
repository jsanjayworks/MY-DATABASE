"""Layer 2: the buffer pool.

Layer 1 copies a page off disk on every read and writes it straight back. That
is correct and hopelessly slow: a B+Tree lookup touches the root page every
single time, and the root should never leave memory.

The buffer pool fixes that. It owns a fixed number of *frames* -- page-sized
buffers in RAM -- and a table mapping page id to frame. Callers `fetch_page` a
page, mutate the bytes in place, and `unpin_page` it when done. Nothing is
written back until the frame is evicted, flushed, or the pool is closed.

Two rules make this safe, and every bug in this layer is a violation of one:

1. **A pinned page is never evicted.** `pin_count` is how a caller says "I am
   holding a pointer into this frame; moving it under me is a use-after-free".
2. **A dirty page is written before its frame is reused.** Otherwise the newest
   version of the page quietly vanishes.

Eviction uses the clock (second-chance) algorithm: a cheap approximation of LRU
that needs one bit per frame instead of a linked list.

From here on, layers above this one talk to the pool, not to `Pager`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from pydb.pager import NULL_PAGE_ID, PAGE_SIZE, Pager, PagerError

DEFAULT_CAPACITY = 64


class BufferPoolError(PagerError):
    """Base class for buffer pool misuse."""


class AllFramesPinnedError(BufferPoolError):
    """Every frame is pinned, so there is nowhere to put the requested page.

    This is not a corruption; it means the caller is holding too many pages at
    once for the configured pool size. Either unpin something or grow the pool.
    """


class PinnedPageError(BufferPoolError):
    """An operation needs exclusive access to a page someone else is holding."""


class Frame:
    """One page-sized slot in memory, plus the bookkeeping that makes it safe."""

    __slots__ = ("index", "page_id", "data", "pin_count", "dirty", "referenced")

    def __init__(self, index: int) -> None:
        self.index = index
        self.page_id = NULL_PAGE_ID  # 0 means "this frame holds nothing"
        self.data = bytearray(PAGE_SIZE)
        self.pin_count = 0
        self.dirty = False
        # The clock's second-chance bit: set on every access, cleared when the
        # hand passes over it. A frame is only evicted if the hand finds it with
        # the bit already clear, i.e. it has not been touched in a full sweep.
        self.referenced = False

    def __repr__(self) -> str:
        return (
            f"<Frame {self.index} page={self.page_id} pins={self.pin_count}"
            f"{' dirty' if self.dirty else ''}>"
        )


class BufferPoolStats:
    """Counters that make the cache's behaviour testable instead of a guess."""

    __slots__ = ("hits", "misses", "evictions", "disk_reads", "disk_writes")

    def __init__(self) -> None:
        self.hits = 0  # fetch found the page already in a frame
        self.misses = 0  # fetch had to go to disk
        self.evictions = 0  # a resident page was kicked out of its frame
        self.disk_reads = 0
        self.disk_writes = 0  # dirty frames actually written back

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
    """A fixed-size cache of pages sitting on top of a `Pager`.

    Typical use, with the context manager that cannot forget to unpin::

        with BufferPool.open("my.db", capacity=128) as pool:
            page_id, page = pool.new_page()
            page[:5] = b"hello"
            pool.unpin_page(page_id, dirty=True)

            with pool.pinned(page_id) as page:
                assert page[:5] == b"hello"
    """

    def __init__(self, pager: Pager, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"a pool needs at least one frame, got {capacity}")
        self.pager = pager
        self.capacity = capacity
        self.stats = BufferPoolStats()
        self._frames = [Frame(i) for i in range(capacity)]
        self._table: dict[int, Frame] = {}  # page id -> resident frame
        self._free: list[Frame] = list(reversed(self._frames))  # never-used frames
        self._hand = 0  # the clock hand
        self._owns_pager = False
        self._closed = False
        # Layer 5 attaches itself here. Two methods, and the pool needs nothing
        # else: `note_dirty(page_id)` to learn what a transaction has touched, and
        # `holds_uncommitted(page_id)` to answer "would writing this page to the
        # data file break the write-ahead rule?".
        self.wal = None

    @classmethod
    def open(
        cls, path: str | os.PathLike[str], capacity: int = DEFAULT_CAPACITY
    ) -> "BufferPool":
        """Open `path` and wrap it in a pool that will close the pager too."""
        pager = Pager(path)
        try:
            pool = cls(pager, capacity)
        except Exception:
            pager.close()
            raise
        pool._owns_pager = True
        return pool

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush every dirty frame, then release the pager. Safe to call twice."""
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

    # ------------------------------------------------------------------
    # the two calls everything above this layer uses
    # ------------------------------------------------------------------

    def fetch_page(self, page_id: int) -> bytearray:
        """Pin `page_id` and return the frame's live buffer.

        The returned `bytearray` *is* the cached page, not a copy: writing to it
        changes what the database will see. Say so with `unpin_page(...,
        dirty=True)` or the change may never reach the disk.

        Every `fetch_page` must be paired with exactly one `unpin_page`.
        """
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
        """Release one pin on `page_id`, recording whether it was modified.

        `dirty` is sticky: once any holder reports a change the frame stays
        dirty until it is written back. It is never cleared by an unpin.
        """
        self._require_open()
        frame = self._table.get(page_id)
        if frame is None:
            raise BufferPoolError(f"page {page_id} is not in the pool")
        if frame.pin_count == 0:
            raise BufferPoolError(f"page {page_id} is not pinned (unbalanced unpin)")
        if dirty and len(frame.data) != PAGE_SIZE:
            # Slice-assigning a different number of bytes to a bytearray resizes
            # it (`page[:4] = b"toolong"` grows the page). Catching that here
            # names the culprit; letting it through would silently truncate or
            # overflow the page on the next write-back.
            raise BufferPoolError(
                f"page {page_id} was resized to {len(frame.data)} bytes; a page "
                f"must stay exactly {PAGE_SIZE} bytes"
            )
        frame.pin_count -= 1
        if dirty:
            self._mark_dirty(frame)

    @contextmanager
    def pinned(self, page_id: int, dirty: bool = False) -> Iterator[bytearray]:
        """`fetch_page` / `unpin_page` as a block, so an exception cannot leak a pin.

        Pass `dirty=True` when the block intends to modify the page. If the block
        raises, the frame is still marked dirty: a half-applied change is a
        change, and the in-memory page no longer matches the disk either way.
        """
        data = self.fetch_page(page_id)
        try:
            yield data
        finally:
            self.unpin_page(page_id, dirty=dirty)

    # ------------------------------------------------------------------
    # allocation
    # ------------------------------------------------------------------

    def new_page(self) -> tuple[int, bytearray]:
        """Allocate a fresh zeroed page, already pinned. Returns `(page_id, data)`."""
        self._require_open()
        page_id = self.pager.allocate_page()
        if page_id in self._table:
            # The pager handed back a page the pool still has cached, which means
            # something freed it behind the pool's back. Fail loudly here rather
            # than let two frames claim one page id.
            raise BufferPoolError(
                f"pager allocated page {page_id}, which is still resident; it "
                f"was freed without going through BufferPool.free_page"
            )
        frame = self._claim_frame()
        # There is nothing worth reading: the page is new, so zero the frame.
        frame.data[:] = bytes(PAGE_SIZE)
        self._install(frame, page_id)
        # A new page starts dirty. With a log attached the pager deliberately does
        # not zero the page on disk, so those zeros exist only in this frame and
        # have to be written like any other change.
        self._mark_dirty(frame)
        return page_id, frame.data

    def free_page(self, page_id: int) -> None:
        """Return `page_id` to the pager's free list and drop any cached copy.

        The cached copy is discarded, not flushed: the page's contents are dead,
        and the pager is about to overwrite the page with a free-list link.
        """
        self._require_open()
        frame = self._table.get(page_id)
        if frame is not None and frame.pin_count > 0:
            raise PinnedPageError(
                f"cannot free page {page_id}: still pinned {frame.pin_count} time(s)"
            )
        if self.wal is not None:
            # Freeing writes a free-list link into the page, which is a change to
            # the data file like any other and may not reach disk ahead of its log
            # record. The log defers it to commit time.
            self.wal.stage_free(page_id)
            return
        if frame is not None:
            self._evict(frame, flush=False)
        self.pager.free_page(page_id)

    # ------------------------------------------------------------------
    # flushing
    # ------------------------------------------------------------------

    def flush_page(self, page_id: int) -> bool:
        """Write `page_id` back if it is dirty. Returns whether a write happened.

        The frame stays resident and keeps its pins; only the dirty flag clears.
        Layer 5 calls this to honour the write-ahead rule.
        """
        self._require_open()
        frame = self._table.get(page_id)
        if frame is None or not frame.dirty:
            return False
        self._write_back(frame)
        return True

    def flush_all(self) -> int:
        """Write every dirty frame and fsync. Returns the number of pages written."""
        written = 0
        for frame in self._frames:
            if frame.page_id != NULL_PAGE_ID and frame.dirty:
                self._write_back(frame)
                written += 1
        if written:
            self.pager.sync()
        return written

    # ------------------------------------------------------------------
    # introspection, mostly for tests and assertions
    # ------------------------------------------------------------------

    def peek_page(self, page_id: int) -> bytearray | None:
        """The cached bytes of `page_id`, or None if it is not resident.

        No pin, no read, no effect on eviction order -- for code that wants to
        look at a page only if looking is free. Layer 5 uses it to log a page's
        image without disturbing the cache.
        """
        frame = self._table.get(page_id)
        return frame.data if frame is not None else None

    def discard_page(self, page_id: int) -> bool:
        """Drop `page_id` from the cache **without writing it**, if it is resident.

        This throws away changes on purpose: it is how a rollback undoes one, by
        forcing the page to be read from the data file again.
        """
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
        """Raise unless every page has been unpinned.

        Call this at the end of any operation that is supposed to be balanced. A
        leaked pin is silent until the pool fills up and starts throwing
        `AllFramesPinnedError` from somewhere unrelated, so it pays to catch it
        at the source.
        """
        leaked = {f.page_id: f.pin_count for f in self._frames if f.pin_count > 0}
        if leaked:
            raise BufferPoolError(f"leaked pins: {leaked}")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
        """Return an empty frame, evicting something if necessary."""
        if not self._free:
            victim = self._find_victim()
            self._evict(victim, flush=True)  # which puts it back on the free list
            self.stats.evictions += 1
        return self._free.pop()

    def _find_victim(self) -> Frame:
        """Clock sweep: the first unpinned frame whose reference bit is clear.

        Two sweeps are enough. The first clears reference bits, so if every
        unpinned frame was referenced, the second one finds a victim.
        """
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
                # Writing this page out would put an uncommitted change in the
                # data file, where a rollback could no longer take it back.
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
        """Empty `frame` and put it back on the free list.

        Both callers rely on that last part: `_claim_frame` pops the frame it just
        emptied, and `free_page` would otherwise strand a frame that holds nothing
        yet is invisible to the free list -- leaving the clock hand to trip over
        it later, a long way from the code that caused it.
        """
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
