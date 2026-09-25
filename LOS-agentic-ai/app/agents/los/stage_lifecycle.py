"""
THE ONE PLACE A CASE CHANGES STAGE.

`stages.resolve()` READS where a case is; this module is the only thing
that MOVES it. Before it, nothing in the runtime moved a case past FOS --
the only STAGE_ENTERED events were the ones `demo_seed` writes for its
fixtures, so a real case sat at FOS for ever and every later stage existed
only in demonstration data.

WHAT MAY CALL IT. A workflow operation, never a conversation:

    * the transition endpoint (POST /api/v1/los/cases/{case_id}/stage),
      behind a stage-write scope and the case-ownership check;
    * a future LOS / workflow-engine integration, through the same call.

The Copilot has no path here, and neither has the language model: no tool
contract, intent or confirmation reaches `transition`. A question that
sounds like "move my case to CPA" is answered, never acted on.

WHAT IT GUARANTEES.

    * ONE CURRENT STAGE. `case_stage` holds it; `stage_transitions` holds
      the append-only history that explains it; both are written in one
      SQLite transaction, with the same transition mirrored on the case
      timeline as a STAGE_ENTERED / STAGE_STATUS_CHANGED event.
    * VALIDATED. The target must be a configured edge from the current
      stage (app/config/stage_lifecycle.yaml). Backward moves are refused
      unless an edge says otherwise. A configured predicate, when set,
      must hold.
    * IDEMPOTENT. Asking for the stage the case is already in is a no-op
      that reports NO_CHANGE. A retried request carrying the same
      idempotency key reports the transition it already made.
    * NEVER OVERWRITES A NEWER STAGE. Writes are compare-and-set on a
      version; a caller that read a stage which has since moved on is
      told STALE_STAGE instead of winning.
    * AUDITABLE. Every transition records previous and new stage and
      status, actor, source, reason, request and correlation ids, time.

WHAT IT DOES NOT DECIDE. Whether a case OUGHT to move. No business rule
for a handoff is confirmed in this repository, so none is invented here:
the caller decides, and this module checks the move is a legal one.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agents.los import stages
from app.agents.los.stages import LosStage

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "stage_lifecycle.yaml"

STAGE_ENTERED = "STAGE_ENTERED"
STAGE_STATUS_CHANGED = "STAGE_STATUS_CHANGED"

IN_PROGRESS = "IN_PROGRESS"

#: How many times a transition re-reads and re-validates after losing a
#: compare-and-set race before reporting the conflict.
_ATTEMPTS = 3

_MAX_REASON = 200
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


# ==========================================================================
# CONFIGURATION
# ==========================================================================

@dataclass(frozen=True)
class LifecycleConfig:
    initial_stage: LosStage
    allow_backward: bool
    stage_statuses: tuple[str, ...]
    sources: tuple[str, ...]
    transition_scope: str
    next_stages: dict[LosStage, tuple[LosStage, ...]]
    requires_status: dict[LosStage, str | None]
    status: str

    def allowed_next(self, stage: LosStage | None) -> tuple[LosStage, ...]:
        return self.next_stages.get(stage, ()) if stage is not None else ()


@lru_cache(maxsize=1)
def config() -> LifecycleConfig:
    """The lifecycle configuration. Cached; `reload()` re-reads it."""
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # NO CONFIG, NO TRANSITIONS. An empty graph refuses every move,
        # which is the safe reading of a lifecycle nobody could load.
        logger.error("Stage lifecycle config unreadable: %r", exc)
        raw = {}

    next_stages: dict[LosStage, tuple[LosStage, ...]] = {}
    requires: dict[LosStage, str | None] = {}
    for name, entry in (raw.get("stages") or {}).items():
        stage = stages.parse(name)
        if stage is None:
            logger.warning("Stage lifecycle names an unknown stage %r; "
                           "ignored.", name)
            continue
        entry = entry or {}
        targets = tuple(s for s in (stages.parse(t) for t in
                                    entry.get("next") or ()) if s is not None)
        next_stages[stage] = targets
        wanted = entry.get("requires_stage_status")
        requires[stage] = str(wanted).upper() if wanted else None

    return LifecycleConfig(
        initial_stage=stages.parse(raw.get("initial_stage")) or LosStage.FOS,
        allow_backward=bool(raw.get("allow_backward", False)),
        stage_statuses=tuple(str(s).upper() for s in
                             raw.get("stage_statuses") or (IN_PROGRESS,)),
        sources=tuple(str(s).upper() for s in
                      raw.get("sources") or ("WORKFLOW",)),
        transition_scope=str(raw.get("transition_scope") or "los.stage:write"),
        next_stages=next_stages,
        requires_status=requires,
        status=str(raw.get("status") or "UNCONFIRMED"),
    )


def reload() -> None:
    config.cache_clear()


def transition_scope() -> str:
    return config().transition_scope


# ==========================================================================
# ERRORS
# ==========================================================================

class StageTransitionError(Exception):
    """A transition that was not made, and why. Nothing was written."""

    def __init__(self, code: str, message: str, http_status: int = 409,
                 **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail

    def public(self) -> dict[str, Any]:
        body = {"code": self.code, "message": self.message}
        body.update({k: v for k, v in self.detail.items() if v is not None})
        return body


# ==========================================================================
# CURRENT STATE
# ==========================================================================

@dataclass(frozen=True)
class _Current:
    stage: LosStage
    status: str
    since: datetime | None
    version: int


def _repository():
    from app.store import RepositoryError, get_repository

    try:
        return get_repository()
    except RepositoryError as exc:
        raise StageTransitionError("CASE_STORE_UNAVAILABLE", str(exc), 503) from exc


def _current(repository, case_id: str, application) -> _Current:
    """
    Where the case is now.

    The stage record when there is one. A case never transitioned has
    none: its stage is what `stages.resolve` has always derived for it
    (the timeline, then the application status), else the configured
    initial stage -- never a caller's claim.
    """
    record = repository.get_case_stage(case_id)
    if record is not None:
        stage = stages.parse(record.stage)
        if stage is not None:
            return _Current(stage, record.stage_status,
                            record.stage_started_at, record.version)

    derived = stages.resolve(case_id)
    stage = derived.stage or config().initial_stage
    since = _parse_time(derived.since) or getattr(application, "created_at", None)
    return _Current(stage, derived.status or IN_PROGRESS, since, 0)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ==========================================================================
# THE TRANSITION
# ==========================================================================

def transition(
    case_id: str,
    target_stage: object,
    *,
    reason: str,
    actor: str | None,
    source: str = "WORKFLOW",
    stage_status: str | None = None,
    expected_stage: object = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """
    Move `case_id` to `target_stage` (or, naming its current stage with a
    different `stage_status`, change where it is within the stage).

    Returns the case's stage state after the call and what happened
    (`result`: APPLIED, NO_CHANGE or REPLAYED). Raises
    StageTransitionError, having written nothing, for anything refused.
    AUTHORISATION IS THE CALLER'S: this is reached only after the route
    has checked scope and case ownership.
    """
    cfg = config()
    target = stages.parse(target_stage)
    if target is None or target not in cfg.next_stages:
        raise StageTransitionError(
            "UNKNOWN_STAGE", "The target stage is not a configured LOS stage.",
            422, target_stage=str(target_stage or "")[:40] or None)

    expected = None
    if expected_stage not in (None, ""):
        expected = stages.parse(expected_stage)
        if expected is None:
            raise StageTransitionError(
                "UNKNOWN_STAGE", "The expected stage is not an LOS stage.", 422)

    wanted_status = None
    if stage_status not in (None, ""):
        wanted_status = str(stage_status).strip().upper()
        if wanted_status not in cfg.stage_statuses:
            raise StageTransitionError(
                "INVALID_STAGE_STATUS",
                "The stage status is not one of the configured statuses.",
                422, allowed=list(cfg.stage_statuses))

    source = str(source or "").strip().upper()
    if source not in cfg.sources:
        raise StageTransitionError(
            "INVALID_SOURCE", "The transition source is not a configured "
            "source.", 422, allowed=list(cfg.sources))

    reason = " ".join(str(reason or "").split())
    if not reason:
        raise StageTransitionError(
            "REASON_REQUIRED", "A transition must record its reason.", 422)
    reason = reason[:_MAX_REASON]

    if idempotency_key is not None and not _KEY_RE.match(str(idempotency_key)):
        raise StageTransitionError(
            "INVALID_IDEMPOTENCY_KEY", "The idempotency key must be 1-64 "
            "letters, digits or . _ : -", 422)

    repository = _repository()
    application = repository.get_application(case_id)
    if application is None:
        raise StageTransitionError("CASE_NOT_FOUND",
                                   "No case with this id exists.", 404)

    transition_id = (_keyed_id(case_id, idempotency_key)
                     if idempotency_key else f"STG-{uuid.uuid4().hex}")

    for _attempt in range(_ATTEMPTS):
        if idempotency_key:
            replay = _replayed(repository, transition_id, target, wanted_status)
            if replay is not None:
                return _outcome(repository, case_id, "REPLAYED", replay)

        current = _current(repository, case_id, application)
        new_status = wanted_status or (
            current.status if target is current.stage else IN_PROGRESS)

        # IDEMPOTENT: already there, as asked.
        if target is current.stage and new_status == current.status:
            return _outcome(repository, case_id, "NO_CHANGE", None)

        # STALE: the caller acted on a stage the case has since left.
        if expected is not None and expected is not current.stage:
            raise StageTransitionError(
                "STALE_STAGE", "The case is no longer at the expected stage.",
                409, current_stage=current.stage.value,
                expected_stage=expected.value)

        kind = STAGE_STATUS_CHANGED if target is current.stage else STAGE_ENTERED
        if kind == STAGE_ENTERED:
            _check_edge(cfg, current, target)

        from app.store.models import CaseEvent, CaseStage, StageTransition, utcnow

        now = utcnow()
        record = StageTransition(
            transition_id=transition_id, case_id=case_id,
            version=current.version + 1, kind=kind,
            from_stage=current.stage.value, to_stage=target.value,
            from_status=current.status, to_status=new_status,
            previous_stage_started_at=current.since,
            source=source, actor=actor, reason=reason,
            request_id=request_id, correlation_id=correlation_id,
            created_at=now,
        )
        state = CaseStage(
            case_id=case_id, stage=target.value, stage_status=new_status,
            stage_started_at=(now if kind == STAGE_ENTERED
                              else current.since or now),
            updated_at=now, version=current.version + 1,
        )
        event = CaseEvent(
            event_id=f"EV-{transition_id}", case_id=case_id,
            event_type=kind, stage=target.value,
            summary=_summary(record), ref_id=transition_id, created_at=now,
        )

        try:
            written = repository.apply_stage_transition(
                current.version, state, record, event)
        except NotImplementedError as exc:
            raise StageTransitionError(
                "CASE_STORE_UNAVAILABLE",
                "This case store does not record stage transitions.",
                503) from exc
        except Exception as exc:
            logger.error("Stage transition write failed: %s",
                         type(exc).__name__)
            raise StageTransitionError(
                "CASE_STORE_UNAVAILABLE",
                "The stage transition could not be recorded.", 503) from exc

        if written:
            _audit(record, "APPLIED")
            return _outcome(repository, case_id, "APPLIED", record)
        # LOST A RACE. Somebody else moved the case between our read and
        # our write; nothing of ours was written. Re-read and re-judge --
        # their move may make ours a no-op, a legal next step, or illegal.

    raise StageTransitionError(
        "CONCURRENT_UPDATE",
        "The case's stage changed while this transition was being applied.",
        409)


def _check_edge(cfg: LifecycleConfig, current: _Current, target: LosStage) -> None:
    allowed = cfg.allowed_next(current.stage)
    if target not in allowed:
        backward = (target in stages.ORDER and current.stage in stages.ORDER
                    and stages.ORDER.index(target) < stages.ORDER.index(current.stage))
        raise StageTransitionError(
            "INVALID_TRANSITION",
            ("Moving a case back to an earlier stage is not configured."
             if backward else
             "This stage change is not an allowed transition."),
            409, current_stage=current.stage.value, target_stage=target.value,
            allowed_next=[s.value for s in allowed])

    required = cfg.requires_status.get(current.stage)
    if required and current.status != required:
        raise StageTransitionError(
            "TRANSITION_PRECONDITION_NOT_MET",
            f"The case must be {required} before it leaves this stage.",
            409, current_stage=current.stage.value,
            stage_status=current.status, required_stage_status=required)


def _keyed_id(case_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{case_id}\x00{key}".encode("utf-8")).hexdigest()
    return f"STK-{digest[:32]}"


def _replayed(repository, transition_id: str, target: LosStage,
              wanted_status: str | None):
    """The transition this idempotency key already made, or None."""
    previous = repository.get_stage_transition(transition_id)
    if previous is None:
        return None
    if previous.to_stage != target.value or (
            wanted_status and previous.to_status != wanted_status):
        raise StageTransitionError(
            "IDEMPOTENCY_KEY_REUSED",
            "This idempotency key was already used for a different "
            "transition.", 409)
    return previous


def _summary(record) -> str:
    if record.kind == STAGE_ENTERED:
        return f"Case entered {record.to_stage} from {record.from_stage}."
    return (f"Stage {record.to_stage} status changed from "
            f"{record.from_status} to {record.to_status}.")


def _audit(record, result: str) -> None:
    """
    One structured line per applied transition. The history row is the
    durable record; this is for log search. No token, and the free-text
    reason passes through the audit redactor.
    """
    try:
        from app.agents.applicant.audit import redact

        reason = redact(record.reason or "")
    except Exception:
        reason = "[unavailable]"
    logger.info(
        "stage_transition result=%s case_id=%s transition_id=%s kind=%s "
        "from=%s/%s to=%s/%s source=%s actor=%s request_id=%s "
        "correlation_id=%s reason=%r",
        result, record.case_id, record.transition_id, record.kind,
        record.from_stage, record.from_status, record.to_stage,
        record.to_status, record.source, record.actor, record.request_id,
        record.correlation_id, reason)


# ==========================================================================
# WHAT A CALLER IS TOLD
# ==========================================================================

def _iso(value: object) -> str | None:
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else (str(value) if value else None)


def transition_public(record) -> dict[str, Any] | None:
    """One transition, as an API returns it."""
    if record is None:
        return None
    return {
        "transition_id": record.transition_id,
        "event_type": record.kind,
        "stage": record.to_stage,
        "previous_stage": record.from_stage,
        "stage_status": record.to_status,
        "previous_stage_status": record.from_status,
        "timestamp": _iso(record.created_at),
        "source": record.source,
        "actor": record.actor,
        "reason": record.reason,
        "request_id": record.request_id,
        "correlation_id": record.correlation_id,
    }


def state(case_id: str) -> dict[str, Any]:
    """The case's stage state, as a frontend renders it."""
    context = stages.resolve(case_id)
    stage = context.stage
    return {
        "case_id": case_id,
        "stage": stage.value if stage is not None else None,
        "stage_status": context.status,
        "stage_since": context.since,
        "stage_resolution": context.resolution.value,
        "stage_source": context.source,
        "allowed_next": [s.value for s in config().allowed_next(stage)],
        "history": [dict(entry) for entry in context.history],
        "lifecycle_policy_status": config().status,
    }


def _outcome(repository, case_id: str, result: str, record) -> dict[str, Any]:
    published = state(case_id)
    published["result"] = result
    published["transition"] = transition_public(record)
    return published


__all__ = ["STAGE_ENTERED", "STAGE_STATUS_CHANGED", "LifecycleConfig",
           "StageTransitionError", "config", "reload", "state", "transition",
           "transition_public", "transition_scope"]
