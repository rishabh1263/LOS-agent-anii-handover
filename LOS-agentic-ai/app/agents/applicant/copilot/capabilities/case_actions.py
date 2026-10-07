"""
6i CASE ACTIONS (COPILOT_CASE_ACTIONS, default off; docs/STEP6I_CASE_ACTIONS.md).

  VIEW_DOCUMENT    a short-lived signed link (5 min), scope-checked, audited; no path ever leaves
  RAISE QUERY      offer -> a DRAFT built from the case's actual reason (Send / Edit) -> created
                   through queries.raise_query ONLY after an explicit confirmation; never auto-sent
  SEND TO CUSTOMER read-only channel check: none exists here -> the query is recorded, a copyable
                   message + "mark as sent" are shown, "no customer channel" is logged
  TRACK QUERY      the case's queries: id, reason, status, days open, reply
  NEW CASE         an OPEN_UI_NEW_CASE button -- no case is created in chat

Phrases, labels, TTL and the channel are applicant_agent.yaml `chatbot.case_actions`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
FLAG = "COPILOT_CASE_ACTIONS"


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("case_actions") or {}


def _label(key: str, **values: Any) -> str:
    return str((_cfg().get("labels") or {}).get(key, key)).format(**values)


def asks(kind: str, message: str) -> bool:
    said = " " + " ".join(re.sub(r"[^\w\sऀ-ॿ]", " ", str(message or "").lower()).split()) + " "
    return any(f" {' '.join(str(p).lower().split())} " in said for p in (_cfg().get("phrases") or {}).get(kind, []))


def _base(request_id: str, case_id: str | None, intent: str, answer: str, **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "case_id": case_id, "intent": intent, "answer": answer,
            "category": "CASE_ONLY", "query_type": "ACTION_REQUEST", "response_source": "STRUCTURED",
            "documents": [], "errors": [], "tools_invoked": [], "suggested_questions": [], **extra}


def _subject(claims: dict[str, Any]) -> str:
    from app.security.auth import get_subject

    return str(get_subject(claims) or "")


def _authorize(claims: dict[str, Any], case_id: str) -> None:
    """The ordinary ownership check, then the conversation layer (raises AccessDenied -> 403)."""
    from app.security import access

    access.authorize_claims(claims, case_id=case_id)
    access.authorize_conversation(_subject(claims), access.get_scopes(claims), case_id=case_id)


def _audit(request_id: str, claims: dict[str, Any], case_id: str, intent: str, status: str, detail: str = "") -> None:
    from app.agents.applicant import audit

    audit.record(request_id=request_id, subject=_subject(claims), applicant_id=None, case_id=case_id,
                 intent=intent, tools=[], write=intent != "VIEW_DOCUMENT", status=status, detail=detail[:200])


# --------------------------------------------------------------------------
# 1. VIEW_DOCUMENT -- signed, short-lived, scope-checked; no path
# --------------------------------------------------------------------------

def _key() -> bytes:
    """Derived from the existing data-encryption key (no new secret): a link signed with it opens nothing else."""
    from app.store import crypto

    material = (os.getenv(crypto.ENV_KEY) or crypto._DEV_KEY).encode()
    return hashlib.sha256(b"los-view-document-v1:" + material).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign(case_id: str, document_id: str, subject: str, *, now: float | None = None) -> tuple[str, int]:
    ttl = int(_cfg().get("view_ttl_seconds", 300))
    payload = _b64(json.dumps({"c": case_id, "d": document_id, "s": subject,
                               "e": int((now or time.time()) + ttl)}, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{mac}", ttl


def verify(token: str, subject: str, *, now: float | None = None) -> dict[str, str] | None:
    """The token's case + document when it is genuine, unexpired and the SAME subject's; else None."""
    try:
        payload, mac = str(token or "").split(".", 1)
        good = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, mac):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001 - anything malformed is simply not a valid link
        return None
    if int(data.get("e", 0)) < int(now or time.time()) or data.get("s") != subject:
        return None
    return {"case_id": data["c"], "document_id": data["d"]}


def view_document(case_id: str, document_id: str, claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    from app.store import get_repository

    _authorize(claims, case_id)
    document = get_repository().get_document(document_id)
    if document is None or document.case_id != case_id:
        _audit(request_id, claims, case_id, "VIEW_DOCUMENT", "NOT_FOUND")
        return _base(request_id, case_id, "VIEW_DOCUMENT", "📄 That document is not on this case.")
    token, ttl = sign(case_id, document_id, _subject(claims))
    _audit(request_id, claims, case_id, "VIEW_DOCUMENT", "LINK_ISSUED", document.document_type)
    label = str(document.document_type or "Document").replace("_", " ").title()
    return _base(request_id, case_id, "VIEW_DOCUMENT", _label("view", document=label, minutes=max(1, ttl // 60)),
                 actions=[{"type": "OPEN_URL", "url": f"/api/v1/fos/documents/view?token={token}",
                           "expires_in": ttl}])


# --------------------------------------------------------------------------
# 2-3. RAISE QUERY (draft -> confirm) and SEND TO CUSTOMER
# --------------------------------------------------------------------------

def draft(case_id: str, claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    """A draft from the case's ACTUAL reason (step 4's view): a KYC mismatch first, then a rejected document."""
    from app.agents.applicant.copilot.answering import document_actions

    _authorize(claims, case_id)
    view = document_actions.build(case_id)
    target, text, payload = None, None, {}
    for issue in view.get("kyc_issues") or []:
        values = ", ".join(f"{v['label']} shows \"{v['value']}\"" for v in issue.get("values") or [] if v.get("value"))
        target = f"{issue.get('party_label', 'Applicant')} -- {issue.get('field', 'KYC')}"
        text = f"KYC mismatch on {issue.get('field', 'a field')}: {values}. Please share the correct document."
        payload = {"target_type": "CASE", "query_type": "DOCUMENT_DISCREPANCY", "related_field": issue.get("field")}
        break
    if text is None:
        for row in view.get("reupload") or []:
            reason = (row.get("reasons") or ["it did not pass verification"])[0]
            target = f"{row.get('party_label', 'Applicant')} -- {row.get('label')}"
            text = f"{row.get('label')} could not be verified: {reason}. Please upload a clear copy."
            payload = ({"target_type": "DOCUMENT", "query_type": "VERIFICATION_ISSUE", "target_id": row["document_id"]}
                       if row.get("document_id") else {"target_type": "CASE", "query_type": "VERIFICATION_ISSUE"})
            break
    if text is None:
        return _base(request_id, case_id, "RAISE_QUERY", _label("nothing_to_query"))
    send = {"type": "RAISE_QUERY", "case_id": case_id, "confirm": True, "query": {**payload, "text": text}}
    return _base(request_id, case_id, "RAISE_QUERY_DRAFT", _label("draft", target=target, text=text),
                 query_draft={**payload, "text": text},
                 actions=[{**send, "label": "Send"}, {**send, "confirm": False, "label": "Edit", "editable": True}])


def raise_confirmed(case_id: str, query: dict[str, Any], claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Created ONLY here, after the explicit Send. Then the customer channel (none here) or a copyable message."""
    from app.agents.los import queries
    from app.security import access

    _authorize(claims, case_id)
    text = str(query.get("text") or "").strip()
    created = queries.raise_query(case_id, target_type=str(query.get("target_type") or "CASE"),
                                  query_type=str(query.get("query_type") or "CLARIFICATION"), text=text,
                                  actor=_subject(claims), scopes=set(access.get_scopes(claims)),
                                  target_id=query.get("target_id"), party_id=query.get("party_id"),
                                  related_field=query.get("related_field"), request_id=request_id,
                                  idempotency_key=f"chat:{case_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}")
    query_id = created.get("query_id")
    answer = _label("sent", query_id=query_id)
    actions: list[dict[str, Any]] = []
    if not _cfg().get("customer_channel"):
        logger.warning("case_actions: no customer channel -- query %s on %s shown for manual sending", query_id, case_id)
        answer += "\n\n" + _label("no_channel", text=text)
        actions.append({"type": "MARK_QUERY_SENT", "case_id": case_id, "query_id": query_id, "label": "Mark as sent",
                        "copy_text": text})
    _audit(request_id, claims, case_id, "RAISE_QUERY", "CREATED", str(query_id))
    return _base(request_id, case_id, "RAISE_QUERY", answer, query=created, actions=actions)


