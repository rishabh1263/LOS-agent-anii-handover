"""
FOS PLAN 9b -- officer tools: what-if, review on open, status tables, customer message draft (never sent), visit
checklist. All from the readiness report (the gate's own sources).
"""

from __future__ import annotations

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

TOOLS = ("COPILOT_WHAT_IF", "COPILOT_AUTOPILOT_REVIEW", "COPILOT_STATUS_TABLES", "COPILOT_CUSTOMER_MESSAGE",
         "COPILOT_VISIT_CHECKLIST", "COPILOT_READINESS_REPORT")


@pytest.fixture
def case(client, demo, _store, monkeypatch):
    for flag in TOOLS:
        monkeypatch.setenv(flag, "true")
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    _store.save_document(Document(document_id=f"{c}:{a}:dl", case_id=c, applicant_id=a, party_id=a,
                                  document_type="DRIVING_LICENCE", status=DocumentStatus.REJECTED,
                                  reason_codes=["DOCUMENT_UNREADABLE"]))
    opened = client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c}).json()
    return a, c, opened


def ask(client, message, **extra):
    return client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message, **extra}).json()


def test_review_card_on_open(case):
    _, _, opened = case
    assert opened["review_card"]["total"] > opened["review_card"]["passed"]
    assert "Review:" in opened["answer"] and "Address Proof" in opened["answer"]


def test_what_if_one_document_is_not_enough(client, case):
    reply = ask(client, "agar bank statement upload karu toh ready ho jayega?")
    assert reply["intent"] == "WHAT_IF" and "Not yet" in reply["answer"] and "Address Proof" in reply["answer"]


def test_what_if_the_last_blocker_says_yes_and_a_person_confirms(client, case, monkeypatch):
    from app.agents.applicant.copilot.answering import officer_tools

    real = officer_tools._todo
    monkeypatch.setattr(officer_tools, "_todo",
                        lambda report: [i for i in real(report) if "BANK_STATEMENT" in i["id"]])
    reply = ask(client, "agar bank statement upload karu toh ready ho jayega?")
    assert reply["answer"].endswith("A person still confirms the move to CPA."), reply["answer"]


def test_customer_message_is_a_draft_never_sent(client, case):
    reply = ask(client, "customer ko bata do kya lana hai", reply_language="en")
    draft = reply["customer_message"]
    assert draft["sent"] is False and "Bank Statement" in draft["text"] and "Address Proof" in draft["text"]
    assert {a["type"] for a in reply["actions"]} == {"COPY_TEXT", "EDIT_TEXT"}


def test_visit_checklist_is_printable_per_party(client, case):
    reply = ask(client, "visit pe kya le jaun")
    assert reply["intent"] == "VISIT_CHECKLIST" and reply["printable"] is True
    assert "**Applicant**" in reply["answer"] and "- [ ] Collect the Bank Statement" in reply["answer"]


def test_status_tables_in_the_presentation(client, case):
    reply = ask(client, "kya baaki hai?")
    table = reply["presentation"]["document_table"]
    assert {r["document_type"] for r in table} == {"PAN", "DRIVING_LICENCE"}
    assert reply["presentation"]["progress"]["total"] >= 2
