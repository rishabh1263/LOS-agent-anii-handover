"""
The Eligibility Agent, as the orchestrator sees it.

A THIN ENTRY POINT OVER A DETERMINISTIC ENGINE. Everything that decides
anything is in `engine.py`; this validates what it was handed and returns
what came back. There is no model on this path at any setting, no
retrieval and no store access -- the inputs arrive already gated by the
stage that produced them.

INDEPENDENTLY INVOCABLE, which is the point of registering it. A verdict
that can only be reproduced by running a whole document pipeline cannot
be audited, so this takes the same inputs the journey passes and can be
called on its own with them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agents.eligibility import config as policy_file
from app.agents.eligibility.engine import evaluate
from app.agents.eligibility.schemas import (
    EligibilityInputs,
    EligibilityResult,
    EligibilityStatus,
    ReasonCode,
)

logger = logging.getLogger(__name__)

AGENT_ID = "eligibility_agent"
VERSION = "1.0.0"


def assess(inputs: EligibilityInputs) -> EligibilityResult:
    """One application's affordability verdict, from validated inputs."""
    return evaluate(inputs)


def assess_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Assess from a plain mapping, for the orchestration handler.

    AN UNPARSEABLE PAYLOAD IS NOT A FAILED APPLICATION. Bad input here is
    a caller error, and it is reported as an assessment that could not be
    made rather than as an applicant who could not afford the loan.
    """
    started = time.perf_counter()

    try:
        inputs = EligibilityInputs(**(payload or {}))
    except Exception as exc:
        logger.warning("Eligibility inputs rejected: %r", exc)
        result = EligibilityResult(
            status=EligibilityStatus.SKIPPED,
            reason_codes=[ReasonCode.ELIGIBILITY_NOT_COMPARABLE],
            # No policy was consulted: the request never got that far.
            policy_status="NOT_CONSULTED",
            policy_provider=policy_file.provider_name(),
            basis=("The inputs supplied could not be read as an "
                   "affordability request, so nothing was assessed."),
        )
    else:
        result = assess(inputs)

    return {
        "agent": AGENT_ID,
        "version": VERSION,
        "eligibility": result.public(),
        "processing_ms": round((time.perf_counter() - started) * 1000, 2),
    }


__all__ = ["AGENT_ID", "VERSION", "assess", "assess_payload"]
