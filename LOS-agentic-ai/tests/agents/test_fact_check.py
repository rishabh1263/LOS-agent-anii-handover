"""answering/fact_check: every claim about the case is checked against its facts before a reply is published."""

from __future__ import annotations

from app.agents.applicant.copilot.answering import fact_check
from app.agents.applicant.copilot.facts.case_facts import CaseFacts

PENDING = ("PAN", "ADDRESS_PROOF", "BANK_STATEMENT", "SIGNATURE")


def _facts(**overrides) -> CaseFacts:
    values = dict(case_id="CASE-TEST", stage="FOS", stage_label="FOS", workflow_step="APPLICATION_CREATED",
                  ready=False, pending_documents=PENDING, blocking_count=4, loan_amount=500000.0,
                  tenure_months=60, product="PL")
    values.update(overrides)
    return CaseFacts(**values)


def test_the_summary_names_the_los_stage_not_the_workflow_step():
    out, fixes = fact_check.correct("- Stage: Application Created\n- Pending (4): PAN", _facts())
    assert "Stage: FOS" in out and fixes == ["stage_line"]


def test_a_wrong_stage_phrase_is_corrected_and_a_right_one_left_alone():
    out, fixes = fact_check.correct("Your application is in the CPA stage.", _facts())
    assert out == "Your application is in the FOS stage." and fixes == ["stage_phrase"]
    same = "Your application is in the FOS stage, at Basic Document Verification."
    assert fact_check.correct(same, _facts()) == (same, [])


def test_a_wrong_pending_count_is_corrected():
    out, _ = fact_check.correct("Pending (5): PAN", _facts())
    assert out == "Pending (4): PAN"
    out, _ = fact_check.correct("5 documents are still pending.", _facts())
    assert out.startswith("4 documents are still pending")


def test_a_wrong_loan_amount_is_corrected():
    out, fixes = fact_check.correct("The records show a loan amount of ₹6,00,000.", _facts())
    assert "5,00,000" in out and fixes == ["loan_amount"]


def test_ready_claimed_for_a_case_that_is_not_ready_is_corrected():
    out, fixes = fact_check.correct("This case is ready for CPA.", _facts())
    assert "not ready" in out.lower() and fixes == ["readiness_claimed_ready"]


def test_conditionals_and_true_answers_are_left_alone():
    for text in ("Yes. Once PAN and Signature are uploaded and verified, nothing else is holding this file back from CPA.",
                 "Not yet. PAN is still pending.",
                 "No, the case can't move to CPA yet."):
        assert fact_check.correct(text, _facts()) == (text, [])


def test_action_links_are_never_rewritten():
    line = "[Upload PAN](action:upload?doc=PAN) Stage: Application Created"
    assert fact_check.correct(line, _facts()) == (line, [])


def test_check_reply_skips_conversation_and_missing_case(monkeypatch):
    reply = {"answer": "Stage: Application Created", "category": "CONVERSATION"}
    assert fact_check.check_reply(reply, "CASE-TEST") == []
    assert fact_check.check_reply({"answer": "Stage: Application Created"}, None) == []


def test_check_reply_corrects_answer_and_spoken_text(monkeypatch):
    from app.agents.applicant.copilot.facts import case_facts

    monkeypatch.setattr(case_facts, "read", lambda case_id: _facts())
    reply = {"answer": "- Stage: Application Created", "tts_override": "5 documents are pending."}
    assert fact_check.check_reply(reply, "CASE-TEST")
    assert "Stage: FOS" in reply["answer"] and reply["tts_override"].startswith("4 documents")


def test_a_question_about_a_co_applicant_the_case_does_not_have(monkeypatch):
    from app.agents.applicant.copilot.facts import case_facts

    monkeypatch.setattr(case_facts, "read", lambda case_id: _facts(has_co_applicant=False))
    reply = {"answer": "No document needs any action right now.", "_question": "and for the co-applicant?"}
    assert fact_check.check_reply(reply, "CASE-TEST") == ["no_co_applicant"]
    assert "co-applicant" in reply["answer"].lower() and reply["intent"] == "NO_CO_APPLICANT"


def test_the_applicant_question_and_a_real_co_applicant_are_left_alone(monkeypatch):
    from app.agents.applicant.copilot.facts import case_facts

    monkeypatch.setattr(case_facts, "read", lambda case_id: _facts(has_co_applicant=False))
    reply = {"answer": "No document needs any action right now.", "_question": "what is pending?"}
    assert fact_check.check_reply(reply, "CASE-TEST") == []
    monkeypatch.setattr(case_facts, "read", lambda case_id: _facts(has_co_applicant=True))
    reply = {"answer": "No document needs any action right now.", "_question": "and for the co-applicant?"}
    assert fact_check.check_reply(reply, "CASE-TEST") == []
