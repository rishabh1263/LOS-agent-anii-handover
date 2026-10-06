"""
JEV, THE SEMANTIC DECISION LAYER -- contract tests (app/jev).

The provider here is a STUB OF THE HTTP REPLY, shaped exactly like the Jev API's
/v1/systemone response, injected only in this file. The acceptance run against
the REAL provider is tests/integration/test_jev_live.py (marker live_jev).

Proves: one batched call for every question; typed choice / score / noul
decisions with probabilities and confidence; confidence bands; gated actions
that never touch a domain status; append-only persistence; idempotency per
evidence version; CONFIGURATION_GAP / EXTERNAL_DEPENDENCY_REQUIRED /
JEV_RESPONSE_INVALID with no decision fabricated; authorization before any
state or provider call; independence from the chatbot LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.integration.test_fos_stage_boundary import FOS_SCOPES, open_case, upload  # noqa: F401
from app.agents.applicant import config as agent_config
from app.jev import config as jev_config
from app.jev import engine, metrics
from app.store import set_repository
from app.store.testing import fresh_repository

PAN = Path("samples/documents/rpan.jpg")
DL = Path("samples/documents/driving_license.jpg")
OTHER_PAN = Path("samples/lPan.jpg")
pytestmark = pytest.mark.skipif(not (PAN.exists() and DL.exists() and OTHER_PAN.exists()),
                                reason="real samples not present")


class StubJev:
    """Answers /v1/systemone like the real API; records every request."""

    def __init__(self, answers=None, status=200, raise_exc=None, body=None):
        self.calls: list[dict] = []
        self.answers = answers or {}
        self.status, self.raise_exc, self.body = status, raise_exc, body

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raise_exc:
            raise self.raise_exc
        body = self.body if self.body is not None else {"answers": {
            qid: self.answers.get(qid) or _default(q) for qid, q in json["questions"].items()},
            "usage": {"input_tokens": 100, "output_tokens": 0}, "elapsedMs": 120, "model": json["model"]}
        return httpx.Response(self.status, json=body, request=httpx.Request("POST", url))


def _default(q):
    if q["type"] == "noul":
        return {"noul": 0.04}
    if q["type"] == "choice":
        first = next(iter(q["criteria"]))
        return {"choice": "NONE" if "NONE" in q["criteria"] else first,
                "probabilities": {k: (0.93 if k == "NONE" else 0.01) for k in q["criteria"]}, "confidence": 0.9}
    return {"score": 0.0, "legend": {str(i): c for i, c in enumerate(q["criteria"])},
            "probabilities": {"0": 0.9, "1": 0.1}, "confidence": 0.88}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.test")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("JEV_MODEL", "jev-1.13")
    monkeypatch.delenv("FOS_KYC_ON_UPLOAD", raising=False)
    agent_config.reload()
    jev_config.reload()
    metrics.reset()
    repository = fresh_repository(tmp_path / "jev.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    jev_config.reload()


@pytest.fixture
def client(make_token):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def _automate(monkeypatch, qid):
    """Switch one question to AUTOMATE (as a provider that measured well would be)."""
    import copy

    original = jev_config.question_set

    def patched(scope):
        qset = copy.deepcopy(original(scope))
        qset["questions"][qid]["automate"] = True
        return qset

    monkeypatch.setattr(jev_config, "question_set", patched)


def _case(client, monkeypatch, items, types):
    from app.jev import triggers

    monkeypatch.setattr(triggers, "document_processed", lambda *a, **k: [])   # evaluate explicitly below
    applicant_id, case_id = open_case(client)
    upload(client, applicant_id, case_id, items, types)
    return applicant_id, case_id


def test_one_call_carries_every_question_and_returns_typed_decisions(client, monkeypatch, _env):
    stub = StubJev()
    monkeypatch.setattr(httpx, "post", stub)
    applicant_id, case_id = _case(client, monkeypatch, [("pan.jpg", PAN), ("dl.jpg", DL)], ["PAN", "DRIVING_LICENCE"])

    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()

    assert len(stub.calls) == 1                                    # ONE batched call
    sent = stub.calls[0]["json"]
    assert set(sent["questions"]) == set(jev_config.question_set("CASE_TRIAGE")["questions"])
    assert {q["type"] for q in sent["questions"].values()} == {"noul", "choice", "score"}
    assert stub.calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert stub.calls[0]["url"] == "https://jev.test/v1/systemone"
    # the state is authoritative evidence, identifiers masked, no chat history
    state = json.dumps(sent["state"])
    assert "RISHABH AJIT SINGH" in state and "messages" not in state
    assert not any(len(t) == 10 and t[:5].isalpha() and t[5:9].isdigit() for t in state.replace('"', " ").split())

    assert r["jev_status"] == "COMPLETED"
    kinds = {d["question_type"] for d in r["semantic_decisions"]}
    assert kinds == {"NOUL", "CHOICE", "SCORE"}
    review = next(d for d in r["semantic_decisions"] if d["decision_type"] == "SEMANTIC_REVIEW")
    assert review["answer"] == "NO" and review["probabilities"] == {"yes": 0.04, "no": 0.96}
    assert review["confidence"] == 0.96 and review["confidence_band"] == "AUTO"
    # JEV is not an authority for KYC or identity: no such question is asked
    assert "identity_inconsistent" not in sent["questions"] and "issue_category" not in sent["questions"]
    assert not [d for d in r["semantic_decisions"] if "IDENTITY" in d["decision_type"]]
    severity = next(d for d in r["semantic_decisions"] if d["decision_type"] == "SEMANTIC_SEVERITY")
    assert severity["answer"] == "LOW"
    assert r["authoritative_statuses_changed"] is False


def test_a_review_recommendation_routes_without_touching_kyc(client, monkeypatch, _env):
    stub = StubJev(answers={
        "needs_manual_review": {"noul": 0.91},
        "route_to": {"choice": "KYC", "probabilities": {"KYC": 0.9, "NONE": 0.05}, "confidence": 0.9},
        "severity": {"score": 2.0, "legend": {"2": "HIGH: blocks progress"}, "probabilities": {"2": 0.9},
                     "confidence": 0.89},
    })
    monkeypatch.setattr(httpx, "post", stub)
    applicant_id, case_id = _case(client, monkeypatch, [("pan.jpg", OTHER_PAN), ("dl.jpg", DL)],
                                  ["PAN", "DRIVING_LICENCE"])
    kyc_before = [f.status for f in _env.get_current_findings(case_id, kind="KYC")]

    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()

    actions = {a["action"]: a for a in r["semantic_actions"]}
    assert actions["HUMAN_REVIEW"]["status"] == "EXECUTED" and actions["HUMAN_REVIEW"]["severity"] == "HIGH"
    # ROUTING IS ADVISORY on the measured provider: recorded, never executed
    assert actions["ROUTE"]["status"] == "ADVISORY" and actions["ROUTE"]["target"] == "KYC"
    assert actions["FINANCIAL_REVIEW"]["status"] == "SKIPPED"
    route = next(d for d in r["semantic_decisions"] if d["decision_type"] == "ROUTING")
    assert route["decision_policy"] == "ADVISORY" and route["status"] == "INFO"
    flagged = next(d for d in r["semantic_decisions"] if d["decision_type"] == "SEMANTIC_REVIEW")
    assert flagged["status"] == "OPEN" and flagged["recommended_action"] == "HUMAN_REVIEW"
    # executed = recorded on the timeline; the KYC agent's verdict is untouched
    events = [e.event_type for e in _env.get_case_timeline(case_id)]
    assert "JEV_HUMAN_REVIEW" in events and "JEV_ROUTE" not in events
    assert [f.status for f in _env.get_current_findings(case_id, kind="KYC")] == kyc_before
    # the copilot says what was flagged, in code-written words, and that the record stands
    answer = client.post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                      "action": "CUSTOM_QUERY", "message": "what is kyc status"}).json()
    assert "needs a person to review it" in answer["answer"] and "unchanged" in answer["answer"]
    assert "high severity" in answer["answer"] and "JEV" not in answer["answer"]
    assert answer["answer"].startswith("Your KYC check needs review")


def test_a_route_whose_prerequisite_is_missing_is_blocked(client, monkeypatch, _env):
    _automate(monkeypatch, "route_to")
    monkeypatch.setattr(httpx, "post", StubJev(answers={
        "route_to": {"choice": "CREDIT", "probabilities": {"CREDIT": 0.95}, "confidence": 0.95}}))
    _, case_id = _case(client, monkeypatch, [("pan.jpg", PAN)], ["PAN"])
    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    route = next(a for a in r["semantic_actions"] if a["action"] == "ROUTE")
    assert route["status"] == "BLOCKED" and "eligibility" in route["reason"]


def test_low_confidence_defers_to_the_fallback_and_automates_nothing(client, monkeypatch, _env):
    monkeypatch.setattr(httpx, "post", StubJev(answers={
        "needs_manual_review": {"noul": 0.7},                         # leans yes, not decisively
        "route_to": {"choice": "KYC", "probabilities": {"KYC": 0.4, "DOCUMENT": 0.35}, "confidence": 0.3}}))
    _, case_id = _case(client, monkeypatch, [("pan.jpg", OTHER_PAN), ("dl.jpg", DL)], ["PAN", "DRIVING_LICENCE"])
    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    by_type = {d["decision_type"]: d for d in r["semantic_decisions"]}
    assert by_type["SEMANTIC_REVIEW"]["confidence_band"] == "REVIEW"
    assert by_type["ROUTING"]["confidence_band"] == "NO_AUTOMATE"
    deferred = {a["action"]: a for a in r["semantic_actions"]}
    assert deferred["HUMAN_REVIEW"]["status"] == "DEFERRED" and deferred["ROUTE"]["status"] == "ADVISORY"
    assert not [e for e in _env.get_case_timeline(case_id) if e.event_type.startswith("JEV_")]
    assert metrics.snapshot()["fallbacks"] >= 2


def test_the_same_evidence_is_evaluated_once_and_new_evidence_again(client, monkeypatch, _env):
    stub = StubJev()
    monkeypatch.setattr(httpx, "post", stub)
    applicant_id, case_id = _case(client, monkeypatch, [("pan.jpg", PAN)], ["PAN"])
    first = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    again = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    assert len(stub.calls) == 1 and again["reused"] is True and again["jev_run_id"] == first["jev_run_id"]

    upload(client, applicant_id, case_id, [("dl.jpg", DL)], ["DRIVING_LICENCE"])     # the evidence changes
    third = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    assert len(stub.calls) == 2 and third["jev_run_id"] != first["jev_run_id"]
    runs = _env.list_jev_runs(case_id)
    assert len(runs) == 2 and runs[0]["evidence_version"] != runs[1]["evidence_version"]   # history kept


@pytest.mark.parametrize("setup, code", [
    ({"JEV_BASE_URL": ""}, "CONFIGURATION_GAP"),
    ({"JEV_API_KEY": ""}, "CONFIGURATION_GAP"),
    ({"_raise": httpx.ConnectError("refused")}, "EXTERNAL_DEPENDENCY_REQUIRED"),
    ({"_status": 503}, "EXTERNAL_DEPENDENCY_REQUIRED"),
    ({"_status": 401}, "CONFIGURATION_GAP"),
    ({"_body": {"answers": {"needs_manual_review": {"noul": 1.7}}}}, "JEV_RESPONSE_INVALID"),
])
def test_a_provider_failure_is_reported_never_fabricated(client, monkeypatch, _env, setup, code):
    stub = StubJev(status=setup.get("_status", 200), raise_exc=setup.get("_raise"), body=setup.get("_body"))
    monkeypatch.setattr(httpx, "post", stub)
    for k, v in setup.items():
        if not k.startswith("_"):
            monkeypatch.setenv(k, v)
    _, case_id = _case(client, monkeypatch, [("pan.jpg", PAN)], ["PAN"])

    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()

    assert r["jev_status"] == code and r["semantic_decisions"] == []
    assert r["semantic_actions"][0]["action"] == "FALLBACK"
    health = client.get("/api/v1/jev/health").json()
    if code == "CONFIGURATION_GAP" and "_status" not in setup:
        assert health["jev_runtime_ready"] is False and health["jev_status"] == "CONFIGURATION_GAP"
    # a failed run does not hold the evidence: once the provider answers, it is evaluated
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.test")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    good = StubJev()
    monkeypatch.setattr(httpx, "post", good)
    assert client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()["jev_status"] == "COMPLETED"


def test_jev_runs_on_the_upload_event_without_any_chat_or_llm(client, monkeypatch, _env):
    from app.jev import triggers

    stub = StubJev()
    monkeypatch.setattr(httpx, "post", stub)
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [("pan.jpg", PAN), ("dl.jpg", DL)], ["PAN", "DRIVING_LICENCE"]).json()
    assert body["semantic_decisions"]["jev_status"] == "SCHEDULED"
    triggers.drain()
    assert len(stub.calls) == 1
    decisions = client.get(f"/api/v1/jev/cases/{case_id}/decisions").json()
    assert decisions["jev_status"] == "COMPLETED" and len(decisions["semantic_decisions"]) == 5
    # the copilot carries the recorded decisions on a grounded answer
    answer = client.post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                      "action": "CUSTOM_QUERY", "message": "what is kyc status"}).json()
    assert answer["semantic_decisions"]["jev_run_id"] == decisions["jev_run_id"]


def test_authorization_comes_before_any_state_or_provider_call(client, monkeypatch, make_token, _env):
    stub = StubJev()
    monkeypatch.setattr(httpx, "post", stub)
    _, case_id = _case(client, monkeypatch, [("pan.jpg", PAN)], ["PAN"])
    from fastapi.testclient import TestClient

    import main

    stranger = TestClient(main.app)
    stranger.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, subject='someone-else')}"})
    assert stranger.post(f"/api/v1/jev/cases/{case_id}/evaluate").status_code in (403, 404)
    assert stranger.get(f"/api/v1/jev/cases/{case_id}/decisions").status_code in (403, 404)
    assert stub.calls == []


def test_an_automated_route_executes_only_in_the_auto_band_and_behind_its_gate(client, monkeypatch, _env):
    """Routing switched to AUTOMATE: a confident route whose prerequisite holds executes (a
    timeline event, no tool call); an unsure one is deferred to the fallback."""
    _automate(monkeypatch, "route_to")
    monkeypatch.setattr(httpx, "post", StubJev(answers={
        "route_to": {"choice": "KYC", "probabilities": {"KYC": 0.93}, "confidence": 0.93}}))
    _, case_id = _case(client, monkeypatch, [("pan.jpg", PAN), ("dl.jpg", DL)], ["PAN", "DRIVING_LICENCE"])
    r = client.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    route = next(a for a in r["semantic_actions"] if a["action"] == "ROUTE")
    assert route["status"] == "EXECUTED" and route["target"] == "KYC"
    assert "JEV_ROUTE" in [e.event_type for e in _env.get_case_timeline(case_id)]

    monkeypatch.setattr(httpx, "post", StubJev(answers={
        "route_to": {"choice": "BANKING", "probabilities": {"BANKING": 0.3, "KYC": 0.28}, "confidence": 0.3}}))
    from app.jev import engine as _engine

    unsure = _engine.evaluate(case_id, None, force=True)          # same evidence, asked again
    route = next(a for a in unsure["actions"] if a["action"] == "ROUTE")
    assert route["status"] == "DEFERRED" and route["fallback"] == "HUMAN_REVIEW", route


def test_decide_answers_a_written_state_and_stores_nothing(client, monkeypatch, _env):
    stub = StubJev(answers={"needs_manual_review": {"noul": 0.93}})
    monkeypatch.setattr(httpx, "post", stub)
    r = client.post("/api/v1/jev/decide", json={"state": {"kyc": {"status": "REVIEW", "reason_codes": []}}})
    assert r.status_code == 200, r.text
    body = r.json()
    review = next(d for d in body["decisions"] if d["decision_type"] == "SEMANTIC_REVIEW")
    assert review["answer"] == "YES" and review["band"] == "AUTO" and review["decision_policy"] == "AUTOMATE"
    route = next(d for d in body["decisions"] if d["decision_type"] == "ROUTING")
    assert route["decision_policy"] == "ADVISORY"
    assert body["persisted"] is False and body["actions_executed"] is False
    assert len(stub.calls) == 1 and stub.calls[0]["json"]["state"] == {"kyc": {"status": "REVIEW", "reason_codes": []}}


def test_benchmark_reports_accuracy_per_decision_type(client, monkeypatch, _env):
    monkeypatch.setattr(httpx, "post", StubJev())
    body = client.post("/api/v1/jev/benchmark").json()
    assert body["states"] == len(body["rows"]) >= 9
    assert set(body["accuracy_by_decision"]) >= {"route_to", "needs_manual_review"}
    assert body["automation_policy"]["route_to"] == "ADVISORY"
    assert body["automation_policy"]["needs_manual_review"] == "AUTOMATE"
