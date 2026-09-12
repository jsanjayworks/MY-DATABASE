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

## Layer 5 — Durability (WAL)

Right now a crash mid-allocate can corrupt the file. This is where that stops.

- Log records with LSNs, write-ahead rule (log hits disk before the page does),
  checkpoints, redo on startup.
- **Milestone:** a crash-torture test — a child process writes in a loop and is
  killed at a random moment; on reopen the database is always consistent and
  every acknowledged write is present.
- **Trap:** fsync ordering. The log must be durable *before* the data page, and
  that means two separate fsyncs, not one.

## Layer 6 — Transactions

- `BEGIN` / `COMMIT` / `ROLLBACK`, then isolation. Start with one global lock;
  move to 2PL or MVCC once single-threaded correctness is solid.
- **Milestone:** concurrent transfers between accounts never change the total.
- **Trap:** don't start here. Concurrency bugs on top of a shaky B+Tree are
  almost impossible to diagnose.

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
