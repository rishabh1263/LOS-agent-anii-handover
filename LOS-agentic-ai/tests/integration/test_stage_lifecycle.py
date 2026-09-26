"""
The real LOS stage lifecycle: one authoritative stage per case, moved only
by the stage transition service, read by the one resolver, reflected by
the Universal Copilot and the stage-aware checklist.

A -- current stage at every one of the seven stages
B -- every configured transition
C -- transitions the configuration does not allow
D -- idempotency (same transition twice, replayed idempotency key)
E -- concurrency and stale transitions
F -- history persistence (append-only)
G -- current-stage resolution precedence
H -- the Copilot after a transition
I -- stage-aware document requirements after a transition
J -- an earlier stage's findings are not the current stage's
K -- a caller cannot change the stage
L -- the language model cannot change the stage
M -- demo seeding is not a runtime transition
N -- a missing transition reason is reported honestly
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.los import stage_lifecycle, stages
from app.store import get_repository, set_repository
from app.store.models import CaseEvent
from app.store.sqlite_repo import SQLiteRepository

ALL_STAGES = ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT")
CASE, APP = "case_5ta9e0000000000000000000000000aa", "APP-5TAGE00000AA"
COPILOT = "/api/v1/copilot/query"


def url(case_id: str = CASE) -> str:
    return f"/api/v1/los/cases/{case_id}/stage"


# ==========================================================================
# FIXTURES
# ==========================================================================

@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "lifecycle.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    stage_lifecycle.reload()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()
    stage_lifecycle.reload()


@pytest.fixture
def case(repo):
    """A real FOS case, through the real ingest path, owned by test-subject."""
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-lifecycle", "applicant_id": APP, "case_id": CASE,
        "status": "SUCCESS", "decision": "PASS", "next_action": "PROCEED",
        "documents": [{"source_id": f"{d.lower()}.jpg", "type": d,
                       "party_id": APP, "verification": "PASS",
                       "reason_codes": []} for d in ("PAN", "DRIVING_LICENCE")],
    })
    repo.grant_access("test-subject", "APPLICANT", APP)
    return CASE


def _client(make_token, **token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(**token)}"})
    return c


@pytest.fixture
def service(make_token) -> TestClient:
    """The workflow service: the service write scope."""
    return _client(make_token, subject="los-workflow", scopes=["los.write"])


@pytest.fixture
def officer(make_token) -> TestClient:
    """An ordinary FOS officer who owns the case -- no stage scope."""
    return _client(make_token, scopes=["documents:read", "documents:write",
                                       "upload_document", "agents:execute",
                                       "read_applicant", "read_application",
                                       "read_documents", "read_verification",
                                       "read_pending_items", "read_next_action"])


def move(client, target, reason="WORKFLOW_HANDOFF", case_id=CASE, **extra):
    return client.post(url(case_id), json={"target_stage": target,
                                           "reason": reason, **extra})


def moved(client, target, **extra) -> dict:
    response = move(client, target, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def ask(client, message, case_id=CASE, applicant_id=APP, **extra) -> dict:
    response = client.post(COPILOT, json={"applicant_id": applicant_id,
                                          "case_id": case_id,
                                          "message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# A. CURRENT STAGE -- every one of the seven
# ==========================================================================

def test_a_a_new_case_is_at_fos_from_its_application_status(case):
    context = stages.resolve(case)
    assert context.stage is stages.LosStage.FOS
    assert context.resolution is stages.Resolution.APPLICATION_STATUS
    assert get_repository().get_case_stage(case) is None


def test_a_current_stage_at_each_of_the_seven_stages(case, service):
    for stage in ALL_STAGES[1:]:
        moved(service, stage)
        context = stages.resolve(case)
        assert context.stage.value == stage
        assert context.resolution is stages.Resolution.STAGE_RECORD
        assert context.source == "CASE_STATE"
        assert context.status == "IN_PROGRESS"
    assert get_repository().get_case_stage(case).stage == "DISBURSEMENT"


# ==========================================================================
# B. VALID TRANSITIONS
# ==========================================================================

@pytest.mark.parametrize("index", range(len(ALL_STAGES) - 1))
def test_b_each_configured_transition_is_applied(case, service, index):
    for stage in ALL_STAGES[1:index + 1]:
        moved(service, stage)
    source, target = ALL_STAGES[index], ALL_STAGES[index + 1]

    body = moved(service, target, reason=f"{source}_HANDOFF")

    assert body["result"] == "APPLIED"
    assert body["stage"] == target
    assert body["stage_status"] == "IN_PROGRESS"
    assert body["transition"]["previous_stage"] == source
    assert body["transition"]["event_type"] == "STAGE_ENTERED"
    assert body["transition"]["reason"] == f"{source}_HANDOFF"
    assert body["transition"]["actor"] == "los-workflow"
    assert body["transition"]["source"] == "WORKFLOW"
    assert body["request_id"].startswith("stg_")


def test_b_the_response_is_ready_for_a_frontend(case, service):
    body = moved(service, "CPA")
    for field in ("stage", "stage_status", "stage_since", "stage_resolution",
                  "stage_source", "allowed_next", "history", "result",
                  "transition"):
        assert field in body
    assert body["allowed_next"] == ["CREDIT"]
    assert [h["stage"] for h in body["history"]] == ["FOS", "CPA"]


def test_b_a_status_change_within_a_stage_is_recorded(case, service):
    body = moved(service, "FOS", stage_status="READY_FOR_HANDOFF",
                 reason="FOS_COMPLETE")
    assert body["result"] == "APPLIED"
    assert body["stage"] == "FOS"
    assert body["stage_status"] == "READY_FOR_HANDOFF"
    assert body["transition"]["event_type"] == "STAGE_STATUS_CHANGED"
    assert stages.resolve(case).status == "READY_FOR_HANDOFF"

    # ...and entering the next stage starts it IN_PROGRESS.
    assert moved(service, "CPA")["stage_status"] == "IN_PROGRESS"


# ==========================================================================
# C. INVALID TRANSITIONS
# ==========================================================================

@pytest.mark.parametrize("path,target", [
    ((), "RCU"), ((), "DISBURSEMENT"), ((), "CREDIT"),
    (("CPA",), "HOPS"), (("CPA",), "FOS"), (("CPA", "CREDIT"), "CPA"),
    (("CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"), "FOS"),
])
def test_c_transitions_outside_the_configured_graph_are_refused(
        case, service, path, target):
    for stage in path:
        moved(service, stage)
    before = len(get_repository().get_stage_transitions(case))

    response = move(service, target)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "INVALID_TRANSITION"
    assert len(get_repository().get_stage_transitions(case)) == before
    assert stages.resolve(case).stage.value == (path[-1] if path else "FOS")


@pytest.mark.parametrize("target", ["UNDERWRITING", "REVIEW", "PASS", ""])
def test_c_a_status_or_unknown_word_is_not_a_stage(case, service, target):
    response = move(service, target)
    assert response.status_code == 422
    assert get_repository().get_stage_transitions(case) == []


def test_c_an_unknown_stage_status_is_refused(case, service):
    response = move(service, "CPA", stage_status="APPROVED")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STAGE_STATUS"


def test_c_a_configured_predicate_is_enforced(case, service, monkeypatch):
    config = stage_lifecycle.config()
    monkeypatch.setattr(stage_lifecycle, "config", lambda: config.__class__(
        **{**config.__dict__,
           "requires_status": {**config.requires_status,
                               stages.LosStage.FOS: "READY_FOR_HANDOFF"}}))

    refused = move(service, "CPA")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "TRANSITION_PRECONDITION_NOT_MET"

    moved(service, "FOS", stage_status="READY_FOR_HANDOFF")
    assert moved(service, "CPA")["stage"] == "CPA"


def test_c_an_unknown_case_is_not_found(repo, service):
    response = move(service, "CPA", case_id="case_does_not_exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CASE_NOT_FOUND"


# ==========================================================================
# D. IDEMPOTENCY
# ==========================================================================

def test_d_the_same_transition_twice_is_applied_once(case, service):
    assert moved(service, "CPA")["result"] == "APPLIED"
    second = moved(service, "CPA")
    assert second["result"] == "NO_CHANGE"
    assert second["transition"] is None
    assert len(get_repository().get_stage_transitions(case)) == 1


def test_d_a_retried_idempotency_key_replays_after_the_case_moved_on(case, service):
    first = moved(service, "CPA", idempotency_key="handoff-1")
    moved(service, "CREDIT")

    replay = moved(service, "CPA", idempotency_key="handoff-1")

    assert replay["result"] == "REPLAYED"
    assert replay["transition"]["transition_id"] == first["transition"]["transition_id"]
    assert replay["stage"] == "CREDIT"          # never rolled back
    assert len(get_repository().get_stage_transitions(case)) == 2


def test_d_an_idempotency_key_reused_for_another_move_is_refused(case, service):
    moved(service, "CPA", idempotency_key="k-1")
    response = move(service, "CREDIT", idempotency_key="k-1")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ==========================================================================
# E. CONCURRENCY AND STALE TRANSITIONS
# ==========================================================================

def _parallel(calls):
    results, errors = [None] * len(calls), []

    def run(i, fn):
        try:
            results[i] = fn()
        except Exception as exc:   # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, fn))
               for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def _call(target, **kw):
    def fn():
        try:
            return stage_lifecycle.transition(
                CASE, target, reason="RACE", actor="t", **kw)["result"]
        except stage_lifecycle.StageTransitionError as exc:
            return exc.code
    return fn


def _history_is_consistent(case_id):
    history = get_repository().get_stage_transitions(case_id)
    assert [t.version for t in history] == list(range(1, len(history) + 1))
    for earlier, later in zip(history, history[1:]):
        assert later.from_stage == earlier.to_stage
    return history


def test_e_simultaneous_identical_transitions_apply_exactly_once(case):
    results = _parallel([_call("CPA") for _ in range(8)])
    assert results.count("APPLIED") == 1
    assert set(results) <= {"APPLIED", "NO_CHANGE"}
    assert len(_history_is_consistent(case)) == 1
    assert stages.resolve(case).stage.value == "CPA"


def test_e_concurrent_fos_to_cpa_and_cpa_to_credit_never_corrupt(case):
    for _ in range(5):
        _parallel([_call("CPA"), _call("CREDIT")])
    history = _history_is_consistent(case)
    assert stages.resolve(case).stage.value == history[-1].to_stage
    assert history[-1].to_stage in {"CPA", "CREDIT"}


def test_e_a_stale_caller_cannot_overwrite_a_newer_stage(case, service):
    moved(service, "CPA")
    moved(service, "CREDIT")

    response = move(service, "RCU", expected_stage="CPA")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "STALE_STAGE"
    assert detail["current_stage"] == "CREDIT"
    assert stages.resolve(case).stage.value == "CREDIT"


def test_e_the_store_compare_and_set_rejects_a_stale_version(case, service):
    moved(service, "CPA")
    from app.store.models import CaseStage, StageTransition

    repository = get_repository()
    written = repository.apply_stage_transition(
        0,   # stale: the record is at version 1
        CaseStage(case_id=case, stage="DISBURSEMENT",
                  stage_status="IN_PROGRESS", version=1),
        StageTransition(transition_id="STG-stale", case_id=case, version=1,
                        kind="STAGE_ENTERED", to_stage="DISBURSEMENT",
                        to_status="IN_PROGRESS"),
        CaseEvent(event_id="EV-stale", case_id=case, event_type="STAGE_ENTERED",
                  stage="DISBURSEMENT"))
    assert written is False
    assert repository.get_case_stage(case).stage == "CPA"
    assert repository.get_stage_transition("STG-stale") is None


# ==========================================================================
# F. HISTORY PERSISTENCE
# ==========================================================================

def test_f_history_survives_a_new_connection_and_is_append_only(case, service, repo):
    for stage in ("CPA", "CREDIT", "RCU"):
        moved(service, stage, reason=f"TO_{stage}")

    reopened = SQLiteRepository(repo._path)
    history = reopened.get_stage_transitions(case)
    assert [(t.from_stage, t.to_stage, t.reason) for t in history] == [
        ("FOS", "CPA", "TO_CPA"), ("CPA", "CREDIT", "TO_CREDIT"),
        ("CREDIT", "RCU", "TO_RCU")]
    assert all(t.actor == "los-workflow" and t.request_id for t in history)

    conn = sqlite3.connect(str(repo._path))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE stage_transitions SET to_stage = 'FOS'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM stage_transitions")
    finally:
        conn.close()


def test_f_each_transition_is_on_the_case_timeline(case, service):
    body = moved(service, "CPA")
    timeline = get_repository().get_case_timeline(case)
    entered = [e for e in timeline if e.event_type == "STAGE_ENTERED"]
    assert len(entered) == 1
    assert entered[0].stage == "CPA"
    assert entered[0].ref_id == body["transition"]["transition_id"]


def test_f_resolver_history_carries_when_from_where_and_why(case, service):
    moved(service, "CPA", reason="FOS_HANDOFF")
    moved(service, "CREDIT", reason="CPA_COMPLETE")
    history = stages.resolve(case).history
    assert [h["stage"] for h in history] == ["FOS", "CPA", "CREDIT"]
    assert history[1]["previous_stage"] == "FOS"
    assert history[1]["reason"] == "FOS_HANDOFF"
    assert history[1]["ended_at"] == history[2]["started_at"]
    assert history[2]["ended_at"] is None


# ==========================================================================
# G. CURRENT-STAGE RESOLUTION
# ==========================================================================

def test_g_a_caller_claim_never_overrides_the_record(case, service):
    moved(service, "CPA")
    assert stages.resolve(case, "DISBURSEMENT").stage.value == "CPA"
    assert stages.resolve(case, "DISBURSEMENT").resolution is \
        stages.Resolution.STAGE_RECORD


def test_g_the_record_wins_over_the_timeline(case, service, repo):
    # A case placed at RCU by a timeline event (the legacy / demo shape)...
    repo.record_event(CaseEvent(event_id="E-legacy", case_id=case,
                                event_type="STAGE_ENTERED", stage="RCU"))
    assert stages.resolve(case).resolution is stages.Resolution.CASE_TIMELINE
    assert stages.resolve(case).stage.value == "RCU"

    # ...moves on from where it actually is, through the service...
    assert moved(service, "BOPS")["transition"]["previous_stage"] == "RCU"

    # ...and a later stray timeline event does not move it back.
    repo.record_event(CaseEvent(event_id="E-stray", case_id=case,
                                event_type="STAGE_ENTERED", stage="FOS"))
    context = stages.resolve(case)
    assert context.stage.value == "BOPS"
    assert context.resolution is stages.Resolution.STAGE_RECORD


def test_g_the_record_is_read_with_case_memory_off(case, service, monkeypatch):
    from app.agents.los import config as los_config

    moved(service, "CPA")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "false")
    los_config.reload()
    assert stages.resolve(case).stage.value == "CPA"


# ==========================================================================
# H. THE COPILOT AFTER A TRANSITION
# ==========================================================================

def test_h_the_copilot_reads_the_new_stage_after_each_transition(
        case, service, officer):
    first = ask(officer, "What stage am I in?")
    assert first["stage"] == "FOS"
    assert "FOS" in first["answer"] or "Field" in first["answer"]

    for stage in ("CPA", "CREDIT", "RCU"):
        moved(service, stage)
        body = ask(officer, "What stage am I in?")
        assert body["stage"] == stage, body
        assert body["stage_resolution"] == "STAGE_RECORD"
        assert body["stage_source"] == "CASE_STATE"


def test_h_stage_history_questions(case, service, officer):
    moved(service, "CPA", reason="FOS_HANDOFF")
    moved(service, "CREDIT", reason="CPA_COMPLETE")

    before = ask(officer, "Where was my application before CPA?")["answer"]
    assert "Before the" in before and "FOS" in before.upper()

    when = ask(officer, "When did my application move to CPA?")["answer"]
    assert "moved to the" in when and "UTC" in when

    why = ask(officer, "Why did my application move to CPA?")["answer"]
    assert "recorded reason" in why and "Handoff" in why
    assert "FOS_HANDOFF" not in why

    current = ask(officer, "What stage am I in?")
    assert current["stage"] == "CREDIT"


# ==========================================================================
# I. STAGE-AWARE DOCUMENT REQUIREMENTS AFTER A TRANSITION
# ==========================================================================

def _checklist_stages(case_id):
    from app.agents.applicant.workflow import build_checklist

    repository = get_repository()
    rows = build_checklist(repository.get_application(case_id),
                           repository.list_documents(case_id))
    return {row.get("stage") for row in rows if row.get("stage")}


def test_i_the_checklist_follows_the_stage(case, service):
    assert not ({"CPA", "CREDIT", "RCU"} & _checklist_stages(case))

    moved(service, "CPA")
    assert "CPA" in _checklist_stages(case)
    assert "CREDIT" not in _checklist_stages(case)   # no future-stage rows

    moved(service, "CREDIT")
    assert {"CPA", "CREDIT"} <= _checklist_stages(case)
    assert "RCU" not in _checklist_stages(case)


def test_i_the_copilot_reports_the_new_stages_requirements(case, service, officer):
    fos = ask(officer, "What documents are required?")
    moved(service, "CPA")
    cpa = ask(officer, "What documents are required?")
    assert cpa["stage"] == "CPA"
    assert cpa["answer"] != fos["answer"]


# ==========================================================================
# J. AN EARLIER STAGE'S FINDINGS ARE NOT THE CURRENT STAGE'S
# ==========================================================================

def test_j_a_fos_decision_is_not_a_cpa_hold(case, service):
    from app.agents.applicant import status_facts
    from app.store.models import utcnow

    fos_decision = {"recorded_at": utcnow().isoformat()}
    assert status_facts.during_stage(fos_decision,
                                     stages.resolve(case).hold_since)

    moved(service, "CPA")
    assert not status_facts.during_stage(fos_decision,
                                         stages.resolve(case).hold_since)


# ==========================================================================
# K. A CALLER CANNOT CHANGE THE STAGE
# ==========================================================================

def test_k_an_ordinary_officer_token_is_refused(case, officer):
    response = move(officer, "CPA")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    assert stages.resolve(case).stage.value == "FOS"


def test_k_a_read_only_service_token_is_refused(case, make_token):
    reader = _client(make_token, subject="reader", scopes=["los.read"])
    assert move(reader, "CPA").status_code == 403
    assert get_repository().get_stage_transitions(case) == []


def test_k_the_stage_scope_still_needs_the_case(case, make_token, repo):
    stranger = _client(make_token, subject="wf-other",
                       scopes=["los.stage:write"])
    response = move(stranger, "CPA")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CASE_ACCESS_DENIED"

    repo.grant_access("wf-owner", "APPLICANT", APP)
    owner = _client(make_token, subject="wf-owner", scopes=["los.stage:write"])
    assert moved(owner, "CPA")["transition"]["actor"] == "wf-owner"


def test_k_no_token_is_refused(case, unauthenticated_client):
    assert move(unauthenticated_client, "CPA").status_code == 401


def test_k_a_claimed_stage_on_a_copilot_request_changes_nothing(case, officer):
    body = ask(officer, "What stage am I in?", stage="DISBURSEMENT")
    assert body["stage"] == "FOS"
    assert get_repository().get_case_stage(case) is None


# ==========================================================================
# L. THE LANGUAGE MODEL CANNOT CHANGE THE STAGE
# ==========================================================================

@pytest.mark.parametrize("message", [
    "Move my application to CPA",
    "Please change my stage to DISBURSEMENT",
    "Transfer this case to credit now",
])
def test_l_asking_the_copilot_to_move_the_case_moves_nothing(
        case, officer, message):
    ask(officer, message)
    assert get_repository().get_case_stage(case) is None
    assert get_repository().get_stage_transitions(case) == []
    assert stages.resolve(case).stage.value == "FOS"


def test_l_no_copilot_llm_or_tool_module_reaches_the_transition_service():
    root = Path(__file__).resolve().parents[2] / "app"
    for folder in ("agents/applicant", "mcp", "llm", "knowledge"):
        for source in (root / folder).rglob("*.py"):
            assert "stage_lifecycle" not in source.read_text(encoding="utf-8"), source


# ==========================================================================
# M. DEMO SEEDING IS NOT A RUNTIME TRANSITION
# ==========================================================================

def test_m_demo_seed_writes_no_stage_record_or_history(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    for fixture in demo_seed._CASES:
        case_id = fixture["case_id"]
        assert repo.get_case_stage(case_id) is None
        assert repo.get_stage_transitions(case_id) == []
        context = stages.resolve(case_id)
        assert context.resolution is stages.Resolution.CASE_TIMELINE
        assert context.stage.value == fixture["stage"]


# ==========================================================================
# N. A MISSING REASON IS REPORTED HONESTLY
# ==========================================================================

def test_n_a_transition_without_a_reason_is_refused(case, service):
    response = service.post(url(), json={"target_stage": "CPA", "reason": "  "})
    assert response.status_code == 422
    assert get_repository().get_stage_transitions(case) == []


def test_n_a_stage_entered_with_no_recorded_reason_says_so(repo, make_token):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    fixture = next(c for c in demo_seed._CASES if c["stage"] == "CPA")
    client = _client(make_token, scopes=["los.read"])

    answer = ask(client, "Why did my application move to CPA?",
                 case_id=fixture["case_id"],
                 applicant_id=fixture["applicant_id"])["answer"]

    assert answer.startswith("No reason was recorded")
