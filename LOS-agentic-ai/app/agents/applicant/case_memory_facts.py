"""
What this case was found to be, read back.

THE QUESTION THIS ANSWERS. "Why is this case in review?" The live store
says the case IS in review; it does not say why. The reasons were
computed by the LOS pipeline, returned in one response and -- until
Phase 1B -- lost. They are now in `case_findings`, `case_decisions` and
`case_events`, and this reads them.

IT IS NOT AUTHORISATION. Every function here takes a case_id and filters
on it. Whether the caller may see that case was decided before any of
this ran -- require_jwt, Caller, capability, then
`permissions.check_ownership`. Putting a second answer to that question
here would be two places to get it wrong, and the weaker one would win
by accident.

IT INVENTS NOTHING. Every reason in the summary is a reason code the
pipeline recorded. When nothing was recorded, the answer says so rather
than reaching for a plausible explanation -- an ungrounded "probably
because the PAN failed" is the worst thing this layer could produce,
because it is indistinguishable from a real one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many findings a summary names before it stops. A reviewer reading
#: a chat panel acts on the first few; twenty is a report, not an answer.
_MAX_REASONS = 6

#: Finding kinds whose reason codes explain a verdict. EXTRACTION is
#: excluded deliberately: it records what was read, not what was wrong.
_EXPLAINING_KINDS = ("VERIFICATION", "KYC", "FINANCIAL", "RISK", "RCU")

#: Reason code -> how it reads in a sentence. A code with no entry is
#: rendered from the code itself rather than dropped: an unnamed reason
#: is still a reason the reviewer needs to see.
_READABLE = {
    "DOCUMENT_TYPE_MISMATCH": "a document was not the type it was declared as",
    "NAME_MISMATCH": "the name differs across documents",
    "DOB_MISMATCH": "the date of birth differs across documents",
    "PAN_MISMATCH": "the PAN differs across documents",
    "FATHER_NAME_MISMATCH": "the father's name differs across documents",
    "ADDRESS_MISMATCH": "the address differs across documents",
    "INSUFFICIENT_SOURCES": "there was only one document to compare",
    "VERIFICATION_INCONCLUSIVE": "verification could not reach a conclusion",
    "PROFILE_MISMATCH": "a declared detail did not match the documents",
}


def _readable(code: str) -> str:
    known = _READABLE.get(code)
    if known:
        return known
    return str(code).replace("_", " ").lower()


def available() -> bool:
    """
    Whether case memory is switched on.

    Off by default. When off this module reports nothing rather than
    guessing from live state, because "we did not record it" and "there
    was nothing to record" are different answers.
    """
    from app.agents.los import config as los_config

    return los_config.case_memory_enabled()


def _repository():
    from app.store import get_repository

    return get_repository()


def case_memory(case_id: str, party_id: str | None = None) -> dict[str, Any]:
    """
    Findings, decisions and timeline for one case.

    CASE-SCOPED, AND PARTY-SCOPED WHEN ASKED. `party_id` narrows to that
    party plus the case-level findings that belong to nobody in
    particular -- a cross-document check is the case's, and hiding it
    because a party was named would lose the very thing that explains a
    two-party review.

    Never raises: a store that cannot be reached means no history, and
    the caller already handles that by saying so.
    """
    if not case_id or not available():
        return _empty()

    try:
        repository = _repository()
        findings = repository.get_case_findings(case_id, party_id=party_id)
        decisions = repository.get_case_decisions(case_id)
        timeline = repository.get_case_timeline(case_id)
    except Exception as exc:
        logger.warning("Case memory unavailable for %s: %r", case_id, exc)
        return _empty()

    return {
        "findings": [_public_finding(f) for f in findings],
        "decisions": [_public_decision(d) for d in decisions],
        "timeline": [_public_event(e) for e in timeline],
    }


def _empty() -> dict[str, Any]:
    return {"findings": [], "decisions": [], "timeline": []}


def _public_finding(finding: Any) -> dict[str, Any]:
    """
    One finding, as a caller sees it.

    AN ALLOWLIST. `payload` is deliberately absent: it holds the
    structured detail the pipeline published, and a chat answer needs
    the verdict and the reason, not the values. Publishing it here would
    put extracted identity data into a conversational response that had
    no reason to carry it.
    """
    kind = finding.finding_kind
    row: dict[str, Any] = {
        "finding_kind": getattr(kind, "value", str(kind)),
        "status": finding.status,
    }
    for name in ("party_id", "source_id", "document_id", "score",
                 "confidence"):
        value = getattr(finding, name, None)
        if value is not None:
            row[name] = value
    if finding.reason_codes:
        row["reason_codes"] = list(finding.reason_codes)
    return row


def _public_decision(decision: Any) -> dict[str, Any]:
    row = {"decision": decision.decision, "next_action": decision.next_action,
           "status": decision.status}
    if decision.reason_codes:
        row["reason_codes"] = list(decision.reason_codes)
    for name in ("policy_id", "policy_version"):
        value = getattr(decision, name, None)
        if value:
            row[name] = value
    return {k: v for k, v in row.items() if v is not None}


def _public_event(event: Any) -> dict[str, Any]:
    row = {"event_type": event.event_type, "sequence": event.sequence}
    for name in ("stage", "summary", "party_id"):
        value = getattr(event, name, None)
        if value:
            row[name] = value
    return row


# ==========================================================================
# THE ANSWER
# ==========================================================================


def explain(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    Why the case stands where it does, and what says so.

    Returns the sentence and the sources behind it. EVERY CLAUSE COMES
    FROM A RECORDED REASON CODE -- there is no model here and no
    inference. When nothing was recorded the sentence says exactly that,
    because a confident explanation assembled from nothing is the one
    output a reviewer cannot check.
    """
    findings = memory.get("findings") or []
    decisions = memory.get("decisions") or []

    explaining = [
        f for f in findings
        if f.get("reason_codes")
        and f.get("finding_kind") in _EXPLAINING_KINDS
    ]

    if not explaining and not decisions:
        return (
            "No findings have been recorded for this case yet, so there is "
            "nothing on file explaining its current state.",
            [],
        )

    verdict = decisions[-1] if decisions else {}
    head = _verdict_sentence(verdict)

    if not explaining:
        return (
            f"{head} No individual findings were recorded against it.",
            _sources(decisions=decisions),
        )

    seen: list[str] = []
    for finding in explaining:
        for code in finding.get("reason_codes") or []:
            if code not in seen:
                seen.append(code)

    shown = seen[:_MAX_REASONS]
    clauses = [_readable(code) for code in shown]
    more = len(seen) - len(shown)

    detail = _and_list(clauses)
    tail = f", and {more} further finding(s)" if more > 0 else ""

    return (f"{head} Recorded findings: {detail}{tail}.",
            _sources(findings=explaining, decisions=decisions))


