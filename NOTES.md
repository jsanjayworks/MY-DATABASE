# On-disk format

The single source of truth for byte offsets. Update this *before* changing code —
future-you will not remember why byte 12 is what it is.

## Constants

| Name | Value |
|------|-------|
| Page size | 4096 bytes |
| Byte order | big-endian (`>` in `struct`) |
| Magic | `PYDBFILE` (8 bytes) |
| Format version | 1 |

Every integer on disk is unsigned big-endian. Big-endian is chosen only because
hex dumps read left-to-right in the same order as the number.

## Page 0 — the meta page

Page 0 is reserved for database metadata and is never handed out by the
allocator. Because page id 0 can never be a real page, **0 doubles as the NULL
page id** throughout the codebase.

| Offset | Type | Field | Meaning |
|--------|------|-------|---------|
| 0  | `8s` | magic | `PYDBFILE`; wrong value means "not our file" |
| 8  | `H`  | version | format version, currently 1 |
| 10 | `H`  | page_size | 4096; guards against opening a file written with a different page size |
| 12 | `I`  | page_count | total pages that exist in the file, including page 0 |
| 16 | `I`  | free_list_head | page id of the first free page, or 0 if none |
| 20 | `8Q` | slots | eight 8-byte slots for the layers above; 0 means unset |

