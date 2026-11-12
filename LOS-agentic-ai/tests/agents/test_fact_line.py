"""
THE NATURAL LINE FROM THE FACTS (2026-10-15, answering/fact_line.py): data answers open with a line built from their
own records -- instant, and it names only what the records name.
"""

from __future__ import annotations

import asyncio

from app.agents.applicant.copilot.answering import fact_line, polish

RECORDS = {"applicant": {"full_name": "Rahul Sharma"},
           "readiness": {"status": "NOT_READY", "blocking_items": [
               {"type": "DOCUMENT", "slot": "ADDRESS_PROOF"}, {"type": "DOCUMENT", "slot": "SIGNATURE"},
               {"type": "DOCUMENT", "slot": "SIGNATURE"}]},
           "documents": [{"document_type": "PAN", "status": "VERIFIED"},
                         {"document_type": "SALARY_SLIP", "status": "VERIFIED"},
                         {"document_type": "BANK_STATEMENT", "status": "IN_REVIEW"}]}


def test_pending_line_names_exactly_the_pending_documents_once():
    text = fact_line.line({**RECORDS, "intent": "PENDING_ITEMS"}, "what is pending")
    assert "Address Proof" in text and "Signature" in text and text.count("Signature") == 1
    assert "PAN" not in text and "Bank Statement" not in text          # done / in review are not called pending


def test_summary_line_uses_the_applicants_first_name():
    text = fact_line.line({**RECORDS, "intent": "FULL_SUMMARY"}, "summary of this case")
    assert "Rahul" in text and "Sharma" not in text and "Address Proof and Signature" in text


def test_uploaded_line_says_what_is_done_and_what_is_left():
    text = fact_line.line({**RECORDS, "intent": "DOCUMENTS_UPLOADED"}, "which documents are uploaded")
    assert "PAN and Salary Slip" in text and "Address Proof and Signature" in text


def test_ready_and_other_blockers_and_nothing_clear():
    ready = fact_line.line({"intent": "APPLICATION_STATUS", "readiness": {"status": "READY", "blocking_items": []}}, "s")
    assert "ready for CPA" in ready and "not" not in ready
    kyc = fact_line.line({"intent": "APPLICATION_STATUS", "readiness": {"status": "NOT_READY", "blocking_items": [
        {"type": "KYC", "detail": "KYC needs a reviewer: application name vs PAN"}]}}, "s")
    assert "KYC needs a reviewer" in kyc and "not ready" in kyc
    assert fact_line.line({"intent": "PENDING_ITEMS"}, "x") is None       # no records: no line, the card alone


def test_explanations_keep_the_models_line():
    assert fact_line.line({**RECORDS, "intent": "WHY_STUCK"}, "why is it stuck") is None
    assert not fact_line.applies("NEXT_ACTION")


def test_same_question_same_line_and_no_dash():
    a = fact_line.line({**RECORDS, "intent": "PENDING_ITEMS"}, "what is pending")
    assert a == fact_line.line({**RECORDS, "intent": "PENDING_ITEMS"}, "What is pending ")
    for intent in fact_line._cfg().get("intents"):
        text = fact_line.line({**RECORDS, "intent": intent}, "q") or ""
        assert "—" not in text and "--" not in text


def test_polish_puts_the_fact_line_first_without_calling_the_model(monkeypatch):
    async def no_model(*a, **k):
        raise AssertionError("the model must not be called for a data answer")

    monkeypatch.setattr(polish, "_lead", no_model)
    monkeypatch.setattr(polish, "enabled", lambda: True)
    monkeypatch.setattr(polish, "natural", lambda: False)        # the LEAD style (polish.style lead)
    card = ("Rahul Sharma, Personal Loan, ₹5,00,000\nStage: Basic Document Verification\n"
            "- Pending (2): Address Proof, Signature\nNext: Collect and upload the 2 documents above.")
    out = asyncio.run(polish.apply({**RECORDS, "intent": "FULL_SUMMARY", "answer": card}, "summary of this case"))
    first, _, rest = out["answer"].partition("\n\n")
    assert first.startswith("Rahul's file") or first.startswith("Almost there for Rahul")
    assert "Address Proof" in rest and out["polished"] is True


def test_natural_mode_falls_back_to_the_instant_line_when_the_model_is_slow(monkeypatch):
    """polish.style natural (owner 2026-10-24): Qwen's words first; slow / down -> the fact line, never nothing."""
    async def slow(*a, **k):
        raise TimeoutError("model too slow")

    monkeypatch.setattr(polish, "_ask", slow)
    monkeypatch.setattr(polish, "enabled", lambda: True)
    monkeypatch.setattr(polish, "natural", lambda: True)
    from app.llm import availability

    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    card = ("Rahul Sharma, Personal Loan, ₹5,00,000\nStage: Basic Document Verification\n"
            "- Pending (2): Address Proof, Signature\nNext: Collect and upload the 2 documents above.")
    out = asyncio.run(polish.apply({**RECORDS, "intent": "FULL_SUMMARY", "answer": card}, "summary of this case"))
    first = out["answer"].partition("\n\n")[0]
    assert first.startswith("Rahul's file") or first.startswith("Almost there for Rahul")


# "but after doing all this case can be move to cpa?" (owner, 2026-11-04) got the pending list again: a conditional
# yes/no is answered -- yes when only documents block it, not yet (naming what else) when more does.
def test_after_doing_all_this_can_it_move_is_answered_yes_or_not_yet():
    q = "but after doing all this case can be move to cpa?"
    yes = fact_line.line({**RECORDS, "intent": "READINESS"}, q)
    assert yes.startswith("Yes.") and "Address Proof and Signature" in yes
    more = {"intent": "READINESS", "readiness": {"status": "NOT_READY", "blocking_items": [
        {"type": "DOCUMENT", "slot": "PAN"}, {"type": "KYC", "detail": "KYC needs a reviewer"}]}}
    not_yet = fact_line.line(more, "once these are uploaded will it go to cpa")
    assert not_yet.startswith("Not yet") and "PAN" in not_yet and "KYC needs a reviewer" in not_yet
    assert fact_line.line(more, "ye sab upload karne ke baad cpa ja sakta hai?").startswith("Not yet")


def test_the_learned_act_shapes_the_readiness_line():
    # semantics/answer_act.py (learned; n-gram part in the suite): the same facts, the answer to the question asked
    r = {**RECORDS, "intent": "READINESS"}
    assert fact_line.line(r, "is this case ready for CPA?").startswith("Not yet.")                  # YES_NO
    assert fact_line.line(r, "Why this case cant be move to cpa?").startswith("Because")           # WHY_NOT
    when = fact_line.line(r, "when can this case move to cpa")                                     # WHEN
    assert when.startswith("As soon as") and "Address Proof and Signature" in when
    ready = {"intent": "READINESS", "readiness": {"status": "READY", "blocking_items": []}}
    assert "ready for CPA" in fact_line.line(ready, "is this case ready for CPA?")


def test_an_unsure_act_keeps_the_default_pending_line():
    text = fact_line.line({**RECORDS, "intent": "READINESS"}, "what is left before CPA")
    assert not text.startswith(("Yes", "Not yet", "Because", "As soon as")), text
