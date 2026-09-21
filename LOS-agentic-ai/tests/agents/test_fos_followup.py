"""
Following a conversation, without holding one.

WHAT IS BEING TRADED OFF. A copilot that cannot answer "why?" is a search
box; a copilot that holds session state is one more thing to scale, expire
and keep from leaking between officers. The resolution here is that the
CALLER carries the context and the service resolves against what it is
handed.

THAT MAKES THE CONTEXT UNTRUSTED, and most of this file is about the
consequences. A forged or stale context must never widen what a caller can
see, never select an intent directly, and never turn a question about
documents into one about something else. The rewrite produces a MESSAGE and
nothing else; the message then goes through exactly the classifier a typed
question does.

THE OTHER HALF is restraint. A phrase that might be a standalone question
must not be rewritten, because answering a question the officer did not ask
-- confidently, with no sign anything was substituted -- is worse than
failing to follow up.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import followup
from app.agents.applicant.followup import Context

CHECKLIST_CONTEXT = Context(
    last_query_type="POLICY_REQUIREMENT",
    last_intent="DOCUMENTS_MISSING",
    last_slot="ADDRESS_PROOF",
)


# ==========================================================================
# A. A BARE FOLLOW-UP BECOMES A WHOLE QUESTION
# ==========================================================================


@pytest.mark.parametrize("message", [
    "why?", "Why?", "why", "why is that?", "why is this",
    "but why?", "how come?", "for what reason?",
])
def test_a_bare_why_is_resolved_against_the_last_slot(message):
    resolution = followup.resolve(message, CHECKLIST_CONTEXT)

    assert resolution.followed_up
    assert "address proof" in resolution.message.lower()
    assert resolution.rewritten_from == message


def test_a_resolved_follow_up_says_what_it_was_taken_to_mean():
    """
    An officer who is misunderstood has to be able to see it. A
    substitution that happens silently produces an answer that does not
    match the question and nothing explaining the gap.
    """
    published = followup.resolve("why?", CHECKLIST_CONTEXT).public()

    assert published["original_message"] == "why?"
    assert published["interpreted_as"]
    assert published["reason"]


def test_a_message_that_stands_on_its_own_is_untouched():
    resolution = followup.resolve(
        "Which documents are missing?", CHECKLIST_CONTEXT)

    assert not resolution.followed_up
    assert resolution.message == "Which documents are missing?"
    assert resolution.public() is None


@pytest.mark.parametrize("message", [
    "what now?", "now what", "what next?", "then what?",
])
def test_a_bare_next_step_resolves_without_needing_a_slot(message):
    resolution = followup.resolve(message, Context(last_intent="READINESS"))

    assert resolution.followed_up
    assert "next" in resolution.message.lower()


def test_tell_me_more_follows_the_last_kind_of_answer():
    policy = followup.resolve("tell me more", CHECKLIST_CONTEXT)
    documents = followup.resolve(
        "tell me more", Context(last_query_type="DOCUMENT_STATUS"))

    assert policy.followed_up and documents.followed_up
    assert policy.message != documents.message


def test_a_new_subject_keeps_the_previous_question():
    resolution = followup.resolve(
        "and the passport?",
        Context(last_query_type="DOCUMENT_STATUS",
                last_intent="DOCUMENT_VERIFICATION", last_slot="PAN"))

    assert resolution.followed_up
    assert "passport" in resolution.message.lower()


def test_acronyms_survive_the_rewrite():
    """"Why is pan required" reads as a typo about cookware."""
    resolution = followup.resolve(
        "why?", Context(last_query_type="POLICY_REQUIREMENT",
                        last_intent="DOCUMENTS_MISSING", last_slot="PAN"))

    assert "PAN" in resolution.message
    assert "pan " not in resolution.message


# ==========================================================================
# B. WHEN IT MUST NOT GUESS
# ==========================================================================


def test_no_context_means_no_rewrite():
    for context in (None, Context()):
        resolution = followup.resolve("why?", context)
        assert not resolution.followed_up
        assert resolution.message == "why?"


def test_a_why_with_nothing_to_resolve_against_is_left_alone():
    """
    A context that says only that the last answer was routed downstream
    has nothing to say about what "why?" refers to.
    """
    resolution = followup.resolve("why?", Context(last_query_type="DOWNSTREAM"))
    assert not resolution.followed_up


@pytest.mark.parametrize("message", [
    "why is this case not ready?",
    "why was the PAN rejected?",
    "why do I need a bank statement for a personal loan?",
    "what documents are missing?",
])
def test_a_question_that_could_stand_alone_is_never_rewritten(message):
    """
    THE RESTRAINT THIS PINS. Rewriting a self-contained question against a
    stale context answers something nobody asked, confidently, with an
    explanation that looks like it fits.
    """
    resolution = followup.resolve(message, CHECKLIST_CONTEXT)
    assert not resolution.followed_up
    assert resolution.message == message


def test_an_unknown_subject_is_not_turned_into_a_document_question():
    """"And the weather?" must not become a document status question."""
    resolution = followup.resolve(
        "and the weather?",
        Context(last_intent="DOCUMENT_VERIFICATION", last_slot="PAN"))

    assert not resolution.followed_up


def test_an_empty_message_is_not_rewritten():
    assert not followup.resolve("", CHECKLIST_CONTEXT).followed_up
    assert not followup.resolve("   ", CHECKLIST_CONTEXT).followed_up


# ==========================================================================
# C. THE CONTEXT IS UNTRUSTED INPUT
# ==========================================================================


@pytest.mark.parametrize("payload", [
    None, {}, [], "a string", 42,
    {"last_slot": None}, {"last_slot": 12345},
    {"last_query_type": {"nested": "object"}},
])
def test_a_malformed_context_degrades_to_no_context(payload):
    """
    Never an error. A broken context must not fail a question the officer
    could otherwise have had answered.
    """
    context = Context.from_payload(payload)
    assert isinstance(context, Context)
    resolution = followup.resolve("why?", context)
    assert isinstance(resolution.message, str)


def test_an_oversized_context_field_is_truncated():
    context = Context.from_payload({"last_slot": "A" * 10_000})
    assert len(context.last_slot or "") <= 64


def test_a_slot_name_is_normalised_however_the_client_writes_it():
    """A client echoing what it rendered must behave like one echoing the id."""
    written = Context.from_payload({"last_slot": "address proof"})
    identifier = Context.from_payload({"last_slot": "ADDRESS_PROOF"})

    assert written.last_slot == identifier.last_slot == "ADDRESS_PROOF"


def test_the_rewrite_only_ever_produces_a_message():
    """
    THE SECURITY BOUNDARY, stated as a test. The resolution carries a
    string and provenance -- no intent, no case id, no tool, no scope. A
    forged context cannot reach anything the classifier would not.
    """
    resolution = followup.resolve("why?", CHECKLIST_CONTEXT)
    fields = set(vars(resolution))

    assert fields == {"message", "rewritten_from", "reason"}
    assert isinstance(resolution.message, str)


def test_a_context_naming_a_downstream_subject_does_not_route_there():
    """
    A forged context claiming the last answer was about credit must not
    produce a credit question -- and if it somehow did, the classifier
    would route it rather than answer it.
    """
    resolution = followup.resolve(
        "and the credit score?",
        Context(last_intent="DOCUMENT_VERIFICATION", last_slot="PAN"))

    assert not resolution.followed_up


# ==========================================================================
# D. THE CONTEXT A RESPONSE HANDS BACK
# ==========================================================================


def test_the_context_names_the_most_blocking_slot():
    """
    What the officer is most likely to ask about next is what is most
    wrong, not what happens to be first in the list.
    """
    envelope = {
        "query_type": "POLICY_REQUIREMENT",
        "intent": "DOCUMENTS_MISSING",
        "checklist": [
            {"slot": "PAN", "mandatory": True, "fulfilment": "MISSING"},
            {"slot": "BANK_STATEMENT", "mandatory": True,
             "fulfilment": "FAILED"},
        ],
    }

    context = followup.context_from_response(envelope)
    assert context["last_slot"] == "BANK_STATEMENT"


def test_a_review_outranks_a_missing_slot():
    envelope = {"checklist": [
        {"slot": "PAN", "mandatory": True, "fulfilment": "MISSING"},
        {"slot": "BANK_STATEMENT", "mandatory": True,
         "fulfilment": "UNDER_REVIEW"},
    ]}
    assert (followup.context_from_response(envelope)["last_slot"]
            == "BANK_STATEMENT")


def test_a_satisfied_slot_is_never_the_follow_up_subject():
    envelope = {"checklist": [
        {"slot": "PAN", "mandatory": True, "fulfilment": "SATISFIED"},
    ]}
    assert followup.context_from_response(envelope)["last_slot"] is None


def test_an_optional_slot_is_not_the_follow_up_subject():
    envelope = {"checklist": [
        {"slot": "PHOTO", "mandatory": False, "fulfilment": "MISSING"},
    ]}
    assert followup.context_from_response(envelope)["last_slot"] is None


def test_the_context_round_trips():
    """What a response hands back must be readable as a context."""
    envelope = {
        "query_type": "POLICY_REQUIREMENT", "intent": "DOCUMENTS_MISSING",
        "checklist": [{"slot": "ADDRESS_PROOF", "mandatory": True,
                       "fulfilment": "MISSING"}],
    }

    context = Context.from_payload(followup.context_from_response(envelope))

    assert context.last_slot == "ADDRESS_PROOF"
    assert context.last_query_type == "POLICY_REQUIREMENT"
    assert followup.resolve("why?", context).followed_up


def test_a_response_with_no_checklist_still_produces_a_context():
    context = followup.context_from_response(
        {"query_type": "CASE_FACT", "intent": "APPLICANT_DETAILS"})

    assert context["last_query_type"] == "CASE_FACT"
    assert context["last_slot"] is None
