"""
FOS PLAN 6.6 / 6.7 -- coverage found by the section 6 probe: a document on the case is never "not recorded",
count questions are counted from the store, "ye nahi poocha" gets one question back. Context is sent back each
turn, as the frontend does.
"""

from __future__ import annotations

import pytest

from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def case(client, demo, _store, monkeypatch):
    monkeypatch.setenv("COPILOT_COUNT_ANSWERS", "true")
    a, c = make_case(client, "Rahul Sharma")
    for kind, status in (("PAN", DocumentStatus.REJECTED), ("SALARY_SLIP", DocumentStatus.VERIFIED),
                         ("BANK_STATEMENT", DocumentStatus.VERIFIED)):
        doc_id = f"{c}:{a}:{kind.lower()}"
        _store.save_document(Document(document_id=doc_id, case_id=c, applicant_id=a, party_id=a, document_type=kind,
                                      status=status, reason_codes=[] if status == DocumentStatus.VERIFIED
                                      else ["DOCUMENT_UNREADABLE"]))
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    return a, c


class Chat:
    def __init__(self, client):
        self.client, self.ctx = client, None

    def say(self, message):
        body = {"action": "CUSTOM_QUERY", "message": message} | ({"context": self.ctx} if self.ctx else {})
        reply = self.client.post("/api/v1/fos/copilot", json=body).json()
        self.ctx = reply.get("context") or self.ctx
        return reply


def test_a_rejected_document_is_never_not_recorded(client, case):
    answer = Chat(client).say("PAN pe DOB kya hai")["answer"]
    assert "No **PAN" not in answer and "No PAN" not in answer
    assert "is on this case but it is rejected" in answer


@pytest.mark.parametrize("message, expected", [
    ("kitne documents verified hain", "2 documents are verified: Salary Slip, Bank Statement."),
    ("how many documents were rejected", "1 document was rejected: PAN."),
    ("kitne documents hain", "3 documents are on the case:"),
])
def test_count_questions_are_counted_from_the_store(client, case, message, expected):
    reply = Chat(client).say(message)
    assert reply["intent"] == "DOCUMENT_COUNT" and expected in reply["answer"], reply["answer"]
    assert reply["answer"].startswith(f"📍 {case[1]}")                           # the case header is kept


def test_ye_nahi_poocha_gets_one_question_back(client, case):
    chat = Chat(client)
    chat.say("kya baaki hai?")
    reply = chat.say("ye nahi poocha")
    assert reply["clarification_required"]["reason"] == "MISUNDERSTOOD"
    assert "not recorded" not in reply["answer"] and reply["suggested_questions"]
