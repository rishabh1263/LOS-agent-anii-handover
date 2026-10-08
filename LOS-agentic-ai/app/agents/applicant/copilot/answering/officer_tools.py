"""
OFFICER TOOLS (FOS plan 9b; deterministic, CPU, language lock + professional format). One post-step per feature,
each behind its own flag (config chatbot.officer_tools.<feature>.enabled -- ON in dev; env overrides), all built on
the readiness report (readiness_report.py) -- the same source as the gate, so none of them can disagree with it:

  a. WHAT-IF (COPILOT_WHAT_IF)            "agar PAN upload karu toh ready ho jayega?" -> the FOS checks re-counted with
                                           that item assumed passed: yes / no, what else remains, the shortest path.
  b. REVIEW ON OPEN (COPILOT_AUTOPILOT_REVIEW)  opening a case runs every check and shows ONE review card.
  c. STATUS TABLES (COPILOT_STATUS_TABLES)      presentation.document_table + presentation.progress on case answers.
  d. CUSTOMER MESSAGE (COPILOT_CUSTOMER_MESSAGE) "customer ko bata do kya lana hai" -> a ready-to-send message in the
                                           selected language, exact document names; Copy + Edit; NEVER sent.
  f. VISIT CHECKLIST (COPILOT_VISIT_CHECKLIST)  "visit pe kya le jaun" -> a printable list per party.

The chatbot never moves a stage and never contacts anyone: a what-if "yes" still says a person confirms the move.
"""

from __future__ import annotations

import os
import re
from typing import Any

FLAGS = {"what_if": "COPILOT_WHAT_IF", "review": "COPILOT_AUTOPILOT_REVIEW", "tables": "COPILOT_STATUS_TABLES",
         "customer_message": "COPILOT_CUSTOMER_MESSAGE", "visit": "COPILOT_VISIT_CHECKLIST"}
_ON = {"1", "true", "yes", "on"}


def _cfg(feature: str | None = None) -> dict[str, Any]:
    from app.agents.applicant import config

    section = config.chatbot("officer_tools")
    return (section.get(feature) or {}) if feature else section


def enabled(feature: str) -> bool:
    value = os.getenv(FLAGS[feature])
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg(feature).get("enabled", False))


def _said(message: str) -> str:
    return " " + " ".join(re.findall(r"[\wऀ-ॿ]+", str(message or "").lower())) + " "


def _asks(feature: str, message: str) -> bool:
    said = _said(message)
    return any(f" {' '.join(str(p).lower().split())} " in said for p in _cfg(feature).get("phrases") or [])


