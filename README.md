# pydb: a SQL database from scratch in Python

[![tests](https://github.com/jsanjayworks/MY-DATABASE/actions/workflows/tests.yml/badge.svg)](https://github.com/jsanjayworks/MY-DATABASE/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

A relational database written from the ground up in pure Python, using only the
standard library: its own file format, page cache, B+Tree indexes, write-ahead log,
transactions and SQL engine.

I built it to understand what really happens between `INSERT` and the bytes on
disk. Speed was never the goal; being able to explain every byte in the file was.

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

## Features

- **Storage engine:** one database file split into 4 KB pages, with a free list
  for reusing space and a buffer pool that keeps hot pages in memory (clock
  eviction).
- **B+Tree indexes:** splits, merges and range scans. The tests run an invariant
  checker over the whole tree after every change.
- **Crash safety:** a write-ahead log of page images with CRC32 checksums and
  checkpoints. A crash at any moment loses nothing that was committed and leaves
  nothing half-written.
- **Transactions:** `BEGIN`, `COMMIT` and `ROLLBACK`, serializable through a global
  lock. A statement that fails inside a transaction is rolled back on its own.
- **SQL:** tables and indexes (`CREATE`, `DROP`, `UNIQUE`), `INSERT`, `UPDATE`,
  `DELETE`, and `SELECT` with `WHERE`, `JOIN` (inner, left, cross), `GROUP BY`,
  `HAVING`, `DISTINCT`, `ORDER BY`, `LIMIT`/`OFFSET`, and `COUNT`, `SUM`, `AVG`,
  `MIN`, `MAX`.
- **Query planner:** uses index seeks and range scans instead of full scans, and
  turns joins on an indexed column into index nested-loop joins. `EXPLAIN` shows
  the chosen plan.

## How it works

Seven layers, each built on the one below it and usable on its own:

| Layer | Module | What it does |
|---|---|---|
| 7. SQL | `sql/`, `catalog.py`, `repl.py` | tokenizer → parser → planner → executor; table definitions stored in the database itself; the interactive shell |
| 6. Transactions | `database.py` | begin, commit, rollback, statement savepoints, one global lock |
| 5. Write-ahead log | `wal.py` | page-image log, commit records, checkpoints, crash recovery |
| 4. B+Tree | `btree.py`, `btree_node.py` | ordered index with splits, merges and range scans |
| 3. Records | `record.py`, `slotted_page.py`, `heap.py` | typed rows, slotted pages, heap files |
| 2. Buffer pool | `buffer_pool.py` | page cache with pinning, dirty tracking and clock eviction |
| 1. Pager | `pager.py` | the file as an array of 4 KB pages, allocation, free list, file lock |

Each layer is tagged in git, so `git checkout layer-3` gives a working database
with no index and no SQL yet.

The byte-level file format is documented in [NOTES.md](NOTES.md). The design
decisions, trade-offs and bugs found along the way are in [ROADMAP.md](ROADMAP.md).

## Quick start

Requires Python 3.10 or newer. There is nothing to install.

```sh
git clone https://github.com/jsanjayworks/MY-DATABASE.git
cd MY-DATABASE
python -m pydb my.db
```

Type `.help` in the shell to see its commands.

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

Every error pydb raises on purpose (bad SQL, a duplicate key, a NOT NULL column,
a file already in use) is a `pydb.PydbError`, whichever layer found it.

The lower layers work on their own too:

```python
from pydb import BTree, BufferPool, Wal

pool = BufferPool.open("my.db", capacity=128)
wal = Wal(pool)                      # replays anything a crash left behind
tree = BTree.create(pool)
tree.put(b"key", b"value")
wal.commit()                         # durable here, and not before
tree.verify_invariants()
```

## SQL reference

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
parentheses, with SQL's three-valued logic: a NULL column satisfies neither
`age = 41` nor `age != 41`. Aggregates are `COUNT` (including `COUNT(*)` and
`COUNT(DISTINCT x)`), `SUM`, `AVG`, `MIN` and `MAX`.

Indexes are used for equality, for ranges and for join probes. Measured: a join
over 1000 rows takes **0.06s with an index against 6.9s without**, and a single
lookup in a 4000-row table reads **1 page instead of 30**.

## Testing

```sh
python -m unittest discover -s tests -v
```

505 tests, run on Linux, Windows and macOS with Python 3.10 and 3.14 on every
push. The heaviest ones:

- a 100 MB file driven through a 50-frame buffer pool;
- 100,000 random keys inserted into the B+Tree, verified, range-scanned, half
  deleted and verified again;
- a child process killed mid-write four times, checking after each crash that
  every acknowledged write survived and no half-written transaction did;
- eight threads transferring money between accounts, where the total never
  changes;
- a join measured both ways over the same data, asserting the indexed plan visits
  an order of magnitude fewer pages.

## Limitations

pydb is a learning project, not a production database. It leaves out:

- subqueries, `UNION`, arithmetic in expressions (`n + 1`), `LIKE`, `IN` and
  `BETWEEN`;
- types other than `INT` and `TEXT`, and composite keys;
- concurrent transactions: one runs at a time;
- rows larger than one page (about 4 KB);
- more than one process using the same database file at once.

The full list, with the reasons, is in [ROADMAP.md](ROADMAP.md#where-this-stops).

## What I learned

- Most bugs that show up in a higher layer actually live in a lower one.
- A checker that verifies a data structure after every change finds bugs that
  ordinary tests miss.
- Crash safety is about ordering: write the log, flush it to disk, and only then
  touch the data file.
- After a rollback, anything cached from the file has to be read from it again.

## Project layout

```
pydb/
  errors.py                 PydbError, underneath every layer's own errors
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
  repl.py          layer 7  the shell
tests/             one file per module
```

## Acknowledgements

Built while following Code With Sep's
[*Write a database from scratch*](https://www.youtube.com/watch?v=HHO2K23XxbM)
series.

## License

MIT. See [LICENSE](LICENSE).
