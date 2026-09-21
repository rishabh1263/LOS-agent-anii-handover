"""
What KIND of request this is. Eight of them, and they are the router.

WHY A SECOND CLASSIFICATION EXISTS ALONGSIDE `QueryCategory`. The two answer
different questions and conflating them lost information a frontend needs.

    QueryCategory   what the service had to CONSULT to answer
                    (the store, the handbook, both, or nobody)
    QueryType       what the caller was ASKING FOR

A checklist question and a "which documents are verified" question are both
CASE_ONLY -- the store answers both, neither touches retrieval -- and a UI
renders them completely differently: one is a collection task with a policy
behind it, the other is a verification panel. Category could not tell them
apart because it was never meant to.

THE EIGHT, and what each one commits the service to:

    CASE_FACT            answer from stored records
    DOCUMENT_STATUS      answer from stored records, about documents
    POLICY_REQUIREMENT   answer from the policy engine, WITH the rule
    PROCESS_KNOWLEDGE    answer from the FOS knowledge base, no case read
    MIXED                case facts first, then the rule behind them
    ACTION_REQUEST       a write. Needs a scope and a confirmation.
    DOWNSTREAM           not FOS's to answer. Routed, and no case data goes
                         out with it.
    CLARIFICATION        the service will not guess. It asks.

CLARIFICATION IS A REAL ANSWER, not a failure code. An unrecognised question
used to fall through to the knowledge base, which answered a
creditworthiness question out of the FOS handbook. Asking what the officer
meant is slower and correct; guessing is faster and occasionally invents
lender policy.
"""

from __future__ import annotations

from enum import Enum

from app.agents.applicant.intents import WRITE_INTENTS, Intent


class QueryType(str, Enum):
    """The eight kinds of request the copilot recognises."""

    CASE_FACT = "CASE_FACT"
    DOCUMENT_STATUS = "DOCUMENT_STATUS"
    POLICY_REQUIREMENT = "POLICY_REQUIREMENT"
    PROCESS_KNOWLEDGE = "PROCESS_KNOWLEDGE"
    MIXED = "MIXED"
    ACTION_REQUEST = "ACTION_REQUEST"
    DOWNSTREAM = "DOWNSTREAM"
    CLARIFICATION = "CLARIFICATION"


#: Intents that are about DOCUMENTS as objects -- what exists, what state it
#: is in. Not about what is still needed, which is a policy question.
_DOCUMENT_STATUS = frozenset({
    Intent.DOCUMENTS_UPLOADED,
    Intent.DOCUMENT_VERIFICATION,
})

#: Intents whose answer is "these are needed, and here is the rule". These
#: are the ones that must carry the policy block, because an officer reading
#: them is about to ask a customer for something.
_POLICY_REQUIREMENT = frozenset({
    Intent.DOCUMENTS_REQUIRED,
    Intent.DOCUMENTS_MISSING,
    Intent.DOCUMENTS_PENDING,
    Intent.POLICY_EXPLANATION,
})

#: Intents answered purely from stored records and derived state.
_CASE_FACT = frozenset({
    Intent.APPLICANT_DETAILS,
    Intent.APPLICANT_MISSING_INFO,
    Intent.APPLICATION_STATUS,
    Intent.APPLICATION_STAGE,
    Intent.PENDING_ITEMS,
    Intent.NEXT_ACTION,
    Intent.READINESS,
    Intent.COMPLETENESS,
    Intent.FULL_SUMMARY,
})


def type_for(intent: Intent) -> QueryType:
    """
    The kind of request an intent represents.

    TOTAL BY CONSTRUCTION. Every member of Intent lands somewhere, and an
    intent added later without being mapped here falls to CLARIFICATION --
    the copilot asks rather than guessing, which is the safe direction for
    an unmapped case to fail in. A test asserts the mapping is complete so
    the fallback stays a safety net rather than a silent default.
    """
    if intent in WRITE_INTENTS:
        return QueryType.ACTION_REQUEST
    if intent is Intent.OUT_OF_SCOPE:
        return QueryType.DOWNSTREAM
    if intent is Intent.FOS_KNOWLEDGE:
        return QueryType.PROCESS_KNOWLEDGE
    if intent is Intent.MIXED:
        return QueryType.MIXED
    if intent in _DOCUMENT_STATUS:
        return QueryType.DOCUMENT_STATUS
    if intent in _POLICY_REQUIREMENT:
        return QueryType.POLICY_REQUIREMENT
    if intent in _CASE_FACT:
        return QueryType.CASE_FACT
    return QueryType.CLARIFICATION


#: Which kinds read case records at all.
#:
#: USED AS A BOUNDARY, not as documentation. A PROCESS_KNOWLEDGE answer that
#: carried the applicant record would be a policy answer that looks like a
#: statement about a person, and a DOWNSTREAM refusal that carried it would
#: have disclosed the case on its way to declining to discuss it.
READS_CASE = frozenset({
    QueryType.CASE_FACT,
    QueryType.DOCUMENT_STATUS,
    QueryType.POLICY_REQUIREMENT,
    QueryType.MIXED,
    QueryType.ACTION_REQUEST,
})

#: Which kinds must carry the policy provenance when they carry a checklist.
CARRIES_POLICY = frozenset({
    QueryType.POLICY_REQUIREMENT,
    QueryType.MIXED,
})


# ==========================================================================
# ASKING INSTEAD OF GUESSING
# ==========================================================================

#: What the copilot offers when it did not understand. Phrased as things a
#: FOS actually asks, not as a menu of intent names.
_OFFERS = (
    "What documents are still needed?",
    "Which documents have been verified?",
    "Is this case ready to hand over?",
    "What is pending on this case?",
)


def clarification_for(message: str, *, has_case: bool) -> dict[str, object]:
    """
    The question to ask back, when the service will not guess.

    WHY IT OFFERS OPTIONS rather than saying "I did not understand". An
    unrecognised question is usually a recognised one phrased unusually, and
    a list of what this desk can answer converts a dead end into one more
    click. It also draws the scope line without a lecture: nothing about
    credit, risk or KYC appears in the list, so an officer sees what this
    stage is for.

    NO GUESS IS EMBEDDED IN IT. The options are the same four regardless of
    the message, because ranking them by a similarity score against a
    question the classifier already failed on would be presenting a guess as
    a suggestion.
    """
    return {
        "reason": "INTENT_NOT_RECOGNISED",
        "question": (
            "I can answer questions about this case's documents, checklist "
            "and readiness for handoff. Which of these did you mean?"
            if has_case else
            "I can answer questions about a case's documents, checklist and "
            "readiness for handoff. Open a case, or pick one of these."
        ),
        "options": list(_OFFERS),
        "original_message": (message or "").strip()[:200],
    }


__all__ = ["CARRIES_POLICY", "READS_CASE", "QueryType", "clarification_for",
           "type_for"]
