"""
The copilot as a frontend actually consumes it.

WHAT SEPARATES THIS FROM THE OTHER FOS SUITES. `test_fos_api.py` checks the
envelope holds its shape. `test_fos_query_types.py` checks the router and
the derived fields in isolation. This one asks the question neither of them
does: given a real case and a question typed the way a field officer types
it, is the response one a screen can render and act on?

THE THREE FAILURES IT EXISTS TO CATCH.

A PARAPHRASE THAT FALLS THROUGH. "Show me what I still have to collect"
matched no case pattern, fell to UNKNOWN, and was answered confidently from
the FOS handbook with a paragraph about verification states. A confident
irrelevant answer is worse than none: the officer reads it, learns nothing,
and stops trusting the copilot. Every phrasing below is one a person would
actually type.

A BOUNDARY THAT LEAKS. A knowledge answer or a routed refusal that arrives
with the case header attached has disclosed the case on its way to not
answering about it.

AN AFFORDANCE THAT LIES. An enabled "Upload document" button on a case the
route will refuse teaches an officer to distrust the whole panel, so the
enabled flags are checked against what the API actually does.
"""

from __future__ import annotations

import pytest

from tests.integration.test_fos_api import (  # noqa: F401
    _store, ask, client, fos_token, verify,
)


@pytest.fixture
def case(client):
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Contract Test", "mobile": "9876500321",
                      "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def query(client, case, message):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, "CUSTOM_QUERY", message)
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# A. THE WAY PEOPLE ACTUALLY TYPE
# ==========================================================================

#: Phrasings of "what is still outstanding", none of them the canonical one.
#:
#: The fourth of these is the one that regressed: it contains neither the
#: word "document" nor the word "missing", which is what every pattern was
#: keyed on.
OUTSTANDING = [
    "What documents are still needed?",
    "which docs are left",
    "Which documents are missing?",
    "Show me what I still have to collect",
    "what do I need to collect from the customer",
    "what else is still needed",
    "anything left to gather?",
]


@pytest.mark.parametrize("message", OUTSTANDING)
def test_asking_what_is_outstanding_reaches_the_checklist(client, case,
                                                          message):
    body = query(client, case, message)

    assert body["query_type"] == "POLICY_REQUIREMENT", (
        f"{message!r} was routed to {body['query_type']} "
        f"(intent {body['intent']})"
    )
    assert body["response_source"] == "STRUCTURED"
    assert body.get("checklist")


@pytest.mark.parametrize("message", OUTSTANDING)
def test_no_outstanding_question_is_answered_from_the_handbook(client, case,
                                                               message):
    """
    THE REGRESSION THIS PINS. A case question that falls through to
    retrieval comes back with a confident paragraph about the FOS process
    instead of this case's missing documents.
    """
    body = query(client, case, message)

    assert body["response_source"] != "KNOWLEDGE"
    assert body["intent"] != "FOS_KNOWLEDGE"


VERIFICATION = [
    "Which documents have been verified?",
    "has the PAN been verified",
    "are all documents verified",
    "show me all document issues",
]


@pytest.mark.parametrize("message", VERIFICATION)
def test_asking_about_verification_reaches_document_status(client, case,
                                                           message):
    body = query(client, case, message)
    assert body["query_type"] == "DOCUMENT_STATUS"


KNOWLEDGE = [
    "What can be used as address proof?",
    "What does REVIEW mean?",
]


@pytest.mark.parametrize("message", KNOWLEDGE)
def test_a_question_about_the_rules_is_answered_from_the_handbook(client, case,
                                                                  message):
    body = query(client, case, message)
    assert body["query_type"] == "PROCESS_KNOWLEDGE"


DOWNSTREAM = [
    "Is this applicant creditworthy?",
    "what is the CIBIL score",
    "Is KYC clear?",
    "Analyze the bank transactions.",
    "should we approve this loan",
    "are there any fraud concerns",
]


@pytest.mark.parametrize("message", DOWNSTREAM)
def test_a_downstream_question_is_routed_not_answered(client, case, message):
    body = query(client, case, message)

    assert body["query_type"] == "DOWNSTREAM"
    assert body["response_source"] == "ROUTED"
    assert body["route_to"]


