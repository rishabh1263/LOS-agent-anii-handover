"""
When a question is about this case, the case answers it.

THE THREE LIVE DEFECTS THESE PIN, all on one real case whose PAN and
bank statement passed individually while the application sat in REVIEW
because the two documents name different people.

  A STATUS QUESTION WAS ANSWERED FROM THE HANDBOOK. "What is the
  status of my application" needs "application status" side by side to
  match, so the natural wording missed every pattern, fell to UNKNOWN,
  and the knowledge base answered it confidently -- process
  documentation, about no case in particular, to somebody asking what
  was happening with their file.

  A DOCUMENT VERDICT WAS PRESENTED AS THE CASE'S. "No documents
  currently have verification issues" is true of the documents and
  misleading about the application. A reader told only that walks
  away believing the case is clear.

  AND THE EXPLANATION CARRIED A CLAUSE NOBODY RECORDED. Every clause
  in a case explanation comes from a reason code the pipeline wrote
  down; there is no room for a general observation about there not
  being enough information.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import routing
from app.agents.applicant.intents import Intent, classify
from app.agents.applicant.query_types import QueryType, type_for


# ==========================================================================
# A. CASE STATE OUTRANKS THE HANDBOOK
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What is the current status of my application?",
    "What is the status of my application?",
    "What is happening with my application?",
    "What is the status?",
    "What's the progress on this case?",
    "Any update on my loan?",
])
def test_a_status_question_is_answered_from_the_case(question):
    intent = classify(question).intent

    assert intent is not Intent.FOS_KNOWLEDGE, "answered from the handbook"
    assert intent is not Intent.UNKNOWN, "fell through to the handbook"
    assert routing.category_for(intent) is routing.QueryCategory.CASE_ONLY


@pytest.mark.parametrize("question", [
    "Are my documents verified?",
    "Is the PAN verified?",
    "Have the documents been checked?",
])
def test_a_verification_question_is_answered_from_the_case(question):
    assert classify(question).intent is Intent.DOCUMENT_VERIFICATION


@pytest.mark.parametrize("question", [
    "What documents are required for a personal loan?",
    "What can be used as address proof?",
    "Why is a PAN required?",
])
def test_a_genuine_policy_question_still_reaches_the_handbook(question):
    """
    THE BOUNDARY WORKS BOTH WAYS. Widening the case patterns until
    they swallowed the policy questions would trade one wrong answer
    for another.
    """
    # NOT A CASE INTENT. Some of these classify as FOS_KNOWLEDGE
    # outright and some reach the handbook through UNKNOWN, which is
    # the agent's own fallback; what matters is that widening the
    # case patterns did not capture them.
    intent = classify(question).intent

    assert intent in (Intent.FOS_KNOWLEDGE, Intent.UNKNOWN)
    assert intent is not Intent.APPLICATION_STATUS
    assert intent is not Intent.DOCUMENT_VERIFICATION


def test_a_status_question_is_a_case_fact():
    assert type_for(classify(
        "What is the status of my application?").intent) is QueryType.CASE_FACT


# ==========================================================================
# B. A DOCUMENT VERDICT IS NOT THE CASE'S VERDICT
# ==========================================================================


def memory(decision: str, codes: list[str]) -> dict:
    return {
        "findings": [{"finding_kind": "KYC", "reason_codes": codes,
                      "status": "REVIEW"}],
        "decisions": [{"decision": decision, "status": "PARTIAL",
                       "reason_codes": codes}],
        "events": [],
    }


def test_a_recorded_review_qualifies_a_passing_document_answer(monkeypatch):
    """
    Both documents passed and the application was in REVIEW, because
    the name on the PAN and the name on the bank account belong to
    different people.
    """
    from app.agents.applicant import agent, case_memory_facts

    monkeypatch.setattr(case_memory_facts, "case_memory",
                        lambda *_a, **_k: memory("REVIEW", ["NAME_MISMATCH"]))

    said = agent._case_qualifier("CASE-1", None)

    assert said.startswith("However, your application is under review")
    assert "name" in said.lower()


def test_a_case_that_is_progressing_gets_no_caveat(monkeypatch):
    """
    A qualifier on every answer trains a reader to skip the sentence
    that matters.
    """
    from app.agents.applicant import agent, case_memory_facts

    monkeypatch.setattr(case_memory_facts, "case_memory",
                        lambda *_a, **_k: memory("PASS", []))

    assert agent._case_qualifier("CASE-1", None) == ""


def test_nothing_recorded_means_no_qualifier(monkeypatch):
    from app.agents.applicant import agent, case_memory_facts

    monkeypatch.setattr(case_memory_facts, "case_memory",
                        lambda *_a, **_k: {"findings": [], "decisions": [],
                                           "events": []})

    assert agent._case_qualifier("CASE-1", None) == ""


def test_an_unreachable_store_does_not_break_the_answer(monkeypatch):
    from app.agents.applicant import agent, case_memory_facts

    def boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(case_memory_facts, "case_memory", boom)

    assert agent._case_qualifier("CASE-1", None) == ""


# ==========================================================================
# C. EVERY CLAUSE COMES FROM A RECORDED CODE
# ==========================================================================


def test_the_explanation_names_the_recorded_reason_and_nothing_else():
    """
    The live answer added "and there isn't enough information to reach
    any conclusion" beside a recorded NAME_MISMATCH. One of those was
    written down; the other was not.
    """
    from app.agents.applicant import case_memory_facts

    said, _ = case_memory_facts.explain(memory("REVIEW", ["NAME_MISMATCH"]))

    assert "name" in said.lower()
    assert "enough information" not in said.lower()
    assert "conclusion" not in said.lower()


def test_an_unrecorded_case_says_so_rather_than_explaining():
    from app.agents.applicant import case_memory_facts

    said, sources = case_memory_facts.explain(
        {"findings": [], "decisions": [], "events": []})

    assert said
    assert sources == []


# ==========================================================================
# D. A RECORDED REASON IS REPORTED, NOT PARAPHRASED
# ==========================================================================

#: What the live answer said instead of naming two people. Each of
#: these is a generalisation of a finding that was recorded precisely,
#: and a generalisation of a precise finding is a loss of information
#: dressed as an explanation.
UNSUPPORTED = (
    "enough matching information",
    "enough information",
    "not enough",
    "insufficient information",
)

NAME_MISMATCH_MEMORY = {
    "findings": [{
        "finding_kind": "KYC",
        "status": "REVIEW",
        "reason_codes": ["NAME_MISMATCH"],
        # THE PUBLIC SHAPE, which is what `case_memory` hands back: the
        # failed comparison and what each document said, and nothing
        # else out of the finding's payload.
        "comparisons": [{
            "field": "NAME",
            "sources": [
                {"document_type": "PAN", "value": "RISHABH AJIT SINGH"},
                {"document_type": "BANK_STATEMENT",
                 "value": "PRIYANKAROHANMORE"},
            ],
        }],
    }],
    "decisions": [{"decision": "REVIEW", "status": "PARTIAL",
                   "reason_codes": ["NAME_MISMATCH"]}],
    "events": [],
}


def test_the_explanation_names_both_values():
    """
    An officer cannot decide whether two documents describe one person
    without seeing both names. "The name differs across documents" is
    a description of a reason code; this is the finding.
    """
    from app.agents.applicant import case_memory_facts

    said, sources = case_memory_facts.explain(NAME_MISMATCH_MEMORY)

    assert "RISHABH AJIT SINGH" in said
    assert "PRIYANKAROHANMORE" in said
    assert "PAN" in said
    assert sources, "the values were quoted with nothing to attribute them to"


@pytest.mark.parametrize("phrase", UNSUPPORTED)
def test_the_unsupported_phrase_never_appears_beside_a_name_mismatch(phrase):
    from app.agents.applicant import case_memory_facts

    said, _ = case_memory_facts.explain(NAME_MISMATCH_MEMORY)

    assert phrase not in said.lower()


@pytest.mark.parametrize("phrase", UNSUPPORTED)
def test_the_qualifier_carries_the_concrete_reason(monkeypatch, phrase):
    from app.agents.applicant import agent, case_memory_facts

    monkeypatch.setattr(case_memory_facts, "case_memory",
                        lambda *_a, **_k: NAME_MISMATCH_MEMORY)

    said = agent._case_qualifier("CASE-1", None)

    assert "RISHABH AJIT SINGH" in said
    assert "PRIYANKAROHANMORE" in said
    assert phrase not in said.lower()


def test_the_values_are_recorded_only_for_a_failed_comparison():
    """
    A field that passed needs no evidence kept, and case memory is not
    a place to accumulate extracted values.
    """
    from app.store.ingest import _kyc_field

    sources = [{"document_type": "PAN", "value": "A", "bounding_box": [1, 2]},
               {"document_type": "BANK_STATEMENT", "value": "B"}]

    failed = _kyc_field({"field": "NAME", "status": "FAIL",
                         "sources": sources})
    passed = _kyc_field({"field": "DATE_OF_BIRTH", "status": "PASS",
                         "sources": sources})

    assert [s["value"] for s in failed["sources"]] == ["A", "B"]
    assert "bounding_box" not in failed["sources"][0], "raw OCR detail kept"
    assert "sources" not in passed


# ==========================================================================
# E. DOCUMENTS PASSED, THE CASE DID NOT
# ==========================================================================
#
# THE LIVE ANSWER THAT PROMPTED THIS. Every document on the case
# passed, the case was decided REVIEW because the PAN and the bank
# statement name different people, and the officer was told: "Your
# documents, specifically the PAN and bank statement, have been
# verified as PASS. The application is under review with a PARTIAL
# decision." The one fact they needed -- WHY -- was missing, and
# PARTIAL, which is how far processing got, was presented as the
# decision.
#
# THE CHAIN HAD TWO BREAKS. `_index_case` read KYC out of the party
# sections only, so a single-applicant response -- which carries one
# KYC block at the top level -- recorded the verdict and dropped the
# comparison behind it. And `explain` read reason codes off findings
# only, so with no finding it reported that nothing had been recorded,
# beside a decision that said NAME_MISMATCH.

SINGLE_APPLICANT_RESULT = {
    "applicant_id": "APP-E2E", "case_id": "CASE-E2E", "request_id": "r-e2e",
    "status": "PARTIAL", "decision": "REVIEW", "next_action": "MANUAL_REVIEW",
    "documents": [
        {"type": "PAN", "verification": "PASS", "reason_codes": [],
         "extraction": {"name": "..."}, "source_id": "pan.pdf"},
        {"type": "BANK_STATEMENT", "verification": "PASS", "reason_codes": [],
         "extraction": {"name": "..."}, "source_id": "bank.pdf"},
    ],
    # AT THE TOP LEVEL, which is the shape a one-applicant case
    # produces and the shape the finding loop used to miss.
    "kyc": {
        "status": "REVIEW", "overall_score": 20, "overall_confidence": 90,
        "reason_codes": ["NAME_MISMATCH"],
        "fields": [
            {"field": "NAME", "status": "FAIL", "match_score": 10,
             "confidence": 90, "reason_code": "NAME_MISMATCH",
             "sources": [
                 {"source_id": "pan.pdf", "document_type": "PAN",
                  "value": "RISHABH AJIT SINGH"},
                 {"source_id": "bank.pdf", "document_type": "BANK_STATEMENT",
                  "value": "PRIYANKAROHANMORE"}]},
            {"field": "DATE_OF_BIRTH", "status": "PASS", "match_score": 100,
             "confidence": 90,
             "sources": [{"source_id": "pan.pdf", "document_type": "PAN",
                          "value": "2002-06-12"}]},
        ],
    },
}

E2E_SCOPES = ("read_applicant read_application read_documents "
              "read_verification read_pending_items read_next_action")


@pytest.fixture
def processed_case(tmp_path, monkeypatch):
    """One LOS response, persisted through the real ingest path."""
    from app.agents.applicant import config as agent_config
    from app.store import set_repository
    from app.store.ingest import persist_los_result
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()

    repository = SQLiteRepository(tmp_path / "e2e.sqlite3")
    repository.initialise()
    set_repository(repository)

    persist_los_result(SINGLE_APPLICANT_RESULT)
    # The officer who opened this case owns it (app/security/access.py);
    # the tests below ask as that officer.
    repository.grant_access("fos", "APPLICANT",
                            SINGLE_APPLICANT_RESULT["applicant_id"])
    yield repository

    set_repository(None)
    agent_config.reload()


async def ask(question: str) -> str:
    from app.agents.applicant.agent import answer_question

    response = await answer_question(
        message=question, applicant_id="APP-E2E", case_id="CASE-E2E",
        claims={"sub": "fos", "scope": E2E_SCOPES},
    )
    return response["answer"]


async def test_a_document_verification_answer_carries_the_kyc_reason(
        processed_case):
    said = await ask("What is the verification status of my documents?")

    assert "PAN" in said and "bank statement" in said.lower()
    assert "under review because" in said.lower()
    assert "RISHABH AJIT SINGH" in said
    assert "PRIYANKAROHANMORE" in said


async def test_the_processing_state_is_not_called_the_decision(processed_case):
    """
    PARTIAL is how far processing got. REVIEW is what was decided.
    The live answer called PARTIAL the decision.
    """
    said = await ask("What is the verification status of my documents?")

    assert "partial decision" not in said.lower()
    assert "with a PARTIAL" not in said


@pytest.mark.parametrize("phrase", UNSUPPORTED)
async def test_no_generic_explanation_replaces_the_recorded_one(
        processed_case, phrase):
    said = await ask("What is the verification status of my documents?")

    assert phrase not in said.lower()


async def test_the_reason_is_absent_when_the_case_recorded_none(
        processed_case, monkeypatch):
    """
    THE TEST ABOVE IS ONLY MEANINGFUL IF THIS ONE PASSES. Remove the
    recorded KYC evidence and the reason disappears from the answer --
    which is what proves the sentence came from the case and not from
    the wording of the answer itself.
    """
    from app.agents.applicant import case_memory_facts

    monkeypatch.setattr(case_memory_facts, "case_memory",
                        lambda *_a, **_k: {"findings": [], "decisions": [],
                                           "timeline": []})

    said = await ask("What is the verification status of my documents?")

    assert "RISHABH AJIT SINGH" not in said
    assert "under review" not in said.lower()
    assert "PAN" in said, "the documents' own verdict was lost too"


def test_a_case_level_kyc_block_is_recorded_as_a_finding(processed_case):
    """
    The break that started the chain: a single-applicant response
    carries KYC at the top level, and the party loop never saw it.
    """
    from app.agents.applicant import case_memory_facts

    memory = case_memory_facts.case_memory("CASE-E2E", None)
    kyc = [f for f in memory["findings"] if f["finding_kind"] == "KYC"]

    assert len(kyc) == 1
    assert kyc[0]["reason_codes"] == ["NAME_MISMATCH"]


def test_only_the_failed_comparison_is_published(processed_case):
    """
    The values are published because they ARE the reason. A comparison
    that passed publishes nothing, and no other part of the payload is
    published at all.
    """
    from app.agents.applicant import case_memory_facts

    memory = case_memory_facts.case_memory("CASE-E2E", None)
    kyc = [f for f in memory["findings"] if f["finding_kind"] == "KYC"][0]

    assert [c["field"] for c in kyc["comparisons"]] == ["NAME"]
    assert "2002-06-12" not in str(kyc), "a passing comparison was published"
    assert "payload" not in kyc
    assert "match_score" not in str(kyc["comparisons"])


def test_a_decisions_own_reason_codes_explain_a_case_with_no_findings():
    """
    A case recorded before the finding was written still has its
    verdict, and the verdict carries reason codes. Reading findings
    alone reported that nothing had been recorded.
    """
    from app.agents.applicant import case_memory_facts

    said, _ = case_memory_facts.explain({
        "findings": [],
        "decisions": [{"decision": "REVIEW", "status": "PARTIAL",
                       "reason_codes": ["NAME_MISMATCH"]}],
        "timeline": [],
    })

    assert "name differs across documents" in said.lower()
    assert "no individual findings" not in said.lower()
