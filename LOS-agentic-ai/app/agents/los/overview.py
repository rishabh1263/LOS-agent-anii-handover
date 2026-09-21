"""
The application, arranged for the person reading it.

PRESENTATION ONLY, AND THAT IS THE WHOLE POINT. Every value published
from here is already in the envelope: a status verification reached, a
decision the decision rules made, a reason code KYC recorded. Nothing is
computed, nothing is weighed, no threshold is consulted. Delete this
module and the outcome of every application is identical -- which is the
property that makes it safe to add to a response that decides loans.

WHAT IT FIXES. The result of a two-party application was spread across
`status`, `decision`, `next_action`, `cross_document`, two party
sections and a flat `documents` list, and a frontend had to join them to
answer "what happened, and whose problem is it". `overall` answers that
in one object, with every issue attributed to the party it belongs to.

WHAT IT REFUSES TO INVENT. `missing` is always empty, because
/api/v1/los/process has no checklist: required-document policy lives in
the applicant agent, and this endpoint never computes it. An empty list
here means "this endpoint does not track that", and filling it with
plausible-looking gaps would be the most damaging thing this module
could do -- a reviewer would chase documents nobody asked for.
"""

from __future__ import annotations

from typing import Any

from app.agents.los import kyc_explain

#: Party-section status for somebody who sent nothing. Imported by the
#: flow so the two cannot drift.
NOT_PROVIDED = "NOT_PROVIDED"

#: Worst-wins, over the vocabulary documents actually use.
_RANK = {"SUCCESS": 0, "PASS": 0, "SKIPPED": 0, "NOT_PROVIDED": 0,
         "PARTIAL": 1, "REVIEW": 1, "REJECTED": 2, "FAIL": 2, "FAILED": 3}

_ROLE_WORD = {
    "PRIMARY_APPLICANT": "Primary applicant",
    "CO_APPLICANT": "Co-applicant",
}


def _worst(statuses: list[str]) -> str:
    """The most severe of several verdicts, on the existing scale."""
    ranked = [(s or "").upper() for s in statuses if s]
    if not ranked:
        return "SKIPPED"
    return max(ranked, key=lambda s: _RANK.get(s, 1))


def _issue(code: str, message: str | None = None) -> dict[str, str]:
    published = {"code": str(code)}
    text = message or kyc_explain.field_message(code)
    if text:
        published["message"] = text
    return published


