"""
THE POSTGRES CASE STORE (app/store/postgres_repo.py).

Unit: the SQL translation, the generated DDL and the production guard -- no
server needed. Live (marker live_pg, LOS_TEST_PG_DSN=postgresql://...): the
repository contract on a real PostgreSQL -- migrations, round trips,
constraints, append-only history, compare-and-set, concurrent writers.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import postgres_repo as pg


@pytest.mark.parametrize("sqlite_sql, postgres_sql", [
    ("SELECT * FROM documents WHERE case_id = ?", "SELECT * FROM documents WHERE case_id = %s"),
    ("SELECT * FROM t WHERE IFNULL(party_id, '') = ?", "SELECT * FROM t WHERE COALESCE(party_id, '') = %s"),
    ("SELECT * FROM t ORDER BY created_at, rowid", "SELECT * FROM t ORDER BY created_at"),
    ("INSERT OR IGNORE INTO access_grants (a, b) VALUES (?, ?)",
     "INSERT INTO access_grants (a, b) VALUES (%s, %s) ON CONFLICT DO NOTHING"),
    ("BEGIN IMMEDIATE", "BEGIN"),
    ("SELECT '?' AS q, ? AS v", "SELECT '?' AS q, %s AS v"),          # a quoted ? is data
    ("SELECT * FROM t WHERE x LIKE '5%' AND y = ?", "SELECT * FROM t WHERE x LIKE '5%%' AND y = %s"),
])
def test_statements_translate_to_postgres(sqlite_sql, postgres_sql):
    assert pg.translate(sqlite_sql) == postgres_sql


def test_the_baseline_ddl_is_postgres_and_keeps_every_guarantee():
    ddl = pg.schema_ddl()
    assert "IFNULL" not in ddl and "RAISE(ABORT" not in ddl and "PRAGMA" not in ddl
    # Postgres REAL is a 4-byte float: an epoch timestamp would round by ~a minute
    assert " REAL" not in ddl and "DOUBLE PRECISION" in ddl
    for table in ("applicants", "applications", "documents", "case_findings", "document_versions",
                  "case_decisions", "case_events", "ocr_jobs", "access_grants", "case_stage",
                  "stage_transitions", "jev_runs", "approvals"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table
    # append-only history and runs, as triggers that raise
    assert "CREATE OR REPLACE TRIGGER stage_transitions_no_update BEFORE UPDATE ON stage_transitions" in ddl
    assert "CREATE OR REPLACE TRIGGER stage_transitions_no_delete BEFORE DELETE ON stage_transitions" in ddl
    assert "CREATE OR REPLACE TRIGGER jev_runs_no_update" in ddl
    assert "RAISE EXCEPTION 'stage history is append-only'" in ddl
    # constraints, foreign keys and the expression identity index survive
    assert "FOREIGN KEY (case_id) REFERENCES applications (case_id)" in ddl
    assert "CHECK (checker_id IS NULL OR checker_id <> maker_id)" in ddl
    assert "COALESCE(party_id, '')" in ddl
    # columns added after the baseline, idempotently
    assert "ADD COLUMN IF NOT EXISTS co_applicant_id" in ddl
    assert [m[0] for m in pg.MIGRATIONS] == sorted(m[0] for m in pg.MIGRATIONS)


def test_postgres_is_the_only_store_and_production_needs_a_dsn(monkeypatch):
    from app.store import _build_postgres, store_backend
    from app.store.repository import RepositoryError

    assert store_backend() == "postgres"
    monkeypatch.setenv("LOS_STORE_BACKEND", "sqlite")
    with pytest.raises(RepositoryError, match="not supported"):
        _build_postgres()
    monkeypatch.delenv("LOS_STORE_BACKEND")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("LOS_DATA_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RepositoryError, match="LOS_DATA_ENCRYPTION_KEY"):  # values are encrypted at rest
        _build_postgres()
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LOS_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("LOS_STORE_DSN", "")
    with pytest.raises(RepositoryError, match="LOS_STORE_DSN"):      # no embedded server in production
        _build_postgres()


# ---------------------------------------------------------------------------
# live: a real PostgreSQL
# ---------------------------------------------------------------------------

DSN = os.getenv("LOS_TEST_PG_DSN", "")
live = pytest.mark.skipif(not DSN, reason="set LOS_TEST_PG_DSN=postgresql://... to run against PostgreSQL")


@pytest.fixture
def pgrepo():
    import psycopg

    schema = f"los_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in DSN else "?"
    repo = pg.PostgresRepository(f"{DSN}{sep}options=-csearch_path%3D{schema}", max_size=8)
    repo.initialise()
    yield repo
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.live_pg
@live
def test_live_round_trip_constraints_and_append_only(pgrepo):
    from app.store.models import (Applicant, Application, CaseEvent, CaseFinding, CaseStage, Document,
                                  DocumentStatus, FindingKind, StageTransition)

    pgrepo.save_applicant(Applicant(applicant_id="A1", full_name="Asha Rao"))
    pgrepo.save_application(Application(case_id="C1", applicant_id="A1", product="PERSONAL_LOAN"))
    pgrepo.save_document(Document(document_id="C1:A1:pan.jpg", case_id="C1", applicant_id="A1",
                                  document_type="PAN", status=DocumentStatus.VERIFIED, party_id="A1"))
    assert [d.document_type for d in pgrepo.list_documents("C1")] == ["PAN"]
    pgrepo.save_finding(CaseFinding(finding_id="F1", case_id="C1", finding_kind=FindingKind.KYC, party_id="A1",
                                    status="PASS", payload={"fields": []}, source_type="KYC"))
    assert pgrepo.get_current_findings("C1", kind="KYC")[0].status == "PASS"
    # a foreign key holds: no document on a case that does not exist
    with pytest.raises(Exception):
        pgrepo.save_document(Document(document_id="X", case_id="NO-CASE", applicant_id="A1",
                                      document_type="PAN"))
    # stage history is append-only, enforced by the database itself
    pgrepo.apply_stage_transition(
        0, CaseStage(case_id="C1", stage="CPA", stage_status="IN_PROGRESS", version=1),
        StageTransition(transition_id="T1", case_id="C1", version=1, kind="STAGE_ENTERED", to_stage="CPA",
                        to_status="IN_PROGRESS"),
        CaseEvent(event_id="E1", case_id="C1", event_type="STAGE_ENTERED", stage="CPA"))
    from app.store.repository import RepositoryError

    with pytest.raises(RepositoryError, match="append-only"):          # the database refuses it
        pgrepo._write("UPDATE stage_transitions SET reason = ? WHERE transition_id = ?", ("x", "T1"))


@pytest.mark.live_pg
@live
def test_live_compare_and_set_lets_exactly_one_concurrent_writer_win(pgrepo):
    from app.store.models import Applicant, Application

    pgrepo.save_applicant(Applicant(applicant_id="A2", full_name="Ravi"))
    pgrepo.save_application(Application(case_id="C2", applicant_id="A2", product="PERSONAL_LOAN"))
    now = "2026-10-05T00:00:00+00:00"
    pgrepo.save_approval({"approval_id": "APR-1", "case_id": "C2", "action_type": "STAGE_OVERRIDE",
                          "resource_type": "CASE_STAGE", "resource_id": "C2", "maker_id": "m", "checker_id": None,
                          "status": "PENDING_CHECK", "reason": "r", "comments": None, "payload": {}, "result": None,
                          "policy_reference": "x", "evidence_version": "FOS:0", "version": 1,
                          "created_at": now, "updated_at": now, "expires_at": now})

    def decide(checker):
        row = dict(pgrepo.get_approval("APR-1"), status="APPROVED", checker_id=checker, updated_at=now)
        return pgrepo.update_approval(row, expected_version=1)

    with ThreadPoolExecutor(max_workers=6) as pool:
        wins = list(pool.map(decide, [f"c{i}" for i in range(6)]))
    assert wins.count(True) == 1                                         # four eyes, one decision


@pytest.mark.live_pg
@live
def test_live_migrations_are_recorded_and_idempotent(pgrepo):
    pgrepo._initialised = False
    pgrepo.initialise()                                                  # second start: nothing re-applied
    assert pgrepo.health()["schema_version"] == pg.MIGRATIONS[-1][0]


@pytest.mark.live_pg
@live
def test_live_conversation_state_is_shared_through_the_case_store(pgrepo, monkeypatch):
    """Copilot memory in PostgreSQL: one conversation across workers and restarts."""
    import time as _time

    pgrepo.put_conversation("subj", "conv-1", '{"case_id": "C1"}', _time.time())
    pgrepo.put_conversation("subj", "conv-1", '{"case_id": "C1", "x": 1}', _time.time())   # upsert
    assert pgrepo.get_conversation("subj", "conv-1")[0] == '{"case_id": "C1", "x": 1}'
    assert pgrepo.delete_conversations(older_than=_time.time() + 1) == 1
    assert pgrepo.get_conversation("subj", "conv-1") is None
