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

## Durability

`page_count` and `free_list_head` live on the meta page, so every allocate and
free rewrites page 0. Right now that write is not crash-safe: a crash between
writing a page and writing the meta page can leak or double-allocate a page.
**Layer 5 (the write-ahead log) is what fixes this** — until then, treat a crash
mid-write as "the file may be inconsistent".