def mark_sent(case_id: str, query_id: str, claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Recorded as an audit event; the query's lifecycle status is not changed (no SENT status exists)."""
    _authorize(claims, case_id)
    _audit(request_id, claims, case_id, "QUERY_SENT_TO_CUSTOMER", "MANUAL", str(query_id))
    return _base(request_id, case_id, "MARK_QUERY_SENT", _label("marked"))


# --------------------------------------------------------------------------
# 4-5. TRACK QUERY, NEW CASE
# --------------------------------------------------------------------------

def list_view(case_id: str, claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    from app.agents.los import queries

    _authorize(claims, case_id)
    rows = []
    for q in queries.list_queries(case_id):
        created = q.get("created_at")
        try:
            days = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).days if created else None
        except ValueError:
            days = None
        rows.append({"query_id": q.get("query_id"), "reason": q.get("text") or q.get("query_type"),
                     "status": q.get("status"), "days_open": days, "reply": q.get("response") or q.get("reply")})
    if not rows:
        return _base(request_id, case_id, "LIST_QUERIES", _label("no_queries"), queries=[])
    lines = [_label("queries_head")] + [
        f"- **{r['query_id']}** · {r['status']} · {r['days_open'] if r['days_open'] is not None else '?'}d · {r['reason']}"
        + (f" -> {r['reply']}" if r.get("reply") else "") for r in rows]
    return _base(request_id, case_id, "LIST_QUERIES", "\n".join(lines), queries=rows)


def new_case(request_id: str) -> dict[str, Any]:
    return _base(request_id, None, "NEW_CASE", _label("new_case"),
                 actions=[{"type": "OPEN_UI_NEW_CASE", "label": "New case"}])


__all__ = ["FLAG", "asks", "draft", "enabled", "list_view", "mark_sent", "new_case", "raise_confirmed", "sign",
           "verify", "view_document"]
