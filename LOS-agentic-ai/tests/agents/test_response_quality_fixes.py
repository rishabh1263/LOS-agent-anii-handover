"""Fixes from the conversational acceptance run (2026-10-06)."""

from app.agents.applicant import case_memory_facts as facts
from app.agents.applicant.copilot.answering import attention
from app.agents.applicant.copilot.facts.document_facts import field_label

FOUR = [{"document_type": "PAN", "value": "RISHABH AJIT SINGH"},
        {"document_type": "DRIVING_LICENCE", "value": "RISHABH AJIT SINGH"},
        {"document_type": "SALARY_SLIP", "value": "VENKATESH GOUD"},
        {"document_type": "BANK_STATEMENT", "value": "PRIYANKA MORE"}]


def test_the_mismatch_names_two_documents_that_actually_differ():
    said = facts._mismatch_detail([{"comparisons": [{"field": "NAME", "sources": FOUR}]}])
    assert "VENKATESH GOUD" in said and said.count("RISHABH AJIT SINGH") == 1


def test_documents_that_all_agree_produce_no_mismatch_sentence():
    same = [{"document_type": "PAN", "value": "A B"}, {"document_type": "DRIVING_LICENCE", "value": "AB"}]
    assert facts._mismatch_detail([{"comparisons": [{"field": "NAME", "sources": same}]}]) == ""


def test_three_values_read_as_a_list_never_but_but():
    said = facts._kyc_mismatch([{"field": "NAME", "sources": FOUR}])
    assert said.count(", but ") == 0 and ", and the bank statement says" in said


def test_an_empty_checklist_answer_names_the_open_review(monkeypatch):
    monkeypatch.setattr(attention, "open_reviews", lambda case_id: [
        {"review_type": "KYC_REVIEW", "reason": "", "affected_documents": ["name"]}])
    out = attention.amend({"intent": "PENDING_ITEMS", "case_id": "C", "answer": "Nothing is pending for this case."})
    assert out["answer"].startswith("Nothing is missing from the checklist, but one thing still needs attention:")
    assert "⚠ KYC needs a review — the name differs across the documents." in out["answer"]


def test_no_open_review_leaves_the_answer_alone(monkeypatch):
    monkeypatch.setattr(attention, "open_reviews", lambda case_id: [])
    out = attention.amend({"intent": "PENDING_ITEMS", "case_id": "C", "answer": "Nothing is pending for this case."})
    assert out["answer"] == "Nothing is pending for this case."


def test_field_labels_read_like_a_person_wrote_them():
    assert field_label("pan_number") == "PAN number" and field_label("father_name") == "Father's name"
    assert field_label("some_new_field") == "Some new field"
