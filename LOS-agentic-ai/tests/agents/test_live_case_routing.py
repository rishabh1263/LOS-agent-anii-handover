"""
Questions about THIS case's KYC, suspicion, credit and queries are answered from the
case's own records -- never from the handbook's definition, never from a generic menu.
Definitions stay definitions.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.capabilities import gates, queries
from app.agents.applicant.copilot.semantics import intents


@pytest.mark.parametrize("question", [
    "what is missing in KYC?", "what failed in KYC?", "what should I do for KYC?",
    "has my KYC been done?", "why is KYC under review?",
])
def test_a_question_about_this_cases_kyc_reads_the_recorded_result(question):
    assert intents.understand(question, has_case=True).intent is intents.Intent.KYC_RESULT


@pytest.mark.parametrize("question", ["what is KYC?", "why is KYC needed?"])
def test_a_kyc_definition_stays_a_definition(question):
    assert intents.understand(question, has_case=True).intent is intents.Intent.FOS_KNOWLEDGE


@pytest.mark.parametrize("question", ["is anything suspicious on my case?", "ye suspicious kyu hai?"])
def test_suspicion_reads_the_recorded_findings(question):
    assert intents.understand(question, has_case=True).intent is intents.Intent.CASE_FINDINGS


def test_fraud_keeps_its_downstream_route():
    assert intents.understand("is this fraud suspicious?", has_case=True).intent is intents.Intent.OUT_OF_SCOPE


@pytest.mark.parametrize("question, target", [
    ("underwriting mein kya issue hai?", "CREDIT"),
    ("what is pending in credit?", "CREDIT"),
    ("what is blocking credit?", "CREDIT"),
])
def test_credit_and_underwriting_questions_read_the_stage_gate(question, target):
    found = gates.request(question)
    assert found is not None and found.kind == gates.EVALUATE and found.target == target


def test_a_plain_pending_question_is_not_a_gate_question():
    assert gates.request("what is pending?") is None


@pytest.mark.parametrize("question, kind", [
    ("query raise kar do", queries.RAISE), ("raise a query for this", queries.RAISE),
    ("please create a query", queries.RAISE), ("open a new query", queries.RAISE),
    ("which queries are open?", queries.LIST),
    ("koi deviation hai?", queries.LIST), ("any open queries?", queries.LIST),
])
def test_query_requests_are_recognised(question, kind):
    found = queries.request(question)
    assert found is not None and found.kind == kind


@pytest.mark.parametrize("question", ["what is a query?", "can you hit a sql query for me", "what is pending?"])
def test_other_questions_are_not_query_requests(question):
    assert queries.request(question) is None


# ---- the loan agent's questions (2026-10-04) ----------------------------------------------
@pytest.mark.parametrize("question", [
    "which fields did not match?", "kya match nahi hua?", "kaunsa field match nahi hua?", "what is not matching?",
])
def test_mismatch_questions_read_the_recorded_kyc_mismatch(question):
    found = intents.understand(question, has_case=True)
    assert found.intent is intents.Intent.KYC_RESULT and found.fields.get("want") == "mismatch"


@pytest.mark.parametrize("question", [
    "what details were extracted from the bank statement?", "PAN se kya kya nikla?",
])
def test_what_was_extracted_reads_the_document_record(question):
    assert intents.understand(question, has_case=True).intent is intents.Intent.DOCUMENT_DETAILS


@pytest.mark.parametrize("question", [
    "does the customer's name match the application?", "does the DOB match the application form?",
])
def test_the_application_form_match_is_live_data_not_the_handbook(question):
    found = intents.understand(question, has_case=True)
    assert found.intent is intents.Intent.KYC_RESULT and found.fields.get("want") == "application"
