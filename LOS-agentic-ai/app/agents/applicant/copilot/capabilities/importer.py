"""
EXCEL IMPORT (MASTER SPEC section 15.3; config product_flow.yaml `import`).

    VALIDATE    an .xlsx is read (openpyxl, read-only), its headers mapped to the form's fields (config synonyms),
                every row checked with the SAME rules as the form (case_form.check) and the abuse rule; errors are
                plain English, row by row. NOTHING IS WRITTEN.
    CONFIRM     the validated import waits `ttl_seconds` for the same caller's Confirm; then each valid row is
                created through the one create path (permissions, ownership grant, audit, activity log).

The pending import is kept in process, bound to the caller (one worker -- the open items say so).
"""

from __future__ import annotations

import io
import re
import threading
import time
import uuid
from typing import Any

_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.cfg().get("import") or {}


def _say(key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((_cfg().get("texts") or {}).get(key), lang, **values)


class ImportProblem(Exception):
    """The file as a whole cannot be imported (unreadable, too big, no known headers): one plain message."""


def _key(text: Any) -> str:
    return re.sub(r"[\s_]+", "", str(text or "").strip().lower())


def validate(content: bytes, subject: str, lang: str) -> dict[str, Any]:
    """{import_id, total, ok, bad, errors: [..], summary, confirm} -- nothing written."""
    from openpyxl import load_workbook

    from app.agents.applicant.copilot.capabilities import abuse_guard, case_form

    try:
        sheet = load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
        rows = list(sheet.iter_rows(values_only=True))
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one plain message
        raise ImportProblem(_say("unreadable", lang)) from exc
    if not rows:
        raise ImportProblem(_say("no_header", lang))
    synonyms = {_key(s): field for field, names in (_cfg().get("headers") or {}).items() for s in names or []}
    columns = {i: synonyms.get(_key(h)) for i, h in enumerate(rows[0])}
    if not any(columns.values()):
        raise ImportProblem(_say("no_header", lang))
    body = [r for r in rows[1:] if any(v not in (None, "") for v in r)]
    limit = int(_cfg().get("max_rows", 200))
    if len(body) > limit:
        raise ImportProblem(_say("too_many", lang, max=limit))
    good: list[dict[str, Any]] = []
    errors: list[str] = []
    for number, row in enumerate(body, start=2):              # the sheet's own row number (row 1 = headers)
        values: dict[str, Any] = {}
        problems: list[tuple[str, str]] = []
        for i, field in columns.items():
            if not field:
                continue
            raw = row[i] if i < len(row) else None
            raw = raw.date().isoformat() if hasattr(raw, "date") and field == "date_of_birth" else raw
            if isinstance(raw, str) and abuse_guard.detect(raw):
                problems.append((field, _say("abuse", lang)))
                continue
            value, why = case_form.check(field, raw, lang)
            if why:
                problems.append((field, why))
            elif value is not None:
                values[field] = value
        for field in case_form._cfg().get("required") or []:
            if field not in values and not any(f == field for f, _ in problems):
                problems.append((field, case_form._why("required", lang)))
        if problems:
            errors += [_say("row_error", lang, row=number, label=case_form.label(f, lang), why=w) for f, w in problems]
        else:
            good.append(values)
    import_id = f"imp_{uuid.uuid4().hex[:12]}"
    with _LOCK:
        _PENDING[import_id] = {"subject": subject, "rows": good,
                               "expires": time.time() + float(_cfg().get("ttl_seconds", 1800))}
    bad = len(body) - len(good)
    return {"import_id": import_id, "total": len(body), "ok": len(good), "bad": bad,
            "errors": [abuse_guard.mask_text(e) for e in errors],
            "summary": _say("summary", lang, ok=len(good), total=len(body), bad=bad),
            "confirm": _say("confirm_q", lang, ok=len(good)) if good else None}


def take(import_id: str, subject: str) -> list[dict[str, Any]] | None:
    """The validated rows of this caller's import (once), or None when unknown / expired / someone else's."""
    with _LOCK:
        found = _PENDING.get(import_id)
        if not found or found["subject"] != subject:
            return None
        _PENDING.pop(import_id, None)
        return found["rows"] if found["expires"] >= time.time() else None


def body_for(values: dict[str, Any]) -> dict[str, Any]:
    """One row as the create request (the form's applicant / application split)."""
    from app.agents.applicant.copilot.capabilities import case_form

    return {"applicant": {f: values[f] for f in case_form.APPLICANT_FIELDS if f in values},
            "application": {f: values[f] for f in case_form.APPLICATION_FIELDS if f in values}}


def chat_reply(content: bytes, subject: str, request_id: str) -> dict[str, Any]:
    """The chat reply to an uploaded sheet: the summary, the row errors, Confirm / Cancel. Nothing written."""
    from app.agents.applicant.copilot.answering import contract, language_lock

    lang = language_lock.current() or "en"
    base = {"request_id": request_id, "intent": "CASE_IMPORT", "case_id": None, "category": "CASE_ONLY",
            "query_type": "CONVERSATION", "response_source": "STRUCTURED", "documents": [], "actions": [],
            "errors": [], "tools_invoked": [], "suggested_questions": []}
    try:
        result = validate(content, subject, lang)
    except ImportProblem as exc:
        return {**base, "answer": str(exc)}
    lines = [result["summary"]] + [f"- {e}" for e in result["errors"][:20]]
    if result["confirm"]:
        lines += ["", result["confirm"], contract.link("confirm_write", lang, ref=result["import_id"]) + " · "
                  + contract.link("cancel_write", lang, ref=result["import_id"])]
    return {**base, "answer": "\n".join(lines)}


def reset() -> None:
    with _LOCK:
        _PENDING.clear()


__all__ = ["ImportProblem", "body_for", "reset", "take", "validate"]
