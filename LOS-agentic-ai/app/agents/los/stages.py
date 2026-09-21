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

    @property
    def resolved(self) -> bool:
        return self.stage is not None

    def public(self) -> dict[str, str]:
        """What a response says about the stage. Omits what it does not know."""
        published = {"stage_resolution": self.resolution.value}
        if self.stage is not None:
            published["stage"] = self.stage.value
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

      1. the case timeline -- the pipeline recorded it, so it is fact
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

    # Most recent first: the latest recorded stage is the current one.
    for event in reversed(list(timeline)):
        stage = parse(getattr(event, "stage", None))
        if stage is not None:
            return StageContext(stage=stage,
                                resolution=Resolution.CASE_TIMELINE)

    status = _application_status(case_id)
    if status:
        stage = _STATUS_STAGE.get(str(status).strip().upper())
        if stage is not None:
            return StageContext(stage=stage,
                                resolution=Resolution.APPLICATION_STATUS)

    return None


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
