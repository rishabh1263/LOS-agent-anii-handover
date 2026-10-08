"""
FOS PLAN SECTIONS 5 + 7.1 -- readiness, ask anything. Every requirement the FOS gate evaluates, grouped, with its
state, reason and fix; failing items fastest-unblock first; READY is the live gate's verdict and nothing else.
"""

from __future__ import annotations

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def mixed_case(client, demo, _store, monkeypatch):
    monkeypatch.setenv("COPILOT_READINESS_REPORT", "true")
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED, verification_status="PASS"))
    _store.save_document(Document(document_id=f"{c}:{a}:dl", case_id=c, applicant_id=a, party_id=a,
                                  document_type="DRIVING_LICENCE", status=DocumentStatus.REJECTED,
                                  verification_status="FAIL", reason_codes=["DOCUMENT_UNREADABLE"]))
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return a, c


@pytest.mark.parametrize("message", ["CPA ke liye kya chahiye?", "ready hai kya?", "what is blocking CPA?",
                                     "CPA mein kab jayega?"])
@pytest.mark.parametrize("endpoint", ["/api/v1/fos/copilot", "/api/v1/copilot/query"])
def test_any_readiness_question_gets_the_grouped_report(client, mixed_case, endpoint, message):
    body = {"message": message} | ({"action": "CUSTOM_QUERY"} if "fos" in endpoint else {})
    reply = client.post(endpoint, json=body).json()
    assert reply["intent"] == "READINESS", (message, reply["intent"])
    answer = reply["answer"].split("\n", 1)[1] if reply["answer"].startswith("📍") else reply["answer"]
    assert answer.startswith("CPA readiness:") and "checks passed." in answer, answer
    assert "**Applicant documents**" in answer
    lines = answer.splitlines()
    first_failing = next(line for line in lines if line.startswith("- "))
    assert "Address Proof" in first_failing and "FAILED" in first_failing       # the re-upload unblocks fastest
    assert "Upload a correct Address Proof" in first_failing
    assert any("Bank Statement" in line and "PENDING" in line for line in lines)
    assert "Passed: PAN." in answer


def test_the_report_agrees_with_the_gate_and_is_structured(client, mixed_case):
    from app.agents.applicant.copilot.answering import readiness_report

    report = readiness_report.build(mixed_case[1])
    assert report["ready"] is False and report["gate"]["status"] != "PASS"
    assert 0 < report["passed"] < report["total"]
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "ready hai kya?"}).json()
    progress = reply["presentation"]["progress"]
    assert progress == {"passed": report["passed"], "total": report["total"], "ready": False}


def test_it_never_says_ready_while_the_gate_blocks(client, mixed_case, monkeypatch):
    """Every item passing is not enough: the gate decides. A blocker the groups did not name is shown."""
    from app.agents.applicant.copilot.answering import readiness_report
    from app.agents.los import stage_gate

    real_doc = readiness_report._doc_item
    monkeypatch.setattr(readiness_report, "_doc_item",
                        lambda row, party: {**real_doc(row, party), "status": "PASS", "fix": None})
    monkeypatch.setattr(stage_gate, "evaluate_live", lambda case_id, stage: {
        "stage": "FOS", "status": "BLOCKED", "blockers": [{"id": "SOMETHING_ELSE", "label": "A new gate rule",
                                                           "status": "BLOCKED"}]})
    report = readiness_report.build(mixed_case[1])
    assert report["ready"] is False
    assert any(g["group"] == "OTHER" and g["items"][0]["label"] == "A new gate rule" for g in report["groups"])
    assert not readiness_report.render(report).startswith("Case is ready")


def test_a_ready_gate_says_a_person_must_confirm(client, mixed_case, monkeypatch):
    from app.agents.applicant.copilot.answering import readiness_report
    from app.agents.los import stage_gate

    monkeypatch.setattr(stage_gate, "evaluate_live", lambda case_id, stage: {"stage": "FOS", "status": "PASS",
                                                                             "blockers": []})
    assert readiness_report.render(readiness_report.build(mixed_case[1])) == \
        "Ready for CPA. A person must confirm the move."   # MASTER SPEC section 6 wording


def test_a_definition_is_not_a_readiness_question(client, mixed_case):
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "CPA kya hai?"}).json()
    assert reply["intent"] != "READINESS"
