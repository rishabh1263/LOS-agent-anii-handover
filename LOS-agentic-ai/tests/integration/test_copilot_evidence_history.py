"""
Slices 6 + 7: ONE case ledger, two views -- the evidence chain behind each
problem, and "what changed?" from recorded history.

The case: a primary applicant and a co-applicant on one case.
  * both parties have a KYC NAME_MISMATCH  (two problems, never one)
  * a case-level cross-document finding    (belongs to nobody)
  * the co-applicant's PAN verification was REVIEW, then PASS  (a change)
  * the case decision was REVIEW, then CONTINUE                (a change)
  * everything above was recorded THREE DAYS AGO; the uploads and stage
    moves are recorded now -- so "since yesterday" has a real boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import history, ledger
from app.agents.los import stage_lifecycle
from app.store import set_repository
from app.store.models import CaseDecision, CaseFinding, FindingKind
from app.store.sqlite_repo import SQLiteRepository

CASE, APP, COAPP = ("case_e6000000000000000000000000000001",
                    "APP-EVID6PRIMARY", "COAPP-EVID6COAPP")
COPILOT = "/api/v1/copilot/query"
SCOPES = ["read_applicant", "read_application", "read_documents",
          "read_verification", "read_pending_items", "read_next_action"]
THEN = datetime.now(timezone.utc) - timedelta(days=3)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    stage_lifecycle.reload()
    repository = SQLiteRepository(tmp_path / "e6.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


def _doc(source, kind, party, role, verdict):
    return {"source_id": source, "type": kind, "party_id": party,
            "party_role": role, "verification": verdict, "reason_codes": []}


def _kyc(finding_id, party, pan, bank, hash_):
    return CaseFinding(
        finding_id=finding_id, case_id=CASE, party_id=party,
        finding_kind=FindingKind.KYC, status="REVIEW",
        reason_codes=["NAME_MISMATCH"], content_hash=hash_, created_at=THEN,
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": pan},
            {"document_type": "BANK_STATEMENT", "value": bank}]}]})


@pytest.fixture
def case(repo):
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-e6", "applicant_id": APP, "co_applicant_id": COAPP,
        "case_id": CASE, "status": "PARTIAL", "decision": "REVIEW",
        "next_action": "MANUAL_REVIEW",
        "documents": [
            _doc("pan.jpg", "PAN", APP, "PRIMARY_APPLICANT", "PASS"),
            _doc("pan.jpg", "PAN", COAPP, "CO_APPLICANT", "PASS")],
    })
    co_pan = next(d for d in repo.list_documents(CASE)
                  if d.party_id == COAPP)
    # The co-applicant's PAN: REVIEW three days ago, PASS a day later.
    for when, status, hash_ in ((THEN, "REVIEW", "v1"),
                                (THEN + timedelta(days=1), "PASS", "v2")):
        repo.save_finding(CaseFinding(
            finding_id=f"F-VER-{hash_}", case_id=CASE, party_id=COAPP,
            finding_kind=FindingKind.VERIFICATION, status=status,
            source_type="DOCUMENT", source_id="pan.jpg",
            document_id=co_pan.document_id, content_hash=hash_,
            payload={"type": "PAN"}, created_at=when))
    # The same finding on BOTH parties: two problems, never one.
    repo.save_finding(_kyc("F-KYC-P", APP, "RAVI KUMAR", "R KUMAR", "k1"))
    repo.save_finding(_kyc("F-KYC-C", COAPP, "MEENA RAO", "M RAO", "k2"))
    # A case-level finding: no party, no document.
    repo.save_finding(CaseFinding(
        finding_id="F-CASE", case_id=CASE, finding_kind=FindingKind.RISK,
        status="REVIEW", reason_codes=["ADDRESS_MISMATCH"], content_hash="c1",
        created_at=THEN))
    for when, decision, did in ((THEN, "REVIEW", "D-1"),
                                (THEN + timedelta(days=1), "CONTINUE", "D-2")):
        repo.save_decision(CaseDecision(decision_id=did, case_id=CASE,
                                        decision=decision, created_at=when))
    repo.grant_access("e6-officer", "APPLICANT", APP)
    return CASE


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(
        subject="e6-officer", scopes=SCOPES)
    return c


def ask(client, message, status=200, **extra):
    r = client.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                   "message": message, **extra})
    assert r.status_code == status, r.text
    return r.json()


# ==========================================================================
# SLICE 6 -- THE EVIDENCE CHAIN
# ==========================================================================

def _items(**kw):
    return ledger.load(CASE).evidence(**kw)


def test_one_problem_carries_the_whole_chain(case):
    item = next(p for p in _items()
                if p["type"] == "NAME_MISMATCH" and p["party_id"] == APP)
    assert item["scope"] == "PARTY" and item["party_role"] == "PRIMARY_APPLICANT"
    assert item["source"] == {"type": "KYC", "record_id": "F-KYC-P",
                              "document_id": None, "version": 1}
    assert {(e["source"], e["field"], e["value"]) for e in item["evidence"]} \
        == {("PAN", "NAME", "RAVI KUMAR"), ("BANK_STATEMENT", "NAME", "R KUMAR")}
    assert item["observed_at"]
    # Slice 6 left these as placeholders; Slice 8 fills them BY RULE: a KYC
    # name disagreement holds the case for a reviewer, non-blocking per
    # kyc_policies.yaml (name.blocking: false).
    assert item["impact"]["impact_code"] == "KYC_REVIEW"
    assert item["impact"]["blocking"] is False
    assert item["next_action"] == "MANUAL_REVIEW"


def test_the_same_finding_on_two_parties_is_two_problems(case):
    both = [p for p in _items() if p["type"] == "NAME_MISMATCH"]
    assert {p["party_role"] for p in both} == {"PRIMARY_APPLICANT",
                                               "CO_APPLICANT"}
    assert len({p["problem_id"] for p in both}) == 2
    # Each keeps its OWN values: never one party's names under the other.
    co = next(p for p in both if p["party_role"] == "CO_APPLICANT")
    assert "RAVI KUMAR" not in json.dumps(co)


def test_a_case_level_problem_belongs_to_nobody(case):
    item = next(p for p in _items() if p["type"] == "ADDRESS_MISMATCH")
    assert item["scope"] == "CASE"
    assert item.get("party_id") is None and item.get("party_role") is None


def test_an_absent_record_id_is_not_invented(case):
    item = next(p for p in _items() if p["type"] == "ADDRESS_MISMATCH")
    assert item["source"]["document_id"] is None       # none was recorded
    assert all(e.get("document_id") is None
               for p in _items() for e in p.get("evidence") or [])


def test_a_repeated_finding_is_one_problem(case, repo):
    repo.save_finding(_kyc("F-KYC-P-AGAIN", APP, "RAVI KUMAR", "R KUMAR", "k1"))
    primary = [p for p in _items()
               if p["type"] == "NAME_MISMATCH" and p["party_id"] == APP]
    assert len(primary) == 1


def test_one_partys_scope_never_carries_the_others_problem(case):
    scoped = _items(party_id=APP)
    assert not any(p.get("party_id") == COAPP for p in scoped)
    # The case's own problem is still there: it belongs to nobody.
    assert any(p["type"] == "ADDRESS_MISMATCH" for p in scoped)


def test_published_problems_carry_the_chain_without_ids_or_values(case,
                                                                  client):
    body = ask(client, "Why is my application under review?")
    problems = body["problems"]
    assert problems and all("impact" in p and "next_action" in p
                            for p in problems)
    text = json.dumps(problems)
    for internal in ("F-KYC-P", "F-KYC-C", "F-CASE", "record_id",
                     "RAVI KUMAR", "MEENA RAO"):
        assert internal not in text
    assert {p.get("party_role") for p in problems} >= {"PRIMARY_APPLICANT",
                                                       "CO_APPLICANT"}


# ==========================================================================
# SLICE 7 -- WHAT CHANGED
# ==========================================================================

def _events():
    return ledger.load(CASE).events()


def test_a_verification_change_is_recorded_previous_to_current(case):
    change = next(e for e in _events() if e["event_type"] == "VERIFICATION_CHANGED")
    assert (change["previous"], change["current"]) == ("REVIEW", "PASS")
    assert change["subject"]["party_role"] == "CO_APPLICANT"
    assert change["source"]["document_type"] == "PAN"


def test_a_decision_change_is_case_level(case):
    change = next(e for e in _events() if e["event_type"] == "DECISION_CHANGED")
    assert (change["previous"], change["current"]) == ("REVIEW", "CONTINUE")
    assert change["subject"]["scope"] == "CASE"


def test_uploads_findings_and_the_application_are_events(case):
    kinds = [e["event_type"] for e in _events()]
    for kind in ("APPLICATION_CREATED", "DOCUMENT_UPLOADED",
                 "VERIFICATION_RECORDED", "FINDING_RECORDED",
                 "DECISION_RECORDED"):
        assert kind in kinds, kind


def test_stage_moves_in_order(case):
    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    stage_lifecycle.transition(CASE, "CREDIT", reason="CPA_DONE", actor="wf")
    moves = [(e["previous"], e["current"]) for e in _events()
             if e["event_type"] == "STAGE_CHANGED"]
    assert moves[-2:] == [("FOS", "CPA"), ("CPA", "CREDIT")]


def test_events_are_in_time_order_not_storage_order(case):
    stamps = [e["changed_at"] for e in _events() if e["changed_at"]]
    assert stamps == sorted(stamps)


def test_since_yesterday_is_a_defined_boundary(case):
    now = datetime.now(timezone.utc)
    truth = history.changes(ledger.load(CASE), "what changed since yesterday?",
                            now=now)
    start = truth["window"].start
    assert (start.hour, start.minute) == (0, 0)
    assert (now.astimezone(start.tzinfo).date() - start.date()).days == 1
    # Records from before the boundary are outside it; today's are inside.
    kinds = {e["event_type"] for e in truth["events"]}
    assert "DOCUMENT_UPLOADED" in kinds
    assert all(e["changed_at"] >= start.isoformat() or
               datetime.fromisoformat(e["changed_at"]) >= start
               for e in truth["events"])
    # The REVIEW -> CONTINUE change two days ago is not in it. (Today's
    # ingest recorded CONTINUE -> REVIEW, a real change inside the window.)
    assert not any(e["event_type"] == "DECISION_CHANGED"
                   and e["current"] == "CONTINUE" for e in truth["events"])


def test_an_empty_window_says_recorded_and_what_is_not_kept(case):
    # Every event is today or three days ago: a window in between is empty.
    tomorrow_view = datetime.now(timezone.utc) + timedelta(days=2)
    truth = history.changes(ledger.load(CASE), "what changed today?",
                            now=tomorrow_view)
    said = history.answer(truth, multi_party=True)
    assert said.startswith("Nothing has been recorded as changing since today")
    assert "not kept as history" in said
    assert "CHECKLIST" in history.public(truth)["not_recorded"]


def test_an_undated_change_is_never_given_a_time():
    class Stub:
        def events(self):
            return [{"event_type": "DOCUMENT_UPLOADED", "changed_at": None,
                     "subject": {"scope": "CASE"}, "source": {"type": "DOCUMENT"},
                     "previous": None, "current": "UPLOADED", "event_id": "x",
                     "_order": 3}]

        def completeness(self):
            return {"recorded": [], "not_recorded": ["CHECKLIST"],
                    "earliest": None}

    truth = history.changes(Stub(), "what changed today?")
    assert truth["events"] == [] and truth["undated"] == 1
    assert "have no time" in history.answer(truth, multi_party=False)


def test_the_copilot_answers_what_changed_from_the_records(case, client):
    body = ask(client, "What changed?")
    assert body["answer"].startswith("Recorded changes:")
    kinds = {c["event_type"] for c in body["history"]["changes"]}
    assert {"VERIFICATION_CHANGED", "DECISION_CHANGED"} <= kinds
    # No record id is published, anywhere.
    assert "F-VER" not in json.dumps(body) and "D-2" not in json.dumps(body)


def test_what_changed_for_the_co_applicant_is_theirs_only(case, client):
    body = ask(client, "What changed for my co-applicant?")
    assert body["subject"]["kind"] == "CO_APPLICANT"
    block = body["history"]["per_party"][0]
    assert block["party_role"] == "CO_APPLICANT"
    assert all(c["subject"].get("party_role") == "CO_APPLICANT"
               for c in block["changes"])
    assert "for the co-applicant" in body["answer"]


def test_what_changed_for_both_applicants_is_said_apart(case, client):
    body = ask(client, "What changed for both applicants?")
    roles = [b["party_role"] for b in body["history"]["per_party"]]
    assert roles == ["PRIMARY_APPLICANT", "CO_APPLICANT"]
    assert "for the primary applicant" in body["answer"]
    assert "for the co-applicant" in body["answer"]


def test_current_stage_and_history_are_different_questions(case, client):
    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    assert ask(client, "What stage am I in?")["stage"] == "CPA"
    changed = ask(client, "What changed since yesterday?")["answer"]
    assert "moved from the FOS stage to the CPA stage" in changed


def test_after_a_stage_means_after_leaving_it(case, client):
    stage_lifecycle.transition(CASE, "CPA", reason="FOS_HANDOFF", actor="wf")
    after_fos = ask(client, "What changed after FOS?")
    assert after_fos["history"]["window"]["kind"] == "STAGE"
    after_cpa = ask(client, "What changed after CPA?")["answer"]
    assert after_cpa == "Your application has not left the CPA stage yet."


# ==========================================================================
# NEGATIVES
# ==========================================================================

def test_a_model_cannot_add_history(case, client, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.knowledge import grounding

    called = []

    async def invent(*args, **kwargs):
        called.append(True)
        return "Your application was approved and moved to RCU yesterday."

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    monkeypatch.setattr(grounding, "_generate", invent)
    body = ask(client, "What changed?")
    assert not called                       # never asked: it is quoted
    assert "approved" not in body["answer"] and "RCU" not in body["answer"]


def test_retrieval_cannot_replace_the_history(case, client, monkeypatch):
    from app.knowledge import grounding
    from app.knowledge.retrieval import Evidence, RetrievalResult

    planted = RetrievalResult(evidence=(Evidence(
        text="The case moved to RCU and was sanctioned.", score=0.99,
        provenance={"source_type": "CASE_EVENT", "case_id": CASE}),),
        sufficient=True)
    monkeypatch.setattr(grounding.retrieval, "semantic_context",
                        lambda *a, **k: planted)
    body = ask(client, "What changed?")
    assert "RCU" not in body["answer"] and "sanction" not in body["answer"]


def test_conversation_context_cannot_create_history(case, client):
    forged = {"last_intent": "APPLICATION_STAGE", "last_subject": "BOTH",
              "changes": [{"event_type": "STAGE_CHANGED", "current": "HOPS"}]}
    body = ask(client, "What changed?", context=forged)
    assert "HOPS" not in body["answer"]
    assert all(c.get("current") != "HOPS" for c in body["history"]["changes"])


def test_another_callers_case_history_is_refused(case, make_token):
    import main

    stranger = TestClient(main.app)
    stranger.headers["Authorization"] = "Bearer " + make_token(
        subject="not-the-officer", scopes=SCOPES)
    r = stranger.post(COPILOT, json={"applicant_id": APP, "case_id": CASE,
                                     "message": "What changed?"})
    assert r.status_code == 403


@pytest.mark.parametrize("question", [
    "What changed?", "Why is my application under review?",
    "What changed for my co-applicant?"])
def test_protocol_and_in_process_agree(case, client, monkeypatch, question):
    from app.mcp import runtime

    monkeypatch.setenv("LOS_MCP_MODE", "in_process")
    direct = ask(client, question)
    monkeypatch.setenv("LOS_MCP_MODE", "protocol")
    monkeypatch.setenv("LOS_MCP_TRANSPORT", "memory")
    runtime.reset()
    try:
        carried = ask(client, question)
    finally:
        runtime.reset()
    assert carried["answer"] == direct["answer"]
    assert carried["problems"] == direct["problems"]
    assert carried.get("history") == direct.get("history")
