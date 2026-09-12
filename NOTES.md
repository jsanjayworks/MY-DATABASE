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

Bytes 20..4095 of the meta page are reserved (zero). Future layers claim space
here: the B+Tree root page id, the catalog root, the WAL checkpoint LSN.

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

## Durability

`page_count` and `free_list_head` live on the meta page, so every allocate and
free rewrites page 0. Right now that write is not crash-safe: a crash between
writing a page and writing the meta page can leak or double-allocate a page.
**Layer 5 (the write-ahead log) is what fixes this** — until then, treat a crash
mid-write as "the file may be inconsistent".
