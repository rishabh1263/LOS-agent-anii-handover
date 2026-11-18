"""
The schema-migration advisory lock is released after initialise() -- on the session that took it.

Regression (2026-10-21): the lock was taken through the per-statement pooled connection, so it stayed on a pooled
connection after the unlock ran on another session; every later process's start-up then waited on it for ever.
"""

from __future__ import annotations

import threading

from app.store.postgres_repo import _LOCK_KEY, PostgresRepository
from app.store.testing import fresh_repository


def _held(repository) -> int:
    key = _LOCK_KEY & 0xFFFFFFFF
    with repository._pool_get().connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND objid = %s",
                           (key,)).fetchone()
    return int(row["n"])


def test_the_migration_lock_is_released_after_initialise():
    repository = fresh_repository()
    repository.initialise()
    assert _held(repository) == 0


def test_a_second_process_initialises_without_waiting():
    first = fresh_repository()
    first.initialise()
    # a second repository on the same database (another process, as far as Postgres can tell)
    second = PostgresRepository(first._dsn, min_size=0, max_size=2)
    done = threading.Event()
    threading.Thread(target=lambda: (second.initialise(), done.set()), daemon=True).start()
    assert done.wait(15), "the second initialise() waited on a migration lock left held"
    assert _held(first) == 0
