"""Test-only sqlite3 backend that opens write transactions with `BEGIN IMMEDIATE`.

Django 5.1+ exposes this as `OPTIONS["transaction_mode"]`; Django 4.2's sqlite3 backend does not
know that key (it would be passed straight to `sqlite3.connect()` and raise `TypeError`). This
subclass gets the same effect on 4.2 by overriding the private method the base backend uses to
issue `BEGIN`, so `test_two_threads_storing_one_new_fingerprint_create_one_issue` (a genuine
two-connection race) behaves the same under every supported Django version instead of deadlocking
under 4.2's default DEFERRED transactions (see DECISIONS.md).
"""

from __future__ import annotations

from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper


class DatabaseWrapper(SQLiteDatabaseWrapper):
    def _start_transaction_under_autocommit(self) -> None:
        self.cursor().execute("BEGIN IMMEDIATE")
