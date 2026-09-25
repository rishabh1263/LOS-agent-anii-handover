"""
Where a case sits in the LOS lifecycle.

SEVEN STAGES, AND THEY ARE THE WHOLE LIST. FOS hands to CPA, CPA to
CREDIT, and so on to DISBURSEMENT. This enum is the one place that
vocabulary is written down; before it, `stage` was a free string and the
only values anything ever produced were `DOCUMENT_COLLECTION` and
`UNDER_REVIEW` -- which are application STATUSES inside the FOS stage,
not stages.

UNRESOLVED IS NOT A STAGE, so it is not a member. A case is never "in
the UNRESOLVED stage"; we simply have not established which stage it is
in, and those are different claims. That absence is carried by
`StageContext.stage is None` with a `resolution` saying why, so a reader
can tell "we do not know" from "we know, and it is FOS".

THE STAGE IS READ, NEVER INFERRED. Not from the question the user asked
-- a CPA-sounding question does not put a case in CPA -- and not from a
stage the caller supplied when the case record says otherwise. A
frontend that can set the stage can read another stage's answers, which
would make stage scoping a suggestion rather than a boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class LosStage(str, Enum):
    """The seven stages of the LOS lifecycle, in order."""

    FOS = "FOS"
    CPA = "CPA"
    CREDIT = "CREDIT"
    RCU = "RCU"
    BOPS = "BOPS"
    HOPS = "HOPS"
    DISBURSEMENT = "DISBURSEMENT"


#: The lifecycle order, for a reader that needs to know what follows what.
#: Not used to advance anything -- nothing here moves a case between
#: stages, which is a workflow decision this module has no part in.
ORDER = (
    LosStage.FOS,
    LosStage.CPA,
    LosStage.CREDIT,
    LosStage.RCU,
    LosStage.BOPS,
    LosStage.HOPS,
    LosStage.DISBURSEMENT,
)


class Resolution(str, Enum):
    """How the stage on a request came to be known."""

    #: Read from the case's stage record -- the one the stage transition
    #: service (stage_lifecycle) writes. Authoritative; wins over the rest.
    STAGE_RECORD = "STAGE_RECORD"
    #: Read from the case's own timeline -- the pipeline wrote it.
    CASE_TIMELINE = "CASE_TIMELINE"
    #: Derived from the application's status, which is FOS-internal.
    APPLICATION_STATUS = "APPLICATION_STATUS"
    #: The caller said so, and no case record contradicted or confirmed it.
    CALLER_SUPPLIED = "CALLER_SUPPLIED"
    #: Nothing authoritative, and the caller offered nothing usable.
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class StageContext:
    """
    The stage a request is operating in, and where that came from.

    `stage is None` means UNRESOLVED. Callers must handle it rather than
    substituting a default: defaulting to FOS would silently answer every
    unresolvable case out of the FOS corpus.
    """

    stage: LosStage | None
    resolution: Resolution
    #: WHERE THE CASE IS WITHIN ITS STAGE -- a workflow status, kept apart
    #: from the application status, the document statuses and any decision.
    #: IN_PROGRESS, or READY_FOR_HANDOFF for a FOS case ready for CPA.
    status: str | None = None
    #: When the case entered its current stage, ISO 8601, when recorded.
    since: str | None = None
    #: Every stage the case has been through, oldest first:
    #: ({"stage", "started_at", "ended_at"}, ...). ended_at is None for the
    #: current one.
    history: tuple[dict[str, str | None], ...] = ()

    @property
    def resolved(self) -> bool:
        return self.stage is not None

    @property
    def hold_since(self) -> str | None:
        """
        From when a recorded decision counts as THIS stage's hold.

        Only after a transition. In the first stage the case has been in,
        every decision on it was made there -- including one recorded a
        moment before the timeline's first entry event, which is how a case
        and its opening event are written. None means "no cut-off".
        """
        return self.since if len(self.history) > 1 else None

    @property
    def source(self) -> str:
        """CASE_STATE when the case record decided it; else CALLER or NONE."""
        if self.resolution in (Resolution.STAGE_RECORD,
                               Resolution.CASE_TIMELINE,
                               Resolution.APPLICATION_STATUS):
            return "CASE_STATE"
        if self.resolution is Resolution.CALLER_SUPPLIED:
            return "CALLER"
        return "NONE"

    def public(self) -> dict[str, str]:
        """What a response says about the stage. Omits what it does not know."""
        published = {"stage_resolution": self.resolution.value,
                     "stage_source": self.source}
        if self.stage is not None:
            published["stage"] = self.stage.value
        if self.status:
            published["stage_status"] = self.status
        return published


UNRESOLVED = StageContext(stage=None, resolution=Resolution.UNRESOLVED)


def parse(value: object) -> LosStage | None:
    """One stage name, or None. Never raises on rubbish."""
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    try:
        return LosStage(text)
    except ValueError:
        return None


#: Application status -> stage. EVERY ONE IS FOS, because
#: `ApplicationStatus` documents itself as "where an application sits in
#: the FOS stage" -- the four values are steps WITHIN FOS, not stages.
#:
#: READY_FOR_CPA IS STILL FOS. The case is ready to be handed over, which
#: is not the same as having been handed over. Reporting CPA here would
#: claim a transition nobody recorded, and would route the question to a
#: desk that has not received the case.
_STATUS_STAGE = {
    "APPLICATION_CREATED": LosStage.FOS,
    "DOCUMENT_COLLECTION": LosStage.FOS,
    "BASIC_DOCUMENT_VERIFICATION": LosStage.FOS,
    "READY_FOR_CPA": LosStage.FOS,
    # Seen on responses today, and FOS-internal in the same way.
    "UNDER_REVIEW": LosStage.FOS,
}


def resolve(case_id: str | None, claimed: object = None) -> StageContext:
    """
    Which stage this case is in.

    PRECEDENCE, AND THE REASON FOR IT:

      0. the case's stage record -- written only by the stage transition
         service (stage_lifecycle), so it is the authoritative stage
      1. the case timeline -- a case never transitioned; today only the
         demo fixtures (demo_seed) write STAGE_ENTERED events here
      2. the application's status -- derived, but still the case's own
      3. what the caller said -- ONLY when the record is silent
      4. UNRESOLVED

    A caller-supplied stage never overrides 1 or 2. A frontend that could
    set the stage could choose which stage's answers it receives, which
    would turn stage scoping from a boundary into a preference. When the
    caller is the only source, the response says so through
    `resolution`, so nothing downstream mistakes it for the case record.

    Never raises: an unreachable store means UNRESOLVED, and the caller
    already has to handle that.
    """
    supplied = parse(claimed)

    if case_id:
        from_case = _from_case(case_id)
        if from_case is not None:
            if supplied is not None and supplied is not from_case.stage:
                # WORTH A LINE IN THE LOG. Either the frontend is stale or
                # it is asking for a stage it was not given, and both are
                # things somebody should see.
                logger.info(
                    "Stage claimed as %s but the case says %s; using the case.",
                    supplied.value, from_case.stage.value if from_case.stage else None,
                )
            return from_case

    if supplied is not None:
        return StageContext(stage=supplied, resolution=Resolution.CALLER_SUPPLIED)

    return UNRESOLVED


def _from_case(case_id: str) -> StageContext | None:
    """The stage the case record itself establishes, or None."""
    recorded = _from_stage_record(case_id)
    if recorded is not None:
        return recorded

    try:
        from app.agents.los import config as los_config

        if not los_config.case_memory_enabled():
            timeline = []
        else:
            from app.store import get_repository

            timeline = get_repository().get_case_timeline(case_id) or []
    except Exception as exc:
        logger.warning("Case timeline unavailable for %s: %r", case_id, exc)
        timeline = []

    # THE HISTORY, from the same timeline. Only events that name a stage
    # count -- a processing event records its outcome in that column
    # (PARTIAL, REVIEW ...), which `parse` rejects. A stage entered twice
    # in a row is one visit.
    history: list[dict[str, str | None]] = []
    for event in timeline:
        stage = parse(getattr(event, "stage", None))
        if stage is None:
            continue
        started = _iso(getattr(event, "created_at", None))
        if history and history[-1]["stage"] == stage.value:
            continue
        if history:
            history[-1]["ended_at"] = started
        history.append({"stage": stage.value, "started_at": started,
                        "ended_at": None})

    # The latest recorded stage is the current one.
    if history:
        current = parse(history[-1]["stage"])
        return StageContext(
            stage=current, resolution=Resolution.CASE_TIMELINE,
            status=_stage_status(current, case_id),
            since=history[-1]["started_at"], history=tuple(history))

    status = _application_status(case_id)
    if status:
        stage = _STATUS_STAGE.get(str(status).strip().upper())
        if stage is not None:
            return StageContext(
                stage=stage, resolution=Resolution.APPLICATION_STATUS,
                status=_stage_status(stage, case_id, status),
                history=({"stage": stage.value, "started_at": None,
                          "ended_at": None},))

    return None


def _from_stage_record(case_id: str) -> StageContext | None:
    """
    The stage the transition service recorded, with its history, or None
    for a case that has never been transitioned.

    ONE PRIMARY-KEY READ for the stage, one indexed read for the history;
    no timeline scan. Not gated on case memory: the stage is workflow
    state, not a memory feature.
    """
    try:
        from app.store import get_repository

        repository = get_repository()
        record = repository.get_case_stage(case_id)
        if record is None:
            return None
        transitions = repository.get_stage_transitions(case_id)
    except Exception as exc:
        logger.warning("Stage record unavailable for %s: %r", case_id, exc)
        return None

    current = parse(record.stage)
    if current is None:
        return None

    # THE HISTORY: every stage entered, with when, from where and why --
    # exactly as recorded. A reason nobody recorded stays None.
    entered = [t for t in transitions if t.kind == "STAGE_ENTERED"]
    history: list[dict[str, str | None]] = []
    if entered:
        first = entered[0]
        history.append({"stage": first.from_stage,
                        "started_at": _iso(first.previous_stage_started_at),
                        "ended_at": _iso(first.created_at),
                        "previous_stage": None, "reason": None,
                        "source": None})
    for index, t in enumerate(entered):
        ended = entered[index + 1].created_at if index + 1 < len(entered) else None
        history.append({"stage": t.to_stage, "started_at": _iso(t.created_at),
                        "ended_at": _iso(ended), "previous_stage": t.from_stage,
                        "reason": t.reason, "source": t.source})
    if not history:
        # Only status changes so far: the case is still in its first stage.
        history.append({"stage": current.value,
                        "started_at": _iso(record.stage_started_at),
                        "ended_at": None, "previous_stage": None,
                        "reason": None, "source": None})

    status = record.stage_status
    if status == "IN_PROGRESS":
        # Readiness the application itself records (READY_FOR_CPA) still
        # shows through -- the FOS handoff signal that already existed.
        status = _stage_status(current, case_id)
    return StageContext(stage=current, resolution=Resolution.STAGE_RECORD,
                        status=status, since=_iso(record.stage_started_at),
                        history=tuple(history))


def _iso(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def _stage_status(stage: LosStage | None, case_id: str,
                  application_status: object = None) -> str:
    """
    Where the case is WITHIN its stage. A workflow status, never a decision.

    READY_FOR_HANDOFF only where the record says so -- a FOS case whose
    application status is READY_FOR_CPA. Every other resolved stage is
    IN_PROGRESS: the timeline records entering a stage, and nothing in this
    build records completing one, so nothing else is claimed.
    """
    if stage is LosStage.FOS:
        status = application_status or _application_status(case_id)
        if str(status or "").upper() == "READY_FOR_CPA":
            return "READY_FOR_HANDOFF"
    return "IN_PROGRESS"


def _application_status(case_id: str) -> str | None:
    try:
        from app.store import get_repository

        application = get_repository().get_application(case_id)
    except Exception as exc:
        logger.warning("Application unavailable for %s: %r", case_id, exc)
        return None

    if application is None:
        return None

    status = getattr(application, "status", None)
    return getattr(status, "value", status)


__all__ = ["ORDER", "LosStage", "Resolution", "StageContext", "UNRESOLVED",
           "parse", "resolve"]
