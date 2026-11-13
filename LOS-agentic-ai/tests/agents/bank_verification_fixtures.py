"""Shared fixtures for the bank verification tests: a seeded store built once, copied fresh for each test.

The store runs on SQLite (the demo default) -- or, with BANK_VERIFICATION_TEST_PG=1, on PostgreSQL: the template is a
database on the test PostgreSQL server (app.store.testing: the disposable pgserver cluster or LOS_TEST_PG_DSN), each
test gets its own clone (CREATE DATABASE ... TEMPLATE), dropped afterwards. The same tests prove both backends.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

ON_POSTGRES = (os.getenv("BANK_VERIFICATION_TEST_PG") or "").strip().lower() in {"1", "true", "yes", "on"}
_PG: dict[str, str] = {}                                   # admin / template DSNs when on PostgreSQL


def _pg_admin() -> str:
    from app.store.testing import _admin_dsn

    if "admin" not in _PG:
        _PG["admin"] = _admin_dsn()
    return _PG["admin"]


def _pg_create(name: str, template: str | None = None) -> str:
    import psycopg

    from app.store.testing import _with_db

    with psycopg.connect(_pg_admin(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
    return _with_db(_pg_admin(), name)


def _pg_drop(name: str) -> None:
    import psycopg

    with psycopg.connect(_pg_admin(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def bv_template(tmp_path_factory) -> Path:
    """The demo store, seeded and processed ONCE (twelve scenarios + two historical cases)."""
    root = tmp_path_factory.mktemp("bv_template")
    saved = {k: os.environ.get(k) for k in ("BANK_VERIFICATION_HOME", "BANK_VERIFICATION_DSN")}
    os.environ["BANK_VERIFICATION_HOME"] = str(root)
    os.environ["BANK_VERIFICATION_AI_SUMMARY"] = "off"      # tests never call the model
    os.environ["BANK_VERIFICATION_LOS"] = "off"             # tests never read the real LOS case store
    os.environ["BANK_VERIFICATION_SCANS"] = "off"           # OCR only in the tests marked `ocr`
    if ON_POSTGRES:
        _PG["template"] = f"bv_tpl_{uuid.uuid4().hex[:8]}"
        os.environ["BANK_VERIFICATION_DSN"] = _pg_create(_PG["template"])
    try:
        from app.agents.credit.bank_verification.demo_data import seed
        from app.agents.credit.bank_verification.repository import Repository, close_pools

        seed.reset(Repository(root), start_state=False)      # every case processed: the expected results are checked
        if ON_POSTGRES:
            close_pools(os.environ["BANK_VERIFICATION_DSN"])  # a template must have no open connection to be copied
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    yield root
    if ON_POSTGRES:
        _pg_drop(_PG["template"])


@pytest.fixture
def bv_home(bv_template, tmp_path, monkeypatch) -> Path:
    """A private copy of the seeded store for one test; the stored file paths are re-pointed to the copy."""
    home = tmp_path / "bv"
    shutil.copytree(bv_template, home)
    monkeypatch.setenv("BANK_VERIFICATION_HOME", str(home))
    monkeypatch.setenv("BANK_VERIFICATION_AI_SUMMARY", "off")
    monkeypatch.setenv("BANK_VERIFICATION_LOS", "off")
    monkeypatch.setenv("BANK_VERIFICATION_SCANS", "off")
    if ON_POSTGRES:
        import psycopg

        name = f"bv_t_{uuid.uuid4().hex[:10]}"
        dsn = _pg_create(name, _PG["template"])
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE documents SET stored_path = replace(stored_path, %s, %s)", (str(bv_template), str(home)))
        monkeypatch.setenv("BANK_VERIFICATION_DSN", dsn)
        yield home
        from app.agents.credit.bank_verification.repository import close_pools

        close_pools(dsn)
        _pg_drop(name)
        return
    import sqlite3

    db = sqlite3.connect(home / "bank_verification.sqlite3")
    db.execute("UPDATE documents SET stored_path = replace(stored_path, ?, ?)", (str(bv_template), str(home)))
    db.commit()
    db.close()
    yield home


@pytest.fixture
def bv_repo(bv_home):
    from app.agents.credit.bank_verification.repository import Repository

    return Repository(bv_home)


@pytest.fixture
def bv_client(bv_home):
    from fastapi.testclient import TestClient

    from app.agents.credit.bank_verification.main import create_app

    with TestClient(create_app()) as client:
        yield client


CPA = {"X-Demo-Role": "CPA", "X-Demo-User": "test-cpa"}
AGENCY = {"X-Demo-Role": "AGENCY", "X-Demo-User": "test-agency"}
