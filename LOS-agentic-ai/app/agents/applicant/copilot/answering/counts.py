"""
COUNT QUESTIONS (FOS plan 6.6; flag COPILOT_COUNT_ANSWERS, config chatbot.counts -- ON in dev).

"kitne documents verified hain", "how many documents are pending", "kitne reject hue" -- a number, counted from the
store's document rows (superseded ones excluded), never a list of something else and never a model's guess. Applied
after the agent on BOTH endpoints: when the message is a count question about documents in an opened case, the
answer becomes the count, with the documents named.
"""

from __future__ import annotations

import os
import re
from typing import Any

FLAG = "COPILOT_COUNT_ANSWERS"
_ON = {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("counts")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def asked(message: str) -> str | None:
    """The status a count question asks about (VERIFIED / REJECTED / PENDING / ALL), or None."""
    said = " " + " ".join(re.findall(r"[\wऀ-ॿ]+", str(message or "").lower())) + " "
    if not any(f" {c} " in said for c in _cfg().get("cues") or []):
        return None
    if not any(f" {n} " in said for n in _cfg().get("nouns") or []):
        return None
    for status, words in (_cfg().get("statuses") or {}).items():
        if any(f" {w} " in said for w in words or []):
            return str(status).upper()
    return "ALL"


def answer(case_id: str, status: str, repository: Any = None) -> str:
    from app.agents.applicant.copilot.answering import language_lock
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.store import get_repository

    repository = repository or get_repository()
    rows = [d for d in repository.list_documents(case_id) or []
            if str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper() != "SUPERSEDED"]
    groups = {k: [str(s).upper() for s in v] for k, v in (_cfg().get("groups") or {}).items()}
    wanted = groups.get(status) if status != "ALL" else None
    chosen = [d for d in rows if wanted is None or str(getattr(getattr(d, "status", ""), "value",
                                                                getattr(d, "status", ""))).upper() in wanted]
    names = ", ".join(_readable(d.document_type) for d in chosen)
    labels = _cfg().get("labels") or {}
    key = "one" if len(chosen) == 1 else ("none" if not chosen else "many")
    template = language_lock.pick((labels.get(status) or labels.get("ALL") or {}).get(key) or
                                  "{n} documents: {names}.")
    return template.format(n=len(chosen), names=names, total=len(rows))


def keep_case_header(old: Any, new: str, case_id: str) -> str:
    """A replaced answer keeps the opened case's header line ("📍 CASE-x", added before this step)."""
    first = str(old or "").split("\n", 1)[0]
    return f"{first}\n{new}" if case_id and first.strip().endswith(case_id) and first.strip() != new else new


def attach(published: dict[str, Any], message: str) -> dict[str, Any]:
    if not isinstance(published, dict) or not enabled() or not published.get("case_id"):
        return published
    status = asked(message)
    if status is None:
        return published
    published["answer"] = keep_case_header(published.get("answer"), answer(published["case_id"], status),
                                           published["case_id"])
    published.pop("answer_markdown", None)
    published["intent"] = "DOCUMENT_COUNT"
    return published


__all__ = ["FLAG", "answer", "asked", "attach", "enabled"]
