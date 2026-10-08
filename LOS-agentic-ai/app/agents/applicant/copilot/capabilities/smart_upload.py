"""
SMART UPLOAD (FOS plan 7.2; flag COPILOT_SMART_UPLOAD, config chatbot.smart_upload -- ON in dev).

The officer uploads any file inside a case without saying what it is. The EXISTING classification identifies the
type (the upload pipeline already does: a blank document type means "classify this one"); this step says, per file,
WHAT it looks like, WHOSE it looks like -- the name on the document against the applicant's and the
co-applicant's names, by the existing name matcher -- and WHERE it was filed:

    "This looks like the applicant's PAN. Filed under Applicant > PAN. Verification passed."

LOW CONFIDENCE -> ONE QUESTION: the type could not be told ("Which document is this?", with the case's open slots
as options), or the name on it fits the OTHER party better than the one it was filed under ("Is this the applicant's
or the co-applicant's document?", with an upload-again action for the other party). Nothing is moved silently.
"""

from __future__ import annotations

import os
from typing import Any

FLAG = "COPILOT_SMART_UPLOAD"
_ON = {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("smart_upload")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    defaults = {
        "filed": "This looks like {whose} {document}. Filed under {party} > {document}. {verdict}",
        "unknown": "I couldn't tell what document {file} is. Which document is it?",
        "other_party": "The name on {file} looks like {other_name}'s ({other_party}), but it was filed under the "
                       "{party}. Is this the {party_lower}'s or the {other_lower}'s document?",
        "passed": "Verification passed.", "review": "It needs a review.", "failed": "It did not pass verification.",
        "started": "Verification started.",
    }
    return language_lock.pick((_cfg().get("labels") or {}).get(key, defaults.get(key, key))).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    from app.services.name_match import match_names

    return float(match_names(str(a), str(b)).score)


def describe(case_id: str, outcomes: list[dict[str, Any]], *, filed_as_co: bool,
             repository: Any = None) -> tuple[list[str], dict[str, Any] | None, list[dict[str, Any]]]:
    """(lines, clarification or None, actions) for the files of one upload."""
    from app.store import get_repository

    repository = repository or get_repository()
    application = repository.get_application(case_id)
    applicant = repository.get_applicant(application.applicant_id) if application else None
    applicant_name = getattr(applicant, "full_name", None)
    co_name, co_id = None, None
    try:
        from app.agents.los import co_applicants

        rows = co_applicants.list_for_case(case_id, repository)
        if rows:
            co_name, co_id = rows[0].get("name"), rows[0].get("co_applicant_id")
    except Exception:  # noqa: BLE001 - no co-applicant on record: only the applicant can match
        pass
    margin = float(_cfg().get("party_margin", 0.15))
    lines: list[str] = []
    question: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = []
    party, party_lower = ("Co-applicant", "co-applicant") if filed_as_co else ("Applicant", "applicant")
    other, other_lower = ("Applicant", "applicant") if filed_as_co else ("Co-applicant", "co-applicant")
    for o in outcomes:
        kind = str(o.get("document_type") or "").upper()
        file = str(o.get("source_id") or "the file")
        if not kind or kind == "UNKNOWN":
            slots = [_readable(s) for s in _cfg().get("ask_slots") or []]
            question = question or {"reason": "UPLOAD_TYPE_UNKNOWN", "question": _label("unknown", file=file),
                                    "options": slots}
            continue
        name = o.get("name_on_document")
        mine = _score(name, co_name if filed_as_co else applicant_name)
        theirs = _score(name, applicant_name if filed_as_co else co_name)
        if name and theirs >= float(_cfg().get("match_threshold", 0.85)) and theirs - mine >= margin:
            other_name = applicant_name if filed_as_co else co_name
            question = question or {"reason": "UPLOAD_PARTY_UNSURE", "question": _label(
                "other_party", file=file, other_name=other_name or other_lower, other_party=other_lower,
                party=party_lower, party_lower=party_lower, other_lower=other_lower),
                "options": [f"{party}'s", f"{other}'s"]}
            actions.append({"type": "UPLOAD_DOCUMENT", "document_type": kind,
                            "label": f"Upload again as the {other_lower}'s {_readable(kind)}",
                            **({"co_applicant_id": co_id} if not filed_as_co and co_id else {})})
            continue
        verdict = str(o.get("verification") or "").upper()
        said = {"PASS": _label("passed"), "VERIFIED": _label("passed"), "REVIEW": _label("review"),
                "FAIL": _label("failed"), "REJECTED": _label("failed")}.get(verdict, _label("started"))
        whose = "the co-applicant's" if filed_as_co else "the applicant's"
        lines.append(_label("filed", whose=whose, document=_readable(kind), party=party, verdict=said).strip())
    return lines, question, actions


__all__ = ["FLAG", "describe", "enabled"]
