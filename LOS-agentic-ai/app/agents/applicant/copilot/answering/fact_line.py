"""
THE NATURAL LINE FROM THE FACTS (owner 2026-10-15: "fewer model calls" -- fast AND accurate AND natural on a CPU).

A data answer (summary, pending, status, uploaded documents) opens with ONE conversational line built from the SAME
records its card was built from: `readiness.blocking_items`, `pending_items`, `documents`, `case_state`, the
applicant's name. No model, so ~0 ms instead of ~3 s, and no fact can be wrong -- every document named is one the
records name. The wording lives in config (copilot_reply.yaml polish.fact_lines), with a few variants per situation
so the chat does not repeat itself; the variant is chosen from the question, so the same question reads the same.

Explanations (why stuck, next step, process) keep the model's line; understanding unclear messages stays Qwen's.
Nothing here decides or changes a fact: when the records do not give a clear situation, no line (the card alone).
"""

from __future__ import annotations

import hashlib
from typing import Any

#: words kept in capitals when a slot code becomes a name ("ADDRESS_PROOF" -> "Address Proof", "PAN" -> "PAN")
_ACRONYMS = {"PAN", "ITR", "KYC", "GST", "DL", "LAP", "CPA", "EMI", "NOC", "PDC", "ID"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import polish

    return dict(polish._cfg().get("fact_lines") or {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def applies(intent: str) -> bool:
    return enabled() and intent in {str(i) for i in _cfg().get("intents") or []}


def _name(slot: Any) -> str:
    words = str(slot or "").replace("-", "_").split("_")
    return " ".join(w if w.upper() in _ACRONYMS and w.isupper() else w.capitalize() for w in words if w)


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _pick(variants: list[str], seed: str) -> str:
    if not variants:
        return ""
    index = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(variants)
    return str(variants[index])


def facts(reply: dict[str, Any]) -> dict[str, Any]:
    """The situation, from the reply's own records: pending document names, other blockers, done documents, ready."""
    readiness = reply.get("readiness") or {}
    blocking = list(readiness.get("blocking_items") or [])
    items = blocking or list(reply.get("pending_items") or [])
    pending: list[str] = []
    other: list[str] = []
    for item in items:
        if str(item.get("type") or "").upper() == "DOCUMENT" and item.get("slot"):
            name = _name(item["slot"])
            if name not in pending:
                pending.append(name)
        elif item.get("detail"):
            other.append(str(item["detail"]).rstrip("."))
    done, review = [], []
    for doc in reply.get("documents") or []:
        status = str(doc.get("status") or "").upper()
        name = _name(doc.get("document_type"))
        if status == "VERIFIED" and name not in done:
            done.append(name)
        elif status in ("IN_REVIEW", "UNDER_REVIEW", "PROCESSING", "UPLOADED") and name not in review:
            review.append(name)
    status = str(readiness.get("status") or (reply.get("case_state") or {}).get("readiness") or "").upper()
    full = str((reply.get("applicant") or {}).get("full_name") or "").strip()
    return {"pending": pending, "other": other, "done": done, "review": review,
            "ready": status == "READY", "known": bool(status), "first_name": full.split(" ")[0] if full else ""}


def act_of(question: str) -> str | None:
    """The learned answer act (semantics/answer_act.py) when the model is sure, else None (the default shape)."""
    try:
        from app.agents.applicant.copilot.semantics import answer_act

        return answer_act.sure_act(question)
    except Exception:  # noqa: BLE001
        return None


#: readiness answers shaped by the act: the act -> (template when only documents block, when other things block too,
#: when only other things block). The words are config (copilot_reply.yaml fact_lines.texts).
_READINESS_SHAPES = {
    "CONDITIONAL": ("conditional_only_documents", "conditional_with_blockers", "conditional_other_only"),
    "YES_NO": ("yes_no_pending", "yes_no_with_blockers", "yes_no_other"),
    "WHY_NOT": ("why_not_pending", "why_not_with_blockers", "why_not_other"),
    "WHY": ("why_not_pending", "why_not_with_blockers", "why_not_other"),
    "WHEN": ("when_pending", "when_with_blockers", "when_other"),
}


def line(reply: dict[str, Any], question: str = "") -> str | None:
    """One natural line for this data answer, or None (not a data intent / the records give no clear situation)."""
    intent = str(reply.get("intent") or "")
    if not applies(intent):
        return None
    f = facts(reply)
    texts = _cfg().get("texts") or {}
    seed = f"{intent}|{question.strip().lower()}"
    values = {"pending": _join(f["pending"]), "is_are": "is" if len(f["pending"]) == 1 else "are",
              "done": _join(f["done"]), "done_is_are": "is" if len(f["done"]) == 1 else "are",
              "review": _join(f["review"]), "review_is_are": "is" if len(f["review"]) == 1 else "are",
              "name": f["first_name"], "it_they": "it" if len(f["pending"]) == 1 else "they"}
    if intent == "DOCUMENTS_UPLOADED":
        if not f["done"] and not f["review"]:
            return None
        key = "uploaded_with_pending" if f["pending"] else "uploaded_all"
        if not f["done"]:
            key = "in_review_only"
    elif (intent == "READINESS" and not f["ready"] and (f["pending"] or f["other"])
          and (act := act_of(question)) in _READINESS_SHAPES):
        only_docs, with_other, other_only = _READINESS_SHAPES[act]
        if f["other"]:
            values["blockers"] = "; ".join(o[0].lower() + o[1:] if len(o) > 1 and o[1].islower() else o
                                           for o in f["other"])
            key = with_other if f["pending"] else other_only
        else:
            key = only_docs
    elif intent == "READINESS" and f["ready"] and act_of(question) in ("YES_NO", "CONDITIONAL", "WHEN"):
        key = "ready"
    elif (f["pending"] and intent in ("DOCUMENTS_PENDING", "DOCUMENTS_MISSING", "PENDING_ITEMS")
          and act_of(question) in ("WHY", "WHY_NOT")):
        # "why is it still pending?" asks for the REASONS. When no pending document was uploaded at all the reason is
        # that (said here, from the same records); otherwise the engine's own answer gives it per document
        # ("rejected: ...", "under review") and the list line would only repeat the names (conversation eval j2)
        uploaded = {_name(d.get("document_type")) for d in reply.get("documents") or []}
        if any(p in uploaded for p in f["pending"]):
            return None
        key = "why_not_uploaded"
        values["has_have"] = "has" if len(f["pending"]) == 1 else "have"
    elif f["pending"] and intent in ("DOCUMENTS_PENDING", "DOCUMENTS_MISSING", "PENDING_ITEMS", "READINESS") \
            and act_of(question) == "COUNT":
        # "how many are pending?" -- the number first, counted from the same records as the names
        key = "count_pending"
        values["count"] = str(len(f["pending"]))
        values["document_s"] = "document is" if len(f["pending"]) == 1 else "documents are"
    elif f["pending"]:
        key = "pending_named" if f["first_name"] and intent == "FULL_SUMMARY" else "pending"
    elif f["other"] and not f["ready"]:
        key = "other_blockers"
        first = f["other"][0]
        # lower-case the first letter only when it starts a plain word ("Application ..." -> "application ..."),
        # never an acronym ("KYC needs a reviewer" stays as it is)
        values["blockers"] = first[0].lower() + first[1:] if len(first) > 1 and first[1].islower() else first
    elif f["ready"]:
        key = "ready"
    else:
        return None                                   # no clear situation in the records: the card alone
    template = _pick(list(texts.get(key) or []), seed)
    if not template:
        return None
    try:
        text = template.format(**values)
    except (KeyError, IndexError):
        return None
    return text[0].upper() + text[1:] if text else None


__all__ = ["act_of", "applies", "enabled", "facts", "line"]