def _verdict_sentence(verdict: dict[str, Any]) -> str:
    decision = verdict.get("decision")
    status = verdict.get("status")

    if decision and status:
        return f"This case was recorded as {status} with a {decision} decision."
    if decision:
        return f"This case was recorded with a {decision} decision."
    if status:
        return f"This case was recorded as {status}."
    return "This case has recorded findings."


def _sources(
    findings: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    What the answer rests on.

    One entry per reason, naming the finding that carried it -- so a
    reviewer can go to the document rather than take the sentence on
    trust. Never a payload, never a raw row.
    """
    sources: list[dict[str, Any]] = []

    for finding in (findings or [])[:_MAX_REASONS]:
        for code in (finding.get("reason_codes") or [])[:_MAX_REASONS]:
            entry = {
                "kind": "case_finding",
                "finding_kind": finding.get("finding_kind"),
                "reason_code": code,
            }
            for name in ("document_id", "source_id", "party_id"):
                if finding.get(name):
                    entry[name] = finding[name]
            sources.append(entry)

    for decision in (decisions or [])[-1:]:
        sources.append({
            "kind": "case_decision",
            "decision": decision.get("decision"),
            "status": decision.get("status"),
        })

    return sources


def _and_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


__all__ = ["available", "case_memory", "explain"]
