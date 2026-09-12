# Roadmap

Seven layers, bottom-up. Each one is finished when its milestone passes, not
when the code looks nice. Tag each completed layer (`git tag layer-1`) so you
can always get back to a working database.

## Layer 1 — Pager ✅

One file becomes an array of 4 KB pages. Allocate, free, read, write, fsync.

- Code: `pydb/pager.py` · Tests: `tests/test_pager.py` · Format: `NOTES.md`
- **Milestone (met):** write a page, kill the process, reopen, data is intact.

## Layer 2 — Buffer pool

Pages stop being copied off disk on every read and start living in memory.

- Frame table, `pin` / `unpin`, dirty flags, LRU (or clock) eviction.
- Nothing above this layer calls `Pager.read_page` directly any more.
- **Milestone:** operate on a 100 MB file with a 50-frame pool, correct results,
  and an eviction counter proving pages really were flushed and re-read.
- **Trap:** evicting a pinned page. Pin counts are the whole point; assert on them.

## Layer 3 — Records

Rows get a shape: a schema, typed values, and a byte encoding inside a page.

- Types (INT, TEXT, NULL), row encode/decode, slotted-page layout for variable
  length rows, a heap file that scans them.
- **Milestone:** insert 10 000 rows, reopen, scan them all back in order.
- **Trap:** deletes leave holes. Decide now whether you compact or tombstone,
  and write it in `NOTES.md`.

## Layer 4 — B+Tree

The hard one. Budget more time than the previous three layers combined.

- Leaf and internal node layouts, search, insert with splits, delete with
  merge/redistribute, sibling pointers for range scans.
- **Milestone:** insert 100 000 random keys, verify every one by lookup, then
  range-scan and confirm sorted order; delete half and re-verify.
- **Traps:** the root split (the root is the only node allowed to be underfull,
  and splitting it changes the root page id); off-by-one in the split point.
- **Do this:** write a `verify_invariants()` that walks the whole tree checking
  key order, node fill, and parent/child links. Call it after every operation in
  tests. It will save you days.

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
