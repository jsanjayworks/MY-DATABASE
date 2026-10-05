from __future__ import annotations

import os
import random
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.buffer_pool import AllFramesPinnedError
from pydb.database import Database, TransactionError
from pydb.pager import META_SLOT_ROOT, FileInUseError
from pydb.record import ColumnType, encode_key


def int_key(value: int) -> bytes:
    return encode_key(ColumnType.INT, value)


class DatabaseTestCase(unittest.TestCase):
    CAPACITY = 32

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")

    def open(self, capacity: int | None = None) -> Database:
        db = Database(self.path, capacity=capacity or self.CAPACITY)
        self.addCleanup(db.close)
        return db


class TestTransactionControl(DatabaseTestCase):
    def test_a_fresh_database_has_no_transaction(self):
        db = self.open()
        self.assertFalse(db.in_transaction)

    def test_begin_commit_returns_to_idle(self):
        db = self.open()
        self.assertEqual(db.begin(), 1)
        self.assertTrue(db.in_transaction)
        db.commit()
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.begin(), 2)
        db.rollback()
        self.assertFalse(db.in_transaction)

    def test_committing_without_a_transaction_is_an_error(self):
        db = self.open()
        with self.assertRaises(TransactionError):
            db.commit()
        with self.assertRaises(TransactionError):
            db.rollback()

    def test_nested_transactions_are_refused_rather_than_faked(self):
        db = self.open()
        db.begin()
        with self.assertRaises(TransactionError):
            db.begin()
        db.rollback()

    def test_the_context_manager_commits_on_success(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            tree.put(b"k", b"v")
        self.assertFalse(db.in_transaction)
        self.assertEqual(tree.get(b"k"), b"v")

    def test_the_context_manager_rolls_back_on_any_exception(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            tree.put(b"keep", b"1")

        with self.assertRaises(ZeroDivisionError):
            with db.transaction():
                tree.put(b"drop", b"2")
                raise ZeroDivisionError
        self.assertFalse(db.in_transaction)
        self.assertEqual(tree.get(b"keep"), b"1")
        self.assertIsNone(tree.get(b"drop"))
        self.assertEqual(db.rollbacks, 1)

    def test_a_transaction_too_big_for_the_pool_rolls_back_cleanly(self):
        db = self.open(capacity=6)
        tree = db.open_tree()
        with self.assertRaises(AllFramesPinnedError):
            with db.transaction():
                for i in range(5000):
                    tree.put(int_key(i), b"x" * 400)
        self.assertFalse(db.in_transaction)
        with db.transaction():
            tree.put(b"after", b"still works")
        self.assertEqual(tree.get(b"after"), b"still works")
        tree.verify_invariants()

    def test_autocommit_is_its_own_transaction_when_alone(self):
        db = self.open()
        tree = db.open_tree()
        with db.autocommit():
            tree.put(b"k", b"v")
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.transactions, 2)

    def test_autocommit_joins_an_open_transaction_instead_of_splitting_it(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            with db.autocommit():
                tree.put(b"a", b"1")
            with db.autocommit():
                tree.put(b"b", b"2")
            self.assertTrue(db.in_transaction, "the outer transaction is still open")
            started = db.transactions
        self.assertEqual(db.transactions, started, "no extra transactions started")

    def test_a_rollback_inside_an_outer_transaction_undoes_everything(self):
        db = self.open()
        tree = db.open_tree()
        with self.assertRaises(ValueError):
            with db.transaction():
                with db.autocommit():
                    tree.put(b"a", b"1")
                raise ValueError
        self.assertIsNone(tree.get(b"a"))

    def test_a_failed_statement_inside_a_transaction_undoes_only_itself(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            with db.autocommit():
                tree.put(b"a", b"1")
            with self.assertRaises(ValueError):
                with db.autocommit():
                    tree.put(b"a", b"overwritten")
                    tree.put(b"b", b"2")
                    raise ValueError
            self.assertTrue(db.in_transaction)
            self.assertEqual(tree.get(b"a"), b"1")
            self.assertIsNone(tree.get(b"b"))
        self.assertEqual((db.rollbacks, db.statement_rollbacks), (0, 1))
        db.close()

        tree = self.open().open_tree()
        self.assertEqual(list(tree.items()), [(b"a", b"1")])

    def test_a_failed_statement_gives_back_the_pages_it_allocated(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            with db.autocommit():
                for i in range(50):
                    tree.put(int_key(i), b"x" * 40)
            root, pages = tree.root_page_id, len(db.pager)
            with self.assertRaises(RuntimeError):
                with db.autocommit():
                    for i in range(50, 600):
                        tree.put(int_key(i), b"x" * 40)
                    self.assertNotEqual(tree.root_page_id, root, "root never moved")
                    raise RuntimeError
            self.assertEqual(tree.root_page_id, root)
            self.assertEqual(len(db.pager), pages)
            self.assertEqual(tree.count(), 50)
            tree.verify_invariants()
            with db.autocommit():
                tree.put(b"after", b"1")
        db.close()

        tree = self.open().open_tree()
        self.assertEqual(tree.count(), 51)
        tree.verify_invariants()

    def test_a_statement_too_big_for_the_pool_fails_alone(self):
        db = self.open(capacity=8)
        tree = db.open_tree()
        with db.transaction():
            with db.autocommit():
                tree.put(b"keep", b"1")
            with self.assertRaises(AllFramesPinnedError):
                with db.autocommit():
                    for i in range(5000):
                        tree.put(int_key(i), b"x" * 400)
            with db.autocommit():
                tree.put(b"after", b"2")
        self.assertEqual(list(tree.items()), [(b"after", b"2"), (b"keep", b"1")])
        tree.verify_invariants()

    def test_a_second_database_on_the_same_file_is_refused(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            tree.put(b"first", b"1")
        with self.assertRaises(FileInUseError):
            Database(self.path)
        with db.transaction():
            tree.put(b"second", b"2")
        db.close()

        tree = self.open().open_tree()
        self.assertEqual(list(tree.items()), [(b"first", b"1"), (b"second", b"2")])

    def test_using_a_closed_database_is_an_error(self):
        db = Database(self.path)
        db.close()
        db.close()
        with self.assertRaises(TransactionError):
            db.begin()


class TestOpenTree(DatabaseTestCase):
    def test_a_tree_is_created_once_and_found_again(self):
        db = self.open()
        tree = db.open_tree()
        root = tree.root_page_id
        self.assertEqual(db.pager.read_meta_slot(META_SLOT_ROOT), root)
        self.assertEqual(db.open_tree().root_page_id, root)

    def test_the_root_page_id_survives_a_reopen(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            for i in range(2000):
                tree.put(int_key(i), str(i).encode())
        moved_root = tree.root_page_id
        db.close()

        db = self.open()
        tree = db.open_tree()
        self.assertEqual(tree.root_page_id, moved_root)
        self.assertEqual(tree.count(), 2000)
        tree.verify_invariants()

    def test_rolling_back_the_transaction_that_moved_the_root_restores_it(self):
        db = self.open()
        tree = db.open_tree()
        with db.transaction():
            for i in range(300):
                tree.put(int_key(i), str(i).encode())
        root_before = tree.root_page_id

        with self.assertRaises(RuntimeError):
            with db.transaction():
                for i in range(300, 3000):
                    tree.put(int_key(i), str(i).encode())
                raise RuntimeError
        self.assertEqual(tree.root_page_id, root_before)
        self.assertEqual(db.pager.read_meta_slot(META_SLOT_ROOT), root_before)
        self.assertEqual(tree.count(), 300)
        tree.verify_invariants()

    def test_separate_slots_hold_separate_trees(self):
        db = self.open()
        first = db.open_tree(META_SLOT_ROOT)
        second = db.open_tree(META_SLOT_ROOT + 1)
        self.assertNotEqual(first.root_page_id, second.root_page_id)
        with db.transaction():
            first.put(b"k", b"in the first")
            second.put(b"k", b"in the second")
        self.assertEqual(first.get(b"k"), b"in the first")
        self.assertEqual(second.get(b"k"), b"in the second")


class Accounts:
    def __init__(self, db: Database, count: int, opening_balance: int) -> None:
        self.db = db
        self.tree = db.open_tree()
        self.count = count
        self.total = count * opening_balance
        with db.transaction():
            for account in range(count):
                self.tree.put(int_key(account), self._encode(opening_balance))

    @staticmethod
    def _encode(balance: int) -> bytes:
        return balance.to_bytes(8, "big", signed=True)

    def balance(self, account: int) -> int:
        return int.from_bytes(self.tree[int_key(account)], "big", signed=True)

    def sum_all(self) -> int:
        return sum(
            int.from_bytes(value, "big", signed=True)
            for _key, value in self.tree.items()
        )

    def transfer(self, source: int, target: int, amount: int) -> bool:
        with self.db.transaction():
            available = self.balance(source)
            if available < amount:
                return False
            self.tree.put(int_key(source), self._encode(available - amount))
            self.tree.put(int_key(target), self._encode(self.balance(target) + amount))
            return True


class TestMilestone(DatabaseTestCase):
    ACCOUNTS = 20
    OPENING = 1000
    THREADS = 8
    TRANSFERS = 150

    def test_concurrent_transfers_conserve_the_total(self):
        db = self.open(capacity=64)
        bank = Accounts(db, self.ACCOUNTS, self.OPENING)

        moved = [0] * self.THREADS
        failures: list[BaseException] = []

        def worker(index: int) -> None:
            rng = random.Random(index)
            try:
                for _ in range(self.TRANSFERS):
                    source = rng.randrange(self.ACCOUNTS)
                    target = rng.randrange(self.ACCOUNTS)
                    if source == target:
                        continue
                    if bank.transfer(source, target, rng.randint(1, 300)):
                        moved[index] += 1
            except BaseException as error:
                failures.append(error)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(self.THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [], "a worker raised")
        self.assertGreater(sum(moved), 0, "no transfer succeeded; test proved nothing")
        self.assertEqual(bank.sum_all(), bank.total, "money was created or destroyed")
        self.assertFalse(db.in_transaction)
        bank.tree.verify_invariants()

    def test_the_total_still_holds_after_a_reopen(self):
        db = self.open(capacity=64)
        bank = Accounts(db, self.ACCOUNTS, self.OPENING)

        def worker(index: int) -> None:
            rng = random.Random(100 + index)
            for _ in range(self.TRANSFERS):
                bank.transfer(
                    rng.randrange(self.ACCOUNTS),
                    rng.randrange(self.ACCOUNTS),
                    rng.randint(1, 500),
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        db.close()

        db = self.open(capacity=64)
        tree = db.open_tree()
        tree.verify_invariants()
        total = sum(
            int.from_bytes(value, "big", signed=True) for _key, value in tree.items()
        )
        self.assertEqual(total, self.ACCOUNTS * self.OPENING)

    def test_a_transfer_that_rolls_back_moves_nothing(self):
        db = self.open(capacity=64)
        bank = Accounts(db, 4, 100)

        def failing_transfer() -> None:
            with db.transaction():
                bank.tree.put(int_key(0), bank._encode(0))
                bank.tree.put(int_key(1), bank._encode(200))
                raise RuntimeError("changed my mind")

        def worker() -> None:
            for _ in range(50):
                try:
                    failing_transfer()
                except RuntimeError:
                    pass
                bank.transfer(2, 3, 1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(bank.balance(0), 100, "a rolled-back write was visible")
        self.assertEqual(bank.balance(1), 100)
        self.assertEqual(bank.sum_all(), bank.total)

    def test_transactions_are_serialised_not_interleaved(self):
        db = self.open()
        inside = []
        clashes = []

        def worker() -> None:
            for _ in range(100):
                with db.transaction():
                    inside.append(1)
                    if len(inside) != 1:
                        clashes.append(len(inside))
                    inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(clashes, [], "two transactions were open at once")
        self.assertEqual(db.transactions, 600)


if __name__ == "__main__":
    unittest.main()
