"""The test store's speed-ups (app/store/testing.py a-c) change nothing a test can see: isolation, schema, gated
migrations and cleanup all hold."""

from __future__ import annotations

import os
import time

import psycopg
import pytest

from app.store import testing
from app.store.models import Applicant
from app.store.postgres_repo import PostgresRepository


def _exists(name: str) -> bool:
    with psycopg.connect(testing._STATE["admin"], autocommit=True) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def test_a_clone_is_ready_without_the_migration_check_and_fully_usable():
    repo = testing.fresh_repository()
    t = time.perf_counter()
    repo.initialise()
    assert time.perf_counter() - t < 0.2                        # no lock / catalogue round trips
    assert isinstance(repo, PostgresRepository)
    repo.save_applicant(Applicant(applicant_id="A-INFRA", full_name="Infra Test"))
    assert repo.get_applicant("A-INFRA").full_name == "Infra Test"
    assert repo.table_exists("schema_migrations") and repo.table_exists("applications")


def test_two_clones_share_nothing():
    one, two = testing.fresh_repository(), testing.fresh_repository()
    one.initialise()
    two.initialise()
    one.save_applicant(Applicant(applicant_id="A-ONLY-ONE"))
    assert two.get_applicant("A-ONLY-ONE") is None


def test_a_gated_migration_due_under_the_current_flags_still_runs_the_full_path(monkeypatch):
    import app.store.postgres_repo as pg

    calls: list[str] = []
    real = pg.PostgresRepository.initialise

    def spy(self):
        calls.append("full")
        return real(self)

    monkeypatch.setattr(pg.PostgresRepository, "initialise", spy)
    pending = [v for v, *_ in pg.GATED_MIGRATIONS if v not in testing._STATE.get("versions", frozenset())]
    if not pending:
        pytest.skip("the template already holds every gated migration")
    monkeypatch.setattr(pg, "_gated_auto_apply_allowed", lambda version="0004": (True, "test"))
    repo = testing.fresh_repository()
    try:
        repo.initialise()
    except Exception:  # noqa: BLE001 - the full path ran; whether its DDL applies here is not the point
        pass
    assert calls == ["full"]


def test_retired_clones_are_dropped_in_the_background():
    first = testing.fresh_repository()
    first_name = first._dsn.rsplit("/", 1)[-1].split("?")[0]
    for _ in range(testing._KEEP_OPEN + 1):
        testing.fresh_repository()
    testing._DROPS.join()                                        # the worker caught up
    assert not _exists(first_name)


def test_the_disposable_test_cluster_runs_without_durability():
    if (os.getenv("LOS_TEST_PG_DSN") or "").strip() or os.getenv("LOS_TEST_PG_FAST", "true").lower() == "false":
        pytest.skip("not the disposable pgserver cluster")
    testing._setup()
    with psycopg.connect(testing._STATE["admin"], autocommit=True) as conn:
        assert {k: conn.execute(f"SHOW {k}").fetchone()[0] for k in ("fsync", "synchronous_commit")} == {
            "fsync": "off", "synchronous_commit": "off"}


# ---- safety (2026-10-22): the real flags, and never anyone else's database -----------------------------------------
def test_the_real_gated_flag_is_rechecked_on_every_clone(monkeypatch, tmp_path):
    backup = tmp_path / "synthetic_pg_dump.sql"
    backup.write_text("-- PostgreSQL database dump (synthetic, for the test)\n", encoding="utf-8")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("LOS_COAPP_IDENTITY", "false")
    off = testing.fresh_repository()
    off.initialise()
    assert not off.table_exists("co_applicants")              # flag off: the validated clone, nothing applied
    monkeypatch.setenv("LOS_COAPP_IDENTITY", "true")
    monkeypatch.setenv("LOS_MIGRATION_BACKUP_FILE", str(backup))
    on = testing.fresh_repository()
    on.initialise()
    assert on.table_exists("co_applicants")                   # flag on NOW: the full path applied gated 0004


def test_a_named_server_is_never_reconfigured(monkeypatch):
    import psycopg as _psycopg

    monkeypatch.setenv("LOS_TEST_PG_DSN", "postgresql://someone-else:5432/postgres")
    monkeypatch.setattr(_psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    testing._fast_settings_on_disposable_cluster("postgresql://someone-else:5432/postgres")   # returns untouched


def test_a_data_directory_outside_the_test_cluster_is_left_alone(monkeypatch, tmp_path):
    executed: list[str] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *args):
            executed.append(sql)

            class Row:
                @staticmethod
                def fetchone():
                    return ["C:/somewhere/runtime/pgdata" if "data_directory" in sql else "on"]
            return Row()

    import psycopg as _psycopg

    monkeypatch.delenv("LOS_TEST_PG_DSN", raising=False)
    monkeypatch.setenv("LOS_TEST_PGDATA", str(tmp_path / "los_pg_test"))
    monkeypatch.setattr(_psycopg, "connect", lambda *a, **k: Conn())
    testing._fast_settings_on_disposable_cluster("postgresql://local/postgres")
    assert executed == ["SHOW data_directory"]                # looked, refused: no ALTER SYSTEM