# ==========================================================================
# B. THE BOUNDARY HOLDS
# ==========================================================================


@pytest.mark.parametrize("message", DOWNSTREAM + KNOWLEDGE)
def test_an_answer_that_did_not_read_the_case_carries_no_case_state(
        client, case, message):
    """
    A header stating this case's document counts, on an answer that never
    looked at the case, is the disclosure the routing boundary exists to
    prevent.
    """
    body = query(client, case, message)

    assert body.get("case_state") is None
    assert not body.get("document_highlights")
    assert not body.get("available_actions")


@pytest.mark.parametrize("message", DOWNSTREAM)
def test_a_routed_question_carries_no_case_data_at_all(client, case, message):
    body = query(client, case, message)

    for field in ("applicant", "application", "documents", "checklist",
                  "pending_items", "readiness"):
        assert not body.get(field), f"{field} went out with a routed refusal"


def test_a_case_question_does_carry_the_state(client, case):
    body = query(client, case, "What documents are still needed?")

    assert body["case_state"]
    assert body["case_state"]["documents_required"] > 0


# ==========================================================================
# C. THE HEADER AGREES WITH THE DETAIL
# ==========================================================================


def test_the_counts_match_the_checklist_in_the_same_response(client, case):
    """
    Counted from the response's own checklist, so a screen showing the
    header and the rows below it cannot show two different answers.
    """
    body = query(client, case, "Show me the document checklist.")

    required = [e for e in body["checklist"] if e["mandatory"]]
    satisfied = [e for e in required if e["fulfilment"] == "SATISFIED"]

    assert body["case_state"]["documents_required"] == len(required)
    assert body["case_state"]["documents_satisfied"] == len(satisfied)


def test_progress_moves_as_documents_are_verified(client, case, _store):
    applicant_id, case_id = case
    before = query(client, case, "Show me the document checklist.")

    verify(_store, case_id, applicant_id, "PAN", "PASS")
    after = query(client, case, "Show me the document checklist.")

    assert (after["case_state"]["collection_progress"]
            > before["case_state"]["collection_progress"])
    assert (after["case_state"]["documents_satisfied"]
            == before["case_state"]["documents_satisfied"] + 1)


# ==========================================================================
# D. THE AFFORDANCES ARE TRUE
# ==========================================================================


def test_upload_is_offered_while_documents_are_outstanding(client, case):
    body = query(client, case, "Show me the document checklist.")
    actions = {a["action"]: a for a in body["available_actions"]}

    assert actions["UPLOAD_DOCUMENT"]["enabled"] is True


def test_reupload_turns_on_only_once_something_needs_it(client, case, _store):
    applicant_id, case_id = case

    before = query(client, case, "Show me the document checklist.")
    assert not next(a for a in before["available_actions"]
                    if a["action"] == "MARK_FOR_REUPLOAD")["enabled"]

    verify(_store, case_id, applicant_id, "PAN", "REVIEW",
           ["DOCUMENT_UNREADABLE"])
    after = query(client, case, "Show me the document checklist.")

    assert next(a for a in after["available_actions"]
                if a["action"] == "MARK_FOR_REUPLOAD")["enabled"]


def test_a_disabled_action_says_why(client, case):
    body = query(client, case, "Show me the document checklist.")

    for action in body["available_actions"]:
        if not action["enabled"]:
            assert action.get("disabled_reason"), (
                f"{action['action']} is disabled with no reason")


def test_every_offered_api_action_is_one_the_endpoint_accepts(client, case):
    """
    THE AFFORDANCE HAS TO BE REAL. An action name the copilot endpoint does
    not recognise is a button that 422s.
    """
    from app.api.routes.fos_api import FosAction

    body = query(client, case, "Show me the document checklist.")
    known = {a.value for a in FosAction}

    for action in body["available_actions"]:
        if action.get("external"):
            continue
        # An action that is not a dropdown value has to say how it IS
        # called. MARK_FOR_REUPLOAD goes through CUSTOM_QUERY and the
        # write-confirmation step; offering it under `action` alone would
        # have been a button that 422s.
        invoke = action.get("invoke")
        if invoke:
            assert invoke["action"] in known, (
                f"{action['action']} says to call {invoke['action']}, "
                f"which is not a FosAction")
            assert invoke.get("message_template")
            continue
        assert action["action"] in known, (
            f"{action['action']} is offered, is not a FosAction, and says "
            f"nothing about how to invoke it")


