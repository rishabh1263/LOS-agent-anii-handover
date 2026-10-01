"""
The frontend contract (copilot/answering/structured.py) and the conversation
layer's new kinds. Every structured value is copied from a record; nothing
is computed -- in particular a KYC or verification score is never invented.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import conversation
from app.agents.applicant.copilot.answering import structured


@pytest.mark.parametrize("intent,expected", [
    ("GREETING", "CONVERSATION"), ("OFF_TOPIC", "CONVERSATION"), ("FRUSTRATION", "CONVERSATION"),
    ("GUARDRAIL_BLOCKED", "REFUSAL"), ("FOS_KNOWLEDGE", "KNOWLEDGE_ANSWER"),
    ("APPLICANT_PROFILE", "CASE_FACT"), ("DOCUMENTS_REQUIRED", "DOCUMENT_CHECKLIST"),
    ("DOCUMENT_VERIFICATION", "DOCUMENT_STATUS"), ("KYC_RESULT", "KYC_RESULT"),
    ("NEXT_ACTION", "NEXT_ACTION"), ("OUT_OF_SCOPE", "ROUTED"),
])
def test_every_answer_has_a_response_type(intent, expected):
    assert structured.response_type({"intent": intent}) == expected


def test_a_clarification_is_typed_as_one():
    assert structured.response_type({"intent": "UNKNOWN", "clarification_required": {"options": []}}) \
        == "CLARIFICATION"


def test_a_kyc_score_is_never_fabricated():
    block = structured.kyc_block({"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"],
                                  "comparisons": [{"field": "NAME"}]}, party_role="PRIMARY_APPLICANT")
    assert block["score"] is None and block["score_recorded"] is False
    assert block["failed"] == ["NAME"]
    assert block["reason"]
    assert block["next_action"]["action"] == "AWAIT_REVIEW"
    assert "signed_off" in block["policy"]


def test_a_recorded_kyc_score_is_published_as_recorded():
    block = structured.kyc_block({"status": "PASS", "score": 92,
                                  "checked": [{"field": "NAME", "status": "PASS"},
                                              {"field": "DATE_OF_BIRTH", "status": "PASS"}]},
                                 party_role="CO_APPLICANT")
    assert block["score"] == 92 and block["score_recorded"] is True
    assert block["passed"] == ["NAME", "DATE_OF_BIRTH"] and block["failed"] == []
    assert block["party_role"] == "CO_APPLICANT"


def test_no_kyc_record_is_said_as_not_recorded():
    block = structured.kyc_block(None, party_role="CO_APPLICANT")
    assert block["status"] == "NOT_RECORDED" and block["score"] is None


def test_a_verification_card_carries_verdict_reason_and_next_action():
    block = structured.verification_block(
        [{"document_id": "d1", "document_type": "PAN", "status": "REJECTED", "verification_status": "FAIL",
          "reason_codes": ["DOCUMENT_TYPE_MISMATCH"], "party_role": "CO_APPLICANT"}],
        [{"slot": "PAN", "accepts": ["PAN"], "status": "REJECTED"}])
    card = block["documents"][0]
    assert card["verdict"] == "FAIL" and card["score"] is None and card["reason"]
    assert card["next_action"]["action"] == "UPLOAD_DOCUMENT"
    assert block["needs_attention"] == ["PAN"]


@pytest.mark.parametrize("message,kind", [
    ("ye kya bakwaas hai", conversation.FRUSTRATION),
    ("this is useless", conversation.FRUSTRATION),
    ("tell me a joke", conversation.OFF_TOPIC),
    ("what is the capital of France?", conversation.OFF_TOPIC),
    ("bhai help chahiye", conversation.HELP),
    ("good morning", conversation.GREETING),
])
def test_conversation_kinds(message, kind):
    turn = conversation.classify(message)
    assert turn is not None and turn.kind == kind


@pytest.mark.parametrize("message", [
    "this is useless, what is my stage?", "why is my loan stuck, this is frustrating",
    "what is my kyc score", "cricket ke baad mera PAN status batao",
])
def test_a_business_word_keeps_a_message_a_business_question(message):
    turn = conversation.classify(message)
    assert turn is None or turn.kind not in (conversation.FRUSTRATION, conversation.OFF_TOPIC)


def test_replies_vary_across_turns_and_greet_in_kind():
    replies = {conversation.reply(conversation.GREETING, "en", seed=s)[0] for s in range(6)}
    assert len(replies) > 1
    assert conversation.reply(conversation.GREETING, "en", text="good evening")[0].startswith("Good evening")
