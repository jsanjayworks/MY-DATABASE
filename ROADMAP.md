# Roadmap

Seven layers, bottom-up. Each one is finished when its milestone passes, not
when the code looks nice. Tag each completed layer (`git tag layer-1`) so you
can always get back to a working database.

## Layer 1 — Pager ✅

One file becomes an array of 4 KB pages. Allocate, free, read, write, fsync.

- Code: `pydb/pager.py` · Tests: `tests/test_pager.py` · Format: `NOTES.md`
- **Milestone (met):** write a page, kill the process, reopen, data is intact.

## Layer 2 — Buffer pool ✅

Pages stop being copied off disk on every read and start living in memory.

- Code: `pydb/buffer_pool.py` · Tests: `tests/test_buffer_pool.py`
- Frame table, `pin` / `unpin`, dirty flags, clock (second-chance) eviction.
- Nothing above this layer calls `Pager.read_page` directly any more.
- **Milestone (met):** a 100 MB file through a 50-frame pool, every page read
  back correctly, with an eviction counter proving the pages really were
  flushed and re-read.
- **Trap:** evicting a pinned page. Pin counts are the whole point; assert on them.
- **Also bit me:** `page[:3] = b"oops"` *resizes* a `bytearray`. `unpin_page`
  now rejects a frame whose length changed.

## Layer 3 — Records ✅

Rows get a shape: a schema, typed values, and a byte encoding inside a page.

- Code: `pydb/record.py`, `pydb/slotted_page.py`, `pydb/heap.py` ·
  Tests: `tests/test_record.py`, `tests/test_slotted_page.py`, `tests/test_heap.py`
- Types (INT, TEXT, NULL), row encode/decode, slotted-page layout for variable
  length rows, a heap file that scans them.
- **Milestone (met):** 10 000 variable-length rows, reopened through an 8-frame
  pool, scanned back in insertion order.
- **Trap:** deletes leave holes. **Decided: tombstone the slot, compact the page
  lazily when an insert needs the space.** Compaction never moves a slot index,
  so no row id is invalidated. Written up in `NOTES.md`.
- **Also decided:** a row must fit in one page (4080 bytes). No overflow pages.

## Layer 4 — B+Tree ✅

The hard one. It was.

- Code: `pydb/btree_node.py`, `pydb/btree.py` ·
  Tests: `tests/test_btree_node.py`, `tests/test_btree.py`
- Leaf and internal node layouts, search, insert with splits, delete with
  merge/redistribute, sibling pointers for range scans.
- **Milestone (met):** 100 000 random keys inserted, every one verified by
  lookup, range-scanned in sorted order, reopened, then half deleted and
  re-verified — with the invariant checker run over the whole tree at each stage.
- **Traps, as advertised:** the root split changes the root page id, so nothing
  may cache it (`on_root_change` exists for exactly this, and there is a test
  showing what a stale root loses). Merging is tried before borrowing, because
  merging only *removes* a separator from the parent while borrowing rewrites one
  and can need room the parent does not have.
- **Did this, and it paid for itself:** `verify_invariants()` walks the tree
  checking key order, per-subtree key bounds, uniform leaf depth, node fill, and
  that the leaf chain visits exactly the leaves left to right. Called after every
  mutation in the tests.
- **What it caught:** (1) `MAX_CELL_SIZE` and `MIN_USED` cannot be chosen
  independently — with cells up to a quarter of a page, a *split* can leave a half
  below a third full, so the invariant was violated by correct code. Capping a
  cell at an eighth of a page and the threshold at a fifth makes the arithmetic
  work out (see `NOTES.md`). (2) Replacing a value with a shorter one shrinks a
  node exactly as a delete does, so the *insert* path has to be able to rebalance
  too. (3) A buffer pool bug from layer 2: a frame emptied by `free_page` never
  rejoined the free list, which only became reachable once the tree started
  freeing pages.

## Layer 5 — Durability (WAL) ✅

A crash mid-allocate used to be able to corrupt the file. This is where that
stopped.

- Code: `pydb/wal.py` · Tests: `tests/test_wal.py`
- Whole page images rather than byte diffs, so replay is idempotent and recovery
  is one forward pass with no undo phase. LSNs, a commit flag per transaction,
  CRC32 per frame, checkpoints, redo on startup.
- **Milestone (met):** a child process writes batches in a loop and is killed at
  a moment it does not choose, four rounds, each continuing on the database the
  last crash left. On reopen the tree passes `verify_invariants()`, every
  acknowledged batch is present, and every unacknowledged batch is either
  entirely there or entirely absent — never half applied.
- **Trap, as advertised:** fsync ordering. Two fsyncs, log first. One covering
  both would prove nothing.
- **What this forced downstream:** three writes that were quietly going straight
  to the data file had to stop. The pager no longer writes page 0 itself (a torn
  meta page loses everything); freeing a page no longer writes its free-list link
  immediately, because that is a data write like any other; and allocation no
  longer zeroes the page on disk, so a new page is dirty from birth instead.
- **The price, and it is a real one:** no stealing. An uncommitted page may not be
  evicted, or a rollback could not take it back, so a transaction cannot outgrow
  the buffer pool. It raises rather than quietly writing uncommitted data. The
  alternative is undo records as well as redo — a layer of its own.

## Layer 6 — Transactions ✅

- Code: `pydb/database.py` · Tests: `tests/test_database.py`
- `BEGIN` / `COMMIT` / `ROLLBACK` on a `Database` that owns the pager, pool and
  log. Isolation is **one global lock held from begin to commit** — serializable
  by mutual exclusion, and nothing below this layer is thread-safe anyway. 2PL or
  MVCC would need a thread-safe buffer pool first.
- **Milestone (met):** eight threads making 150 random transfers each between 20
  accounts. The total is unchanged, holds across a close and reopen, and a
  transfer that raises mid-way leaves no half-transfer behind.
- **Trap, and it was good advice:** starting here would have been miserable. Every
  failure the concurrency tests produced was a storage bug, not a race — because a
  global lock makes races impossible, which is exactly why it is the right first
  mechanism.
- **What it caught:** a tree caches its root page id in memory, and a rolled-back
  transaction can move it — to a page the rollback then un-allocated. The durable
  meta slot is the truth; the in-memory copy has to be re-read from it after every
  rollback.
- **Cost, stated plainly:** no two transactions ever run at once, readers
  included.

## Layer 7 — SQL

- Tokenizer → parser → AST → planner → executor, plus a REPL and a catalog
  table describing the user's tables.
- Start with exactly: `CREATE TABLE`, `INSERT`, `SELECT ... WHERE`. Add `ORDER
  BY`, joins, and aggregates only after those work end to end.
- **Milestone:** a REPL session that creates a table, inserts rows, restarts the
  process, and queries them back.

## Ordering advice

Build a throwaway vertical slice early — insert one hard-coded row through a
fake "SQL" call and read it back — so you have seen the whole path work before
you invest weeks in any single layer.
