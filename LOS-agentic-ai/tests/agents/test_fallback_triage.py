"""
FALLBACK TRIAGE: every question the desk cannot answer gets a deliberate behaviour
-- off-topic declined, domain-adjacent routed with its reason and suggestions,
unreadable input asked to be rephrased, a vague document question asked about THAT
document, "who are you" answered as identity -- while recorded facts stay answers.
"""

import pytest

from app.agents.applicant import conversation
from app.agents.applicant.copilot.semantics import semantic_frame
from app.agents.applicant.copilot.semantics.intents import Intent, understand
from app.agents.applicant.query_types import clarification_for


@pytest.mark.parametrize("question, route", [
    ("how much loan can I get", "ELIGIBILITY_AMOUNT"), ("what interest rate will I get?", "LOAN_TERMS"),
    ("can you reduce my EMI", "LOAN_TERMS"), ("EMI kam kar do", "LOAN_TERMS"),
    ("best mutual fund to invest", "INVESTMENT_ADVICE"),
    ("should I take a personal loan or home loan", "PRODUCT_ADVICE"),
])
def test_domain_adjacent_questions_are_routed_with_a_reason(question, route):
    c = understand(question, has_case=True)
    assert c.intent is Intent.OUT_OF_SCOPE and c.route_to == route


@pytest.mark.parametrize("question", ["what is my interest rate", "kitni emi already chal rahi h meri",
                                      "what is my loan amount", "what is my tenure"])
def test_recorded_values_are_still_answers(question):
    assert understand(question, has_case=True).intent is Intent.APPLICANT_PROFILE


@pytest.mark.parametrize("question, reason", [
    ("kal mausam kaisa rahega", "OFF_TOPIC"), ("how to cook biryani", "OFF_TOPIC"),
    ("asdfgh jkl", "UNCLEAR_INPUT"), ("??", "UNCLEAR_INPUT"), ("qwerty 123 zzz", "UNCLEAR_INPUT"),
    ("wibble", "INTENT_NOT_RECOGNISED"), ("date", "INTENT_NOT_RECOGNISED"),
])
def test_the_fallback_says_what_kind_of_not_understood(question, reason):
    assert clarification_for(question, has_case=True)["reason"] == reason


def test_who_are_you_is_identity_not_how_are_you():
    assert conversation.classify("who are you?").kind == conversation.IDENTITY
    assert conversation.classify("aap kaun ho").kind == conversation.IDENTITY
    assert conversation.classify("how are you").kind == conversation.SMALL_TALK


def test_a_vague_question_about_one_document_asks_about_that_document():
    frame = semantic_frame.parse("help with PAN")
    asked = semantic_frame.clarification(frame, has_case=True)
    assert asked and all("PAN" in option for option in asked["options"])