# ==========================================================================
# E. SUGGESTIONS GO SOMEWHERE
# ==========================================================================


def test_a_suggested_question_is_one_the_copilot_can_answer(client, case):
    """
    The test of a suggestion is that clicking it returns something. Each
    one is put back through the endpoint.
    """
    body = query(client, case, "Show me the document checklist.")
    assert body["suggested_questions"]

    for suggestion in body["suggested_questions"]:
        answered = query(client, case, suggestion)
        assert answered["query_type"] != "CLARIFICATION", (
            f"the copilot suggested {suggestion!r} and then could not "
            f"understand it"
        )
        assert answered["answer"].strip()


def test_suggestions_respond_to_what_is_wrong_with_the_case(client, case,
                                                            _store):
    applicant_id, case_id = case
    before = query(client, case, "Show me the document checklist.")

    verify(_store, case_id, applicant_id, "PAN", "FAIL",
           ["DOCUMENT_TYPE_MISMATCH"])
    after = query(client, case, "Show me the document checklist.")

    assert before["suggested_questions"] != after["suggested_questions"]
    assert any("rejected" in s.lower() for s in after["suggested_questions"])


# ==========================================================================
# F. THE COPILOT ASKS RATHER THAN GUESSING
# ==========================================================================

UNRECOGNISED = [
    "wibble wobble zort",
    "asdfghjkl",
    "tell me about the weather",
]


@pytest.mark.parametrize("message", UNRECOGNISED)
def test_an_unrecognised_question_produces_a_clarification(client, case,
                                                           message):
    body = query(client, case, message)

    assert body["query_type"] == "CLARIFICATION"
    assert body["clarification_required"]
    assert body["clarification_required"]["options"]
    assert body["suggested_questions"]


@pytest.mark.parametrize("message", UNRECOGNISED)
def test_an_unrecognised_question_still_reports_that_it_failed(client, case,
                                                               message):
    """
    The clarification is friendlier, not quieter. A caller distinguishing
    "answered" from "not answered" reads `errors`, and dropping the error
    to make the response look better would make an unanswered question
    look answered.
    """
    body = query(client, case, message)

    assert body["errors"]
    assert body["errors"][0]["code"] == "UNSUPPORTED_REQUEST"


def test_the_clarification_options_are_questions_the_copilot_answers(client,
                                                                     case):
    body = query(client, case, "wibble wobble zort")

    for option in body["clarification_required"]["options"]:
        answered = query(client, case, option)
        assert answered["query_type"] != "CLARIFICATION", (
            f"the copilot offered {option!r} and could not understand it"
        )


def test_a_recognised_question_carries_no_clarification(client, case):
    """A null here is a claim that the request WAS understood."""
    body = query(client, case, "What documents are still needed?")
    assert body.get("clarification_required") is None


# ==========================================================================
# G. DOCUMENT HIGHLIGHTS
# ==========================================================================


def test_a_document_in_review_is_highlighted_with_its_reason(client, case,
                                                             _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "BANK_STATEMENT", "REVIEW",
           ["VERIFICATION_TIMEOUT"])

    body = query(client, case, "Which documents have been verified?")
    card = next(c for c in body["document_highlights"]
                if c["document_type"] == "BANK_STATEMENT")

    assert card["severity"] == "ATTENTION"
    assert card["primary_reason_code"] == "VERIFICATION_TIMEOUT"
    assert card["needs_attention"] is True


