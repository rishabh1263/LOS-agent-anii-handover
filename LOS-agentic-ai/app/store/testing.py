"""
A FRESH POSTGRESQL CASE STORE FOR A TEST OR AN EVAL -- the engine production runs.

    from app.store.testing import fresh_repository
    repo = fresh_repository()          # its own database, migrated, isolated

One private PostgreSQL server for test runs (pgserver: real PostgreSQL
binaries, no install, no credentials) under the system temp directory, or the
server named by LOS_TEST_PG_DSN. Each process migrates ONE template database
once; every fresh_repository() clones it (CREATE DATABASE ... TEMPLATE), which
costs milliseconds and shares nothing with any other test. The clones are
dropped when the process exits.

Arguments are accepted and ignored, so a call written for a file-based store
(`fresh_repository(tmp_path / "x.db")`) needs no other change.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import threading
import uuid
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}


def _admin_dsn() -> str:
    configured = (os.getenv("LOS_TEST_PG_DSN") or "").strip()
    if configured:
        return configured
    import pgserver

    root = os.getenv("LOS_TEST_PGDATA") or os.path.join(tempfile.gettempdir(), "los_pg_test")
    return pgserver.get_server(root, cleanup_mode=None).get_uri()


def _with_db(dsn: str, name: str) -> str:
    base, _, query = dsn.partition("?")
    return base.rsplit("/", 1)[0] + "/" + name + (("?" + query) if query else "")


def _setup() -> tuple[str, str]:
    with _LOCK:
        if "template" in _STATE:
            return _STATE["admin"], _STATE["template"]
        import psycopg

        from app.store.postgres_repo import PostgresRepository

        admin = _admin_dsn()
        template = f"los_tpl_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{template}"')
        repo = PostgresRepository(_with_db(admin, template), max_size=2)
        repo.initialise()
        repo.close()                                # the thread's checked-out connection back...
        repo._pool_get().close()                    # ...and the pool shut: nothing stays on a template
        _STATE.update(admin=admin, template=template, clones=[], live=[])
        atexit.register(_cleanup)
        return admin, template


def fresh_repository(*_args: Any, **_kwargs: Any):
    """A new, migrated, empty PostgreSQL case store (its own database)."""
    import psycopg

    from app.store.postgres_repo import PostgresRepository

    admin, template = _setup()
    name = f"los_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        # BOUNDED: only the last few test stores stay open. Each one holds pooled
        # connections, and thousands of tests would exhaust max_connections.
        live = _STATE["live"]
        while len(live) >= _KEEP_OPEN:
            old_name, old_repo = live.pop(0)
            try:
                old_repo.dispose()
                conn.execute(f'DROP DATABASE IF EXISTS "{old_name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001
                pass
    repository = PostgresRepository(_with_db(admin, name), min_size=0, max_size=8)
    _STATE["live"].append((name, repository))
    _STATE["clones"].append(name)
    return repository


#: Test stores kept open at once (older ones are disposed and dropped).
_KEEP_OPEN = 6


def _cleanup() -> None:
    try:
        import psycopg

        with psycopg.connect(_STATE["admin"], autocommit=True) as conn:
            for name in [*_STATE.get("clones", []), _STATE.get("template")]:
                if name:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception:  # noqa: BLE001 - best effort at exit
        pass


def session_dsn() -> str:
    """A database for code that asks get_repository() itself (not rotated; dropped at exit)."""
    import psycopg

    admin, template = _setup()
    name = f"los_s_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    _STATE["clones"].append(name)
    return _with_db(admin, name)
