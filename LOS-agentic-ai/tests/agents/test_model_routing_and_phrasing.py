"""The model-routing policy and the deterministic phrasing layer."""

from __future__ import annotations

from app.agents.applicant.copilot.routing import model_routing
from app.agents.applicant.copilot.answering import phrasing


def test_quoted_records_and_refusals_never_reach_the_model():
    for intent in ("APPLICANT_PROFILE", "CASE_HISTORY", "KYC_RESULT", "DOCUMENT_VERIFICATION",
                   "GUARDRAIL_BLOCKED", "ELIGIBILITY"):
        assert model_routing.decide(intent).route is model_routing.Route.NEVER, intent
    assert model_routing.decide("NEXT_ACTION", clarification=True).route is model_routing.Route.NEVER
    assert model_routing.decide("NEXT_ACTION", category="CONVERSATION").route is model_routing.Route.NEVER


def test_simple_answers_take_the_fast_path_and_prose_the_model_path():
    for intent in ("APPLICATION_STAGE", "APPLICATION_STATUS", "DOCUMENTS_PENDING",
                   "DOCUMENTS_REQUIRED", "PENDING_ITEMS"):
        assert model_routing.decide(intent).route is model_routing.Route.FAST, intent
    for intent in ("FOS_KNOWLEDGE", "FULL_SUMMARY", "NEXT_ACTION", "READINESS", "STAGE_PROCESS"):
        assert model_routing.decide(intent).route is model_routing.Route.MODEL, intent


def test_an_unreachable_model_means_the_fast_path():
    assert model_routing.decide("NEXT_ACTION", model_reachable=False).route is model_routing.Route.FAST


def test_the_first_turn_keeps_the_canonical_wording_and_later_turns_vary():
    first = phrasing.field_sentence("loan_amount", "₹5,00,000", language="en",
                                    seed=phrasing.seed_for("c", 1, "loan_amount"))
    assert first == "Your application records show a loan amount of ₹5,00,000."
    later = {phrasing.field_sentence("loan_amount", "₹5,00,000", language="en",
                                     seed=phrasing.seed_for("c", turn, "loan_amount"))
             for turn in range(2, 12)}
    assert len(later) > 1 and all("₹5,00,000" in s for s in later)


def test_every_variant_carries_the_value_and_the_language():
    for field, shapes in phrasing._FIELD_VARIANTS.items():
        for language, family in shapes.items():
            for shape in family:
                assert "{value}" in shape, (field, language, shape)
    for state, shapes in phrasing._STATE_VARIANTS.items():
        for language, family in shapes.items():
            for shape in family:
                assert "{field}" in shape, (state, language, shape)
    assert "hai" in phrasing.field_sentence("mobile", "98", language="hi-Latn", seed=0)


def test_acknowledgements_are_short_and_only_for_the_turns_that_call_for_them():
    said = phrasing.acknowledge("The tenure is 36 months.", turn_type="CORRECTION", language="en", seed=3)
    # the answer is unchanged but for continuing the sentence ("Sure -- the tenure ...")
    assert said.lower().endswith("the tenure is 36 months.")
    assert len(said) - len("The tenure is 36 months.") < 16
    assert "PAN" in phrasing.acknowledge("PAN is verified.", turn_type="CORRECTION",
                                         language="en", seed=3)
    assert phrasing.acknowledge("x", turn_type="NEW_REQUEST", language="en", seed=1) == "x"
    assert phrasing.acknowledge("", turn_type="CORRECTION", language="en", seed=1) == ""
