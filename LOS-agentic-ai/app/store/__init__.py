"""
The case store, and the one place its backend is chosen: PostgreSQL.

Callers ask for `get_repository()` and receive the PostgreSQL repository
(app/store/postgres_repo.py over the queries in app/store/sql_repo.py). One
engine everywhere -- development, tests and production -- so what is tested
is what runs; there is no second (SQLite) code path to drift from it.

    LOS_STORE_DSN          postgresql://user:password@host:5432/los   (never logged)
    LOS_STORE_POOL_MIN/MAX pool bounds (1 / 16; connections are borrowed per statement)
    LOS_DEV_EMBEDDED_PG    development only, when LOS_STORE_DSN is unset: start a
                           private local PostgreSQL under runtime/pgdata (pgserver,
                           real PostgreSQL binaries; no install, no credentials).
                           Default true outside production; never in production.
"""

from __future__ import annotations

import logging
import os
import threading

from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    Document,
    DocumentStatus,
    status_for_verdict,
)
from app.store.repository import Repository, RepositoryError

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_REPOSITORY: Repository | None = None


def store_backend() -> str:
    """Always "postgres". A configured other value is refused at build time."""
    return "postgres"


def store_dsn() -> str:
    return (os.getenv("LOS_STORE_DSN") or "").strip()


def _environment() -> str:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower()


def _embedded_dsn() -> str:
    """
    DEVELOPMENT ONLY: a private PostgreSQL under runtime/pgdata, started on
    first use and left running for the next start. Never in production -- a
    production deployment names its database (LOS_STORE_DSN) or does not start.
    """
    import pgserver

    import subprocess
    import time

    root = os.getenv("LOS_DEV_PGDATA") or os.path.join("runtime", "pgdata")
    # CRASH RECOVERY OUTLASTS pgserver's 10 s start timeout: after an unclean
    # stop the server fsyncs and replays WAL first (33 s measured, 2026-10-06),
    # the start "times out", and the app refused to boot while PostgreSQL went on
    # to become ready. Retried while recovery finishes, then the real error.
    for attempt in range(6):
        try:
            server = pgserver.get_server(os.path.abspath(root), cleanup_mode=None)
            break
        except subprocess.TimeoutExpired:
            if attempt == 5:
                raise
            logger.warning("Embedded PostgreSQL still starting (crash recovery?) -- retrying")
            time.sleep(10)
    logger.warning("Case store: embedded development PostgreSQL at %s (set LOS_STORE_DSN for a real one)", root)
    return server.get_uri()


def _build_postgres() -> Repository:
    from app.store.postgres_repo import PostgresRepository

    configured = (os.getenv("LOS_STORE_BACKEND") or "postgres").strip().lower()
    if configured != "postgres":
        raise RepositoryError(f"LOS_STORE_BACKEND={configured!r} is not supported: the case store is PostgreSQL")
    # document values are encrypted at rest; production refuses to run without the key
    from app.store import crypto

    try:
        crypto.require_in_production()
    except crypto.EncryptionConfigError as exc:
        raise RepositoryError(str(exc)) from exc
    dsn = store_dsn()
    if not dsn:
        embedded = (os.getenv("LOS_DEV_EMBEDDED_PG") or "true").strip().lower() == "true"
        if _environment() in {"production", "prod"} or not embedded:
            raise RepositoryError("LOS_STORE_DSN is not set: the case store needs a PostgreSQL database")
        dsn = _embedded_dsn()
    return PostgresRepository(dsn,
                              min_size=int(os.getenv("LOS_STORE_POOL_MIN") or 1),
                              max_size=int(os.getenv("LOS_STORE_POOL_MAX") or 16))


def get_repository() -> Repository:
    """
    The process-wide repository, built once.

    Built lazily rather than at import so configuration read from .env is in
    place before the backend is chosen, and so importing the package never
    touches the disk.
    """
    global _REPOSITORY

    if _REPOSITORY is not None:
        return _REPOSITORY

    with _LOCK:
        if _REPOSITORY is not None:
            return _REPOSITORY

        name = store_backend()
        repository = _build_postgres()
        repository.initialise()
        _REPOSITORY = repository
        logger.info("Case store backend: %s", name)
        return repository


def set_repository(repository: Repository | None) -> None:
    """
    Replace the process-wide repository.

    For tests, which point it at a fresh database, and for a deployment that
    builds its own backend at startup. Passing None drops it so the next call
    rebuilds from configuration.
    """
    global _REPOSITORY

    with _LOCK:
        if _REPOSITORY is not None and _REPOSITORY is not repository:
            try:
                _REPOSITORY.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("Closing the previous repository failed", exc_info=True)
        _REPOSITORY = repository


__all__ = [
    "Applicant",
    "Application",
    "ApplicationStatus",
    "Document",
    "DocumentStatus",
    "Repository",
    "RepositoryError",
    "get_repository",
    "set_repository",
    "status_for_verdict",
    "store_backend",
    "store_dsn",
]
