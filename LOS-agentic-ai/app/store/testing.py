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

SPEED, MEASURED 2026-10-22 (9,055 tests, ~1 s setup each; nothing here changes what a test sees):
  a. RETIRED CLONES ARE DROPPED ON A BACKGROUND THREAD. Dropping the oldest clone inside the next test's setup
     (DROP DATABASE ... WITH (FORCE)) cost 1.2-8.5 s every 7th store. Still bounded (a full drop queue makes the
     next setup wait), still every clone dropped at exit.
  b. A CLONE OF THE VALIDATED TEMPLATE SKIPS initialise()'s MIGRATION CHECK -- only when it would do nothing: every
     regular migration is in the template (checked once, at setup) and no gated migration would be applied under the
     CURRENT flags. Anything to apply (a test that switched LOS_COAPP_IDENTITY on) runs the full, locked path.
  c. THE DISPOSABLE TEST CLUSTER ONLY (pgserver under the temp dir -- never LOS_TEST_PG_DSN, never the dev store
     under runtime/pgdata, never production): fsync / synchronous_commit / full_page_writes off. A crash of the
     machine can corrupt that cluster; it holds nothing but throwaway test databases and is rebuilt on demand.
     LOS_TEST_PG_FAST=false keeps the server's defaults.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import tempfile
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}
#: retired clones awaiting their DROP (bounded: a full queue makes the next fresh_repository wait)
_DROPS: "queue.Queue[tuple[str, Any] | None]" = queue.Queue(maxsize=12)


def _fast_settings_on_disposable_cluster(admin: str) -> None:
    """(c) The throwaway pgserver cluster only: durability off. Checked by its data directory, never assumed."""
    if (os.getenv("LOS_TEST_PG_DSN") or "").strip():
        return                                       # someone else's server: never touched
    if (os.getenv("LOS_TEST_PG_FAST") or "true").strip().lower() in {"false", "0", "no", "off"}:
        return
    import psycopg

    root = os.path.realpath(os.getenv("LOS_TEST_PGDATA") or os.path.join(tempfile.gettempdir(), "los_pg_test"))
    with psycopg.connect(admin, autocommit=True) as conn:
        data_dir = os.path.realpath(conn.execute("SHOW data_directory").fetchone()[0])
        if not data_dir.lower().startswith(root.lower()) or "runtime" in data_dir.lower().split(os.sep):
            logger.warning("test store: data directory is not the disposable test cluster; settings unchanged")
            return
        current = {k: conn.execute(f"SHOW {k}").fetchone()[0] for k in ("fsync", "synchronous_commit",
                                                                       "full_page_writes")}
        if all(v == "off" for v in current.values()):
            return
        for key in current:
            conn.execute(f"ALTER SYSTEM SET {key} = off")
        conn.execute("SELECT pg_reload_conf()")


def _drop_worker() -> None:
    """(a) Drop retired clones off the test's critical path."""
    import psycopg

    while True:
        item = _DROPS.get()
        try:
            if item is None:
                return
            name, repo = item
            try:
                repo.dispose()
            except Exception:  # noqa: BLE001
                pass
            try:
                with psycopg.connect(_STATE["admin"], autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - _cleanup drops anything left at exit
                pass
        finally:
            _DROPS.task_done()


def _template_versions(admin: str, template: str) -> frozenset[str]:
    """(b) What the template has applied -- validated: every regular migration must be there."""
    import psycopg

    from app.store.postgres_repo import MIGRATIONS

    with psycopg.connect(_with_db(admin, template), autocommit=True) as conn:
        versions = frozenset(r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall())
    missing = {v for v, *_ in MIGRATIONS} - versions
    if missing:
        raise RuntimeError(f"test template is missing migrations {sorted(missing)}")
    return versions


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
        _fast_settings_on_disposable_cluster(admin)
        template = f"los_tpl_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{template}"')
        repo = PostgresRepository(_with_db(admin, template), max_size=2)
        repo.initialise()
        repo.close()                                # the thread's checked-out connection back...
        repo._pool_get().close()                    # ...and the pool shut: nothing stays on a template
        _STATE.update(admin=admin, template=template, clones=[], live=[],
                      versions=_template_versions(admin, template))
        threading.Thread(target=_drop_worker, name="test-db-drops", daemon=True).start()
        atexit.register(_cleanup)
        return admin, template


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=1)
def _clone_class():
    """(b) PostgresRepository whose initialise() is a no-op exactly when it would change nothing."""
    from app.store import postgres_repo as pg

    class TemplateClone(pg.PostgresRepository):
        def initialise(self) -> None:
            versions = _STATE["versions"]
            with self._init_lock:
                if self._initialised:
                    return
                # looked up at CALL time: the flags (and the module's own functions) as they are now
                regular_missing = any(v not in versions for v, *_ in pg.MIGRATIONS)
                gated_due = any(pg._gated_auto_apply_allowed(v)[0] for v, *_ in pg.GATED_MIGRATIONS
                                if v not in versions)
                if not regular_missing and not gated_due:
                    self._initialised = True       # the clone IS the validated template: nothing to apply
                    return
            super().initialise()                   # something to apply: the full, locked path, as before

    return TemplateClone


def fresh_repository(*_args: Any, **_kwargs: Any):
    """A new, migrated, empty PostgreSQL case store (its own database)."""
    import psycopg

    admin, template = _setup()
    name = f"los_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    # BOUNDED: only the last few test stores stay open. Each one holds pooled
    # connections, and thousands of tests would exhaust max_connections. The
    # retired ones are dropped by the background worker (a); a full queue waits.
    live = _STATE["live"]
    while len(live) >= _KEEP_OPEN:
        _DROPS.put(live.pop(0))
    repository = _clone_class()(_with_db(admin, name), min_size=0, max_size=8)
    _STATE["live"].append((name, repository))
    _STATE["clones"].append(name)
    return repository


#: Test stores kept open at once (older ones are disposed and dropped).
_KEEP_OPEN = 6


def _cleanup() -> None:
    try:                                             # the background drops finish first (bounded wait)
        _DROPS.put(None, timeout=30)
        for _ in range(300):
            if _DROPS.unfinished_tasks == 0:
                break
            threading.Event().wait(0.1)
    except Exception:  # noqa: BLE001
        pass
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
