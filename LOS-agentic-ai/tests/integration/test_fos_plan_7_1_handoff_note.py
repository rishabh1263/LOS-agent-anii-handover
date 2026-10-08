"""
FOS PLAN 7.1 -- the CPA handoff note: only for a case the live FOS gate passes; in chat and as md / html / pdf;
refused when not ready (with what blocks it); someone else's case 403; every note audited.
"""

from __future__ import annotations

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def case(client, demo, _store, monkeypatch):
    for flag in ("COPILOT_HANDOFF_NOTE", "COPILOT_READINESS_REPORT", "COPILOT_KYC_TABLE"):
        monkeypatch.setenv(flag, "true")
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED, verification_status="PASS"))
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return a, c


@pytest.fixture
def ready(monkeypatch):
    from app.agents.los import stage_gate

    monkeypatch.setattr(stage_gate, "evaluate_live", lambda case_id, stage: {"stage": "FOS", "status": "PASS",
                                                                             "blockers": []})


def ask(client, message):
    return client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message}).json()


def test_not_ready_is_refused_with_what_blocks_it(client, case):
    reply = ask(client, "handoff note banao")
    assert reply["intent"] == "HANDOFF_NOTE"
    assert "A handoff note is made once the case is ready for CPA" in reply["answer"]
    assert client.get(f"/api/v1/fos/handoff-note?case_id={case[1]}&format=md").status_code == 409


def test_a_ready_case_gets_the_note_in_chat_and_as_a_file(client, case, ready):
    reply = ask(client, "CPA handoff note")
    note = reply["answer"]
    for part in (f"CPA Handoff Note -- {case[1]}", "A person must confirm the move.", "## Case and parties",
                 "## Loan details", "## Documents", "| Applicant | PAN | Verified |", "## KYC",
                 "## Exceptions and overrides", "**Generated:**", "**Officer:**"):
        assert part in note, part
    assert any(a.get("url", "").endswith(f"case_id={case[1]}&format=pdf") for a in reply["actions"])
    md = client.get(f"/api/v1/fos/handoff-note?case_id={case[1]}&format=md")
    assert md.status_code == 200 and md.text.startswith(f"# CPA Handoff Note -- {case[1]}")
    pdf = client.get(f"/api/v1/fos/handoff-note?case_id={case[1]}&format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_someone_elses_case_is_refused(client, demo, make_token, monkeypatch, ready):
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    monkeypatch.setenv("COPILOT_HANDOFF_NOTE", "true")
    own = dict(client.headers)
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    _, theirs = make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(own)
    assert client.get(f"/api/v1/fos/handoff-note?case_id={theirs}&format=md").status_code in (403, 404)
