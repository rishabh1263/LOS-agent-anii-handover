"""
What kind of question this is, and therefore where the answer comes from.

FOUR CATEGORIES, AND THEY DECIDE THE WHOLE REQUEST:

    CASE_ONLY       the store answers it. No retrieval, no model needed.
    KNOWLEDGE_ONLY  the FOS handbook answers it. No case data is read.
    MIXED           both, combined. Case facts first, policy second.
    DOWNSTREAM      neither. FOS does not own it, so it is routed.

The category is what makes the latency targets reachable: a CASE_ONLY
question must not pay for retrieval it does not use, and a KNOWLEDGE_ONLY
question must not read records it has no business reading.

RESPONSE SOURCE IS NOT THE SAME QUESTION. The category says what was
consulted; `response_source` says what the answer was actually built from.
They usually agree, and where they do not the source is the honest one: a
MIXED question whose retrieval came back unconfident produces a STRUCTURED
answer, because only the store contributed.
"""

from __future__ import annotations

from enum import Enum

from app.agents.applicant.intents import Intent


class QueryCategory(str, Enum):
    CASE_ONLY = "CASE_ONLY"
    KNOWLEDGE_ONLY = "KNOWLEDGE_ONLY"
    MIXED = "MIXED"
    DOWNSTREAM = "DOWNSTREAM"
    UNSUPPORTED = "UNSUPPORTED"


class ResponseSource(str, Enum):
    """
    Where the answer came from. Published on every response.

    `deterministic` used to be returned for everything that was not a model
    call, which made a knowledge answer and a checklist lookup
    indistinguishable -- and a caller auditing which answers touched the
    handbook had no way to tell.
    """

    #: Computed from stored records. No retrieval, no model.
    STRUCTURED = "STRUCTURED"
    #: Retrieved from the stage knowledge base.
    KNOWLEDGE = "KNOWLEDGE"
    #: Stored records AND retrieved knowledge, combined.
    MIXED = "MIXED"
    #: A model phrased it. The FACTS still came from one of the above --
    #: this says who wrote the sentence, never who decided the content.
    LLM = "LLM"
    #: Routed downstream. No answer was produced at all.
    ROUTED = "ROUTED"


def category_for(intent: Intent) -> QueryCategory:
    """The category an intent belongs to."""
    if intent is Intent.OUT_OF_SCOPE:
        return QueryCategory.DOWNSTREAM
    if intent is Intent.FOS_KNOWLEDGE:
        return QueryCategory.KNOWLEDGE_ONLY
    if intent is Intent.MIXED:
        return QueryCategory.MIXED
    if intent is Intent.UNKNOWN:
        return QueryCategory.UNSUPPORTED
    return QueryCategory.CASE_ONLY


#: Which response fields each intent actually needs.
#:
#: WHY THIS EXISTS. Every action used to return the whole case -- applicant,
#: application, every document, the full checklist, every pending item -- for
#: a question like "what is the next action?". That is a large payload a
#: frontend has to ignore, and it puts extracted applicant details into a
#: chat response that had no reason to carry them.
#:
#: An intent lists what its answer is ABOUT. Everything else is dropped.
#: `None` means "everything", which is what GET_CASE_360 is for.
RELEVANT_FIELDS: dict[Intent, tuple[str, ...]] = {
    Intent.APPLICANT_DETAILS: ("applicant",),
    Intent.APPLICANT_MISSING_INFO: ("applicant", "pending_items"),
    Intent.APPLICATION_STATUS: ("application", "stage"),
    Intent.APPLICATION_STAGE: ("application", "stage", "checklist",
                               "required_documents"),
    Intent.DOCUMENTS_UPLOADED: ("documents",),
    Intent.DOCUMENTS_REQUIRED: ("checklist", "required_documents"),
    # The explanation IS the policy block, so the answer would be
    # unsupportable without it.
    Intent.POLICY_EXPLANATION: ("checklist", "required_documents"),
    Intent.DOCUMENTS_MISSING: ("checklist", "required_documents",
                               "pending_items"),
    Intent.DOCUMENTS_PENDING: ("pending_items",),
    Intent.DOCUMENT_VERIFICATION: ("documents", "verification"),
    Intent.PENDING_ITEMS: ("pending_items", "next_action"),
    Intent.NEXT_ACTION: ("next_action", "pending_items"),
    Intent.READINESS: ("readiness", "next_action"),
    Intent.COMPLETENESS: ("readiness", "pending_items", "next_action"),
    # A 360 view is the one that means "give me everything".
    Intent.FULL_SUMMARY: None,

    # A knowledge answer carries NO case fields. It did not read a record and
    # attaching one would imply the answer came from it.
    Intent.FOS_KNOWLEDGE: (),

    # A routed question carries nothing either -- and this one matters. A
    # credit question that comes back with the applicant record and every
    # document has disclosed the case on its way to refusing to discuss it.
    Intent.OUT_OF_SCOPE: (),
    Intent.UNKNOWN: (),

    # MIXED is resolved from the CASE intent underneath it, not from this
    # table: "why is this not ready and what should I collect?" needs what
    # READINESS needs. See `fields_for`.
    Intent.MIXED: (),
}

