"""
THE CREDIT-STAGE MCP CAPABILITIES -- kept apart from the FOS read set (app/mcp/applicant.py), whose every tool declares
that it never answers credit assessment or transaction analysis. These are the credit stage's own reads.

    repayment.get   the repayment report: the vendor's verified bank analysis read with the case's eligibility data
                    under config/credit_underwriting/policy.yaml -- verdict, reasons, 5 rules, sections, suggestion
    legal_verification.get
                    the legal collateral verification of an application (app/agents/credit/legal_verification):
                    workflow status, agency, the latest recommendation, the ten rule results, missing evidence, the
                    next action, the CPA's decision. Scoped by the CALLER's own branches (its claims, below).

Thin, like every capability: validate the input, call the existing service, wrap the answer in a ToolEnvelope. The
report is an assessment for the credit officer; nothing here approves or rejects a loan.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any

from app.mcp.errors import NotFound, Unavailable
from app.mcp.schemas import ToolEnvelope

#: what a credit-stage tool must never be extended to answer
NEVER = ("loan approval or rejection", "sanction terms", "disbursal")

#: THE CALLER of the current tool call: its verified claims (the MCP server sets them after validating the token;
#: the in-process runtime from its authenticated caller). A tool that scopes by user / branch reads them here --
#: never from the tool's arguments.
CALLER_CLAIMS: "contextvars.ContextVar[dict[str, Any] | None]" = contextvars.ContextVar("credit_mcp_claims",
                                                                                       default=None)


@dataclass(frozen=True)
class CreditToolContract:
    name: str
    summary: str
    input_schema: dict[str, Any]
    provider: str
    writes: bool = False
    never: tuple[str, ...] = field(default_factory=lambda: NEVER)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.summary, "inputSchema": self.input_schema,
                "annotations": {"readOnlyHint": not self.writes}, "provider": self.provider}


CONTRACTS: dict[str, CreditToolContract] = {
    "repayment.get": CreditToolContract(
        name="repayment.get",
        summary=("Can the applicant repay the proposed EMI? The repayment report built from the vendor's verified "
                 "bank analysis and the case's eligibility data: verdict (RECOMMENDED / REFER / NOT_RECOMMENDED) "
                 "with reason points, the 5 policy rules, the obligation map, vendor-vs-our checks and the vendor's "
                 "own fields. `recorded: false` when no bank analysis has arrived. Not a credit decision."),
        input_schema={"type": "object", "properties": {"case_id": {"type": "string", "maxLength": 64}},
                      "required": ["case_id"], "additionalProperties": False},
        provider="credit_repayment",
    ),
    "legal_verification.get": CreditToolContract(
        name="legal_verification.get",
        summary=("The legal collateral verification of an application (case_id = the application id): workflow "
                 "status, the legal agency, the latest recommendation (CLEAR_FOR_REVIEW / REVIEW_REQUIRED / ESCALATE) "
                 "with key findings, missing evidence and the next action, the ten rule results (status + reason), "
                 "and the CPA's decision. `recorded: false` when no validation has run. Never declares a title clear."),
        input_schema={"type": "object", "properties": {"case_id": {"type": "string", "maxLength": 64}},
                      "required": ["case_id"], "additionalProperties": False},
        provider="credit_legal_verification",
    ),
}


async def repayment_get(case_id: str) -> ToolEnvelope:
    from app.agents.credit.repayment import service
    from app.mcp.applicant import _envelope, _require

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        try:
            report = await asyncio.to_thread(service.report_for, cid)
        except service.RepaymentError as exc:
            if exc.code in ("NO_BANK_ANALYSIS", "INVALID_CASE_ID"):
                # an id the bank-analysis store cannot hold has no analysis either: not received, not a failure
                return {"recorded": False, "report": None, "reason": exc.code}
            if exc.code == "CASE_NOT_FOUND":
                raise NotFound(exc.message, case_id=cid) from None
            raise Unavailable(exc.code, exc.message) from None
        return {"recorded": True, "report": report}

    return await _envelope("repayment.get", run)


async def legal_verification_get(case_id: str) -> ToolEnvelope:
    from app.agents.credit.legal_verification.services import cases
    from app.agents.credit.legal_verification.services.access import caller_from_claims
    from app.agents.credit.legal_verification.services.errors import ServiceError
    from app.mcp.applicant import _envelope, _require
    from app.mcp.errors import ToolError, ToolStatus

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        claims = CALLER_CLAIMS.get()
        if claims is None:
            raise ToolError(ToolStatus.FORBIDDEN, "UNAUTHENTICATED", "The caller of this tool is not known.")
        who = caller_from_claims(claims)
        try:
            ws = await asyncio.to_thread(cases.workspace, who, cid)
        except ServiceError as exc:
            if exc.status_code == 404:
                raise NotFound(exc.message, case_id=cid) from None
            if exc.status_code == 403:
                raise ToolError(ToolStatus.FORBIDDEN, exc.code, exc.message) from None
            raise Unavailable(exc.code, exc.message) from None
        run = ws["run"]
        case = ws["case"]
        out: dict[str, Any] = {
            "application_id": cid, "status": case["status"], "status_label": case["status_label"],
            "agency": case["agency_name"], "documents": [d["kind"] for d in ws["documents"]],
            "agency_report_received": ws["agency_report"] is not None,
            "decision": case["decision"], "decision_label": case["decision_label"], "recorded": run is not None}
        if run is not None:
            ov = run["overview"]
            out |= {"recommendation": ov["recommendation"], "recommendation_label": ov["recommendation_label"],
                    "counts": ov["counts"], "findings": ov["findings"], "missing_evidence": ov["missing"],
                    "next_action": ov["next_action_text"], "validated_at": run["run_at"],
                    "rules": [{"id": r["id"], "name": r["name"], "status": r["status"], "reason": r["reason"]}
                              for r in run["rules"]]}
        return out

    return await _envelope("legal_verification.get", run)


#: capability name -> coroutine (case-scoped reads only)
READ_TOOLS = {"repayment.get": repayment_get, "legal_verification.get": legal_verification_get}


def catalogue() -> list[dict[str, Any]]:
    return [CONTRACTS[name].public() for name in sorted(CONTRACTS)]


__all__ = ["CALLER_CLAIMS", "CONTRACTS", "NEVER", "READ_TOOLS", "catalogue", "legal_verification_get", "repayment_get"]