def test_a_verified_document_needs_no_attention(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")

    body = query(client, case, "Which documents have been verified?")
    card = next(c for c in body["document_highlights"]
                if c["document_type"] == "PAN")

    assert card["severity"] == "OK"
    assert card["needs_attention"] is False
    assert card["primary_reason_code"] is None


# ==========================================================================
# H. A DROPDOWN ACTION AND THE SAME QUESTION TYPED OUT AGREE
# ==========================================================================


@pytest.mark.parametrize("action,message", [
    ("GET_DOCUMENT_CHECKLIST", "Show me the document checklist."),
    ("GET_VERIFICATION_STATUS", "Show me all document issues."),
    ("CHECK_CPA_READINESS", "Is this ready for CPA?"),
])
def test_the_dropdown_and_the_typed_question_reach_the_same_kind(
        client, case, action, message):
    applicant_id, case_id = case
    structured = ask(client, applicant_id, case_id, action).json()
    typed = query(client, case, message)

    assert structured["query_type"] == typed["query_type"]
    assert structured["intent"] == typed["intent"]


# ==========================================================================
# I. A CONVERSATION, THROUGH THE API
# ==========================================================================


def test_a_bare_why_is_answered_when_the_context_comes_back(client, case):
    """
    The whole loop, as a client runs it: ask, keep the context, ask "why?".

    THE STATE LIVES IN THE CALLER. Nothing between these two requests is
    remembered by the service, which is what makes it safe to scale and
    impossible for one officer's session to reach another's.
    """
    first = query(client, case, "Which documents are missing?")
    assert first["context"], "no context was handed back to follow up with"

    applicant_id, case_id = case
    second = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY", "message": "why?",
        "context": first["context"],
    }).json()

    assert second["query_type"] != "CLARIFICATION"
    assert second["followed_up"]
    assert second["followed_up"]["original_message"] == "why?"
    assert second["followed_up"]["interpreted_as"] != "why?"


def test_the_same_bare_why_without_context_is_not_guessed_at(client, case):
    """
    The control. Without the context there is nothing to resolve against,
    and the service must not invent a subject.
    """
    applicant_id, case_id = case
    body = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY", "message": "why?",
    }).json()

    assert body["followed_up"] is None


def test_a_forged_context_cannot_reach_a_downstream_answer(client, case):
    """
    The context is caller-supplied, so it is untrusted. A context claiming
    the last answer was about credit must not produce a credit answer --
    the rewrite only ever makes a message, and the message is classified
    and routed exactly as a typed one is.
    """
    applicant_id, case_id = case
    body = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY", "message": "why?",
        "context": {"last_query_type": "CREDIT_DECISION",
                    "last_intent": "CREDIT_SCORE",
                    "last_slot": "CIBIL_SCORE"},
    }).json()

    # The service does not answer it, and does not echo the forged subject
    # back. An unrecognised slot is not resolved against at all, so the
    # bare "why?" reaches the clarification that asks what was meant.
    assert body["followed_up"] is None
    assert body["query_type"] == "CLARIFICATION", (
        "an unresolved follow-up must ask what was meant, not answer "
        "something else"
    )

    import json

    assert "cibil" not in json.dumps(body).lower()
    for field in ("applicant", "documents", "checklist"):
        assert not body.get(field)


@pytest.mark.parametrize("context", [
    {"last_slot": None}, {"last_slot": 12345}, {"nonsense": True},
    {"last_query_type": ["not", "a", "string"]},
])
def test_a_malformed_context_does_not_fail_the_request(client, case, context):
    applicant_id, case_id = case
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY", "message": "What documents are missing?",
        "context": context,
    })

    assert response.status_code == 200
    assert response.json()["checklist"]


def test_a_dropdown_action_ignores_the_context(client, case):
    """
    A button is unambiguous by construction. Resolving one against a stale
    context would change what the button does.
    """
    applicant_id, case_id = case
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_DOCUMENT_CHECKLIST",
        "context": {"last_query_type": "DOCUMENT_STATUS",
                    "last_slot": "PAN"},
    })
    body = response.json()

    assert body["followed_up"] is None
    assert body["intent"] == "DOCUMENTS_REQUIRED"


def test_a_three_turn_conversation_stays_on_the_case(client, case):
    """Each turn hands back the context the next one needs."""
    applicant_id, case_id = case
    context = None
    seen = []

    for message in ("Which documents are missing?", "why?", "what now?"):
        payload = {"applicant_id": applicant_id, "case_id": case_id,
                   "action": "CUSTOM_QUERY", "message": message}
        if context:
            payload["context"] = context
        body = client.post("/api/v1/fos/copilot", json=payload).json()
        seen.append(body["query_type"])
        context = body["context"]
        assert body["answer"].strip()

    assert "CLARIFICATION" not in seen
