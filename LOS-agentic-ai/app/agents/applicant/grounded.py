"""
Is this answer supported by authoritative case evidence?

THE DEFECT THIS CLOSES. Asked "why is my application under review?", the
Universal Copilot answered from the case's own recorded KYC finding and
decision -- and cited both -- while publishing `grounded: false`. The
field had quietly meant something narrower than its name: whether VECTOR
RETRIEVAL had grounded the answer. A case-history answer is read from case
memory, retrieval found nothing it was confident about, and an answer
resting entirely on recorded findings was reported as ungrounded.

ONE RULE, FOR EVERY SURFACE. The FOS endpoint already meant "the records
answered it"; the Universal Copilot did not. Both now ask this module.

WHAT COUNTS AS EVIDENCE, and what does not:

    counts     case-memory sources -- recorded findings, decisions,
               income and eligibility results -- that the answer cites
    counts     a non-empty record the answer was built from: applicant,
               application, documents, checklist, pending items, next
               action, readiness, verification
    never      a clarification, a downstream route, an out-of-scope or
               unknown question, or an empty answer -- none of those
               rests on anything

NOT `response_source`. STRUCTURED says who PHRASED the answer, not what it
rests on; a structured "no answer is available" is structured and
ungrounded.

NO WORDING IS MATCHED. Case-memory answers only produce sources when
something was recorded -- "no findings have been recorded" and
"eligibility has not been evaluated" come back with none -- so an answer
that reports an absence is ungrounded by construction.
"""

from __future__ import annotations

from typing import Any

#: Records the answer can be built from. Present and non-empty means the
#: store answered.
_RECORDS = ("applicant", "application", "documents", "checklist",
            "pending_items", "next_action", "readiness", "verification")

#: Outcomes that rest on nothing, however they were phrased.
_UNSUPPORTED_INTENTS = {"OUT_OF_SCOPE", "UNKNOWN"}
_UNSUPPORTED_CATEGORIES = {"DOWNSTREAM"}

#: Intents answered from recorded findings, whose only evidence is what
#: they cite.
_FROM_FINDINGS = {"CASE_HISTORY", "ELIGIBILITY", "INCOME_EVIDENCE",
                  "DOCUMENT_DETAILS"}


def supported_by_case_evidence(envelope: dict[str, Any]) -> bool:
    """Whether authoritative case evidence supports this answer."""
    if not str(envelope.get("answer") or "").strip():
        return False
    if envelope.get("clarification_required"):
        return False
    if envelope.get("route_to"):
        return False
    if str(envelope.get("intent") or "").upper() in _UNSUPPORTED_INTENTS:
        return False
    if str(envelope.get("category") or "").upper() in _UNSUPPORTED_CATEGORIES:
        return False

    # Recorded findings, decisions, income and eligibility results the
    # answer cites. Case memory produces these only when something WAS
    # recorded.
    if envelope.get("sources"):
        return True

    # AN ANSWER READ FROM RECORDED FINDINGS RESTS ON THOSE FINDINGS ALONE.
    # These intents attach the application record only to name the case;
    # "eligibility has not been evaluated" or "no PAN has been recorded"
    # arrives beside it and rests on nothing. Without a cited finding
    # they are not grounded, whatever else the envelope carries.
    answered_by = str(envelope.get("base_intent")
                      or envelope.get("intent") or "").upper()
    if answered_by in _FROM_FINDINGS:
        return False

    return any(envelope.get(name) for name in _RECORDS)


__all__ = ["supported_by_case_evidence"]
