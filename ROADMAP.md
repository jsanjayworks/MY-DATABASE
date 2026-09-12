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

## Layer 7 — SQL ✅

- Code: `pydb/catalog.py`, `pydb/sql/` (`tokenizer`, `nodes`, `parser`, `planner`,
  `engine`), `pydb/repl.py` ·
  Tests: `tests/test_catalog.py`, `tests/test_sql.py`, `tests/test_repl.py`
- Tokenizer → parser → AST → planner → executor, a REPL (`python -m pydb my.db`),
  and a catalog stored in the database it describes.
- Started with exactly `CREATE TABLE`, `INSERT`, `SELECT ... WHERE`, then added
  `ORDER BY`, `LIMIT`/`OFFSET`, `UPDATE`, `DELETE`, `DROP TABLE` and
  `BEGIN`/`COMMIT`/`ROLLBACK` once those worked end to end. No joins, no
  aggregates.
- **Milestone (met):** `python -m pydb` run three times over one file — create and
  insert, restart and query, restart and modify. Also 1000 rows inserted in one
  transaction through the REPL and read back by a second process.
- **Where layer 4 pays off:** a `PRIMARY KEY` gets a B+Tree index, and the planner
  uses it for `=` (an index lookup) and for `<` `<=` `>` `>=` (an index range),
  including when the condition is one half of an `AND`. An `OR` falls back to a
  scan, because the index would miss the rows matching its other half. Measured on
  a 4000-row table with a 16-frame pool: **1 page read through the index, 30 for
  the equivalent scan.**
- **Worth knowing about `WHERE`:** comparisons use SQL's three-valued logic, so a
  row whose column is NULL satisfies neither `age = 41` nor `age != 41`. There is a
  test asserting exactly that, because it looks like a bug until you remember it
  is the specification.
- **What it caught:** `Table.verify()` — the counterpart to the tree's invariant
  check — asserts the heap and the index agree about every row. An insert that
  updates one and not the other gives a table that answers the same query
  differently depending on which plan runs, which is close to undiagnosable any
  other way.

## Beyond the roadmap

The seven layers were the plan; these went in afterwards, because a database
without them is a toy:

- **Joins** — INNER, LEFT and CROSS, with aliases and qualified column names.
  Executed as a left-deep nested loop, and when the inner table's join column is
  indexed the planner turns it into an **index nested-loop join**: one seek per
  outer row instead of a full inner scan. On a 40-row table joined to a 4000-row
  one that is measured as an order of magnitude fewer pages visited; on 1000 rows
  it was 0.06s against 6.9s.
- **Aggregates** — `COUNT` (including `COUNT(*)` and `COUNT(DISTINCT x)`), `SUM`,
  `AVG`, `MIN`, `MAX`, with `GROUP BY`, `HAVING` and `DISTINCT`. `COUNT(*)` of
  nothing is 0 and every other aggregate of nothing is NULL, which is the
  specification and looks like a bug.
- **Secondary indexes** — `CREATE [UNIQUE] INDEX` / `DROP INDEX`, any number per
  table, used by the planner for equality, ranges and join probes. A non-unique
  index appends the row id to the key and escapes the value part so that one
  value's keys cannot be confused with a longer value's; the escaping is in
  `NOTES.md` and is the most interesting ten lines in the layer.
- **`EXPLAIN`** — in front of any query, printing the access paths and the pipeline
  above them. The access-path tests assert on it, because a query that returns the
  right rows the slow way is still broken.
- **Page reclamation** — `DROP TABLE` and `DROP INDEX` now free their pages, in
  batches, and `VACUUM` compacts the dead space out of heap pages.

### What it found, one layer down

A `DROP TABLE` freeing hundreds of pages surfaced a layer 5 bug that nothing else
could reach: committing a free left the page on the pager's free list **while its
frame was still in the buffer pool**, so the next allocation handed out a page id
the pool already held. The fix is three lines; finding it took the guard added back
in layer 2 for a different reason.

## Where this stops

The honest list of what a real database has that this one still does not:

- **Subqueries and set operations.** No `IN (SELECT ...)`, no `UNION`.
- **Overflow pages.** A row is capped at 4080 bytes and a tree value at ~496, which
  also caps a table definition at roughly 24 columns.
- **Real isolation.** One global lock, so no two transactions ever overlap. 2PL or
  MVCC needs a thread-safe buffer pool first.
- **Undo logging.** Redo only, which is why an uncommitted page may not be evicted
  and a transaction cannot outgrow the buffer pool.
- **A cost-based optimiser.** The planner prefers an index whenever one applies and
  joins in the order written. With no statistics it cannot know that scanning a
  four-page table beats descending a tree, and there is a test documenting exactly
  that case.
- **Composite keys and indexes.** One column each.
- **Floating point, dates, and NULL ordering options.** INT and TEXT; NULLs sort
  first, always.

## Ordering advice

Build a throwaway vertical slice early — insert one hard-coded row through a
fake "SQL" call and read it back — so you have seen the whole path work before
you invest weeks in any single layer.

## What the layers taught, in one place

Bottom-up was right, and the reason is narrower than "it is tidy": **every bug
found in an upper layer turned out to live in a lower one**, and it was findable
because the lower one was already trusted.

- The buffer pool had a frame that `free_page` emptied without returning it to the
  free list. Nothing freed pages until layer 4 did, and the symptom appeared in
  eviction, pages away from the cause.
- The B+Tree's fill invariant was violated by *correct* code, because
  `MAX_CELL_SIZE` and `MIN_USED` had been chosen independently when they are a
  pair. Writing the invariant down as an assertion is what turned that from a
  vague unease into arithmetic.
- Layer 5 found three places where a write was reaching the data file without a log
  record in front of it — and all three were in layers 1 and 2, which had looked
  finished for days.
- Layers 6 and 7 found the same class of bug three times: **after a rollback,
  anything derived from the file has to be derived again.** A cached root page id,
  a cached page chain, a cached table definition.

The two habits that paid for themselves many times over: an invariant checker per
structure, called after every mutation in tests, and closing the file and reopening
it in any test that claims something was stored.