def verification_of(documents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    How this party's documents came out, as one object.

    None when the party sent nothing. NOT an object reading SKIPPED: a
    check that never ran has established nothing, and saying SKIPPED
    would claim verification looked and reached a verdict.
    """
    if not documents:
        return None

    issues: list[dict[str, str]] = []
    seen: set[str] = set()

    for document in documents:
        verdict = str(document.get("verification") or "").upper()
        if verdict in {"PASS", "SKIPPED", ""}:
            continue
        for code in document.get("reason_codes") or []:
            if code in seen:
                continue
            seen.add(str(code))
            issues.append({
                "code": str(code),
                "source_id": str(document.get("source_id") or ""),
            })

    return {
        "status": _worst([str(d.get("verification") or "") for d in documents]),
        "issues": issues,
    }


def issues_of(kyc: dict[str, Any] | None) -> list[dict[str, str]]:
    """This party's KYC reason codes, as issues a reader acts on."""
    return [_issue(code) for code in (kyc or {}).get("reason_codes") or []]


def kyc_of(kyc: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    This party's KYC, trimmed to what a reader acts on.

    None when there is no KYC for this party -- which is not the same as
    a KYC that ran and skipped, and the caller must be able to tell
    those apart.
    """
    if not kyc:
        return None

    published: dict[str, Any] = {"status": kyc.get("status", "SKIPPED")}

    if kyc.get("score"):
        published["score"] = kyc["score"]

    published["issues"] = [
        _issue(code) for code in kyc.get("reason_codes") or []
    ]
    return published


def _party_issues(role: str, section: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything wrong with one party, tagged with whose it is."""
    issues: list[dict[str, Any]] = []

    for source in (section.get("kyc") or {}, section.get("verification") or {}):
        for issue in source.get("issues") or []:
            issues.append({"party": role, **issue})

    return issues


def build(
    *,
    status: str,
    decision: str,
    next_action: str,
    sections: dict[str, dict[str, Any]],
    documents: list[dict[str, Any]],
    processing_ms: float,
) -> dict[str, Any]:
    """
    The application-level answer: what happened, and whose problem it is.

    `status`, `decision` and `next_action` are passed in exactly as the
    existing rules produced them. This function does not rank, override
    or re-derive any of them -- it puts them beside the reasons, which
    were previously in three other places.
    """
    issues: list[dict[str, Any]] = []
    for key, role in (("primary_applicant", "PRIMARY_APPLICANT"),
                      ("co_applicant", "CO_APPLICANT")):
        section = sections.get(key)
        if section:
            issues.extend(_party_issues(role, section))

    return {
        "status": status,
        "decision": decision,
        "next_action": next_action,
        "issues": issues,
        # ALWAYS EMPTY, AND HONESTLY SO. See the module docstring: this
        # endpoint has no checklist to compare against, so it cannot
        # know what is missing. The key is present because its absence
        # would be read as "nothing is missing", which is a claim.
        "missing": [],
        "summary": summarise(sections, status),
        # NAMED `processing_summary`, NOT `processing`. The latter is a
        # reserved name in this response: it is the INTERNAL per-stage
        # timings dict (ocr_ms, classification_ms, ...) that must never
        # be published, and `test_no_internal_field_is_reachable_from_
        # the_response` holds that line. Taking the name would make a
        # reviewer grepping the response believe stage timings leaked.
        "processing_summary": {
            # EQUAL BY CONSTRUCTION. Every upload produces a result row,
            # including one that failed, so nothing received can go
            # unprocessed. Both are published because a reader checking
            # for silently dropped documents should be able to see that
            # they match rather than have to trust it.
            "documents_received": len(documents),
            "documents_processed": len(documents),
            "processing_ms": round(float(processing_ms or 0.0), 2),
        },
    }


def summarise(sections: dict[str, dict[str, Any]], status: str) -> str:
    """
    Why the application stands where it does, one party at a time.

    DETERMINISTIC, AND SEPARATE FROM THE LLM SUMMARY. The top-level
    `summary` may be written by a model; this one never is. It is
    assembled from the party statuses and the recorded reason codes, so
    it cannot describe an outcome the response does not contain -- which
    is exactly what a fluent sentence about the wrong party would be.
    """
    said: list[str] = []

    for key, role in (("primary_applicant", "PRIMARY_APPLICANT"),
                      ("co_applicant", "CO_APPLICANT")):
        section = sections.get(key)
        if not section:
            continue
        said.append(_sentence(_ROLE_WORD[role], section))

    return " ".join(s for s in said if s) or "Nothing was submitted."


def _sentence(who: str, section: dict[str, Any]) -> str:
    state = str(section.get("status") or "").upper()

    if state == NOT_PROVIDED:
        # NOT A FAILURE, AND NOT DESCRIBED AS ONE. Nobody sent anything
        # for this party; there is nothing to report about them.
        return f"{who} documents were not provided."

    reasons = [i["code"] for i in _party_issues("", section)]
    because = f" due to {_and_list(_unique(reasons))}" if reasons else ""

    if state in {"SUCCESS", "PASS"}:
        return f"{who} verification passed."
    if state in {"FAILED", "FAIL", "REJECTED"}:
        return f"{who} verification failed{because}."
    if state in {"REVIEW", "PARTIAL"}:
        return f"{who} verification requires review{because}."
    return f"{who} verification reported {state.lower()}{because}."


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _and_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


__all__ = ["NOT_PROVIDED", "build", "issues_of", "kyc_of",
           "summarise", "verification_of"]
