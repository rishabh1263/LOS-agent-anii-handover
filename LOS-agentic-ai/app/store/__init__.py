"""
The case store, and the one place its backend is chosen.

Callers ask for `get_repository()` and receive something implementing
`Repository`. Which implementation that is comes from configuration, so
replacing SQLite with PostgreSQL or with an adapter onto an existing LOS is a
configuration change plus one new class -- not an edit to the Applicant Agent,
the MCP tools or the orchestrator.

    LOS_STORE_BACKEND   sqlite (default) or postgres. The implementation.
    LOS_STORE_PATH      ./runtime/los_store.sqlite3, for the sqlite backend.
    LOS_STORE_DSN       Host=...;Port=5432;Database=...;Username=...;Password=...
                        (or postgresql://user:password@host:5432/db), for postgres.

To add a backend: implement Repository, register it in _BACKENDS, and set
LOS_STORE_BACKEND. Nothing above this module changes.
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


def store_dsn() -> str:
    return (os.getenv("LOS_STORE_DSN") or "").strip()


def _build_sqlite() -> Repository:
    from app.store.sql_repo import SQLiteRepository



def _build_postgres() -> Repository:
    from app.store.postgres_repo import PostgresRepository

    return PostgresRepository(store_dsn())


#: backend name -> factory. The seam a new storage technology plugs into.
_BACKENDS: dict[str, Callable[[], Repository]] = {
    "sqlite": _build_sqlite,
    "postgres": _build_postgres,
}


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
    "store_path",
]
