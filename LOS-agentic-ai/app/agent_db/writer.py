"""
A BACKGROUND TABLE WRITER for rows that must never slow a request down (the chatbot's audit and turn rows).

    w = TableWriter(agent, migrations, table, columns)
    w.put(row)          # non-blocking; the row (a tuple in `columns` order) is queued
    w.flush()           # tests / shutdown: write what is queued now
    prepare(cur, rows)  # optional: runs in the batch's transaction before the insert and returns the rows to
                        # insert (the audit store records the rule sets the rows point at)

The first batch applies the agent's migrations (once per database). Rows are written in batches by one daemon thread.
A full queue drops the row (counted); a batch the database refuses is dropped and said ONCE on stderr -- callers that
must not lose a row keep their own copy (the chatbot keeps its JSONL files).
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Any

from app.agent_db import core


class TableWriter:
    def __init__(self, agent: str, migrations: list[core.Migration], table: str, columns: tuple[str, ...],
                 *, casts: dict[str, str] | None = None, prepare: Any = None):
        s = dict(core.config().get("logs") or {})
        self.agent, self.table, self.columns = agent, table, columns
        self._migrations = migrations
        self._prepare = prepare
        marks = [f"%s::{(casts or {}).get(c)}" if (casts or {}).get(c) else "%s" for c in columns]
        self._sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(marks)})"
        self._queue: queue.Queue = queue.Queue(maxsize=int(s.get("queue_size", 5000)))
        self._batch = int(s.get("batch_size", 200))
        self._every = float(s.get("flush_seconds", 2))
        self._migrated: set[str] = set()
        self._lock = threading.Lock()
        self._down_said = False
        self.dropped = self.written = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{agent}-{table}-writer", daemon=True)
        self._thread.start()

    def put(self, row: tuple) -> None:
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def _drain(self) -> list[tuple]:
        rows: list[tuple] = []
        while len(rows) < self._batch:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def _write(self, rows: list[tuple]) -> None:
        import psycopg

        value = core.dsn(self.agent)
        if not value:
            self.dropped += len(rows)
            return
        try:
            with psycopg.connect(value, connect_timeout=5) as conn:
                if value not in self._migrated:
                    core.apply_migrations(conn, self.agent, self._migrations)
                    self._migrated.add(value)
                with conn.cursor() as cur:
                    if self._prepare is not None:
                        rows = self._prepare(cur, rows)
                    cur.executemany(self._sql, rows)
                conn.commit()
            self.written += len(rows)
            self._down_said = False
        except Exception as exc:  # noqa: BLE001 - said once on stderr; never through logging (no loop)
            self.dropped += len(rows)
            if not self._down_said:
                self._down_said = True
                print(f"[agent_db] {self.agent}.{self.table}: could not store {len(rows)} row(s) "
                      f"({type(exc).__name__})", file=sys.stderr)

    def flush(self) -> None:
        with self._lock:
            while True:
                rows = self._drain()
                if not rows:
                    return
                self._write(rows)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._every)
            self.flush()

    def close(self) -> None:
        self._stop.set()
        self.flush()


_WRITERS: dict[tuple[str, str], TableWriter] = {}
_LOCK = threading.Lock()


def writer(agent: str, migrations: list[core.Migration], table: str, columns: tuple[str, ...],
           casts: dict[str, str] | None = None, prepare: Any = None) -> TableWriter | None:
    """The process-wide writer of one table, or None when the agent's DSN is not set (nothing to write to)."""
    if not core.dsn(agent):
        return None
    key = (agent, table)
    with _LOCK:
        if key not in _WRITERS:
            _WRITERS[key] = TableWriter(agent, migrations, table, columns, casts=casts, prepare=prepare)
        return _WRITERS[key]


def flush_all() -> None:
    for w in list(_WRITERS.values()):
        w.flush()


def close_all() -> None:
    with _LOCK:
        for w in _WRITERS.values():
            w.close()
        _WRITERS.clear()


__all__ = ["TableWriter", "close_all", "flush_all", "writer"]