#: Fields that are part of the envelope regardless of intent. Identity and
#: provenance, not case content.
ALWAYS_KEPT = frozenset({
    "request_id", "applicant_id", "case_id", "action", "intent", "answer",
    "category", "knowledge", "route_to", "response_source", "processing_ms",
    "errors", "actions",
    # Conversation plumbing and UI affordances, not case data. Pruning
    # these would remove the fields a chat client needs MOST -- a typed
    # question is exactly the case where the follow-up context and the
    # suggestions matter.
    "query_type", "case_state", "suggested_questions", "available_actions",
    "document_highlights", "clarification_required", "followed_up",
    "context",
})


def relevant_fields(
    intent: Intent,
    base_intent: Intent | None = None,
) -> tuple[str, ...] | None:
    """
    The case fields this answer is about, or None for all of them.

    `base_intent` resolves MIXED: its case half is the same question asked
    alone, so it keeps the same fields.

    An intent nobody has mapped returns None rather than an empty tuple.
    Withholding data because a mapping was not updated is the worse failure
    of the two, so an unmapped intent sends everything and is caught by a
    test rather than by a caller missing a field.
    """
    if intent is Intent.MIXED and base_intent is not None:
        return relevant_fields(base_intent)
    if intent not in RELEVANT_FIELDS:
        return None
    return RELEVANT_FIELDS[intent]


#: Case fields that may be omitted. Everything else is envelope.
PRUNABLE = (
    "applicant", "application", "stage", "documents", "checklist",
    "required_documents", "pending_items", "verification", "kyc",
    "next_action", "readiness", "policy",
)

#: Fields that follow another field rather than standing alone.
#:
#: `policy` explains where the checklist came from. Keeping it on an answer
#: with no checklist is noise; dropping it from an answer WITH one leaves a
#: requirement the caller cannot trace to a rule, which is the thing the
#: policy block exists to prevent.
FOLLOWS = {"policy": "checklist"}


def prune(
    envelope: dict,
    intent: Intent,
    *,
    base_intent: Intent | None = None,
    enabled: bool = True,
) -> dict:
    """
    OMIT the case fields this answer is not about.

    Removed, not nulled. "What documents are pending?" used to come back with
    the applicant record, three full document rows, the whole checklist and
    eight null fields -- a payload a chat client throws away, carrying
    extracted applicant details a chat reply had no reason to hold.

    Only a typed question is pruned. A dropdown action is a SCREEN and keeps
    the full envelope, so a frontend still binds one model for the views it
    renders.

    A field that is present but empty keeps its meaning: `pending_items: []`
    on a pending-items answer means nothing is pending. That distinction is
    the reason this omits rather than empties.
    """
    if not enabled:
        return envelope

    keep = relevant_fields(intent, base_intent)
    if keep is None:
        return envelope

    kept = set(keep) | ALWAYS_KEPT
    kept |= {follower for follower, leader in FOLLOWS.items() if leader in kept}
    pruned = {k: v for k, v in envelope.items()
              if k not in PRUNABLE or k in kept}

    # `knowledge` and `route_to` are envelope, but carrying them as null on
    # an answer that used neither is the same noise this exists to remove.
    for optional in ("knowledge", "route_to"):
        if pruned.get(optional) in (None, {}, []):
            pruned.pop(optional, None)

    return pruned


__all__ = [
    "ALWAYS_KEPT", "FOLLOWS", "QueryCategory", "RELEVANT_FIELDS",
    "ResponseSource", "category_for", "prune", "relevant_fields",
]
