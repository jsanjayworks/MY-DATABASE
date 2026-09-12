"""Layer 6: transactions.

Layer 5 can already make a group of page writes atomic and durable. What it
cannot do is stop two threads from interleaving inside that group, and it has no
opinion about where a transaction begins. That is this layer: `Database` ties the
pager, pool and log into one object, gives it `begin` / `commit` / `rollback`, and
decides who is allowed to be inside a transaction at a time.

The isolation mechanism is **one global lock, held from begin to commit**. That
is a deliberately blunt instrument, and it is the right one to start with for two
reasons. The obvious one is that nothing below this layer is thread-safe -- the
buffer pool's frame table, the clock hand, the log's dirty set are all plain
Python objects with no synchronisation -- so fine-grained locking would mean
making all of that safe first. The subtler one is the roadmap's own warning:
concurrency bugs on top of a shaky B+Tree are almost impossible to diagnose. A
global lock gives serializability for free, which means any bug the concurrency
tests find is a *storage* bug, not a race.

What that costs is honest and total: **no two transactions ever run at the same
time**, readers included. Real isolation -- two-phase locking over row or page
locks, or MVCC with per-transaction snapshots -- is the next layer down that road
and needs a thread-safe buffer pool underneath it first.

A transaction is the unit of atomicity *and* of isolation here, so the lock and
the log's commit are acquired and released together.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from pydb.btree import BTree
from pydb.buffer_pool import DEFAULT_CAPACITY, BufferPool
from pydb.pager import META_SLOT_ROOT, NULL_PAGE_ID
from pydb.wal import Wal


class TransactionError(Exception):
    """A transaction was used in a way the state machine does not allow."""


class Database:
    """A database file, its cache, its log, and transaction control over them.

        >>> with Database("my.db") as db:
        ...     tree = db.open_tree()
        ...     with db.transaction():
        ...         tree.put(b"key", b"value")
        ...     # committed and durable here; an exception instead would have
        ...     # rolled the whole thing back

    Opening one recovers whatever a previous crash left behind, because that is
    what constructing the log does.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        capacity: int = DEFAULT_CAPACITY,
        wal_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.pool = BufferPool.open(path, capacity=capacity)
        try:
            self.wal = Wal(self.pool, wal_path)
        except Exception:
            self.pool.close()
            raise
        self.pager = self.pool.pager
        self.path = self.pager.path
        self.transactions = 0  # how many have been started, ever
        self.rollbacks = 0

        self._lock = threading.Lock()
        self._owner: int | None = None  # thread id inside the transaction
        self._trees: dict[int, BTree] = {}  # meta slot -> the tree rooted there
        self._rollback_hooks: list[Callable[[], None]] = []
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Roll back anything open, checkpoint, and close up. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self.wal.close()  # rolls back an open transaction, then checkpoints
        self.pool.close()
        if self._owner is not None:
            self._owner = None
            self._lock.release()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "in a transaction" if self.in_transaction else "idle"
        return f"<Database {self.path!r} {state}, {self.transactions} started>"

    # ------------------------------------------------------------------
    # transaction control
    # ------------------------------------------------------------------

    @property
    def in_transaction(self) -> bool:
        return self._owner is not None

    def begin(self) -> int:
        """Start a transaction, waiting for any other one to finish. Returns its id.

        Blocks rather than failing when another thread holds the lock: a caller
        that asked to begin a transaction wants one, not an error about timing.
        """
        self._require_open()
        if self._owner == threading.get_ident():
            raise TransactionError(
                "this thread is already in a transaction; there are no nested "
                "transactions or savepoints"
            )
        self._lock.acquire()
        self._owner = threading.get_ident()
        self.transactions += 1
        return self.transactions

    def commit(self) -> int:
        """Make this thread's transaction durable and release the lock."""
        self._require_mine("commit")
        try:
            return self.wal.commit()
        finally:
            self._release()

    def rollback(self) -> None:
        """Undo this thread's transaction and release the lock."""
        self._require_mine("roll back")
        try:
            self.wal.rollback()
            self._resync_trees()
            for hook in self._rollback_hooks:
                hook()
            self.rollbacks += 1
        finally:
            self._release()

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Run a block as one transaction: commit on success, roll back on error.

        Any exception rolls back -- including one from the storage layers, such as
        a transaction that outgrew the buffer pool. Half-applied work never
        survives a raise.
        """
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        self.commit()

    @contextmanager
    def autocommit(self) -> Iterator["Database"]:
        """Like `transaction()`, but does nothing if one is already open.

        This is what a statement wraps itself in: run alone it is its own
        transaction, run inside `BEGIN ... COMMIT` it is part of that one.
        """
        if self._owner == threading.get_ident():
            yield self
            return
        with self.transaction():
            yield self

    # ------------------------------------------------------------------
    # storage
    # ------------------------------------------------------------------

    def open_tree(self, slot: int = META_SLOT_ROOT) -> BTree:
        """The B+Tree whose root page id lives in meta `slot`, creating it if new.

        This is the piece that makes a tree reachable after a reopen: the root
        moves as the tree grows, and the slot is where the new id is written --
        inside the current transaction, so a rollback takes it back with
        everything else.

        One tree object per slot, cached. Two objects for the same slot would each
        keep their own idea of where the root is, and one of them would be wrong
        the moment the other split it.
        """
        self._require_open()
        if slot in self._trees:
            return self._trees[slot]

        def remember(page_id: int) -> None:
            self.pager.write_meta_slot(slot, page_id)

        root = self.pager.read_meta_slot(slot)
        if root != NULL_PAGE_ID:
            tree = BTree(self.pool, root, on_root_change=remember)
        else:
            with self.autocommit():
                tree = BTree.create(self.pool, on_root_change=remember)
                remember(tree.root_page_id)
        self._trees[slot] = tree
        return tree

    def checkpoint(self) -> int:
        """Bring the data file fully up to date and empty the log."""
        self._require_open()
        return self.wal.checkpoint()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def add_rollback_hook(self, hook: Callable[[], None]) -> None:
        """Register something to run after every rollback.

        Anything above this layer that caches a page id -- a table's index root, a
        heap's page chain -- is holding a value a rollback can invalidate, and has
        to re-read it from the file. This is where it gets told to.
        """
        self._rollback_hooks.append(hook)

    def _resync_trees(self) -> None:
        """Point every open tree back at the root the rollback restored.

        A tree caches its root page id in memory, and a rolled-back transaction
        may have moved it -- possibly to a page that no longer exists, since the
        rollback gave the allocation back too. The meta slot is the durable truth;
        this is where the in-memory copy is made to agree with it again.
        """
        for slot, tree in self._trees.items():
            tree.root_page_id = self.pager.read_meta_slot(slot)

    def _release(self) -> None:
        self._owner = None
        self._lock.release()

    def _require_open(self) -> None:
        if self._closed:
            raise TransactionError("the database is closed")

    def _require_mine(self, action: str) -> None:
        self._require_open()
        if self._owner is None:
            raise TransactionError(f"cannot {action}: no transaction is open")
        if self._owner != threading.get_ident():
            raise TransactionError(
                f"cannot {action}: the open transaction belongs to another thread"
            )
