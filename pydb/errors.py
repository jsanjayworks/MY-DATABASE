"""The one exception every deliberate pydb error descends from.

Each layer has its own family -- `PagerError`, `BTreeError`, `SqlError` and the
rest -- and a layer may not import from the ones above it, so a duplicate key
found by the B+Tree (layer 4) cannot be a kind of `SqlError` (layer 7). This
class sits underneath all of them instead, which is what lets a caller tell "the
database refused" from "the database has a bug"::

    try:
        engine.execute(sql)
    except PydbError as error:  # bad SQL, a duplicate key, a NOT NULL column...
        ...

Anything else escaping pydb -- a `TypeError`, an `AssertionError` -- is a bug.
"""


class PydbError(Exception):
    """Base class for every error pydb raises on purpose."""
