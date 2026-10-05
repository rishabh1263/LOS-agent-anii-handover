"""BARE FOLLOW-UPS (2026-10-05, HTTP E2E): "Why?" after "PAN verified?" answered a checklist
requirement; "Continue", "क्यों?", Marathi "का?" and "address ka?" were UNKNOWN."""

import pytest

from app.agents.applicant.copilot.conversation import followup as F


@pytest.mark.parametrize("said, english", [
    ("क्यों?", "why?"), ("kyu", "why?"), ("का?", "why?"),
    ("Continue", "what next?"), ("aage", "what next?"), ("पुढे", "what next?"),
    ("address ka?", "what about address?"), ("bank statement ke baare mein?", "what about bank statement?"),
    ("PAN ka lagto?", "PAN ka lagto?"), ("What is pending?", "What is pending?"),
])
def test_bare_follow_ups_in_the_agents_language(said, english):
    assert F._in_english(said) == english


def _context(**kw):
    return F.Context(**{"last_intent": None, "last_document": None, **kw})


def test_why_after_a_verification_answer_is_about_that_document():
    r = F.resolve("Why?", _context(last_intent="DOCUMENT_VERIFICATION", last_document="PAN",
                                   last_slot="ADDRESS_PROOF"))
    assert r.message == "Why does my PAN have this verification result?"


def test_why_after_a_kyc_answer_is_about_kyc_in_any_language():
    for said in ("Why?", "क्यों?", "का?"):
        assert F.resolve(said, _context(last_intent="KYC_RESULT")).message == "Why is my KYC in this state?"


def test_continue_asks_for_the_next_step():
    assert F.resolve("Continue", _context(last_intent="PENDING_ITEMS")).message == "What should I do next?"


@pytest.mark.parametrize("said, question", [
    ("Why?", "Why is my eligibility in this state?"), ("Why not eligible?", "Why is my eligibility in this state?"),
    ("Which rule failed?", "Which eligibility rules failed?"), ("What is missing?", "What is missing for my eligibility?"),
    ("What is blocking it?", "What is blocking eligibility?"), ("What should I do?", "What should I do next for eligibility?"),
    ("क्यों?", "Why is my eligibility in this state?"), ("pudhe kay karaycha?", "What should I do next for eligibility?"),
])
def test_follow_ups_to_an_eligibility_answer_stay_on_eligibility(said, question):
    assert F.resolve(said, _context(last_intent="ELIGIBILITY")).message == question
