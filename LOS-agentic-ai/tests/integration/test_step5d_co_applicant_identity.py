"""
PHASE 3 STEP 5d -- co-applicant identity (LOS_COAPP_IDENTITY, default off).

Migration 0004/0005 (gated), the revoked_at-aware access check, system-generated
COAPP-<12 hex> ids, encrypted profiles, the KYC-verified name, access through the
case, the backfill (dry-run / apply / revert, backup required), the chat by id and
name, the 👥 header, session memory by id, and the MCP co_applicant.get tool.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.los import co_applicants
from app.security import access
from app.store import set_repository
from app.store.models import (Applicant, Application, CaseFinding, Document, DocumentStatus, FindingKind)
from app.store.postgres_repo import _gated_auto_apply_allowed, apply_gated
from app.store.testing import fresh_repository
from tests.integration.test_fos_stage_boundary import FOS_SCOPES

OWNER, OTHER = "fos-owner", "fos-other"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv(co_applicants.FLAG, raising=False)
    monkeypatch.delenv("LOS_MIGRATION_BACKUP_FILE", raising=False)
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")


@pytest.fixture(autouse=True)
def backfill_logs_in_tmp(monkeypatch, tmp_path):
    """Run logs and default plan files go to tmp_path; runs/backfills/ holds real runs only."""
    from scripts import backfill_co_applicants as bf
    monkeypatch.setattr(bf, "ROOT", tmp_path)


@pytest.fixture
def repo():
    repository = fresh_repository()
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def ready(repo, monkeypatch):
    apply_gated(repo, "0004")
    monkeypatch.setenv(co_applicants.FLAG, "true")
    return repo


@pytest.fixture
def backup(tmp_path):
    path = tmp_path / "los_test.dump"
    path.write_bytes(b"PGDMP" + b"\x00" * 64)
    return str(path)


def make_case(repo, *, co_id=None, owner=OWNER, created=None):
    applicant_id, case_id = f"APP-{uuid.uuid4().hex[:12].upper()}", f"CASE-{uuid.uuid4().hex[:12].upper()}"
    repo.save_applicant(Applicant(applicant_id=applicant_id, full_name="Rahul Sharma"))
    repo.save_application(Application(case_id=case_id, applicant_id=applicant_id, product="PERSONAL_LOAN",
                                      co_applicant_id=co_id, **({"created_at": created} if created else {})))
    if owner:
        repo.grant_access(owner, "CASE", case_id)
        repo.grant_access(owner, "APPLICANT", applicant_id)
    return case_id, applicant_id


def co_doc(repo, case_id, applicant_id, co_id, doc_type="PAN"):
    repo.save_document(Document(document_id=f"{case_id}:{co_id}:{doc_type.lower()}.jpg", case_id=case_id,
                                applicant_id=applicant_id, document_type=doc_type, party_id=co_id,
                                party_role="CO_APPLICANT", status=DocumentStatus.VERIFIED))


def kyc_name(repo, case_id, party_id, status="PASS", value="PRIYA SHARMA"):
    repo.save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex, case_id=case_id, finding_kind=FindingKind.KYC, party_id=party_id, stage="FOS",
        status=status, source_type="KYC", source_id=f"kyc-{uuid.uuid4().hex[:6]}", document_id=None,
        payload={"fields": [{"field": "NAME", "status": status,
                             "sources": [{"document_type": "PAN", "value": value},
                                         {"document_type": "AADHAAR", "value": value.title()}]}]},
        content_hash=uuid.uuid4().hex))


# ---- backup check ------------------------------------------------------------------------------
def test_a_fresh_pg_dump_is_required(tmp_path, backup):
    from app.store.backup_check import BackupRequired, require_fresh_backup

    assert str(require_fresh_backup(backup)) == backup
    plain = tmp_path / "plain.sql"
    plain.write_text("--\n-- PostgreSQL database dump\n--\n")
    assert require_fresh_backup(plain)
    empty, junk, old = tmp_path / "e.dump", tmp_path / "j.dump", tmp_path / "o.dump"
    empty.write_bytes(b"")
    junk.write_bytes(b"not a dump")
    old.write_bytes(b"PGDMP....")
    os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
    for bad in (None, tmp_path / "missing.dump", empty, junk, old):
        with pytest.raises(BackupRequired):
            require_fresh_backup(bad)


# ---- migrations --------------------------------------------------------------------------------
def test_0004_creates_the_tables_once(repo):
    assert not repo.table_exists("co_applicants") and not repo.grants_revocable()
    assert apply_gated(repo, "0004") is True
    assert repo.table_exists("co_applicants") and repo.table_exists("co_applicant_id_remap")
    assert repo.grants_revocable()
    assert apply_gated(repo, "0004") is False


def test_0005_refuses_while_a_duplicate_remains_then_enforces_uniqueness(ready):
    from app.store.repository import RepositoryError

    make_case(ready, co_id="COAPP-DUP")
    second, _ = make_case(ready, co_id="COAPP-DUP")
    with pytest.raises(RepositoryError):
        apply_gated(ready, "0005")
    ready._write("UPDATE applications SET co_applicant_id = 'COAPP-OTHER' WHERE case_id = ?", (second,))
    assert apply_gated(ready, "0005") is True
    with pytest.raises(Exception):
        make_case(ready, co_id="COAPP-OTHER")


def test_gated_migrations_never_auto_apply_in_production_or_without_a_backup(monkeypatch, backup):
    monkeypatch.setenv(co_applicants.FLAG, "true")
    assert _gated_auto_apply_allowed()[0] is False                       # no backup named
    monkeypatch.setenv("LOS_MIGRATION_BACKUP_FILE", backup)
    assert _gated_auto_apply_allowed()[0] is True                        # dev, flag, backup
    monkeypatch.setenv("ENVIRONMENT", "production")
    allowed, why = _gated_auto_apply_allowed()
    assert allowed is False and why.startswith("production")
    monkeypatch.delenv("ENVIRONMENT")
    monkeypatch.delenv(co_applicants.FLAG)
    assert _gated_auto_apply_allowed()[0] is False


# ---- revoked grants ----------------------------------------------------------------------------
def test_has_access_works_before_0004_and_ignores_revoked_grants_after(repo):
    repo.grant_access(OWNER, "APPLICANT", "COAPP-X")
    assert repo.has_access(OWNER, "APPLICANT", "COAPP-X")              # no revoked_at column yet
    apply_gated(repo, "0004")
    repo._write("UPDATE access_grants SET revoked_at = 'now', revoked_reason = 'test' WHERE resource_id = 'COAPP-X'",
                ())
    assert not repo.has_access(OWNER, "APPLICANT", "COAPP-X")
    repo.grant_access(OWNER, "APPLICANT", "COAPP-X")                    # a re-grant does not un-revoke
    assert not repo.has_access(OWNER, "APPLICANT", "COAPP-X")


# ---- ids, records, encryption ------------------------------------------------------------------
def test_ids_are_generated_in_the_applicant_convention_and_unique(ready):
    ids = {co_applicants.generate_id(ready) for _ in range(20)}
    assert len(ids) == 20 and all(len(i) == 18 and i.startswith("COAPP-") and i[6:].isalnum() for i in ids)


def test_a_supplied_id_is_accepted_only_on_its_own_case(ready):
    case_id, _ = make_case(ready, co_id="COAPP-MINE")
    other, _ = make_case(ready)
    assert co_applicants.accept_supplied(case_id, "COAPP-MINE", ready) == "COAPP-MINE"
    for case, co in ((other, "COAPP-MINE"), (case_id, "COAPP-UNKNOWN"), (None, "COAPP-MINE")):
        with pytest.raises(co_applicants.CoApplicantError) as refused:
            co_applicants.accept_supplied(case, co, ready)
        assert refused.value.code == "CO_APPLICANT_ID_NOT_ON_CASE"


def test_the_declared_profile_is_stored_encrypted(ready):
    case_id, applicant_id = make_case(ready, co_id="COAPP-ENC")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-ENC",
                                {"name": "Priya Sharma", "pan_number": "ABCDE1234F", "date_of_birth": "1990-01-01"},
                                "spouse", ready)
    raw = ready._one("SELECT * FROM co_applicants WHERE co_applicant_id = 'COAPP-ENC'", ())
    assert "Priya" not in str(dict(raw)) and "ABCDE1234F" not in str(dict(raw))
    record = co_applicants.get("COAPP-ENC", ready)
    assert record["name"] == "Priya Sharma" and record["pan"] == "ABCDE1234F"
    assert record["name_source"] == "DECLARED" and record["relationship"] == "SPOUSE"


def test_a_name_is_filled_only_from_a_passed_kyc_name_check(ready):
    case_id, applicant_id = make_case(ready, co_id="COAPP-KYC")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-KYC", {}, None, ready)
    kyc_name(ready, case_id, "COAPP-KYC", status="REVIEW")
    assert co_applicants.fill_verified_names(case_id, ready) == 0       # unverified: nothing
    kyc_name(ready, case_id, "COAPP-KYC", status="PASS")
    assert co_applicants.fill_verified_names(case_id, ready) == 1
    record = co_applicants.get("COAPP-KYC", ready)
    assert record["name"] == "PRIYA SHARMA" and record["name_source"] == "KYC_VERIFIED"   # the PAN's spelling


# ---- access through the case -------------------------------------------------------------------
def test_access_goes_through_the_case(ready):
    case_id, _ = make_case(ready, co_id="COAPP-OWN")
    assert access.authorize_co_applicant(OWNER, [], "COAPP-OWN") == case_id
    for subject, co, case in ((OTHER, "COAPP-OWN", None), (OWNER, "COAPP-NOPE", None), (OWNER, "COAPP-OWN", "CASE-X")):
        with pytest.raises(access.AccessDenied):
            access.authorize_co_applicant(subject, [], co, case_id=case)


def test_a_duplicate_id_on_two_owned_cases_is_never_guessed(ready):
    make_case(ready, co_id="COAPP-TWICE")
    make_case(ready, co_id="COAPP-TWICE")
    with pytest.raises(access.AccessDenied):
        access.authorize_co_applicant(OWNER, [], "COAPP-TWICE")


# ---- backfill ----------------------------------------------------------------------------------
def duplicate_world(repo):
    now = datetime.now(timezone.utc)
    older, a1 = make_case(repo, co_id="COAPP-J1", created=now - timedelta(minutes=2))
    newer, a2 = make_case(repo, co_id="COAPP-J1", created=now)
    for case, applicant in ((older, a1), (newer, a2)):
        co_doc(repo, case, applicant, "COAPP-J1", "PAN")
        co_doc(repo, case, applicant, "COAPP-J1", "VOTER_ID")
        kyc_name(repo, case, "COAPP-J1", status="PASS")
    repo.grant_access("joiner", "APPLICANT", "COAPP-J1")                 # the stray grant
    return older, newer


def test_the_dry_run_plans_without_writing(ready):
    from scripts import backfill_co_applicants as bf

    older, newer = duplicate_world(ready)
    plan = bf.build_plan(ready)
    assert [(r["case_id"], r["old_id"], r["kept_on"]) for r in plan["remaps"]] == [(newer, "COAPP-J1", older)]
    assert plan["remaps"][0]["rows"]["documents"] == 2 and plan["remaps"][0]["rows"]["applications"] == 1
    assert len(plan["inserts"]) == 2 and all(i["verified_name_available"] for i in plan["inserts"])
    assert [g["resource_id"] for g in plan["grants_to_revoke"]] == ["COAPP-J1"]
    assert bf.public_plan(plan)["grants_to_revoke"][0]["subject"] == "joi***"
    assert plan["safe_to_apply"] and plan["grant_impact"][0]["lost_cases"] == []
    assert ready._one("SELECT count(*) AS n FROM co_applicants", ())["n"] == 0


def plan_file(repo, tmp_path, **kwargs):
    from scripts import backfill_co_applicants as bf

    plan = bf.build_plan(repo, **kwargs)
    return plan, str(bf.write_plan(plan, str(tmp_path / f"plan_{uuid.uuid4().hex[:6]}.json")))


def test_apply_refuses_without_a_fresh_backup_or_a_plan_file(ready, backup, tmp_path):
    from scripts import backfill_co_applicants as bf

    duplicate_world(ready)
    _, path = plan_file(ready, tmp_path)
    with pytest.raises(bf.Refused):
        bf.apply(ready, None, path)
    with pytest.raises(bf.Refused):
        bf.apply(ready, backup, None)


def test_apply_refuses_when_the_database_changed_since_the_plan(ready, backup, tmp_path):
    from scripts import backfill_co_applicants as bf

    older, newer = duplicate_world(ready)
    _, path = plan_file(ready, tmp_path)
    case_id, applicant_id = make_case(ready, co_id="COAPP-LATE")            # a new co-applicant appears
    with pytest.raises(bf.Refused, match="changed since the plan"):
        bf.apply(ready, backup, path)
    assert ready._one("SELECT count(*) AS n FROM co_applicants", ())["n"] == 0


def test_a_tampered_plan_file_is_refused(ready, backup, tmp_path):
    import json

    from scripts import backfill_co_applicants as bf

    duplicate_world(ready)
    _, path = plan_file(ready, tmp_path)
    stored = json.loads(open(path, encoding="utf-8").read())
    stored["remaps"][0]["new_id"] = "COAPP-CHOSENBYHAND"
    open(path, "w", encoding="utf-8").write(json.dumps(stored))
    with pytest.raises(bf.Refused, match="not an intact plan"):
        bf.apply(ready, backup, path)


def test_the_revoked_subject_keeps_every_case_they_own(ready):
    from scripts import backfill_co_applicants as bf

    older, newer = duplicate_world(ready)
    mine, my_applicant = make_case(ready, co_id=None, owner="joiner")
    impact = bf.build_plan(ready)["grant_impact"][0]
    assert impact["subject"] == "joiner"
    assert mine in impact["before"]["cases"] and impact["before"] == impact["after"]
    assert impact["lost_cases"] == [] and impact["lost_applicants"] == []


def test_a_plan_that_would_cut_legitimate_access_is_refused(ready, backup, tmp_path, monkeypatch):
    """Defense in depth: by construction a revoked grant opens nothing, but if it ever did, nothing runs."""
    from scripts import backfill_co_applicants as bf

    duplicate_world(ready)
    real = bf._grant_impact
    monkeypatch.setattr(bf, "_grant_impact", lambda *a: [{**i, "lost_cases": ["CASE-LEGIT"]} for i in real(*a)])
    plan, path = plan_file(ready, tmp_path)
    assert plan["safe_to_apply"] is False
    with pytest.raises(bf.Refused):
        bf.apply(ready, backup, path)
    assert ready._one("SELECT count(*) AS n FROM co_applicants", ())["n"] == 0


def test_an_applicant_id_conflict_is_remapped_and_an_excluded_case_left_alone(ready, tmp_path):
    from scripts import backfill_co_applicants as bf

    clash, a1 = make_case(ready, co_id=None)
    ready._write("UPDATE applications SET co_applicant_id = ? WHERE case_id = ?", (a1, clash))   # co id == applicant id
    evalcase, _ = make_case(ready, co_id=None)
    ready.save_applicant(Applicant(applicant_id="COAPP-EVALX"))
    ready._write("UPDATE applications SET co_applicant_id = 'COAPP-EVALX' WHERE case_id = ?", (evalcase,))
    plan = bf.build_plan(ready, exclude_cases=[evalcase])
    assert [(r["case_id"], r["reason"]) for r in plan["remaps"]] == [(clash, "APPLICANT_ID_CONFLICT")]
    assert [e["case_id"] for e in plan["excluded"]] == [evalcase] and plan["excluded"][0]["also_an_applicant_id"]
    assert evalcase not in {i["case_id"] for i in plan["inserts"]}


def test_apply_executes_exactly_the_reviewed_ids_then_reverts(ready, backup, tmp_path):
    from scripts import backfill_co_applicants as bf

    older, newer = duplicate_world(ready)
    plan, path = plan_file(ready, tmp_path)
    reviewed = plan["remaps"][0]["new_id"]
    done = bf.apply(ready, backup, path)
    new_id = ready.get_application(newer).co_applicant_id
    assert new_id == reviewed                                              # the id that was reviewed, exactly
    assert ready.get_application(older).co_applicant_id == "COAPP-J1" and new_id.startswith("COAPP-") \
        and new_id != "COAPP-J1"
    assert {d.party_id for d in ready.list_documents(newer) if d.party_role == "CO_APPLICANT"} == {new_id}
    assert {co["co_applicant_id"] for co in co_applicants.list_for_case(newer, ready)} == {new_id}
    assert co_applicants.get(new_id, ready)["name_source"] == "KYC_VERIFIED"
    grant = ready._one("SELECT * FROM access_grants WHERE resource_id = 'COAPP-J1'", ())
    assert grant is not None and grant["revoked_at"]                     # kept, revoked -- never deleted
    assert not ready.has_access("joiner", "APPLICANT", "COAPP-J1")
    assert os.path.isfile(done["log_file"])
    assert bf.build_plan(ready)["remaps"] == [] and bf.build_plan(ready)["inserts"] == []   # idempotent
    with pytest.raises(bf.Refused):
        bf.apply(ready, backup, path)                                     # the same plan cannot run twice

    bf.revert(ready, done["run_id"], backup)
    assert ready.get_application(newer).co_applicant_id == "COAPP-J1"
    assert {d.party_id for d in ready.list_documents(newer) if d.party_role == "CO_APPLICANT"} == {"COAPP-J1"}
    assert ready._one("SELECT count(*) AS n FROM co_applicants", ())["n"] == 0
    assert ready.has_access("joiner", "APPLICANT", "COAPP-J1")


# ---- the chat, the header, memory --------------------------------------------------------------
@pytest.fixture
def client(make_token):
    from fastapi.testclient import TestClient

    import main

    def build(subject):
        c = TestClient(main.app)
        c.headers.update({"Authorization": f"Bearer {make_token(subject=subject, scopes=FOS_SCOPES)}"})
        return c
    return build


def test_another_cases_co_applicant_id_is_refused_before_anything_runs(ready, client, monkeypatch):
    from app.api.routes import fos_api

    case_id, applicant_id = make_case(ready, co_id="COAPP-THEIRS", owner=OTHER)
    mine, my_applicant = make_case(ready)
    called = []

    async def never(*args, **kwargs):
        called.append(1)
        raise AssertionError("no downstream call may happen")
    monkeypatch.setattr(fos_api, "_answer_action", never)
    r = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": my_applicant, "case_id": mine,
                                                        "message": "COAPP-THEIRS ke docs kya baaki hai?"})
    assert r.status_code == 403 and not called, r.text
    r = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": my_applicant,
                                                        "co_applicant_id": "COAPP-THEIRS", "message": "status?"})
    assert r.status_code == 403 and not called, r.text


def test_the_id_in_a_question_answers_for_that_co_applicant_with_the_header(ready, client):
    case_id, applicant_id = make_case(ready, co_id="COAPP-1023ABCDEF01")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-1023ABCDEF01", {"name": "Priya Sharma"}, None, ready)
    r = client(OWNER).post("/api/v1/fos/copilot", json={"applicant_id": applicant_id,
                                                        "message": "COAPP-1023ABCDEF01 ke docs kya baaki hai?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_id"] == case_id
    assert body["answer"].startswith("👥 Co-applicant: Priya Sharma (COAPP-1023ABCDEF01)"), body["answer"]
    assert body["co_applicant"] == {"co_applicant_id": "COAPP-1023ABCDEF01", "name": "Priya Sharma"}


def test_a_name_in_a_question_is_understood_on_the_callers_own_case(ready, client):
    from app.api.routes.fos_api import CopilotRequest, FosAction, _resolve_co_applicant

    case_id, applicant_id = make_case(ready, co_id="COAPP-PRIYA0000001")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-PRIYA0000001", {"name": "Priya Sharma"}, None, ready)
    payload = CopilotRequest(applicant_id=applicant_id, case_id=case_id, message="Priya ke docs?")
    claims = {"sub": OWNER, "scope": " ".join(FOS_SCOPES)}
    resolved, case, message = _resolve_co_applicant(payload, claims, "r1", case_id, "Priya ke docs?",
                                                    FosAction.CUSTOM_QUERY)
    assert resolved == (case_id, "COAPP-PRIYA0000001") and message == "co-applicant ke docs?"
    claims = {"sub": OTHER, "scope": " ".join(FOS_SCOPES)}
    resolved, _, message = _resolve_co_applicant(payload, claims, "r2", case_id, "Priya ke docs?",
                                                 FosAction.CUSTOM_QUERY)
    assert resolved is None and message == "Priya ke docs?"           # not their case: nothing is read


def test_session_memory_keeps_the_active_party_by_id(ready):
    from app.agents.applicant.copilot.conversation.state import ConversationState, _party_id_for

    case_id, applicant_id = make_case(ready, co_id="COAPP-MEM000000001")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-MEM000000001", {}, None, ready)
    state = ConversationState(conversation_id="c1", subject_key=OWNER, case_id=case_id)
    state.last_subject_party = "CO_APPLICANT"
    assert _party_id_for(state, {"case_id": case_id}) == "COAPP-MEM000000001"
    state.last_subject_party = "PRIMARY_APPLICANT"
    assert _party_id_for(state, {"applicant_id": applicant_id}) == applicant_id


# ---- MCP ---------------------------------------------------------------------------------------
def test_the_mcp_tool_returns_only_that_co_applicants_documents(ready):
    from app.mcp.applicant import co_applicant_get

    case_id, applicant_id = make_case(ready, co_id="COAPP-MCP000000001")
    co_applicants.record_intake(case_id, applicant_id, "COAPP-MCP000000001", {"name": "Priya"}, None, ready)
    co_doc(ready, case_id, applicant_id, "COAPP-MCP000000001", "PAN")
    envelope = asyncio.run(co_applicant_get(case_id, "COAPP-MCP000000001"))
    data = envelope.as_tool_payload()
    assert data["ok"] is True, data
    result = data.get("data") or data.get("result") or {}
    assert result["case_id"] == case_id and [d["document_type"] for d in result["documents"]] == ["PAN"]
    other, _ = make_case(ready)
    wrong = asyncio.run(co_applicant_get(other, "COAPP-MCP000000001")).as_tool_payload()
    assert wrong["ok"] is False and wrong["error"]["code"] == "CO_APPLICANT_NOT_FOUND"   # not on that case


# ---- flag off ----------------------------------------------------------------------------------
def test_flag_off_nothing_changes(repo):
    assert co_applicants.enabled(repo) is False
    assert co_applicants.config_error(repo) is None


def test_flag_on_without_the_migration_is_reported_not_guessed(repo, monkeypatch):
    monkeypatch.setenv(co_applicants.FLAG, "true")
    assert co_applicants.enabled(repo) is False
    assert "0004" in co_applicants.config_error(repo)