def _label(feature: str, key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    value = (_cfg(feature).get("labels") or {}).get(key, key)
    return language_lock.pick(value).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _report(case_id: str) -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import readiness_report

    return readiness_report.build(case_id)


def _todo(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The counted items still to clear, fastest-unblock first."""
    items = {i["id"]: i for g in report.get("groups") or [] if g.get("counted") for i in g["items"]}
    return [items[i] for i in report.get("unblock_order") or [] if i in items]


# ---- a. what-if ------------------------------------------------------------------------------------------------
def _named_document(message: str) -> str | None:
    from app.agents.applicant.copilot.semantics import intents

    return intents._document_type(message)


def what_if(case_id: str, message: str) -> str | None:
    document = _named_document(message)
    if not document:
        return None
    report = _report(case_id)
    todo = _todo(report)
    slot_words = {document.upper()} | {s.upper() for s in (_cfg("what_if").get("slot_of") or {}).get(document.upper(), [])}
    fixed = [i for i in todo if any(w in i["id"].upper() for w in slot_words)]
    remaining = [i for i in todo if i not in fixed]
    label = _readable(document)
    if not fixed:
        return _label("what_if", "not_needed", document=label)
    if not remaining and not [g for g in report.get("groups") or [] if g["group"] == "OTHER"]:
        return _label("what_if", "yes", document=label)
    lines = [_label("what_if", "no", document=label, n=len(remaining))]
    lines += [f"{n}. {i['label']}: {i['fix'] or i['reason']}" for n, i in enumerate(remaining, 1) if i.get("fix") or i.get("reason")]
    return "\n".join(lines)


# ---- b. review card on open ------------------------------------------------------------------------------------
def review_card(case_id: str) -> dict[str, Any]:
    report = _report(case_id)
    todo = _todo(report)
    top = int(_cfg("review").get("max_items", 4))
    card = {"passed": report.get("passed", 0), "total": report.get("total", 0), "ready": report.get("ready", False),
            "issues": [{"label": i["label"], "status": i["status"], "fix": i.get("fix")} for i in todo[:top]],
            "more": max(0, len(todo) - top)}
    lines = [_label("review", "heading", passed=card["passed"], total=card["total"])]
    lines += [f"- {i['label']}: {i['status']}" + (f" -- {i['fix']}" if i.get("fix") else "") for i in card["issues"]]
    if card["more"]:
        lines.append(_label("review", "more", n=card["more"]))
    open_anywhere = [i for g in report.get("groups") or [] for i in g.get("items") or [] if i.get("status") != "PASS"]
    if not todo and open_anywhere:
        # FINAL FIX A1: an advisory item (e.g. KYC not run) is NOT passed -- never "every check passes" beside it
        lines += [f"- {i['label']}: {i['status']}" for i in open_anywhere[:top]]
    card["text"] = "\n".join(lines) if (todo or open_anywhere or not report.get("ready")) \
        else _label("review", "clear")
    return card


# ---- c. status tables ------------------------------------------------------------------------------------------
def document_table(case_id: str) -> list[dict[str, Any]]:
    from app.store import get_repository

    rows = []
    for d in get_repository().list_documents(case_id) or []:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper()
        if status == "SUPERSEDED":
            continue
        role = str(getattr(getattr(d, "party_role", None), "value", getattr(d, "party_role", None)) or "")
        rows.append({"party": "Co-applicant" if role == "CO_APPLICANT" else "Applicant",
                     "document": _readable(d.document_type), "document_type": str(d.document_type).upper(),
                     "status": status, "document_id": d.document_id})
    return rows


# ---- d. customer message ---------------------------------------------------------------------------------------
def customer_message(case_id: str) -> str | None:
    todo = [i for i in _todo(_report(case_id)) if i["id"].startswith(("DOC:", "SIG:"))]
    if not todo:
        return None
    redo = [i["label"] for i in todo if i["status"] in ("FAILED", "REVIEW")]
    bring = [i["label"] for i in todo if i["status"] == "PENDING"]
    parts = [_label("customer_message", "greeting")]
    if bring:
        parts.append(_label("customer_message", "bring", items=", ".join(bring)))
    if redo:
        parts.append(_label("customer_message", "redo", items=", ".join(redo)))
    parts.append(_label("customer_message", "closing"))
    return " ".join(p for p in parts if p)


# ---- f. visit checklist ----------------------------------------------------------------------------------------
def visit_checklist(case_id: str) -> str | None:
    todo = [i for i in _todo(_report(case_id)) if i["id"].startswith(("DOC:", "SIG:"))]
    if not todo:
        return None
    by_party: dict[str, list[str]] = {}
    for i in todo:
        party = "Co-applicant" if ":CO:" in i["id"] or ":CO_APPLICANT:" in i["id"] else "Applicant"
        action = _label("visit", "redo" if i["status"] in ("FAILED", "REVIEW") else "collect", document=i["label"])
        by_party.setdefault(party, []).append(action)
    lines = [_label("visit", "heading")]
    for party, items in by_party.items():
        lines += ["", f"**{party}**"] + [f"- [ ] {x}" for x in items]
    if _label("visit", "footer"):
        lines += ["", _label("visit", "footer")]
    return "\n".join(lines)


# ---- the one post-step both routes call ------------------------------------------------------------------------
def attach(published: dict[str, Any], message: str) -> dict[str, Any]:
    if not isinstance(published, dict) or not published.get("case_id"):
        return published
    from app.agents.applicant.copilot.answering.counts import keep_case_header

    case_id = published["case_id"]
    intent = str(published.get("intent") or "")

    def replace(text: str, new_intent: str, **extra: Any) -> None:
        published.update(intent=new_intent, answer=keep_case_header(published.get("answer"), text, case_id), **extra)
        published.pop("answer_markdown", None)
        published["clarification_required"] = None        # the replaced answer asked nothing

    if enabled("what_if") and _asks("what_if", message):
        text = what_if(case_id, message)
        if text:
            replace(text, "WHAT_IF")
    elif enabled("customer_message") and _asks("customer_message", message):
        text = customer_message(case_id) or _label("customer_message", "nothing")
        replace(_label("customer_message", "intro") + "\n> " + text, "CUSTOMER_MESSAGE_DRAFT",
                customer_message={"text": text, "sent": False})
        published["actions"] = [{"type": "COPY_TEXT", "label": _label("customer_message", "copy"), "text": text},
                                {"type": "EDIT_TEXT", "label": _label("customer_message", "edit"), "text": text}]
    elif enabled("visit") and _asks("visit", message):
        text = visit_checklist(case_id) or _label("visit", "nothing")
        replace(text, "VISIT_CHECKLIST", printable=True)
    if enabled("review") and intent == "CASE_OPENED" and not published.get("brief"):
        card = review_card(case_id)
        published["review_card"] = card
        published["answer"] = str(published.get("answer") or "").rstrip() + "\n\n" + card["text"]
    if enabled("tables") and published.get("intent") not in ("CASE_LIST", "CASE_SELECTION"):
        presentation = published.setdefault("presentation", {}) if isinstance(published.get("presentation"), dict) \
            else None
        if presentation is not None:
            report = _report(case_id)
            presentation["document_table"] = document_table(case_id)
            presentation.setdefault("progress", {"passed": report.get("passed", 0), "total": report.get("total", 0),
                                                 "ready": report.get("ready", False)})
    return published


__all__ = ["FLAGS", "attach", "customer_message", "document_table", "enabled", "review_card", "visit_checklist",
           "what_if"]
