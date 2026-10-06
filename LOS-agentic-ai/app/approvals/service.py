"""
MAKER / CHECKER -- one generic approval entity for configured high-risk actions.

    request(action, case, resource, maker, reason, payload)  -> PENDING_CHECK
    decide(approval, checker, APPROVE | REJECT | RETURN)     -> APPROVED (executes now)
                                                                / REJECTED / RETURNED
    cancel(approval, maker)                                   -> CANCELLED
    a request past its ttl                                    -> EXPIRED (on next touch)

Every rule is enforced HERE, on the server (config/maker_checker.yaml):
  maker != checker (also a CHECK constraint in the store); the checker holds
  the checker scope and passes case authorization again (the route); the
  checker re-reads authoritative state -- the evidence version captured at
  request time must still hold, else STALE_STATE and nothing executes; no
  model / agent / service identity may check; each change is a
  compare-and-set on the row's version, so two checkers cannot both decide.

An executor per action type performs the action ONLY on approval, as the
approved request describes it. An action type with no executor is refused at
request time -- never approved into nothing.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parents[1] / "config" / "maker_checker.yaml"
OPEN = "PENDING_CHECK"
FINAL = {"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}


class ApprovalError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    with open(_PATH, encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("maker_checker") or {}


def reload() -> None:
    _config.cache_clear()


def enabled() -> bool:
    raw = (os.getenv("MAKER_CHECKER_ENABLED") or "").strip().lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return bool(_config().get("enabled", False))


def requires_check(action_type: str) -> bool:
    rule = (_config().get("actions") or {}).get(action_type) or {}
    return enabled() and bool(rule.get("requires_check"))


def checker_scope() -> str:
    return str(_config().get("checker_scope") or "los.approvals.check")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# evidence versions and executors, per action type
# ---------------------------------------------------------------------------

def _stage_version(case_id: str, _resource_id: str) -> str:
    from app.store import get_repository

    row = get_repository().get_case_stage(case_id)
    stage = getattr(getattr(row, "stage", None), "value", getattr(row, "stage", None))
    return f"{stage}:{getattr(row, 'version', 0)}" if row else "FOS:0"


def _deviation_version(case_id: str, deviation_id: str) -> str:
    from app.agents.los import queries

    row = queries._get(case_id, "DEVIATION", deviation_id, None)
    return f"{row.status}:{getattr(row, 'version', 1)}"


def _run_stage_override(approval: dict[str, Any]) -> dict[str, Any]:
    from app.agents.los import stage_lifecycle

    p = approval["payload"]
    return stage_lifecycle.transition(
        approval["case_id"], p["target_stage"],
        reason=(f"{p['reason']} [four-eyes {approval['approval_id']}: made by {approval['maker_id']}, "
                f"checked by {approval['checker_id']}]")[:200],
        actor=approval["checker_id"], source="MAKER_CHECKER", expected_stage=p.get("expected_stage"),
        idempotency_key=f"mc-{approval['approval_id']}", request_id=f"mc_{approval['approval_id']}")


def _run_deviation(approval: dict[str, Any]) -> dict[str, Any]:
    from app.agents.los import queries

    p = approval["payload"]
    return queries.decide_deviation(
        approval["case_id"], approval["resource_id"], decision=p["decision"], actor=approval["maker_id"],
        scopes=set(p.get("maker_scopes") or []),
        justification=(f"{p.get('justification') or approval['reason']} "
                       f"[checked by {approval['checker_id']}]")[:500],
        request_id=f"mc_{approval['approval_id']}")


#: action type -> (evidence version of the resource, executor). Only these can
#: be requested; a configured type without an entry is refused.
REGISTRY: dict[str, tuple[Callable[[str, str], str], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "STAGE_OVERRIDE": (_stage_version, _run_stage_override),
    "DEVIATION_APPROVAL": (_deviation_version, _run_deviation),
}


# ---------------------------------------------------------------------------
# the lifecycle
# ---------------------------------------------------------------------------

def request(action_type: str, *, case_id: str, resource_id: str, maker_id: str, reason: str,
            payload: dict[str, Any], policy_reference: str | None = None) -> dict[str, Any]:
    from app.store import get_repository

    rule = (_config().get("actions") or {}).get(action_type)
    if not rule or action_type not in REGISTRY:
        raise ApprovalError("ACTION_NOT_CONTROLLABLE",
                            f"{action_type} has no maker/checker executor in this deployment.", 422)
    if not (reason or "").strip():
        raise ApprovalError("REASON_REQUIRED", "A controlled action must record its reason.", 422)
    if not maker_id:
        raise ApprovalError("MAKER_REQUIRED", "The request has no authenticated maker.", 401)
    version_of, _run = REGISTRY[action_type]
    now = _now()
    approval = {
        "approval_id": f"APR-{uuid.uuid4().hex[:12].upper()}", "case_id": case_id, "action_type": action_type,
        "resource_type": rule.get("resource_type") or "CASE", "resource_id": resource_id, "maker_id": maker_id,
        "checker_id": None, "status": OPEN, "reason": reason.strip()[:500], "comments": None,
        "payload": payload, "result": None, "policy_reference": policy_reference or "maker_checker.yaml",
        "evidence_version": version_of(case_id, resource_id), "version": 1,
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=float(_config().get("ttl_hours", 24)))).isoformat(),
    }
    get_repository().save_approval(approval)
    _audit(approval, "MAKER_CHECKER_REQUESTED", f"{action_type} requested by {maker_id}: {approval['reason']}")
    return approval


def _expire_if_due(approval: dict[str, Any]) -> dict[str, Any]:
    if approval["status"] == OPEN and datetime.fromisoformat(approval["expires_at"]) < _now():
        _change(approval, status="EXPIRED", comments="expired before it was checked")
    return approval


def _change(approval: dict[str, Any], **fields: Any) -> dict[str, Any]:
    from app.store import get_repository

    expected = approval["version"]
    updated = {**approval, **fields, "updated_at": _now().isoformat()}
    if not get_repository().update_approval(updated, expected_version=expected):
        raise ApprovalError("APPROVAL_CHANGED", "The request was changed by someone else; re-read it.")
    updated["version"] = expected + 1
    approval.update(updated)
    return approval


def get(approval_id: str) -> dict[str, Any]:
    from app.store import get_repository

    found = get_repository().get_approval(approval_id)
    if not found:
        raise ApprovalError("APPROVAL_NOT_FOUND", "No such approval request.", 404)
    return _expire_if_due(found)


def decide(approval_id: str, *, checker_id: str, checker_scopes: set[str], decision: str,
           comments: str | None = None) -> dict[str, Any]:
    approval = get(approval_id)
    decision = str(decision or "").strip().upper()
    if decision not in {"APPROVE", "REJECT", "RETURN"}:
        raise ApprovalError("INVALID_DECISION", "decision must be APPROVE, REJECT or RETURN.", 422)
    if approval["status"] != OPEN:
        raise ApprovalError("NOT_PENDING", f"The request is {approval['status']}, not pending a check.")
    if not checker_id:
        raise ApprovalError("CHECKER_REQUIRED", "The decision has no authenticated checker.", 401)
    if checker_id == approval["maker_id"]:
        raise ApprovalError("MAKER_CANNOT_CHECK", "The maker of a request cannot check it.", 403)
    if any(fnmatch.fnmatch(checker_id.lower(), p) for p in _config().get("forbidden_checker_subjects") or []):
        raise ApprovalError("CHECKER_NOT_HUMAN", "Models, agents and services cannot act as checker.", 403)
    if checker_scope() not in checker_scopes:
        raise ApprovalError("CHECKER_SCOPE_REQUIRED", f"Checking needs the {checker_scope()} permission.", 403)
    version_of, run = REGISTRY[approval["action_type"]]
    if decision == "APPROVE":
        # THE CHECKER RE-READS AUTHORITATIVE STATE: the case must be as the maker saw it
        current = version_of(approval["case_id"], approval["resource_id"])
        if current != approval["evidence_version"]:
            raise ApprovalError("STALE_STATE", "The case changed since this was requested "
                                f"({approval['evidence_version']} -> {current}); it must be requested again.")
        _change(approval, status="APPROVED", checker_id=checker_id, comments=comments)
        try:
            approval["result"] = run(approval)
            _change(approval, result=approval["result"])
        except Exception as exc:  # noqa: BLE001 - approved but the action refused: said, not hidden
            logger.warning("approved action failed %s: %r", approval_id, exc)
            approval["result"] = {"executed": False, "error": getattr(exc, "code", type(exc).__name__),
                                  "message": str(exc)[:300]}
            _change(approval, result=approval["result"])
        _audit(approval, "MAKER_CHECKER_APPROVED",
               f"{approval['action_type']} approved by {checker_id} (maker {approval['maker_id']})")
        return approval
    status = "REJECTED" if decision == "REJECT" else "RETURNED"
    _change(approval, status=status, checker_id=checker_id, comments=comments)
    _audit(approval, f"MAKER_CHECKER_{status}", f"{approval['action_type']} {status.lower()} by {checker_id}")
    return approval


def cancel(approval_id: str, *, maker_id: str) -> dict[str, Any]:
    approval = get(approval_id)
    if approval["maker_id"] != maker_id:
        raise ApprovalError("ONLY_MAKER_CAN_CANCEL", "Only the maker can cancel a request.", 403)
    if approval["status"] not in (OPEN, "RETURNED"):
        raise ApprovalError("NOT_CANCELLABLE", f"The request is {approval['status']}.")
    _change(approval, status="CANCELLED")
    _audit(approval, "MAKER_CHECKER_CANCELLED", f"{approval['action_type']} cancelled by its maker")
    return approval


def _audit(approval: dict[str, Any], event_type: str, summary: str) -> None:
    try:
        from app.store import get_repository
        from app.store.models import CaseEvent

        get_repository().record_event(CaseEvent(
            event_id=f"EV-{approval['approval_id']}-{event_type}-{approval['version']}", case_id=approval["case_id"],
            event_type=event_type, summary=summary[:400], ref_id=approval["approval_id"]))
    except Exception:  # noqa: BLE001 - the approval row is the record; the timeline is best effort
        logger.warning("approval audit event not recorded %s", approval.get("approval_id"))


def public(approval: dict[str, Any]) -> dict[str, Any]:
    keys = ("approval_id", "case_id", "action_type", "resource_type", "resource_id", "maker_id", "checker_id",
            "status", "reason", "comments", "policy_reference", "evidence_version", "created_at", "updated_at",
            "expires_at", "result")
    out = {k: approval.get(k) for k in keys}
    out["payload"] = {k: v for k, v in (approval.get("payload") or {}).items() if k != "maker_scopes"}
    return out
