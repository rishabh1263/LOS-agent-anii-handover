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

import re
from enum import Enum

from app.agents.applicant.copilot.semantics.intents import WRITE_INTENTS, Intent


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
    Intent.POLICY_EXPLANATION,
})

#: Intents answered purely from stored records and derived state.
_CASE_FACT = frozenset({
    # WHAT IS PENDING IS A FACT ABOUT THIS CASE, not a rule about
    # the product. It sat with the policy intents and was published
    # as POLICY_REQUIREMENT, which tells a client the answer came
    # from the handbook -- it comes from the documents on the case
    # and the state each of them is in. The answer itself never
    # changed; only what the response called it.
    #
    # DOCUMENTS_REQUIRED AND DOCUMENTS_MISSING STAY WHERE THEY ARE.
    # "What is required for a personal loan" is a rule, and the
    # missing-documents answer carries the policy block a client
    # renders beside it.
    Intent.DOCUMENTS_PENDING,
    Intent.APPLICANT_DETAILS,
    Intent.APPLICANT_MISSING_INFO,
    Intent.APPLICANT_PROFILE,
    Intent.APPLICATION_STATUS,
    Intent.APPLICATION_STAGE,
    Intent.PENDING_ITEMS,
    Intent.NEXT_ACTION,
    Intent.READINESS,
    Intent.COMPLETENESS,
    Intent.FULL_SUMMARY,
    # FOS plan 6: a free question answered from the case's own fact sheet is a fact about the case
    Intent.CASE_SNAPSHOT,
    # Recorded findings are stored records too. Not DOCUMENT_STATUS: the
    # question is why the case stands where it does, not what state a
    # document is in, and a UI renders those differently. Not
    # POLICY_REQUIREMENT either -- nothing here is a rule the officer is
    # about to act on, it is what was already found.
    Intent.CASE_HISTORY,
    # The recorded findings themselves, and the recorded KYC result.
    Intent.CASE_FINDINGS,
    Intent.KYC_RESULT,
    # What the documents recorded about income is a stored record.
    Intent.INCOME_EVIDENCE,
    # And so is what affordability concluded.
    Intent.ELIGIBILITY,
    # And so is what a document was read to say.
    Intent.DOCUMENT_DETAILS,
    # The applicant's applications are stored records too -- about the
    # person rather than one case, but facts either way.
    Intent.CASE_PORTFOLIO,
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
    if intent in (Intent.FOS_KNOWLEDGE, Intent.STAGE_PROCESS):
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
    "What are my application details?",
    "What is my application status?",
    "What documents are still needed?",
    "Which documents have been verified?",
    "What is pending on this case?",
)


#: Keyboard runs and repeated characters: input that is not words at all.
_MASH = re.compile(r"(qwer|wert|erty|asdf|sdfg|dfgh|fghj|ghjk|hjkl|zxcv|xcvb|cvbn|vbnm|(.)\2{2,})", re.IGNORECASE)


def _unreadable(message: str) -> bool:
    """No letters at all, or mostly keyboard-mash / repeated characters."""
    words = re.findall(r"[A-Za-z\u0900-\u0DFF\u0600-\u06FF]+", message or "")
    if not words:
        return True
    mash = sum(1 for w in words if _MASH.search(w) or (
        len(w) >= 4 and not re.search(r"[aeiouyAEIOUY\u0900-\u0DFF\u0600-\u06FF]", w)))
    return mash * 2 >= len(words)


def _in_domain(message: str) -> bool:
    """Anything of the loan domain: a concept, a named document or a stage."""
    try:
        from app.agents.applicant.copilot.semantics import intents, semantic_frame

        from app.agents.applicant import config

        # FOS plan 1.6: words of the officer's own work ("list", "details") make a garbled message a
        # clarification, never "outside what I can help with" (applicant_agent.yaml chatbot.domain_words)
        extra = [re.escape(str(w)) for w in config.chatbot("domain_words").get("words") or []]
        return bool(semantic_frame.concepts_in(message) or intents._document_type(message)
                    or semantic_frame._named_stage(message)
                    or re.search(r"\b(loan|application|case|applicant|kyc|emi|bank|credit|cpa|fos)\b", message, re.I)
                    or (extra and re.search(r"\b(" + "|".join(extra) + r")\b", message, re.I)))
    except Exception:  # noqa: BLE001 - unknown: treat as in-domain (the general offer)
        return True


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
    # WHAT KIND OF "NOT UNDERSTOOD" (the industry fallback triage): unreadable
    # input is asked to be rephrased; a question with nothing of the loan
    # domain in it is declined as off-topic; only an in-domain question that
    # was not recognised gets the "which of these did you mean?" offer.
    # A FRAGMENT ("date", then "birth") is never off-topic: the conversation
    # layer completes fragments after the generic offer. Off-topic needs a
    # question of its own (three or more words) with nothing of the domain.
    if _unreadable(message):
        return {"reason": "UNCLEAR_INPUT",
                "question": "Sorry, I didn't catch that. Could you rephrase? For example:",
                "options": list(_OFFERS), "original_message": (message or "").strip()[:200]}
    if len((message or "").split()) >= 3 and not _in_domain(message):
        return {"reason": "OFF_TOPIC",
                "question": ("That's outside what I can help with here -- I'm set up for your loan "
                             "application. I can check its status, documents, verification or KYC for you."),
                "options": list(_OFFERS), "original_message": (message or "").strip()[:200]}
    return {
        "reason": "INTENT_NOT_RECOGNISED",
        "question": (
            "I can help with your application details, its status and stage, your "
            "documents, and what to do next. Which of these did you mean?"
            if has_case else
            "I can help with a case's application details, status and stage, documents "
            "and next steps. Open a case, or pick one of these."
        ),
        "options": list(_OFFERS),
        "original_message": (message or "").strip()[:200],
    }


__all__ = ["CARRIES_POLICY", "READS_CASE", "QueryType", "clarification_for",
           "type_for"]
