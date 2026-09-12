# MY-DATABASE

A SQL database written from scratch in Python, no third-party dependencies —
built while following [Code With Sep's *Write a database from scratch*](https://www.youtube.com/watch?v=HHO2K23XxbM).

The point is not to be fast. The point is that every byte on disk is one I put
there and can explain.

```
$ python -m pydb my.db
pydb> CREATE TABLE people (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);
CREATE TABLE people
pydb> INSERT INTO people VALUES (1, 'ada', 36), (2, 'bob', 41), (3, 'cy', NULL);
INSERT 3
pydb> CREATE TABLE books (id INT PRIMARY KEY, author INT, title TEXT NOT NULL);
CREATE TABLE books
pydb> INSERT INTO books VALUES (10, 1, 'notes on the engine'), (11, 2, 'a novel');
INSERT 2
pydb> SELECT name, age FROM people WHERE age IS NOT NULL ORDER BY age DESC;
name  age
----  ---
bob   41
ada   36
(2 rows)
pydb> SELECT p.name, COUNT(b.id) AS books FROM people p
  ...>   LEFT JOIN books b ON b.author = p.id
  ...>   GROUP BY p.name ORDER BY books DESC, p.name;
name  books
----  -----
ada   1
bob   1
cy    0
(3 rows)
pydb> EXPLAIN SELECT p.name, b.title FROM books b JOIN people p ON p.id = b.author;
plan
----------------------------------------------------
scan b
INNER join probe p using people_pkey (id = b.author)
project name, title
(3 rows)
```

## Status

All seven layers are done, and each one's milestone passes.

| Layer | | |
|-------|---|---|
| 1. Pager | ✅ | pages, allocation, free list, fsync |
| 2. Buffer pool | ✅ | frames, pin/unpin, dirty write-back, clock eviction |
| 3. Records | ✅ | schemas, row codec, slotted pages, heap files |
| 4. B+Tree | ✅ | ordered index, splits, merges, range scans |
| 5. WAL / recovery | ✅ | page-image log, commit/rollback, checkpoints, replay |
| 6. Transactions | ✅ | begin/commit/rollback, one global lock |
| 7. SQL engine | ✅ | tokenizer, parser, planner, executor, catalog, REPL |

Then, past the roadmap: joins, aggregates, secondary indexes, `EXPLAIN`, and page
reclamation. See [ROADMAP.md](ROADMAP.md#beyond-the-roadmap).

Each layer is tagged, so `git checkout layer-3` is a working database with no
index and no SQL.

See [ROADMAP.md](ROADMAP.md) for what each layer involved, which traps it walked
into, and what it deliberately does not do. The on-disk byte layout lives in
[NOTES.md](NOTES.md).

## What it can do

```sql
CREATE TABLE [IF NOT EXISTS] name (col INT|TEXT [NOT NULL] [PRIMARY KEY], ...)
DROP TABLE [IF EXISTS] name
CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table (col)
DROP INDEX [IF EXISTS] name

INSERT INTO name [(cols)] VALUES (...), (...)
UPDATE name SET col = value, ... [WHERE ...]
DELETE FROM name [WHERE ...]
VACUUM

SELECT [DISTINCT] * | expr [AS alias], ...
  FROM table [alias] [[INNER | LEFT [OUTER] | CROSS] JOIN table [alias] ON expr]...
  [WHERE expr] [GROUP BY expr, ...] [HAVING expr]
  [ORDER BY expr | position [ASC|DESC], ...] [LIMIT n [OFFSET n]]

BEGIN | COMMIT | ROLLBACK
EXPLAIN <any query>
```

`WHERE` supports `= != <> < <= > >=`, `AND`, `OR`, `NOT`, `IS [NOT] NULL` and
parentheses, with SQL's three-valued logic — a NULL column satisfies neither
`age = 41` nor `age != 41`. Aggregates are `COUNT` (including `COUNT(*)` and
`COUNT(DISTINCT x)`), `SUM`, `AVG`, `MIN` and `MAX`.

Indexes are used for equality, for ranges, and for **join probes**: when the inner
table of a join has an index on its join column, the planner seeks into it once per
outer row instead of scanning it once per outer row. Measured on 1000 rows: **0.06s
against 6.9s.** A single lookup on a 4000-row table with a 16-frame buffer pool is
**1 page read against 30** for the equivalent scan. `EXPLAIN` shows which path was
chosen.

No subqueries, no `UNION`, no composite keys. `INT` and `TEXT` only.

## Requirements

Python 3.10+ (uses `X | Y` type syntax). Nothing else — `struct`, `os.fsync` and
`zlib.crc32` are the entire toolkit.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

489 tests, about 40 seconds. Several are deliberately heavy, because the
milestones are:

- a 100 MB file driven through a 50-frame buffer pool;
- 100 000 random keys inserted into the B+Tree, verified by lookup, range-scanned,
  half deleted, re-verified;
- a child process killed mid-write, four times, checking after each that every
  acknowledged write survived and no half-written transaction did;
- eight threads transferring between accounts, where the total never changes;
- a join measured two ways over the same data, asserting the indexed plan visits an
  order of magnitude fewer pages than the scan.

## Using it as a library

```python
from pydb import Database, Engine

with Database("my.db") as db:
    sql = Engine(db)
    sql.execute("CREATE TABLE people (id INT PRIMARY KEY, name TEXT)")
    with db.transaction():                    # all of it, or none of it
        sql.execute("INSERT INTO people VALUES (1, 'ada')")
        sql.execute("INSERT INTO people VALUES (2, 'bob')")
    print(sql.execute("SELECT name FROM people ORDER BY id").rows)
```

Every layer is usable on its own, which is how they were tested:

```python
from pydb import BTree, BufferPool, Wal

pool = BufferPool.open("my.db", capacity=128)
wal = Wal(pool)                      # replays anything a crash left behind
tree = BTree.create(pool)
tree.put(b"key", b"value")
wal.commit()                         # durable here, and not before
tree.verify_invariants()
```

## Layout

```
pydb/
  pager.py         layer 1  one file as an array of 4 KB pages
  buffer_pool.py   layer 2  those pages cached, with pins and clock eviction
  record.py        layer 3  schemas, row encoding, order-preserving keys
  slotted_page.py  layer 3  variable-length rows inside a fixed page
  heap.py          layer 3  a table as a chain of slotted pages
  btree_node.py    layer 4  one node's bytes
  btree.py         layer 4  search, split, merge, range scan
  wal.py           layer 5  the write-ahead log and recovery
  database.py      layer 6  transactions and the global lock
  catalog.py       layer 7  tables and indexes, stored in the database
  sql/             layer 7  tokenizer -> parser -> planner -> engine
                             (joins, aggregates, EXPLAIN)
  repl.py          layer 7  the shell
tests/             one file per module, and every milestone
```
