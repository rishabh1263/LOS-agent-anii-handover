"""
The case store, and the one place its backend is chosen.

Callers ask for `get_repository()` and receive something implementing
`Repository`. Which implementation that is comes from configuration, so
replacing SQLite with PostgreSQL or with an adapter onto an existing LOS is a
configuration change plus one new class -- not an edit to the Applicant Agent,
the MCP tools or the orchestrator.

    LOS_STORE_BACKEND   sqlite (default). The name of the implementation.
    LOS_STORE_PATH      ./runtime/los_store.sqlite3, for the sqlite backend.

To add a backend: implement Repository, register it in _BACKENDS, and set
LOS_STORE_BACKEND. Nothing above this module changes.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

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
    return (os.getenv("LOS_STORE_BACKEND") or "sqlite").strip().lower()


def store_path() -> str:
    return (os.getenv("LOS_STORE_PATH") or "./runtime/los_store.sqlite3").strip()


def _build_sqlite() -> Repository:
    from app.store.sqlite_repo import SQLiteRepository

    return SQLiteRepository(store_path())


#: backend name -> factory. The seam a new storage technology plugs into.
_BACKENDS: dict[str, Callable[[], Repository]] = {
    "sqlite": _build_sqlite,
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
        factory = _BACKENDS.get(name)
        if factory is None:
            raise RepositoryError(
                f"Unknown LOS_STORE_BACKEND={name!r}. "
                f"Available: {sorted(_BACKENDS)}"
            )

        repository = factory()
        repository.initialise()
        _REPOSITORY = repository
        logger.info("Case store backend: %s", name)
        return repository


def set_repository(repository: Repository | None) -> None:
    """
    Replace the process-wide repository.

    For tests, which point it at a temporary file, and for a deployment that
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
    "store_path",
]
