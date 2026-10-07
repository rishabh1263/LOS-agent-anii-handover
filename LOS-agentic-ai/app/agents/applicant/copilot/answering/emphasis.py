"""
THE 1-2 WORDS THAT MATTER, BOLD (Phase 3 step 5a; flag COPILOT_EMPHASIS, default off).

The frontend's markdown support is not confirmed, so the emphasis is sent three ways
and the client picks what it can render:

    emphasis         [{"text", "start", "end"}]  -- structured, positions in `answer_plain`
    answer_markdown  the answer with those terms in **bold**
    answer_plain     the answer with every markdown marker removed
    answer           unchanged (existing clients see no difference)

WHAT IS BOLDED comes from the response's own structured data, never from guessing at
the prose, in this order: a KYC mismatch field (document_actions), a status word for a
document or the case, an amount, a document name. A term must appear verbatim in the
answer and be short (at most 5 words) -- a sentence is never bolded. At most two terms.
"""

from __future__ import annotations

import os
import re
from typing import Any

FLAG = "COPILOT_EMPHASIS"
MAX_TERMS = 2
MAX_WORDS = 5

#: status words a reader scans for (English and the Hinglish the localized answers use)
_STATUS_TERMS = ("Needs review", "Not uploaded yet", "Ready for CPA", "ready for CPA", "under review", "Rejected",
                 "rejected", "Verified", "verified", "Pending", "pending", "did not pass", "passed", "failed",
                 "review mein", "reject hua", "baaki", "approved", "not ready")
_AMOUNT = re.compile(r"₹\s?\d[\d,]*(?:\.\d+)?")
_MARKDOWN = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def plain(text: str) -> str:
    """The answer without markdown emphasis markers -- and already PII-masked, so the positions
    in `emphasis` still hold after the route's final masking pass (masking is idempotent)."""
    from app.security import sensitivity

    return sensitivity.mask_identifiers(_MARKDOWN.sub(lambda m: m.group(1) or m.group(2), str(text or "")))


def _document_labels(published: dict[str, Any]) -> list[str]:
    from app.agents.applicant.copilot.answering.answer import _doc_label, _readable

    labels: list[str] = []
    for row in published.get("documents") or []:
        if isinstance(row, dict) and row.get("document_type"):
            labels += [_doc_label(row["document_type"], row.get("party_role")), _readable(row["document_type"])]
    return labels


def _candidates(published: dict[str, Any], text: str) -> list[str]:
    """Terms in priority order, each taken from the structured response."""
    ordered: list[str] = []
    view = published.get("document_actions")
    if isinstance(view, dict):
        ordered += [str(t) for t in view.get("emphasis") or []]          # mismatch field, document to upload
    ordered += [t for t in _STATUS_TERMS if t in text]                   # a status
    ordered += [m.group(0) for m in _AMOUNT.finditer(text)]              # an amount (rendered from the DB)
    ordered += _document_labels(published)                              # a document name
    return ordered


def pick(published: dict[str, Any]) -> list[dict[str, Any]]:
    """Up to two short terms found verbatim in the plain answer, with their positions."""
    text = plain(published.get("answer") or "")
    chosen: list[dict[str, Any]] = []
    for term in _candidates(published, text):
        term = term.strip()
        if not term or len(term.split()) > MAX_WORDS:
            continue
        start = _word_find(text, term)
        if start < 0:
            continue
        end = start + len(term)
        if any(start < c["end"] and c["start"] < end for c in chosen):  # overlaps one already chosen
            continue
        chosen.append({"text": term, "start": start, "end": end})
        if len(chosen) == MAX_TERMS:
            break
    return sorted(chosen, key=lambda c: c["start"])


def _word_find(text: str, term: str) -> int:
    """First occurrence of `term` as whole words ("PAN" not inside "PANEL")."""
    m = re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text)
    return m.start() if m else -1


def markdown(text: str, terms: list[dict[str, Any]]) -> str:
    out, cursor = [], 0
    for t in terms:
        out += [text[cursor:t["start"]], f"**{text[t['start']:t['end']]}**"]
        cursor = t["end"]
    out.append(text[cursor:])
    return "".join(out)


def apply(published: dict[str, Any]) -> dict[str, Any]:
    """Adds emphasis, answer_markdown and answer_plain. `answer` itself is not changed."""
    if not str(published.get("answer") or "").strip():
        return published
    text = plain(published["answer"])
    terms = pick(published)
    published["emphasis"] = terms
    published["answer_plain"] = text
    published["answer_markdown"] = markdown(text, terms)
    return published


__all__ = ["FLAG", "apply", "enabled", "markdown", "pick", "plain"]
