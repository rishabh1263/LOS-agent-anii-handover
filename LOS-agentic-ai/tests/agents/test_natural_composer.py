"""
The natural response layer (copilot/answering/composer.py): a plan for every
answer; a model rewording only behind CHATBOT_NATURAL_COMPOSITION, only for
eligible plans, and DISCARDED unless every fact of the structured truth
survives unchanged. The model is stubbed: these tests check the contract,
not a model.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant.copilot.answering import composer

ADDRESS = "Driving Licence, Passport or Voter ID can satisfy the ADDRESS_PROOF requirement."
KNOWLEDGE_RESPONSE = {"intent": "FOS_KNOWLEDGE", "category": "KNOWLEDGE_ONLY",
                      "answer": ADDRESS + "\n\nSource: FOS handbook, Address proof.",
                      "understanding": {"frame": {"language": "en"}, "conversation": {}}}


def _run(response, reworded, monkeypatch, *, flag="true"):
    monkeypatch.setenv(composer.FLAG, flag)

    async def fake(body, question, plan_name, language):
        return reworded

    monkeypatch.setattr(composer, "_reword", fake)
    return asyncio.run(composer.finish(dict(response, understanding=dict(response["understanding"])),
                                       "What documents are accepted as address proof?"))


def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(composer.FLAG, raising=False)
    assert composer.enabled() is False


def test_flag_off_publishes_the_deterministic_answer_unchanged(monkeypatch):
    out = _run(KNOWLEDGE_RESPONSE, "Anything at all.", monkeypatch, flag="false")
    assert out["answer"] == KNOWLEDGE_RESPONSE["answer"]
    assert out["understanding"]["natural_composition"]["reason"] == "FLAG_OFF"
    assert out["understanding"]["response_plan"] == composer.KNOWLEDGE


def test_a_faithful_rewording_is_used_and_keeps_its_citation(monkeypatch):
    reworded = "A driving licence, passport or voter ID can be used to meet the ADDRESS_PROOF requirement."
    out = _run(KNOWLEDGE_RESPONSE, reworded, monkeypatch)
    assert out["answer"].startswith(reworded)
    assert out["answer"].endswith("Source: FOS handbook, Address proof.")
    assert out["understanding"]["natural_composition"]["accepted"] is True


@pytest.mark.parametrize("unfaithful", [
    "Only a Passport satisfies ADDRESS_PROOF.",                                    # dropped + narrowed
    ADDRESS + " Aadhaar works too.",                                               # added a document
    "Driving Licence, Passport or Voter ID cannot satisfy the ADDRESS_PROOF requirement.",  # negation
])
def test_an_unfaithful_rewording_is_discarded(unfaithful, monkeypatch):
    out = _run(KNOWLEDGE_RESPONSE, unfaithful, monkeypatch)
    assert out["answer"] == KNOWLEDGE_RESPONSE["answer"]
    assert out["understanding"]["natural_composition"]["reason"].startswith("FIDELITY_REJECTED")


def test_a_dropped_clause_is_discarded():
    source = ("Indian lenders commonly ask for Aadhaar and Form 16. None of them is mandatory here, "
              "because a document becomes required only by being listed as a mandatory slot.")
    assert composer.fidelity(source, "Indian lenders typically require Aadhaar and Form 16.")


def test_another_language_is_never_published(monkeypatch):
    response = dict(KNOWLEDGE_RESPONSE, understanding={"frame": {"language": "hi-Latn"}, "conversation": {}})
    out = _run(response, "Driving Licence, Passport ya Voter ID se ADDRESS_PROOF satisfy hota hai.", monkeypatch)
    assert out["answer"] == KNOWLEDGE_RESPONSE["answer"]
    assert "unverifiable language" in out["understanding"]["natural_composition"]["reason"]


@pytest.mark.parametrize("source,candidate", [
    ("Your loan amount is ₹5,00,000 with a tenure of 36 months.", "You've asked for ₹5 lakh over 36 months."),
    ("The co-applicant's mobile number is 9811122233.", "Your mobile number is 9811122233."),
    ("Your case ID is case_abc123.", "Your case ID is case_abc124."),
    ("Your date of birth is 14 May 1990.", "Your date of birth is 15 May 1990."),
    ("Your PAN is verified.", "Your PAN is rejected."),
])
def test_fidelity_rejects_every_changed_fact(source, candidate):
    assert composer.fidelity(source, candidate)


@pytest.mark.parametrize("intent", ["KYC_RESULT", "DOCUMENT_VERIFICATION", "GUARDRAIL_BLOCKED",
                                    "APPLICATION_STATUS", "CASE_HISTORY"])
def test_truth_bearing_answers_are_never_reworded(intent, monkeypatch):
    response = dict(KNOWLEDGE_RESPONSE, intent=intent)
    out = _run(response, "Something else entirely.", monkeypatch)
    assert out["answer"] == response["answer"]
    assert out["understanding"]["natural_composition"]["attempted"] is False


def test_a_single_field_is_never_sent_to_the_model(monkeypatch):
    response = {"intent": "APPLICANT_PROFILE", "category": "CASE_ONLY",
                "answer": "Your mobile number is 9876501234.",
                "understanding": {"frame": {"language": "en"}, "conversation": {}}}
    out = _run(response, "Your mobile number is 9876501234!", monkeypatch)
    assert out["understanding"]["response_plan"] == composer.ANSWER_ONLY
    assert out["understanding"]["natural_composition"]["attempted"] is False


def test_an_unavailable_model_keeps_the_deterministic_answer(monkeypatch):
    out = _run(KNOWLEDGE_RESPONSE, None, monkeypatch)
    assert out["answer"] == KNOWLEDGE_RESPONSE["answer"]
    assert out["understanding"]["natural_composition"]["reason"] == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("response,expected", [
    ({"intent": "GUARDRAIL_BLOCKED"}, composer.REFUSAL),
    ({"intent": "UNKNOWN", "clarification_required": {"options": []}}, composer.CLARIFICATION),
    ({"intent": "NEXT_ACTION"}, composer.NEXT_STEP),
    ({"intent": "CASE_HISTORY"}, composer.EXPLANATION),
    ({"intent": "APPLICANT_PROFILE",
      "understanding": {"conversation": {"note": "the previous answer, expanded"}}}, composer.EXPANSION),
    ({"intent": "APPLICANT_PROFILE"}, composer.ANSWER_ONLY),
])
def test_the_plan_follows_the_turn(response, expected):
    assert composer.plan(response) == expected


@pytest.mark.parametrize("already", [
    {"response_source": "LLM"},                                                    # the model wrote it
    {"understanding": {"frame": {"language": "en"}, "conversation": {},
                       "model_routing": {"phrased_by_model": True}}},              # asked to phrase it
    {"understanding": {"frame": {"language": "en"}, "conversation": {},
                       "llm": {"consulted": True}}},                               # asked to read the question
])
def test_one_model_call_per_request(already, monkeypatch):
    """A turn that already used the model is not handed to it a second time."""
    calls = []

    async def fake(body, question, plan_name, language):
        calls.append(body)
        return "Reworded."

    monkeypatch.setenv(composer.FLAG, "true")
    monkeypatch.setattr(composer, "_reword", fake)
    response = {**KNOWLEDGE_RESPONSE, **already}
    out = asyncio.run(composer.finish(dict(response, understanding=dict(response["understanding"])), "q?"))
    assert calls == []
    assert out["answer"] == KNOWLEDGE_RESPONSE["answer"]
    assert out["understanding"]["natural_composition"]["reason"] == "MODEL_ALREADY_USED"


def test_a_request_that_already_called_the_model_is_not_composed(monkeypatch):
    """The ledger, not the answer's source, decides: an attempted-and-discarded call counts."""
    from app.llm import trace

    tokens = trace.begin()
    try:
        trace.record({"caller": "app.agents.applicant.knowledge_answer._phrase", "outcome": "OK", "ms": 1.0})
        out = _run(KNOWLEDGE_RESPONSE, "Reworded.", monkeypatch)
        assert out["understanding"]["natural_composition"]["reason"] == "MODEL_ALREADY_USED"
    finally:
        trace.end(tokens)


def test_the_next_step_is_never_reworded(monkeypatch):
    response = {**KNOWLEDGE_RESPONSE, "intent": "NEXT_ACTION", "category": "CASE_ONLY",
                "answer": "Your next step is to collect and upload the missing document: Address Proof."}
    out = _run(response, "Please upload your address proof next.", monkeypatch)
    assert out["answer"] == response["answer"]