The slots are where a layer puts a page id that *moves*. Slot 0 holds the root
page id of the top-level B+Tree (layer 7's catalog); slots 1–7 are unclaimed. The
registry lives in `pager.py` so that two layers cannot claim the same slot.

Bytes 84..4095 of the meta page are reserved (zero).

## The lock

One pager per file, at once. A second would keep its own copy of the meta page
and its own cached pages, and whichever wrote last would silently overwrite the
other's commits. That is not a hypothetical: before the lock, two `Database`
objects on one file inserted 900 rows between them and 600 survived. Opening a
file that is already open now raises `FileInUseError`.

- **Windows:** byte `2**32 * 4096` is locked with `msvcrt.locking`. Windows locks
  are mandatory -- a locked byte cannot be read through another handle -- so the
  lock sits on a byte no page can occupy: page ids are four bytes, so no file is
  longer than 2³² pages. Nothing is written there; locking past the end of a file
  is allowed. SQLite locks bytes past its data for the same reason.
- **POSIX:** `flock` on the whole file, and specifically not `fcntl`/`lockf`.
  Those locks belong to the process, so a second open in the same process would
  be granted the lock, and closing either descriptor would drop it for both.

The OS releases either kind when a process dies, so a crash never strands it.

## Free pages

A freed page is not returned to the OS; it joins a singly-linked free list
threaded through the pages themselves.

| Offset | Type | Field |
|--------|------|-------|
| 0 | `I` | next free page id, or 0 for end of list |

The list is LIFO: freeing pushes onto the head, allocating pops from it. This
means allocation reuses the most recently freed page, which is the one most
likely to still be in the OS page cache.

## Heap pages (layer 3)

A table is a singly-linked chain of heap pages. Each one is a *slotted page*: a
header, then a slot array growing forward, then free space, then row bytes
growing backward from the end of the page.

```
+--------+----------------+--------------------+---------------------+
| header | slot array --> |     free space     | <-- rows (data)     |
+--------+----------------+--------------------+---------------------+
0        12                                                      4096
```

| Offset | Type | Field | Meaning |
|--------|------|-------|---------|
| 0  | `B` | page_type | 1 = heap page. A zeroed page reads as 0, which is rejected |
| 1  | `B` | reserved | zero |
| 2  | `H` | slot_count | slots that exist, tombstones included |
| 4  | `H` | free_end | offset of the lowest row byte; free space is `free_start`..`free_end` |
| 6  | `H` | live_count | slots that are not tombstones |
| 8  | `I` | next_page | next page in the chain, 0 for the end |

`free_start` is not stored: it is always `12 + 4 * slot_count`.

Each slot is `>HH` — `(offset, length)` — so the slot array starts at byte 12 and
a row id is `(page_id, slot_index)`.

**A slot with offset 0 is a tombstone.** Offset 0 is inside the header, so it can
never be a real row, which means deletion needs no extra flag byte.

## Rows (layer 3)

| Part | Size | Contents |
|------|------|----------|
| null bitmap | `ceil(columns / 8)` | bit *i* set = column *i* is NULL |
| values | variable | every non-NULL column, in schema order |

A NULL costs one bit and is otherwise absent from the row, so a row of nulls is
one byte. Value encodings:

| Type | Encoding |
|------|----------|
| INT | `>q`, 8-byte signed |
| TEXT | `>H` byte length, then UTF-8 |

Text length is counted in **bytes, not characters**.

### Two decisions worth writing down

**Deletes tombstone, they do not compact.** `delete` clears the slot and leaves
the row's bytes where they are; the space comes back only when the page needs it
and `compact()` squeezes the live rows together. Compaction rewrites row bytes
but never slot indices, which is precisely why rows are addressed by slot: no row
id in the database is invalidated by it. A tombstoned slot *is* reused by a later
insert on that page, so a row id is only meaningful while its row is alive — same
contract as a pointer before a free.

**A row must fit in one page**: 4096 − 12 (header) − 4 (one slot) = **4080 bytes**.
There are no overflow pages, so a single oversized TEXT value is an error rather
than a chain of continuation pages. Overflow pages would be a layer of their own.

## B+Tree nodes (layer 4)

Physically the same idea as a heap page — a pointer array growing forward, cell
bytes growing backward — with one crucial difference: **the pointer array is in
key order**. Inserting in the middle of a node shifts a few bytes of pointer
array, not kilobytes of cells.

| Offset | Type | Field | Meaning |
|--------|------|-------|---------|
| 0  | `B` | page_type | 2 = internal, 3 = leaf |
| 1  | `B` | reserved | zero |
| 2  | `H` | cell_count | cells in this node |
| 4  | `H` | cell_start | offset of the lowest cell byte |
| 6  | `H` | frag_bytes | dead bytes inside the cell area, reclaimed by defragmenting |
| 8  | `I` | extra | **leaf:** next leaf page id, 0 at the end. **internal:** leftmost child |

Each pointer is `>HH` — `(offset, length)`. Cells:

| Node | Cell contents |
|------|---------------|
| leaf | `>H key_len`, key, value (the value is the rest of the cell) |
| internal | `>H key_len`, key, `>I child_page_id` |

Both start with a length-prefixed key, so key comparison does not care which
kind of node it is reading.

An internal node with *n* cells has *n + 1* children, and the ordering invariant
is:

```
child_at(0) < cells[0].key <= child_at(1) < cells[1].key <= child_at(2) ...
```

so a separator key is the **smallest key of the subtree to its right**, and a
search for a key equal to a separator goes right.

### Two constants that have to agree

| Name | Value | Meaning |
|------|-------|---------|
| `MAX_CELL_SIZE` | 506 | an eighth of a page, minus the pointer |
| `MIN_USED` | 816 | a fifth of a page: below this a non-root node is *underfull* |

These are a pair, not two independent knobs. A node splits only when a cell will
not fit, so the cells being divided total more than the 4084 usable bytes; the
cut lands on a cell boundary at or just past the halfway mark, which leaves the
smaller half above `4085/2 − 506 − 506 ≈ 1022` bytes (an internal split also
gives a cell away to its parent, hence subtracting twice). Any threshold below
that is one a fresh split can never violate — which is what makes "no non-root
node is underfull" an invariant `verify_invariants()` can actually assert.

The same cap makes rebalancing always possible: `4084 − 816` is far more than one
cell, so a node below the threshold always has room for a whole cell from its
sibling.

Consequence: **a tree value is capped at ~496 bytes.** That is deliberate. The
tree stores keys and row ids; a real value lives in the heap.

### Keys are bytes

The tree compares keys with plain byte comparison, so typed values are encoded to
sort correctly (`record.encode_key`):

| Type | Key encoding |
|------|--------------|
| INT | big-endian 8 bytes with the sign bit flipped (`value + 2^63`) |
| TEXT | UTF-8 |

The sign flip is the interesting one: without it, −1 (`0xFF...`) would sort above
1. Big-endian matters too — comparison has to start at the most significant byte.

### The root page id moves

Splitting the root allocates a new one, and collapsing an underfull root frees
it, so **the root page id is not stable**. Nothing may cache it: `BTree` reports
every change through `on_root_change`, and whatever owns the tree writes the new
id somewhere durable. A tree reopened with a stale root id is silently missing
most of itself.

## The write-ahead log (layer 5)

A second file, `<database>.wal`, holding **whole page images**. Fatter than
logging individual byte changes, but replay is idempotent, so recovery is one
forward pass with no undo phase. SQLite's WAL makes the same choice.

### Header (24 bytes)

| Offset | Type | Field |
|--------|------|-------|
| 0  | `8s` | magic `PYDBWAL\0` |
| 8  | `H`  | version, currently 1 |
| 10 | `H`  | page_size |
| 12 | `I`  | reserved |
| 16 | `Q`  | first LSN in this generation |

The log is emptied at every checkpoint, and `first_lsn` is how LSNs keep climbing
across those generations instead of restarting.

### Frames (20 + 4096 bytes each)

| Offset | Type | Field |
|--------|------|-------|
| 0  | `Q` | LSN |
| 8  | `I` | page id |
| 12 | `I` | CRC32 of `(lsn, page_id, flags, image)` |
| 16 | `B` | flags: bit 0 = **commit** |
| 17 | `3x` | padding |
| 20 | 4096 bytes | the page image |

The checksum covers the header fields as well as the image, so a torn *header* is
caught too. The commit flag marks the last frame of a transaction: frames after
the final commit flag belong to a transaction that never finished, and recovery
discards them. That is where atomicity comes from.

### The order of a commit

The order *is* the algorithm:

1. deferred page frees are applied **in memory**, so the freed pages and the meta
   page get logged rather than sneaking out on their own;
2. one frame per modified page is appended, the last flagged `commit`, and the log
   is **fsynced**. The transaction is durable at this instant and no earlier;
3. the modified pages are written to the data file, *unsynced* — the log already
   guarantees them;
4. the deferred meta page is written, for the same reason.

Two fsyncs, log first. One fsync covering both would prove nothing.

### What this changed underneath

- **The pager stops writing page 0 itself.** With a log attached, `defer_meta`
  makes meta changes accumulate in memory and the log decides when they are safe.
  A torn meta page is one of the few ways to lose an entire database at once, so
  page 0 is logged like everything else.
- **Freeing a page is deferred to commit.** `free_page` writes a free-list link
  *into* the freed page, which is a data-file write like any other and may not go
  out ahead of its log record. `Pager.stage_free` does it to an in-memory image
  instead.
- **Allocation no longer zeroes the page on disk**, because that would be another
  unlogged write. A new page is dirty from birth instead, so its zeros are written
  from its frame with everything else.

### Recovery

Read frames forward until one is short, fails its checksum, or breaks the LSN
sequence — all three mean "the crash was here". Apply every frame up to the last
commit flag, fsync the data file, then empty the log. Applying a frame twice is
harmless, so a crash *during recovery* is fine too.

### The price: no stealing

An uncommitted page may not be evicted from the buffer pool, because once it is in
the data file a rollback can no longer take it back. So **a transaction cannot
outgrow the buffer pool** — it raises `AllFramesPinnedError` instead. The
alternative is writing undo records as well as redo, which is a layer of its own.
Rollback is then simply forgetting: drop the modified frames and let them be read
from the data file again.

### Rolling back one statement

Forgetting only works for the whole transaction. A failed statement inside
`BEGIN ... COMMIT` must be undone on its own -- an `UPDATE` that hits a duplicate
key on its third row must not leave the first two changed for `COMMIT` to keep --
and a page an earlier statement dirtied exists in its pre-statement form nowhere
but in its frame, which the failing statement has just written over.

So each statement inside a transaction starts with a savepoint: a copy of every
page the transaction has dirtied so far, how many frees are staged, and the meta
state. Undoing to it drops the pages first dirtied since (the data file still has
them as committed), copies the rest back, and re-derives cached root ids and
table definitions exactly as a full rollback does. No-steal is what keeps the
copy cheap: every uncommitted page is resident, and there are at most as many as
the pool has frames. A statement run on its own needs none of this; its
transaction is the statement.

### Still true after a crash, and not before

The layer 1 note below is now out of date in one respect: a crash mid-allocate no
longer corrupts anything, because the meta page is in the log. What remains true
is that **a page freed by a transaction can leak** if the crash lands in the
narrow window after the commit fsync — the page is unreachable and unreclaimed,
which costs space and never correctness.

## The catalog (layer 7)

A database describes itself. Table definitions are rows in a B+Tree whose root
lives in **meta slot 0**. That tree holds two namespaces, separated by a one-byte
key prefix:

| Key | Value |
|-----|-------|
| `t` + table name | the table definition below |
| `i` + index name | the name of the table it belongs to |

The second exists so `DROP INDEX by_age` can find which table to look in; index
names are database-wide, as they are in SQLite. Listing the tables is a range scan
over the `t` prefix, which is why they come back sorted.

### A table definition

| Offset | Type | Field |
|--------|------|-------|
| 0 | `I` | first page of the row heap |
| 4 | `H` | column count |
| 6 | ... | per column: `>H` name length, UTF-8 name, `>B` type, `>B` nullable |
| … | `H` | index count |
| … | ... | per index: `>H` name length, name, `>H` column, `>B` flags, `>I` root page |

Index flags are `1` for unique and `2` for primary key. A primary key is not a
special case: it is an index that happens to be flagged as one.

Because a catalog row is a tree value, it is capped at ~496 bytes — so roughly **24
columns plus a few indexes per table**, depending on name lengths. Over that,
`encode` raises rather than truncating. A real database spreads the definition over
several rows.

Each index's root page id is recorded *here* rather than in a meta slot, because
there is one per index. When an index splits its root, the tree's `on_root_change`
writes the new page id into this row — inside the current transaction, so a
rollback takes it back.

## Index keys (layer 7)

A **unique** index stores `encode_key(value) -> row id`, and that is the whole
story.

A **non-unique** index cannot, because two rows may share a value and a B+Tree
holds each key once. So the row id is appended to the key, which makes it unique
again. Naively that breaks TEXT: `encode_key("ab")` is a prefix of
`encode_key("abc")`, so a range over "keys starting with ab" would sweep up the
rows for "abc" too.

The value part is therefore **escaped** first:

```
every 0x00 byte -> 0x00 0xFF,  then terminate with 0x00 0x00
```

No escaped value can be a prefix of another, and the escaping preserves order,
because the terminator `00 00` compares below both `00 FF` and any other byte. That
makes a point lookup an exact prefix range and a `>` bound exact rather than
approximate. The cost is a few bytes per key, paid only by non-unique indexes.

**Rows whose indexed column is NULL are not in the index at all.** That is safe for
exactly the reason three-valued logic exists: no condition an index is used for can
be true of NULL. It also means a unique index permits any number of NULLs, which is
what SQL says it should.

## Dropping and vacuuming (layer 7)

`DROP TABLE` and `DROP INDEX` free every page they owned, in **batches, each its
own transaction** — a page is dirtied when it is freed (the free-list link is
written into it), so freeing a large table in one transaction would hit the buffer
pool's no-steal limit. A crash part-way through leaves the remainder unreclaimed: a
space leak, never a correctness problem.

`VACUUM` compacts every heap page, squeezing out the dead space that tombstoned
rows leave behind. It does not move rows between pages or rebuild indexes.

## Durability

`page_count` and `free_list_head` live on the meta page, so every allocate and
free rewrites page 0. On its own that is not crash-safe — a crash between writing
a page and writing the meta page can leak or double-allocate one — which is why
**a pager with a log attached stops writing page 0 itself**. Opened without a log
(`Pager` alone, as in its own tests) the old behaviour stands and a crash
mid-write can leave the file inconsistent.

With the log, the one thing a crash can still cost is a *leaked* page: a page
freed by a transaction, where the crash lands in the narrow window after the
commit fsync. The page is unreachable and unreclaimed. That costs space, never
correctness.
