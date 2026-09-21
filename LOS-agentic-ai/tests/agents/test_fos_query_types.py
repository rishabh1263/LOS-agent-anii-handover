"""
The eight-way router, and the frontend fields derived from a response.

WHY THE ROUTER IS TESTED SEPARATELY FROM THE CLASSIFIER. The classifier
decides WHAT WAS MEANT; this decides WHAT THAT COMMITS THE SERVICE TO. The
two fail differently. A classifier miss sends a question to the wrong
handler and produces a wrong answer. A router miss sends a question to the
wrong KIND of handling -- a downstream question answered from the FOS
handbook, a knowledge answer that carries the applicant record -- and those
are boundary failures, not accuracy failures.

THE FRONTEND HALF exists because the alternative is every client
re-implementing it. An "Upload document" button enabled for a case the API
will refuse is worse than no button, so the enabled flag has to be a true
prediction of what the route does. These tests are what keeps it true.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import frontend
from app.agents.applicant.intents import WRITE_INTENTS, Intent
from app.agents.applicant.query_types import (
    CARRIES_POLICY,
    READS_CASE,
    QueryType,
    clarification_for,
    type_for,
)

# ==========================================================================
# A. THE MAPPING IS TOTAL AND THE BOUNDARIES HOLD
# ==========================================================================


@pytest.mark.parametrize("intent", list(Intent))
def test_every_intent_maps_to_a_kind(intent):
    assert isinstance(type_for(intent), QueryType)


@pytest.mark.parametrize("intent", list(Intent))
def test_no_intent_falls_through_to_clarification_by_accident(intent):
    """
    CLARIFICATION is the safety net for an intent nobody mapped. It is the
    right direction to fail in -- the copilot asks rather than guessing --
    but an intent that lands there because it was forgotten produces a
    copilot that cannot answer a question it understands perfectly well.
    """
    if intent is Intent.UNKNOWN:
        assert type_for(intent) is QueryType.CLARIFICATION
        return
    assert type_for(intent) is not QueryType.CLARIFICATION, (
        f"{intent.value} is unmapped in query_types.py"
    )


@pytest.mark.parametrize("intent", sorted(WRITE_INTENTS, key=lambda i: i.value))
def test_every_write_is_an_action_request(intent):
    """
    A write routed as a read would skip the confirmation step. The mapping
    is taken from WRITE_INTENTS rather than restated, so adding a write
    cannot leave it behind.
    """
    assert type_for(intent) is QueryType.ACTION_REQUEST


def test_a_downstream_question_is_never_anything_else():
    assert type_for(Intent.OUT_OF_SCOPE) is QueryType.DOWNSTREAM


def test_a_knowledge_question_is_never_a_case_question():
    assert type_for(Intent.FOS_KNOWLEDGE) is QueryType.PROCESS_KNOWLEDGE


def test_all_eight_kinds_are_reachable():
    """A category nothing routes to is a category that does not exist."""
    reached = {type_for(intent) for intent in Intent}
    assert reached == set(QueryType), (
        f"unreachable: {set(QueryType) - reached}"
    )


# ==========================================================================
# B. WHAT MAY READ THE CASE
# ==========================================================================


def test_knowledge_and_downstream_do_not_read_the_case():
    """
    The boundary this encodes. A policy answer that carried the applicant
    record would read as a statement about that person, and a routed
    refusal that carried it would have disclosed the case on its way to
    declining to discuss it.
    """
    assert QueryType.PROCESS_KNOWLEDGE not in READS_CASE
    assert QueryType.DOWNSTREAM not in READS_CASE
    assert QueryType.CLARIFICATION not in READS_CASE


def test_every_case_kind_reads_the_case():
    for kind in (QueryType.CASE_FACT, QueryType.DOCUMENT_STATUS,
                 QueryType.POLICY_REQUIREMENT, QueryType.MIXED,
                 QueryType.ACTION_REQUEST):
        assert kind in READS_CASE


def test_a_requirement_answer_must_carry_its_policy():
    assert QueryType.POLICY_REQUIREMENT in CARRIES_POLICY


# ==========================================================================
# C. ASKING INSTEAD OF GUESSING
# ==========================================================================


def test_a_clarification_offers_things_this_desk_can_answer():
    clarification = clarification_for("wibble", has_case=True)

    assert clarification["reason"] == "INTENT_NOT_RECOGNISED"
    assert clarification["question"]
    assert len(clarification["options"]) >= 3


def test_a_clarification_never_offers_a_downstream_question():
    """
    Offering "is this applicant creditworthy?" would advertise an answer
    the stage refuses to give, and the officer who clicked it would get a
    routing message.
    """
    options = " ".join(clarification_for("x", has_case=True)["options"]).lower()

    for forbidden in ("credit", "cibil", "risk", "fraud", "kyc", "approve",
                      "sanction", "income", "eligib"):
        assert forbidden not in options


def test_a_clarification_does_not_guess_from_the_message():
    """
    Ranking the options against a message the classifier already failed on
    would present a guess as a suggestion.
    """
    first = clarification_for("analyse the bank transactions", has_case=True)
    second = clarification_for("completely different words", has_case=True)

    assert first["options"] == second["options"]


def test_the_original_message_is_echoed_for_the_caller():
    clarification = clarification_for("  what about that thing  ",
                                      has_case=True)
    assert clarification["original_message"] == "what about that thing"


def test_a_very_long_message_is_truncated_rather_than_echoed_whole():
    clarification = clarification_for("x" * 5000, has_case=True)
    assert len(clarification["original_message"]) <= 200


def test_the_wording_differs_when_there_is_no_case():
    with_case = clarification_for("x", has_case=True)["question"]
    without = clarification_for("x", has_case=False)["question"]
    assert with_case != without


# ==========================================================================
# D. CASE STATE IS COUNTED, NEVER ASSERTED
# ==========================================================================


def checklist(*rows):
    out = []
    for slot, mandatory, fulfilment in rows:
        out.append({"slot": slot, "mandatory": mandatory,
                    "fulfilment": fulfilment, "accepts": [slot]})
    return out


def test_case_state_counts_only_required_slots_for_progress():
    """
    Counting optional slots leaves a complete case short of 100% and sends
    an officer looking for a document nobody needs.
    """
    rows = checklist(("PAN", True, "SATISFIED"),
                     ("ADDRESS_PROOF", True, "SATISFIED"),
                     ("PHOTO", False, "MISSING"))

    state = frontend.case_state({}, {}, [], rows, {"status": "READY"})

    assert state["documents_required"] == 2
    assert state["collection_progress"] == 100


def test_case_state_separates_the_four_ways_a_slot_can_be_outstanding():
    rows = checklist(("A", True, "SATISFIED"), ("B", True, "MISSING"),
                     ("C", True, "UNDER_REVIEW"), ("D", True, "FAILED"))

    state = frontend.case_state({}, {}, [], rows, {})

    assert state["documents_satisfied"] == 1
    assert state["documents_missing"] == 1
    assert state["documents_under_review"] == 1
    assert state["documents_failed"] == 1
    assert state["collection_progress"] == 25


def test_the_header_identifies_its_case_even_on_a_narrow_answer():
    """
    A checklist intent plans one tool and that tool returns slots, not the
    application record. The header reported case `null`, which a client
    cannot key on -- so the identifier comes from the request, which
    always has it.
    """
    state = frontend.case_state(None, None, [], [], {}, case_id="CASE-1")
    assert state["case_id"] == "CASE-1"


def test_the_application_record_wins_when_it_is_present():
    state = frontend.case_state({"case_id": "CASE-FROM-RECORD"}, None, [], [],
                                {}, case_id="CASE-FROM-REQUEST")
    assert state["case_id"] == "CASE-FROM-RECORD"


def test_the_header_names_the_product_from_the_policy_block():
    """
    Same reason as the case id: a checklist answer never fetches the
    application record, and the header sat above a checklist entirely
    determined by a product it could not name.
    """
    state = frontend.case_state(None, None, [], [], {},
                                policy={"product": "PERSONAL_LOAN"})
    assert state["product"] == "PERSONAL_LOAN"


def test_a_case_with_no_requirements_does_not_divide_by_zero():
    state = frontend.case_state({}, {}, [], [], {})
    assert state["collection_progress"] == 0


# ==========================================================================
# E. AVAILABLE ACTIONS PREDICT WHAT THE API WILL DO
# ==========================================================================


def test_upload_is_offered_while_anything_is_outstanding():
    rows = checklist(("PAN", True, "MISSING"))
    actions = {a["action"]: a
               for a in frontend.available_actions([], rows, {})}

    assert actions["UPLOAD_DOCUMENT"]["enabled"] is True


def test_upload_is_disabled_with_a_reason_once_everything_is_collected():
    """
    Disabled and explained, not hidden. An action that vanishes leaves an
    officer wondering where it went.
    """
    rows = checklist(("PAN", True, "SATISFIED"))
    actions = {a["action"]: a
               for a in frontend.available_actions([], rows, {})}

    assert actions["UPLOAD_DOCUMENT"]["enabled"] is False
    assert actions["UPLOAD_DOCUMENT"]["disabled_reason"]


def test_verification_status_is_disabled_before_anything_is_uploaded():
    actions = {a["action"]: a
               for a in frontend.available_actions([], [], {})}

    assert actions["GET_VERIFICATION_STATUS"]["enabled"] is False
    assert actions["GET_VERIFICATION_STATUS"]["disabled_reason"]


def test_reupload_is_offered_only_for_a_document_that_needs_it():
    clean = frontend.available_actions(
        [{"status": "VERIFIED"}], [], {})
    troubled = frontend.available_actions(
        [{"status": "REVIEW"}], [], {})

    assert not next(a for a in clean
                    if a["action"] == "MARK_FOR_REUPLOAD")["enabled"]
    assert next(a for a in troubled
                if a["action"] == "MARK_FOR_REUPLOAD")["enabled"]


def test_the_checklist_alone_is_enough_to_offer_reupload():
    """
    THE BUG THIS PINS. A checklist answer plans one tool, and that tool
    returns slots -- not the document list. Reading only `documents` left
    the panel reporting nothing to re-collect on a case whose bank
    statement was sitting in REVIEW, directly above a checklist row saying
    so.
    """
    rows = checklist(("BANK_STATEMENT", True, "UNDER_REVIEW"))
    actions = {a["action"]: a for a in frontend.available_actions([], rows, {})}

    assert actions["MARK_FOR_REUPLOAD"]["enabled"] is True
    assert actions["GET_VERIFICATION_STATUS"]["enabled"] is False, (
        "an UNDER_REVIEW row with no document_id is not a collected document"
    )


def test_a_slot_with_a_document_counts_as_collected():
    rows = [{"slot": "PAN", "mandatory": True, "fulfilment": "SATISFIED",
             "document_id": "D1", "accepts": ["PAN"]}]
    state = frontend.case_state({}, {}, [], rows, {})

    assert state["documents_collected"] == 1


def test_a_document_in_both_sources_is_counted_once():
    rows = [{"slot": "PAN", "mandatory": True, "fulfilment": "SATISFIED",
             "document_id": "D1", "accepts": ["PAN"]}]
    state = frontend.case_state(
        {}, {}, [{"document_id": "D1", "status": "VERIFIED"}], rows, {})

    assert state["documents_collected"] == 1


def test_the_action_list_keeps_its_shape_whatever_the_case():
    """A panel cannot lay out a list whose length changes per case."""
    empty = [a["action"] for a in frontend.available_actions([], [], {})]
    busy = [a["action"] for a in frontend.available_actions(
        [{"status": "REVIEW"}], checklist(("PAN", True, "MISSING")), {})]

    assert empty == busy


def test_a_ready_case_is_offered_the_handoff_and_it_is_marked_external():
    """
    A handoff is a human decision this service does not perform. Offering
    it keeps the panel useful on a finished case; the flag stops a client
    from POSTing it.
    """
    actions = frontend.available_actions([], [], {"status": "READY"})
    handoff = next(a for a in actions if a["action"] == "HANDOFF_TO_CPA")

    assert handoff["external"] is True


def test_an_unready_case_is_not_offered_the_handoff():
    actions = frontend.available_actions([], [], {"status": "NOT_READY"})
    assert not any(a["action"] == "HANDOFF_TO_CPA" for a in actions)


# ==========================================================================
# F. SUGGESTIONS ARE ABOUT THIS CASE
# ==========================================================================


def test_suggestions_lead_with_what_is_blocking():
    rows = checklist(("PAN", True, "FAILED"), ("ADDRESS_PROOF", True, "MISSING"))
    suggestions = frontend.suggested_questions(rows, {})

    assert "rejected" in suggestions[0].lower()
    assert "pan" in suggestions[0].lower()


def test_a_review_is_suggested_before_a_missing_document():
    rows = checklist(("BANK_STATEMENT", True, "UNDER_REVIEW"),
                     ("PAN", True, "MISSING"))
    suggestions = frontend.suggested_questions(rows, {})

    assert "review" in suggestions[0].lower()


def test_suggestions_change_with_the_case():
    """
    The test of a good suggestion is that clicking it returns something. A
    list identical on every case fails it.
    """
    early = frontend.suggested_questions(
        checklist(("PAN", True, "MISSING")), {"status": "NOT_READY"})
    done = frontend.suggested_questions(
        checklist(("PAN", True, "SATISFIED")), {"status": "READY"})

    assert early != done


def test_suggestions_never_offer_a_downstream_question():
    rows = checklist(("BANK_STATEMENT", True, "UNDER_REVIEW"))
    text = " ".join(frontend.suggested_questions(rows, {})).lower()

    for forbidden in ("creditworth", "cibil", "risk", "fraud", "approve",
                      "income", "eligib", "transactions"):
        assert forbidden not in text


def test_an_unevaluated_rule_becomes_a_suggestion():
    policy = {"unevaluated_rules": [
        {"rule_id": "R1", "missing_attributes": ["employment_type"],
         "reason": "...", "would_require": ["INCOME_PROOF"]},
    ]}
    suggestions = frontend.suggested_questions([], {}, policy)

    assert any("employment type" in s.lower() for s in suggestions)


def test_suggestions_are_capped_and_unique():
    rows = checklist(("A", True, "FAILED"), ("B", True, "UNDER_REVIEW"),
                     ("C", True, "MISSING"), ("D", True, "MISSING"))
    suggestions = frontend.suggested_questions(rows, {}, limit=3)

    assert len(suggestions) == 3
    assert len(set(suggestions)) == 3


# ==========================================================================
# G. DOCUMENT HIGHLIGHTS
# ==========================================================================


@pytest.mark.parametrize("status,severity", [
    ("VERIFIED", "OK"), ("REVIEW", "ATTENTION"), ("REJECTED", "BLOCKED"),
    ("UPLOADED", "PENDING"), ("PROCESSING", "PENDING"),
])
def test_each_document_state_has_a_severity(status, severity):
    card = frontend.document_highlights([{"status": status}])[0]
    assert card["severity"] == severity
    assert card["headline"]


def test_the_reason_codes_are_passed_through_not_rewritten():
    """
    Verification is the only thing entitled to say why a document did not
    pass. This picks which one a one-line card shows; it does not write one
    and it does not drop the rest.
    """
    card = frontend.document_highlights([
        {"status": "REVIEW",
         "reason_codes": ["VERIFICATION_TIMEOUT", "REQUIRED_FIELD_MISSING"]},
    ])[0]

    assert card["primary_reason_code"] == "VERIFICATION_TIMEOUT"
    assert card["reason_codes"] == ["VERIFICATION_TIMEOUT",
                                    "REQUIRED_FIELD_MISSING"]


def test_a_document_with_no_reason_has_no_primary_reason():
    card = frontend.document_highlights([{"status": "VERIFIED"}])[0]
    assert card["primary_reason_code"] is None


def test_an_unknown_status_does_not_raise():
    card = frontend.document_highlights([{"status": "SOMETHING_NEW"}])[0]
    assert card["severity"] == "PENDING"


# ==========================================================================
# H. THE WHOLE BLOCK
# ==========================================================================


def test_an_answer_that_read_no_case_gets_no_state():
    block = frontend.contract({"checklist": [{"slot": "PAN",
                                              "mandatory": True,
                                              "fulfilment": "MISSING"}]},
                              include_state=False)

    assert block["case_state"] is None
    assert block["available_actions"] == []
    assert block["suggested_questions"] == []


def test_the_block_always_has_the_same_keys():
    with_state = frontend.contract({}, include_state=True)
    without = frontend.contract({}, include_state=False)
    assert set(with_state) == set(without)
