"""
"Why is <document> required?" is product knowledge answered from that document's
CONFIGURED reason (facts.authoritative_answer, cited as the configured policy) --
in any language the question is typed in. A document the product does not ask for
is said to be not required; nothing is invented.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import facts
from app.agents.applicant.copilot.semantics.intents import Intent, understand


@pytest.mark.parametrize("question, document", [
    ("why is PAN required?", "PAN"), ("PAN kyu chahiye?", "PAN"), ("why do I need PAN", "PAN"),
    ("PAN क्यों ज़रूरी है?", "PAN"), ("why is the signature needed?", "SIGNATURE"),
    ("why is address proof needed?", "ADDRESS_PROOF"),
])
def test_why_a_named_document_is_required_is_product_knowledge(question, document):
    c = understand(question, has_case=True)
    assert c.intent is Intent.FOS_KNOWLEDGE and c.document_type == document


@pytest.mark.parametrize("question", ["why is my PAN under review?", "what documents do I need",
                                      "why is my case not ready"])
def test_other_why_and_what_questions_keep_their_own_routes(question):
    assert understand(question, has_case=True).intent is not Intent.FOS_KNOWLEDGE


@pytest.mark.parametrize("product", ["PERSONAL_LOAN", "HOME_LOAN"])
def test_pan_is_explained_as_the_identity_proof(product):
    fact = facts.authoritative_answer("why is PAN required?", product)
    assert fact is not None and fact.kind == "why_required"
    assert fact.text.startswith("PAN is required") and "identity proof at the FOS stage" in fact.text


def test_an_accepted_type_is_explained_through_its_slot():
    fact = facts.authoritative_answer("why is a passport needed?")
    assert "one of the documents accepted as Address Proof" in fact.text


def test_the_signature_photo_is_optional_on_both_products():
    for product in ("PERSONAL_LOAN", "HOME_LOAN"):
        assert "optional" in facts.authoritative_answer("why is the signature needed?", product).text


def test_the_answer_is_generic_across_products():
    fact = facts.authoritative_answer("why is PAN required?")
    assert "Personal Loan and Home Loan" in fact.text


def test_a_document_no_product_asks_for_is_not_required():
    fact = facts.authoritative_answer("why is a photo required?")
    assert "isn't on any product's checklist" in fact.text


def test_photo_is_no_longer_on_any_checklist():
    for product in ("PERSONAL_LOAN", "HOME_LOAN"):
        assert "PHOTO" not in facts.fact_set(product).accepts
