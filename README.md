# MY-DATABASE

A SQL database written from scratch in Python, no third-party dependencies —
built while following [Code With Sep's *Write a database from scratch*](https://www.youtube.com/watch?v=HHO2K23XxbM).

The point is not to be fast. The point is that every byte on disk is one I put
there and can explain.

## Status

| Layer | | |
|-------|---|---|
| 1. Pager | ✅ | pages, allocation, free list, fsync |
| 2. Buffer pool | ✅ | frames, pin/unpin, dirty write-back, clock eviction |
| 3. Records | ✅ | schemas, row codec, slotted pages, heap files |
| 4. B+Tree | ✅ | ordered index, splits, merges, range scans |
| 5. WAL / recovery | ✅ | page-image log, commit/rollback, checkpoints, replay |
| 6. Transactions | ✅ | begin/commit/rollback, one global lock |
| 7. SQL engine | ⬜ | |

See [ROADMAP.md](ROADMAP.md) for what each layer involves and how I'll know it's
done. The on-disk byte layout lives in [NOTES.md](NOTES.md).

## Requirements

Python 3.10+ (uses `X | Y` type syntax). Nothing else — the standard library's
`struct` and `os.fsync` are the entire toolkit.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

## Trying it out

```python
from pydb import Pager

with Pager("my.db") as db:
    page_id = db.allocate_page()
    db.write_page(page_id, b"hello from page %d" % page_id)

with Pager("my.db") as db:          # a whole new process would work too
    print(bytes(db.read_page(page_id)).rstrip(b"\x00"))
```
