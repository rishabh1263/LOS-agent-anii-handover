"""
The short summary, and the line between phrasing and deciding.

WHAT THIS GUARDS. The summary is the only generated prose in the FOS
response, and it sits next to fields a person will act on. Every fact
in it is computed first; the model is handed those conclusions and
asked for two sentences. It cannot decide a status, a document, a
readiness or a next action, because it is never given anything it
would need to and its output never replaces a field.

AND THE REPORTED SOURCE HAS TO BE TRUE. `structured+llm` means a model
wrote the words. A model that is off, slow, or produced nothing usable
leaves the deterministic sentence and says `structured` -- claiming
otherwise would misreport how the answer was made, which is the one
thing a reader cannot check.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import case_summary

ENVELOPE = {
    "stage": "BASIC_DOCUMENT_VERIFICATION",
    "applicant": {"full_name": "A PERSON", "missing_fields": []},
    "application": {"status": "BASIC_DOCUMENT_VERIFICATION"},
    "documents": [
        {"document_type": "PAN", "status": "VERIFIED",
         "verification_status": "PASS"},
        {"document_type": "BANK_STATEMENT", "status": "REVIEW",
         "verification_status": "REVIEW",
         "reason_codes": ["DOCUMENT_REQUIRES_OCR"]},
    ],
    "pending_items": [{"code": "DOCUMENT_MISSING"}],
    "readiness": {"ready": False},
    "next_action": {"action": "PROCESS_BANK_STATEMENT_OCR"},
    "processing_queue": [
        {"document_type": "BANK_STATEMENT", "status": "QUEUED"}],
}


# ==========================================================================
# A. WHAT THE MODEL IS ALLOWED TO SEE
# ==========================================================================


def test_the_state_is_the_conclusions_not_the_records():
    state = case_summary.state_of(ENVELOPE)

    assert state["stage"] == "BASIC_DOCUMENT_VERIFICATION"
    assert state["applicant_details_complete"] is True
    assert state["ready_for_handoff"] is False
    assert state["items_outstanding"] == 1


def test_documents_reach_the_model_as_words():
    state = case_summary.state_of(ENVELOPE)

    assert {"document": "PAN", "status": "verified"} in state["documents"]
    assert {"document": "bank statement",
            "status": "under review"} in state["documents"]


def test_nothing_identifying_reaches_the_model():
    """
    A model given a name or an id prints it. The summary describes a
    case; it does not need to name anybody.
    """
    import json

    blob = json.dumps(case_summary.state_of(ENVELOPE))

    for leak in ("A PERSON", "full_name", "applicant_id", "case_id"):
        assert leak not in blob


def test_a_queued_document_is_described_as_still_being_read():
    state = case_summary.state_of(ENVELOPE)

    assert state["still_being_read"] == [
        {"document": "bank statement",
         "state": "still being read in the background"}]


def test_a_finished_job_is_not_described_as_pending():
    """
    Absent rather than empty: the key is omitted when nothing is being
    read, so a model cannot read an empty list as a fact about the
    case. See `state_of`.
    """
    envelope = dict(ENVELOPE, processing_queue=[
        {"document_type": "BANK_STATEMENT", "status": "COMPLETED"}])

    assert "still_being_read" not in case_summary.state_of(envelope)


def test_what_is_not_known_is_left_out_rather_than_guessed():
    """
    THE CONTRADICTION THIS PREVENTS. A pruned envelope carries no
    readiness and no pending items; reported as zero and false, the
    model announced a case was ready while the answer beside it
    listed three things blocking it.
    """
    state = case_summary.state_of({"documents": []})

    for absent in ("items_outstanding", "ready_for_handoff",
                   "applicant_details_complete", "next_action"):
        assert absent not in state


# ==========================================================================
# B. THE DETERMINISTIC SUMMARY STANDS ON ITS OWN
# ==========================================================================


def test_the_computed_summary_says_where_the_case_is():
    said = case_summary.deterministic(case_summary.state_of(ENVELOPE))

    assert "basic document verification" in said.lower()
    assert "PAN verified" in said


def test_the_computed_summary_is_short():
    said = case_summary.deterministic(case_summary.state_of(ENVELOPE))

    assert len(said) <= case_summary.MAX_CHARACTERS


def test_a_case_with_nothing_on_it_still_summarises():
    said = case_summary.deterministic(case_summary.state_of({}))

    assert said.strip()
    assert len(said) <= case_summary.MAX_CHARACTERS


# ==========================================================================
# C. THE SOURCE IS REPORTED HONESTLY
# ==========================================================================


@pytest.mark.anyio
async def test_no_model_means_the_computed_summary_and_structured(monkeypatch):
    monkeypatch.setattr("app.llm.availability.provider_reachable",
                        lambda: False)

    said, source = await case_summary.summarise(ENVELOPE)

    assert source == case_summary.STRUCTURED
    assert said == case_summary.deterministic(case_summary.state_of(ENVELOPE))


@pytest.mark.anyio
async def test_a_model_failure_is_not_reported_as_a_model_answer(monkeypatch):
    monkeypatch.setattr("app.llm.availability.provider_reachable",
                        lambda: True)
    monkeypatch.setattr("app.agents.applicant.config.llm_enabled",
                        lambda: True)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("model is down")

    monkeypatch.setattr(case_summary, "_generate", boom)

    with pytest.raises(RuntimeError):
        await case_summary._generate({}, 1.0)

    # And through the public path, the failure is absorbed.
    monkeypatch.setattr(case_summary, "_generate",
                        lambda *_a, **_k: _empty())
    said, source = await case_summary.summarise(ENVELOPE)

    assert source == case_summary.STRUCTURED
    assert said


async def _empty() -> str:
    return ""


# ==========================================================================
# D. LENGTH IS ENFORCED, NOT REQUESTED
# ==========================================================================


def test_a_long_answer_is_cut_to_two_sentences():
    said = case_summary._trimmed(
        "One. Two. Three. Four.")

    assert said == "One. Two."


def test_a_single_overlong_sentence_is_still_cut():
    said = case_summary._trimmed("word " * 200)

    assert len(said) <= case_summary.MAX_CHARACTERS + 3


def test_an_empty_model_answer_is_nothing_rather_than_whitespace():
    assert case_summary._trimmed("   ") == ""
