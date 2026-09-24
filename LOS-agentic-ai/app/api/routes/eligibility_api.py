"""
The recorded eligibility verdict for one case, read-only.

WHY THIS IS A READ AND NOTHING ELSE. Eligibility is evaluated once, in the
LOS pipeline, by the stage that owns it, and recorded in case memory. This
route returns that record. It does not evaluate, re-evaluate or recompute
anything -- an endpoint that assessed affordability on demand would compute
it from whatever happened to be readable at the moment of the request, and
one applicant would have two verdicts.

ONE ANSWER, THREE DOORS. The LOS process response publishes the verdict
when it is produced; this route and the Universal Copilot read the same
recorded finding through the same `eligibility.get` capability. The three
cannot disagree because two of them are reads of what the first wrote.

AUTHORISATION IS THE CAPABILITY LAYER'S. The required scope comes from the
`eligibility.get` ToolContract (`permissions.check_tool`), ownership from
the store (`permissions.check_ownership`), and the read is audited the
same way a Copilot read is.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.agents.applicant import audit, permissions
from app.agents.applicant.permissions import Caller, PermissionDenied
from app.security.auth import require_jwt

router = APIRouter(prefix="/eligibility", tags=["Eligibility"])

_CAPABILITY = "eligibility.get"


@router.get(
    "/{case_id}",
    summary="The recorded eligibility verdict for a case",
    description=(
        "Returns the affordability verdict the LOS pipeline RECORDED for "
        "this case: status, reason codes, the figures behind them (income "
        "used and its source, obligations and their source, proposed EMI, "
        "FOIR and the limit it was read against) and the policy it was "
        "assessed under.\n\n"
        "**Read-only.** Nothing is computed here. `recorded: false` means "
        "eligibility has not been evaluated for this case yet -- run the "
        "case through `POST /api/v1/los/process`.\n\n"
        "**Not a lending decision.** A PASS says the configured "
        "affordability policy is satisfied on the evidence available. The "
        "shipped policy (`PL_DUMMY_V1`) is a demonstration policy, not "
        "company lending policy, and the result says so in "
        "`policy_status`."
    ),
)
async def get_eligibility(
    case_id: str,
    applicant_id: str = Query(
        ..., description="The applicant the case belongs to. Checked against the store."),
    claims: dict[str, Any] = Depends(require_jwt),
):
    from app.mcp import applicant as tools

    request_id = f"elig_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    try:
        permissions.check_tool(caller, _CAPABILITY)
        permissions.check_ownership(applicant_id, case_id)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="ELIGIBILITY", tools=[], status="DENIED",
                     detail=exc.code)
        # Phrased the same whether the case belongs to someone else or does
        # not exist: confirming which would itself be a disclosure.
        raise HTTPException(403, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc

    envelope = await tools.eligibility_get(case_id)

    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent="ELIGIBILITY", tools=[_CAPABILITY],
                 status="OK" if envelope.ok else "FAILED")

    if not envelope.ok:
        raise HTTPException(502, detail={
            "request_id": request_id,
            "error": envelope.error.code if envelope.error else "FAILED",
            "message": "The recorded eligibility result could not be read.",
        })

    return {
        "request_id": request_id,
        "case_id": case_id,
        "applicant_id": applicant_id,
        "recorded": envelope.result.get("recorded", False),
        "eligibility": envelope.result.get("eligibility"),
    }


__all__ = ["router"]
