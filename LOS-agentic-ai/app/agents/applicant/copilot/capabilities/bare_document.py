"""
A MESSAGE THAT IS ONLY A DOCUMENT NAME (owner, 2026-10-13: "context malum ho toh sirf docs likhne pe bhi samajh jana
chahiye; bina context follow-up question aana chahiye").

    "PAN" / "and the salary slip?" / "what about address proof"
        a case open   -> read as "What is the status of the <document>?" (the same tools answer it)
        no case open  -> ONE follow-up question: which case, or the document in general -- never a guessed case

The names and the words that may accompany them are config (conversation_general.yaml phrases.document_names /
bare_document_lead); the reply is config (texts.bare_document_no_case, texts.bare_document_question).
"""

from __future__ import annotations

import re
from typing import Any


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg()


def document(message: str) -> str | None:
    """The document code when the message is nothing but one document's name (with a lead such as "and the"),
    else None."""
    phrases = _cfg().get("phrases") or {}
    said = re.sub(r"[^\w\s'-]", " ", str(message or "").lower())
    said = " " + re.sub(r"\s+", " ", said).strip() + " "
    leads = sorted((str(w).lower() for w in phrases.get("bare_document_lead") or []), key=len, reverse=True)
    changed = True
    while changed:                                   # "and what about the PAN" -> "pan"
        changed = False
        for lead in leads:
            if said.startswith(f" {lead} "):
                said = said[len(lead) + 1:]
                changed = True
    said = said.strip()
    for code, aliases in (phrases.get("document_names") or {}).items():
        if any(said == str(a).lower() for a in aliases or []):
            return str(code)
    return None


def contents(message: str) -> str | None:
    """"what does the salary slip say?" with a case open -> "show the Salary Slip details" (config
    phrases.document_contents / texts.document_contents_question); None for anything else."""
    from app.agents.applicant.copilot.answering.answer import _readable

    for pattern in (_cfg().get("phrases") or {}).get("document_contents") or []:
        found = re.match(pattern, str(message or ""), re.I)
        if not found:
            continue
        code = document(found.group("doc"))
        if code:
            template = str(((_cfg().get("texts") or {}).get("document_contents_question") or {}).get("en")
                           or "show the {document} details")
            return template.format(document=_readable(code))
    return None


def question(code: str) -> str:
    """The clear question a bare name stands for on an open case."""
    from app.agents.applicant.copilot.answering.answer import _readable

    template = str(((_cfg().get("texts") or {}).get("bare_document_question") or {}).get("en") or "")
    return template.format(document=_readable(code)) if template else str(code)


def no_case_reply(code: str, request_id: str, lang: str) -> dict[str, Any]:
    """The follow-up question when no case is open: which case, or the document in general."""
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.agents.applicant.copilot.capabilities import general

    name = _readable(code)
    asks = [str(a).format(document=name) for a in (_cfg().get("bare_document_asks") or [])]
    return general._reply(request_id, "WHICH_CASE", general._say("bare_document_no_case", lang, document=name),
                          block=general._asks(asks) if asks else "", case_id=None)


__all__ = ["contents", "document", "no_case_reply", "question"]
