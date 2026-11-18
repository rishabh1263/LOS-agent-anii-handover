"""
APPLICATION LOGS IN EACH AGENT'S OWN DATABASE (table agent_logs; config agent_databases.yaml `logs`).

`install()` adds ONE logging handler to each agent's loggers (config databases.<agent>.loggers) whose DSN is set. A
record at `logs.level` or above is masked (app/agent_db/redact.py), cut, and put on a bounded queue; a background thread
writes the queue in batches. Logging never blocks or fails a request: a full queue drops the record (counted), a
database that is down drops the batch (said once on the console, never re-logged into itself).

What is stored: time, level, logger, message, module / function / line, the request id when the request set one,
the exception type and its masked traceback, host and process id. Never a DSN, a token or a raw identifier.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import sys
import threading
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.agent_db import core
from app.agent_db.redact import redact

#: the request id a route sets for the logs of this request (optional)
REQUEST_ID: ContextVar[str | None] = ContextVar("agent_db_request_id", default=None)

_INSTALLED: dict[str, "AgentDbLogHandler"] = {}
_LOCK = threading.Lock()


def _settings() -> dict[str, Any]:
    return dict(core.config().get("logs") or {})


class AgentDbLogHandler(logging.Handler):
    """Queue records for one agent database; a daemon thread writes them."""

    def __init__(self, agent: str, dsn: str):
        s = _settings()
        super().__init__(level=getattr(logging, str(s.get("level", "WARNING")).upper(), logging.WARNING))
        self.agent, self._dsn = agent, dsn
        self._queue: queue.Queue = queue.Queue(maxsize=int(s.get("queue_size", 5000)))
        self._batch = int(s.get("batch_size", 200))
        self._every = float(s.get("flush_seconds", 2))
        self._message_chars = int(s.get("max_message_chars", 2000))
        self._trace_chars = int(s.get("max_traceback_chars", 6000))
        self.dropped = 0
        self.written = 0
        self._down_said = False
        self._host = socket.gethostname()[:120]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"agent-logs-{agent}", daemon=True)
        self._thread.start()

    # ---- the request side: never blocks, never raises
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("app.agent_db"):
                return                                    # never log the log writer into itself
            message = redact(record.getMessage())[: self._message_chars]
            exception = None
            if record.exc_info and record.exc_info[0] is not None:
                text = "".join(traceback.format_exception(*record.exc_info))
                exception = redact(text)[-self._trace_chars:]
            row = (datetime.fromtimestamp(record.created, timezone.utc), record.levelname, record.name[:200], message,
                   (record.module or "")[:120], (record.funcName or "")[:120], record.lineno, REQUEST_ID.get(),
                   exception, self._host, os.getpid())
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - a log line must never fail the caller
            self.dropped += 1

    # ---- the writer side
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

        try:
            with psycopg.connect(self._dsn, connect_timeout=5, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO agent_logs (logged_at, level, logger, message, module, function, line, request_id,"
                        " exception, host, process_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
            self.written += len(rows)
            self._down_said = False
        except Exception as exc:  # noqa: BLE001 - said once on stderr, never through logging (no loop)
            self.dropped += len(rows)
            if not self._down_said:
                self._down_said = True
                print(f"[agent_logs] {self.agent}: could not store {len(rows)} log line(s) ({type(exc).__name__})",
                      file=sys.stderr)

    def flush(self) -> None:
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
        try:
            self.flush()
        finally:
            super().close()


def install() -> dict[str, AgentDbLogHandler]:
    """One handler per agent whose DSN is set (config logs.enabled). Safe to call more than once."""
    if not _settings().get("enabled", True):
        return {}
    with _LOCK:
        for agent in core.agents():
            value = core.dsn(agent)
            if not value or agent in _INSTALLED:
                continue
            handler = AgentDbLogHandler(agent, value)
            for name in core.spec(agent).get("loggers") or []:
                logging.getLogger(str(name)).addHandler(handler)
            _INSTALLED[agent] = handler
        return dict(_INSTALLED)


def uninstall() -> None:
    """Remove and close every handler (tests; shutdown)."""
    with _LOCK:
        for agent, handler in list(_INSTALLED.items()):
            for name in core.spec(agent).get("loggers") or []:
                logging.getLogger(str(name)).removeHandler(handler)
            handler.close()
            _INSTALLED.pop(agent)


def purge_older_than(conn: Any, days: int | None = None) -> int:
    """Delete log rows past the retention (config logs.retention_days). Returns the count."""
    keep = int(days if days is not None else _settings().get("retention_days", 90))
    cur = conn.execute("DELETE FROM agent_logs WHERE logged_at < now() - make_interval(days => %s)", (keep,))
    return cur.rowcount or 0
