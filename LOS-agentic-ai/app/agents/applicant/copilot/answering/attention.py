"""
"NOTHING IS PENDING" MUST NOT HIDE AN OPEN REVIEW (acceptance run, 2026-10-06).

The FOS checklist (workflow.pending_items) lists missing documents and details --
it gates the CPA handoff and is right to ignore a KYC review. But "what is
pending?" answered "Nothing is pending for this case." while KYC was in review
and the next-action answer said to wait for the reviewer: two answers, one case,
contradicting each other.

This adds the OPEN REVIEWS, read from the same recorded state as
GET /applications/{id}/reviews (status_api.reviews) -- so the chat and the API can
never disagree -- in words, never codes. Nothing here decides anything.
"""

from __future__ import annotations

from typing import Any

_EMPTY_ANSWERS = ("Nothing is pending", "No required documents are missing", "No documents are pending",
                  "Nothing uploaded by")
_INTENTS = {"PENDING_ITEMS", "DOCUMENTS_MISSING", "DOCUMENTS_PENDING"}
_WHAT = {"DOCUMENT_REVIEW": "a document needs a check", "DOCUMENT_REJECTED": "a document must be re-uploaded",
         "KYC_REVIEW": "KYC needs a review", "KYC_FAIL": "KYC did not pass",
         "ELIGIBILITY_REVIEW": "eligibility needs a review", "CREDIT_REVIEW": "the credit assessment needs a review"}


def open_reviews(case_id: str) -> list[dict[str, Any]]:
    try:
        from app.api.routes import status_api

        return status_api.reviews(case_id)
    except Exception:  # noqa: BLE001 - a read that fails adds nothing, never an error
        return []


def amend(published: dict[str, Any]) -> dict[str, Any]:
    """Add the open reviews to an empty-checklist answer. English answers only (localized ones keep theirs)."""
    answer = str(published.get("answer") or "")
    if (str(published.get("intent") or "") not in _INTENTS or not published.get("case_id")
            or not answer.startswith(_EMPTY_ANSWERS)
            or (published.get("language_contract") or {}).get("reply_language") not in (None, "en")):
        return published
    reviews = open_reviews(published["case_id"])
    # WHOSE QUESTION: a question about one party gets that party's reviews only
    asked = _asked_party(published)
    if asked:
        reviews = [r for r in reviews if (r.get("party_role") or "PRIMARY_APPLICANT") == asked
                   or not r.get("party_role") and not r["review_type"].startswith("KYC")]
    if not reviews:
        return published
    two_party = len({r.get("party_role") for r in reviews if r.get("party_role")}) > 1
    lead = answer.split(".")[0].replace("Nothing is pending for this case", "Nothing is missing from the checklist")
    lines = [f"{lead}, but {'one thing still needs' if len(reviews) == 1 else 'these still need'} attention:"]
    seen: set[str] = set()
    for r in reviews[:4]:
        what = _WHAT.get(r["review_type"], "something needs a review")
        if r.get("party_role") == "CO_APPLICANT" and (two_party or asked != "CO_APPLICANT"):
            what = what.replace("KYC", "the co-applicant's KYC", 1)
        elif r.get("party_role") == "PRIMARY_APPLICANT" and two_party:
            what = what.replace("KYC", "the applicant's KYC", 1)
        reason = str(r.get("reason") or "").strip().rstrip(".")
        fields = [f for f in r.get("affected_documents") or [] if f]
        if (not reason or reason.lower() == what.lower()) and r["review_type"].startswith("KYC") and fields:
            reason = f"the {' and '.join(fields)} differ{'s' if len(fields) == 1 else ''} across the documents"
        # never "KYC needs a review -- KYC needs a review": no reason, no dash
        line = f"⚠ {what[0].upper()}{what[1:]}" + (f" — {reason}." if reason and reason.lower() != what.lower()
                                                    else ".")
        if line not in seen:                       # the same item twice is said once
            seen.add(line)
            lines.append(line)
    if len(lines) == 2:
        lines[0] = lines[0].replace("these still need", "one thing still needs")
    published["answer"] = "\n".join(lines)
    return published


def _asked_party(published: dict[str, Any]) -> str | None:
    """PRIMARY_APPLICANT / CO_APPLICANT when the answer was about one party, else None."""
    subject = published.get("subject") if isinstance(published.get("subject"), dict) else {}
    kind = str((subject or {}).get("kind") or "").upper()
    return {"CO": "CO_APPLICANT", "CO_APPLICANT": "CO_APPLICANT", "SELF": "PRIMARY_APPLICANT",
            "PRIMARY": "PRIMARY_APPLICANT", "PRIMARY_APPLICANT": "PRIMARY_APPLICANT"}.get(kind)


__all__ = ["amend", "open_reviews"]
