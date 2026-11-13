"""
The Eligibility Agent, as the orchestrator sees it.

A THIN ENTRY POINT OVER A DETERMINISTIC ENGINE. The decision is `engine.evaluate`; the applicant data comes from the
configured DataProvider (providers.py) by case id -- never from a document. There is no model on this path at any
setting. A language model, where one explains a result elsewhere, explains it; it never decides it.

    python -m app.agents.eligibility.agent CASE-E11D00000001          # one case end to end (DATABASE_URL must reach the table)
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.calc.engine import evaluate
from app.agents.eligibility.data.providers import CaseNotFound, ProviderUnavailable, get_provider
from app.agents.eligibility.domain.models import EligibilityInput, EligibilityResult

logger = logging.getLogger(__name__)

AGENT_ID = "eligibility_agent"
VERSION = "2.0.0"


class ConfigNotApproved(RuntimeError):
    """The eligibility config is a demo and may not decide anything in this environment."""


#: the officer's pop-up blocks a check may carry (workbook "Calculations File.xlsx"), onto the case's data
_OVERRIDES = ("product", "requested_amount", "tenure_months", "rate_percent", "income_details", "topup_details",
              "collateral_type", "occupancy_type", "existing_exposure")


def assess(case_id: str, overrides: dict[str, Any] | None = None) -> EligibilityResult:
    """The decision for a case, from the configured provider's data (CaseNotFound / ProviderUnavailable otherwise),
    with the officer's pop-up figures (income working, top-up, collateral) applied when given."""
    if not config.usable():
        raise ConfigNotApproved(f"the eligibility config is {config.status()}, refused in this environment")
    inp = get_provider().get(case_id)
    extra = {k: v for k, v in (overrides or {}).items() if k in _OVERRIDES and v not in (None, {}, "")}
    if extra:
        from dataclasses import replace

        inp = replace(inp, **extra)
    return evaluate(inp)


def assess_input(inp: EligibilityInput) -> EligibilityResult:
    """The decision for data already in hand (tests, a caller that holds the record)."""
    if not config.usable():
        raise ConfigNotApproved(f"the eligibility config is {config.status()}, refused in this environment")
    return evaluate(inp)


def assess_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    The orchestration handler: {"case_id"} -> {"agent", "version", "eligibility", "error", "processing_ms"}.

    A case the provider does not have, a source that cannot be read, or a config refused here is reported as an
    `error`, never as a decision about the applicant.
    """
    started = time.perf_counter()
    case_id = str((payload or {}).get("case_id") or "").strip()
    eligibility, error = None, None
    if not case_id:
        error = "CASE_ID_REQUIRED"
    else:
        try:
            eligibility = assess(case_id, payload.get("overrides")).to_dict()
        except CaseNotFound:
            error = "CASE_NOT_FOUND"
        except ProviderUnavailable as exc:
            logger.warning("eligibility: data source unavailable (%s)", exc)
            error = "DATA_SOURCE_UNAVAILABLE"
        except ConfigNotApproved:
            error = "CONFIG_NOT_APPROVED"
    return {"agent": AGENT_ID, "version": VERSION, "eligibility": eligibility, "error": error,
            "processing_ms": round((time.perf_counter() - started) * 1000, 2)}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m app.agents.eligibility.agent <case_id>")
        return 2
    out = assess_payload({"case_id": argv[0]})
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["error"] is None else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    sys.exit(main(sys.argv[1:]))


__all__ = ["AGENT_ID", "VERSION", "ConfigNotApproved", "assess", "assess_input", "assess_payload"]
