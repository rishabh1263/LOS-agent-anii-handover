"""
QUERIES AND DEVIATIONS -- one service, called by the frontend's Raise Query
button (POST /api/v1/los/cases/{case_id}/queries) and by the Copilot alike.

WHY ONE SERVICE. The chat and the button must not grow two ideas of what a
query is. Both build the same structured action (`raise_action`) and both
create through `raise_query`; the business vocabulary (query types, target
types, lifecycle, who may move what) is configuration (app/config/queries.yaml).

PERSISTENCE WITHOUT A NEW TABLE. A query is a case finding of kind QUERY, a
deviation one of kind DEVIATION, each keyed by its own id (`source_id`), so a
status change updates the same row (the findings store is idempotent on
case, kind, party, source and content hash). Every change also lands on the
case timeline as an event and in the service audit log.

KEPT APART: a QUERY being RESOLVED says the question was answered -- it is not
a stage PASS. A DEVIATION is an exception to a configured rule; none is raised
unless a rule is configured (none is: CONFIGURATION_GAP), and the person who
raised one can never approve it.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import uuid
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config" / "queries.yaml"


class QueryError(Exception):
    """A refused query / deviation operation, with its HTTP status and code."""

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}


def config(kind: str = "queries") -> dict[str, Any]:
    return dict(_load().get(kind) or {})


def reload() -> None:
    _load.cache_clear()


# ---- reading ---------------------------------------------------------------------------
def _kind(kind: str):
    from app.store.models import FindingKind

    return FindingKind(kind)


def _view(finding: Any) -> dict[str, Any]:
    payload = dict(getattr(finding, "payload", None) or {})
    return {
        "query_id" if payload.get("record") == "QUERY" else "deviation_id": finding.source_id,
        "status": finding.status,
        "stage": finding.stage,
        "party_id": finding.party_id,
        "document_id": finding.document_id,
        **{k: v for k, v in payload.items() if k != "record"},
        "created_at": finding.created_at.isoformat() if getattr(finding, "created_at", None) else None,
        "updated_at": finding.updated_at.isoformat() if getattr(finding, "updated_at", None) else None,
    }


def _rows(case_id: str, kind: str, repository: Any = None) -> list[Any]:
    from app.store import get_repository
    from app.store.repository import current_findings

    repository = repository or get_repository()
    return current_findings(repository.get_case_findings(case_id, kind=_kind(kind)))


def list_queries(case_id: str, *, repository: Any = None) -> list[dict[str, Any]]:
    return [_view(f) for f in _rows(case_id, "QUERY", repository)]


def list_deviations(case_id: str, *, repository: Any = None) -> list[dict[str, Any]]:
    return [_view(f) for f in _rows(case_id, "DEVIATION", repository)]


def _get(case_id: str, kind: str, item_id: str, repository: Any = None) -> Any:
    for f in _rows(case_id, kind, repository):
        if f.source_id == item_id:
            return f
    raise QueryError("NOT_FOUND", f"No such {kind.lower()} on this case.", 404)


def open_items(case_id: str, *, repository: Any = None) -> dict[str, list[dict[str, Any]]]:
    """Outstanding queries and pending deviations, as the gate and the Copilot read them."""
    q_open = set(config("queries").get("open_statuses") or [])
    d_open = set(config("deviations").get("open_statuses") or [])
    try:
        return {"queries": [q for q in list_queries(case_id, repository=repository) if q["status"] in q_open],
                "deviations": [d for d in list_deviations(case_id, repository=repository) if d["status"] in d_open]}
    except Exception:  # noqa: BLE001 - unreadable store: reported as nothing outstanding, logged
        logger.warning("open queries/deviations not readable case_id=%s", case_id)
        return {"queries": [], "deviations": []}


def deviation_rules_status() -> str:
    """CONFIGURATION_GAP while no deviation rule is configured."""
    return "CONFIGURED" if config("deviations").get("rules") else "CONFIGURATION_GAP"


# ---- the structured action the chat and the button share ---------------------------------
def raise_action(*, case_id: str, stage: str | None, target_type: str, target_id: str | None = None,
                 query_type: str = "CLARIFICATION", prefill: str = "", party_id: str | None = None,
                 evidence_refs: list[dict[str, Any]] | None = None, label: str | None = None,
                 target_stage: str | None = None, subject: str | None = None,
                 severity: str | None = None) -> dict[str, Any]:
    """
    The RAISE_QUERY action as the frontend renders it -- a button, never parsed
    from text. Posting `body` to `endpoint` creates the query.
    """
    severity = severity or str(config("queries").get("default_severity") or "MEDIUM")
    body = {"target_type": target_type, "target_id": target_id, "query_type": query_type,
            "text": prefill, "party_id": party_id, "evidence_refs": list(evidence_refs or []),
            "target_stage": target_stage, "subject": subject, "severity": severity}
    label = label or (f"Raise Query to {target_stage}" if target_stage else "Raise Query")
    return {"action_id": "RAISE_QUERY", "action": "RAISE_QUERY", "available": True, "label": label,
            "stage": stage, "source_stage": stage, "target_stage": target_stage, "severity": severity,
            "subject": subject, "target_type": target_type, "target_id": target_id, "query_type": query_type,
            "subject_type": target_type, "party_id": party_id, "prefill": prefill, "prefilled_text": prefill,
            "evidence_refs": list(evidence_refs or []), "requires_confirmation": True,
            "method": "POST", "endpoint": f"/api/v1/los/cases/{case_id}/queries",
            "body": {k: v for k, v in body.items() if v not in (None, [])}}


# ---- writing ---------------------------------------------------------------------------
def _held(scopes: set[str], wanted: list[str]) -> bool:
    from app.security.access import write_all_scope

    return bool(scopes & set(wanted)) or write_all_scope() in scopes


def _record(case_id: str, kind: str, item_id: str, *, status: str, stage: str | None, party_id: str | None,
            document_id: str | None, payload: dict[str, Any], event: str, actor: str, summary: str,
            request_id: str, repository: Any = None) -> None:
    from app.agents.applicant import audit
    from app.store import get_repository
    from app.store.models import CaseEvent, CaseFinding

    repository = repository or get_repository()
    repository.save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex, case_id=case_id, finding_kind=_kind(kind), party_id=party_id,
        stage=stage, status=status, payload={"record": kind, **payload}, source_type=kind,
        source_id=item_id, document_id=document_id))
    repository.record_event(CaseEvent(
        event_id=f"EV-{kind[:3]}-{uuid.uuid4().hex[:12]}", case_id=case_id, event_type=event,
        party_id=party_id, stage=stage, summary=summary[:400], ref_id=item_id))
    audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent=event,
                 tools=[f"los.{kind.lower()}"], write=True, confirmed=True, status="OK")


def _current_stage(case_id: str) -> str | None:
    try:
        from app.agents.los import stages

        current = stages.resolve(case_id).stage
        return str(getattr(current, "value", current)) if current is not None else None
    except Exception:  # noqa: BLE001
        return None


def raise_query(case_id: str, *, target_type: str, query_type: str, text: str, actor: str,
                scopes: set[str], target_id: str | None = None, party_id: str | None = None,
                evidence_refs: list[dict[str, Any]] | None = None, idempotency_key: str | None = None,
                request_id: str | None = None, repository: Any = None, target_stage: str | None = None,
                severity: str | None = None, subject: str | None = None,
                related_field: str | None = None) -> dict[str, Any]:
    """Validate, then create (or return the same open query for the same subject)."""
    cfg = config("queries")
    request_id = request_id or f"qry_{uuid.uuid4().hex}"
    if not _held(scopes, list(cfg.get("raise_scopes") or [])):
        raise QueryError("QUERY_NOT_PERMITTED", "Raising a query needs the query permission.", 403)
    target_type = str(target_type or "").strip().upper()
    query_type = str(query_type or "").strip().upper()
    text = " ".join(str(text or "").split())
    if target_type not in (cfg.get("target_types") or []):
        raise QueryError("INVALID_TARGET_TYPE", f"target_type must be one of {cfg.get('target_types')}.")
    if query_type not in (cfg.get("query_types") or {}):
        raise QueryError("INVALID_QUERY_TYPE", f"query_type must be one of {sorted(cfg.get('query_types') or {})}.")
    if not text:
        raise QueryError("QUERY_TEXT_REQUIRED", "A query must say what is being asked.")
    if len(text) > int(cfg.get("max_text_length") or 1000):
        raise QueryError("QUERY_TEXT_TOO_LONG", "The query text is too long.")

    severity = str(severity or cfg.get("default_severity") or "MEDIUM").strip().upper()
    if severity not in (cfg.get("severities") or [severity]):
        raise QueryError("INVALID_SEVERITY", f"severity must be one of {cfg.get('severities')}.")
    source_stage = _current_stage(case_id)
    target_stage = str(target_stage or "").strip().upper() or None
    if target_stage:
        allowed = [str(s).upper() for s in ((cfg.get("routes") or {}).get(source_stage or "") or [])]
        if target_stage not in allowed:
            raise QueryError("INVALID_TARGET_STAGE",
                             f"A query from {source_stage or 'this stage'} can be sent to: "
                             f"{', '.join(allowed) or 'no other stage'}.")

    from app.store import get_repository

    repository = repository or get_repository()
    document_id = None
    if target_type == "DOCUMENT":
        document = repository.get_document(str(target_id or "")) if target_id else None
        # THE TARGET MUST BE THIS CASE'S: never a way to probe another case's ids
        if document is None or document.case_id != case_id:
            raise QueryError("TARGET_NOT_FOUND", "That document is not on this case.", 404)
        document_id, party_id = document.document_id, party_id or document.party_id

    # IDEMPOTENT: a retry with the same key, or a second click while the same
    # question on the same subject is still open, returns the existing query
    open_statuses = set(cfg.get("open_statuses") or [])
    for existing in list_queries(case_id, repository=repository):
        same_key = idempotency_key and existing.get("idempotency_key") == idempotency_key
        same_open = (existing["status"] in open_statuses and existing.get("target_type") == target_type
                     and existing.get("target_id") == target_id and existing.get("query_type") == query_type
                     and existing.get("target_stage") == target_stage
                     and (existing.get("subject") or None) == (subject or None))
        if same_key or same_open:
            return {**existing, "result": "EXISTING"}

    query_id = "QRY-" + hashlib.sha1(f"{case_id}:{uuid.uuid4().hex}".encode()).hexdigest()[:10].upper()
    stage = source_stage
    payload = {"target_type": target_type, "target_id": target_id, "query_type": query_type, "text": text,
               "source_stage": source_stage, "target_stage": target_stage, "severity": severity,
               "subject": (subject or "")[:200] or None, "related_field": related_field,
               "raised_by": actor, "evidence_refs": list(evidence_refs or [])[:10],
               "idempotency_key": idempotency_key,
               "history": [{"status": "OPEN", "by": actor, "note": None}]}
    _record(case_id, "QUERY", query_id, status="OPEN", stage=stage, party_id=party_id, document_id=document_id,
            payload=payload, event="QUERY_RAISED", actor=actor, request_id=request_id, repository=repository,
            summary=f"Query {query_id} raised ({query_type} on {target_type}"
                    + (f", {source_stage} -> {target_stage}" if target_stage else "") + f"): {text}")
    return {**_view(_get(case_id, "QUERY", query_id, repository)), "result": "CREATED"}


def move_query(case_id: str, query_id: str, *, to_status: str, actor: str, scopes: set[str],
               note: str | None = None, request_id: str | None = None, repository: Any = None) -> dict[str, Any]:
    """Respond / resolve / reopen / cancel, as the configured lifecycle allows."""
    cfg = config("queries")
    request_id = request_id or f"qry_{uuid.uuid4().hex}"
    to_status = str(to_status or "").strip().upper()
    needed = cfg.get("resolve_scopes") if to_status == "RESOLVED" else cfg.get("raise_scopes")
    if not _held(scopes, list(needed or [])):
        raise QueryError("QUERY_NOT_PERMITTED", f"Moving a query to {to_status} needs the query permission.", 403)
    row = _get(case_id, "QUERY", query_id, repository)
    allowed = (cfg.get("lifecycle") or {}).get(row.status) or []
    if to_status == row.status:
        return {**_view(row), "result": "NO_CHANGE"}
    if to_status not in allowed:
        raise QueryError("INVALID_QUERY_TRANSITION", f"A {row.status} query cannot become {to_status}; "
                                                     f"allowed: {allowed or 'none'}.", 409)
    payload = {k: v for k, v in (row.payload or {}).items() if k != "record"}
    payload["history"] = list(payload.get("history") or []) + [
        {"status": to_status, "by": actor, "note": (note or "")[:500] or None}]
    # WHO ANSWERED, WHEN, AND HOW IT CLOSED -- the CPA reply is traceable
    if to_status == "RESPONDED":
        from datetime import datetime, timezone

        payload.update(response=(note or "")[:500] or None, responded_by=actor,
                       responded_at=datetime.now(timezone.utc).isoformat())
    if to_status == "RESOLVED":
        payload.update(resolution=(note or "")[:500] or None, resolved_by=actor)
    _record(case_id, "QUERY", query_id, status=to_status, stage=row.stage, party_id=row.party_id,
            document_id=row.document_id, payload=payload, event=f"QUERY_{to_status}", actor=actor,
            request_id=request_id, repository=repository,
            summary=f"Query {query_id} {row.status} -> {to_status}" + (f": {note}" if note else ""))
    return {**_view(_get(case_id, "QUERY", query_id, repository)), "result": "UPDATED"}


def decide_deviation(case_id: str, deviation_id: str, *, decision: str, actor: str, scopes: set[str],
                     justification: str | None = None, request_id: str | None = None,
                     repository: Any = None) -> dict[str, Any]:
    """APPROVED / REJECTED / WITHDRAWN -- by the configured authority, never by the raiser."""
    cfg = config("deviations")
    request_id = request_id or f"dev_{uuid.uuid4().hex}"
    decision = str(decision or "").strip().upper()
    row = _get(case_id, "DEVIATION", deviation_id, repository)
    if decision in ("APPROVED", "REJECTED"):
        if not set(scopes) & set(cfg.get("approve_scopes") or []):
            raise QueryError("DEVIATION_APPROVAL_NOT_PERMITTED",
                             "Only the configured approving authority can decide a deviation.", 403)
        if (row.payload or {}).get("raised_by") == actor:
            raise QueryError("SELF_APPROVAL_FORBIDDEN", "A deviation cannot be decided by the person who raised it.", 403)
        if not (justification or "").strip():
            raise QueryError("JUSTIFICATION_REQUIRED", "A deviation decision must record its justification.")
    allowed = (cfg.get("lifecycle") or {}).get(row.status) or []
    if decision not in allowed:
        raise QueryError("INVALID_DEVIATION_TRANSITION",
                         f"A {row.status} deviation cannot become {decision}; allowed: {allowed or 'none'}.", 409)
    payload = {k: v for k, v in (row.payload or {}).items() if k != "record"}
    payload["history"] = list(payload.get("history") or []) + [
        {"status": decision, "by": actor, "note": (justification or "")[:500] or None}]
    _record(case_id, "DEVIATION", deviation_id, status=decision, stage=row.stage, party_id=row.party_id,
            document_id=row.document_id, payload=payload, event=f"DEVIATION_{decision}", actor=actor,
            request_id=request_id, repository=repository,
            summary=f"Deviation {deviation_id} {row.status} -> {decision}")
    return {**_view(_get(case_id, "DEVIATION", deviation_id, repository)), "result": "UPDATED"}


def gate_checks(case_id: str, *, repository: Any = None) -> list[dict[str, Any]]:
    """
    Outstanding queries / pending deviations as gate checks, when configuration
    says they block a forward move. Same check shape as gates._check.
    """
    items = open_items(case_id, repository=repository)
    checks = []
    if config("queries").get("blocks_forward_move") and items["queries"]:
        checks.append({"id": "OPEN_QUERIES", "label": "Outstanding queries are answered and resolved",
                       "status": "BLOCKED", "reason_code": "OPEN_QUERY", "source": "queries",
                       "evidence": [{"query_id": q["query_id"], "status": q["status"],
                                     "query_type": q.get("query_type"), "target_type": q.get("target_type")}
                                    for q in items["queries"]]})
    if config("deviations").get("blocks_forward_move") and items["deviations"]:
        checks.append({"id": "PENDING_DEVIATIONS", "label": "Deviations are decided by the approving authority",
                       "status": "BLOCKED", "reason_code": "DEVIATION_PENDING_APPROVAL", "source": "deviations",
                       "evidence": [{"deviation_id": d["deviation_id"], "status": d["status"]}
                                    for d in items["deviations"]]})
    return checks


__all__ = ["QueryError", "config", "decide_deviation", "deviation_rules_status", "gate_checks",
           "list_deviations", "list_queries", "move_query", "open_items", "raise_action", "raise_query",
           "reload"]
