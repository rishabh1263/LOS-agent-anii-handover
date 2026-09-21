"""
The answer must be supported by the data, not merely sourced from it.

THE LIVE FAILURE THIS SUITE EXISTS FOR. Asked "what can be used as address
proof?", the service answered:

    "Utility bills, rent agreements, and ration cards can be used as address
    proof."

Nothing is accepted for ADDRESS_PROOF except a driving licence, a passport or
a voter ID. Every word of the wrong answer came from a real retrieved
passage, which said those documents are common in the industry AND THAT THIS
SERVICE CANNOT ACCEPT THEM. The generated sentence kept the list and dropped
the negation.

That is what a grounding failure actually looks like. It is not invention out
of nothing; it is a true passage reproduced with its meaning reversed, with a
genuine citation attached. "Retrieval returned relevant chunks" is therefore
not evidence that an answer is grounded, and this suite refuses to treat it
as any.

Two defences, tested separately:

    1. a configuration question is answered from CONFIGURATION, and the
       model is never asked
    2. anything a model does write is checked against the taxonomy before it
       is returned, and a failure falls back to the deterministic answer
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant import facts, grounding, knowledge_answer

#: The exact sentence the live service returned.
HALLUCINATION = (
    "Utility bills, rent agreements, and ration cards can be used as address "
    "proof."
)

#: What it should have said.
CORRECT = "Driving Licence, Passport or Voter ID can satisfy the ADDRESS_PROOF"


@pytest.fixture(autouse=True)
def _config():
    agent_config.reload()
    yield
    agent_config.reload()


def ask(question: str):
    from app.agents.applicant.agent import _knowledge_reply

    return asyncio.run(_knowledge_reply(question))


# ==========================================================================
# 1 -- THE CONFIGURATION QUESTION NEVER REACHES A MODEL
# ==========================================================================

def test_the_address_proof_answer_is_exactly_the_configured_list():
    answer, source, detail = ask("What can be used as address proof?")

    assert CORRECT in answer
    assert detail["authoritative"] is True
    assert source == "KNOWLEDGE"

    for wrong in ("utility bill", "rent agreement", "ration card"):
        assert wrong not in answer.lower(), (
            f"the answer offered {wrong!r}, which is not accepted"
        )


def test_the_configuration_answer_does_not_append_the_handbook_passage():
    """
    THE SECOND HALF OF THE LIVE BUG.

    The correct list was produced and then followed by the retrieved
    paragraph, which mentions utility bills and rent agreements as common in
    the industry. Both halves were true; together they read as two lists of
    things an officer could bring.
    """
    answer, _source, _detail = ask("What can be used as address proof?")

    assert len(answer) < 220, f"the answer carries a handbook paragraph: {answer}"
    assert "checklist slot" not in answer.lower()


def test_a_configuration_question_never_calls_the_model(monkeypatch):
    """
    Not "does not usually". The model is not reachable from this path.

    A configured value has one correct answer, and there is nothing a model
    can add to it that is not a risk.
    """
    called = []

    async def tripwire(*_args, **_kwargs):
        called.append(True)
        return "a model wrote this"

    monkeypatch.setattr(knowledge_answer, "_phrase", tripwire)
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()

    for question in ("What can be used as address proof?",
                     "What documents are required for a personal loan?",
                     "Can I use a utility bill as address proof?"):
        answer, _source, _detail = ask(question)
        assert "a model wrote this" not in answer

    assert not called, "a configuration question reached the model"


def test_an_unsupported_document_is_refused_with_the_right_polarity():
    """
    The handbook sentence about utility bills is a REFUSAL. The answer has to
    keep it that way, so the refusal is written by something that knows which
    way round it goes.
    """
    answer, _source, detail = ask("Can I use a utility bill as address proof?")

    assert answer.lower().startswith("no")
    assert "not accepted" in answer.lower()
    assert CORRECT in answer
    assert detail["authoritative"] is True


# ==========================================================================
# 2 -- THE VALIDATOR
# ==========================================================================

def test_the_validator_rejects_the_live_hallucination():
    verdict = grounding.validate(HALLUCINATION, facts.fact_set("PERSONAL_LOAN"))

    assert not verdict
    assert "utility bill" in verdict.unsupported
    assert "rent agreement" in verdict.unsupported
    assert "ration card" in verdict.unsupported


def test_the_validator_accepts_the_correct_answer():
    correct = ("Driving Licence, Passport or Voter ID can satisfy the "
               "ADDRESS_PROOF requirement.")
    assert grounding.validate(correct, facts.fact_set("PERSONAL_LOAN"))


def test_the_validator_rejects_an_invented_slot():
    invented = "Upload an INCOME_CERTIFICATE to satisfy the requirement."
    verdict = grounding.validate(invented, facts.fact_set("PERSONAL_LOAN"))

    assert not verdict
    assert "INCOME_CERTIFICATE" in verdict.unsupported


def test_the_validator_allows_configured_types_and_statuses():
    text = ("PAN is VERIFIED and BANK_STATEMENT is MISSING; ADDRESS_PROOF "
            "accepts DRIVING_LICENCE.")
    assert grounding.validate(text, facts.fact_set("PERSONAL_LOAN"))


def test_the_validator_follows_configuration(monkeypatch):
    """
    Widen the configured list and the validator widens with it.

    If the validator carried its own copy of the accepted types, the two
    would drift and it would start rejecting correct answers.
    """
    real = agent_config.checklist_for

    def widened(product):
        entries = [dict(e) for e in real(product)]
        for entry in entries:
            if entry["slot"] == "ADDRESS_PROOF":
                entry["accepts"] = list(entry["accepts"]) + ["AADHAAR"]
        return entries

    monkeypatch.setattr(agent_config, "checklist_for", widened)
    updated = facts.fact_set("PERSONAL_LOAN")

    assert grounding.validate("AADHAAR satisfies ADDRESS_PROOF.", updated)
    assert "Aadhaar" in facts.authoritative_answer(
        "What can be used as address proof?").text


def test_a_rejected_answer_falls_back_rather_than_erroring(monkeypatch):
    """
    A validation failure is not an error anybody sees.

    The deterministic answer was computed first and is always available, so
    the caller gets a correct answer and a STRUCTURED-or-KNOWLEDGE source.
    """
    async def hallucinate(*_args, **_kwargs):
        return HALLUCINATION

    monkeypatch.setattr(knowledge_answer, "_phrase", hallucinate)
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()

    # A question the facts layer does NOT own, so the model path runs.
    answer, source, detail = ask("What happens during FOS verification?")

    assert "utility bill" not in answer.lower()
    assert detail.get("grounding_rejected") is True
    assert source == "KNOWLEDGE"
    assert answer and answer != HALLUCINATION


def test_a_model_answer_that_is_grounded_is_kept(monkeypatch):
    """The validator must not reject everything the model writes."""
    async def sensible(*_args, **_kwargs):
        return ("Verification classifies the document, decides a verdict, "
                "and releases fields only behind a PASS.")

    monkeypatch.setattr(knowledge_answer, "_phrase", sensible)
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()

    answer, source, detail = ask("What happens during FOS verification?")

    assert "releases fields only behind a PASS" in answer
    assert detail.get("grounding_rejected") is not True
    assert source == "LLM"


# ==========================================================================
# CASE CLAIMS
# ==========================================================================

def test_a_case_claim_about_a_document_not_on_the_case_is_rejected():
    verdict = grounding.validate_case_claims(
        "Your PASSPORT is VERIFIED.",
        document_types=frozenset({"PAN", "BANK_STATEMENT"}),
        statuses=frozenset({"VERIFIED"}),
    )
    assert not verdict


def test_a_case_claim_matching_the_records_is_accepted():
    verdict = grounding.validate_case_claims(
        "PAN is VERIFIED and BANK_STATEMENT is MISSING.",
        document_types=frozenset({"PAN", "BANK_STATEMENT"}),
        statuses=frozenset({"VERIFIED", "MISSING"}),
    )
    assert verdict


# ==========================================================================
# THE CORPUS ITSELF
# ==========================================================================

def test_the_handbook_still_carries_the_refusal_it_needs_to():
    """
    The passage is not deleted -- it is genuinely useful, and an officer does
    ask whether a utility bill counts. What changed is that the ANSWER to
    that question is no longer generated from it.
    """
    from app import knowledge as knowledge_layer

    chunks = knowledge_layer.get_repository().chunks("FOS")
    text = " ".join(c.text for c in chunks).lower()

    assert "utility bill" in text
    assert "cannot accept them" in text or "not accepted" in text
