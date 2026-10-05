from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from pydb.btree import BTree
from pydb.buffer_pool import DEFAULT_CAPACITY, BufferPool
from pydb.errors import PydbError
from pydb.pager import META_SLOT_ROOT, NULL_PAGE_ID
from pydb.wal import Wal


class TransactionError(PydbError):
    pass


class Database:
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
        self.transactions = 0
        self.rollbacks = 0
        self.statement_rollbacks = 0

        self._lock = threading.Lock()
        self._owner: int | None = None
        self._in_statement = False
        self._trees: dict[int, BTree] = {}
        self._rollback_hooks: list[Callable[[], None]] = []
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.wal.close()
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

    @property
    def in_transaction(self) -> bool:
        return self._owner is not None

    def begin(self) -> int:
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
        self._require_mine("commit")
        try:
            return self.wal.commit()
        finally:
            self._release()

    def rollback(self) -> None:
        self._require_mine("roll back")
        try:
            self.wal.rollback()
            self._rederive()
            self.rollbacks += 1
        finally:
            self._release()

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        self.commit()

    @contextmanager
    def autocommit(self) -> Iterator["Database"]:
        if self._owner != threading.get_ident():
            with self.transaction():
                self._in_statement = True
                try:
                    yield self
                finally:
                    self._in_statement = False
            return
        if self._in_statement:
            yield self
            return
        savepoint = self.wal.savepoint()
        self._in_statement = True
        try:
            yield self
        except BaseException:
            self.wal.rollback_to(savepoint)
            self._rederive()
            self.statement_rollbacks += 1
            raise
        finally:
            self._in_statement = False

    def open_tree(self, slot: int = META_SLOT_ROOT) -> BTree:
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

    def free_pages(self, pages: list[int]) -> int:
        self._require_open()
        if self.in_transaction:
            for page_id in pages:
                self.pool.free_page(page_id)
            return len(pages)

        batch = max(1, self.pool.capacity // 2)
        freed = 0
        for start in range(0, len(pages), batch):
            with self.transaction():
                for page_id in pages[start : start + batch]:
                    self.pool.free_page(page_id)
                    freed += 1
        return freed

    def checkpoint(self) -> int:
        self._require_open()
        return self.wal.checkpoint()

    def add_rollback_hook(self, hook: Callable[[], None]) -> None:
        self._rollback_hooks.append(hook)

    def _rederive(self) -> None:
        self._resync_trees()
        for hook in self._rollback_hooks:
            hook()

    def _resync_trees(self) -> None:
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
