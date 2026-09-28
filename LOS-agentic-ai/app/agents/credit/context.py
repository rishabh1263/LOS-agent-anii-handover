"""
build_context -- the security gate, then what the case IS.

GATE FIRST, BEFORE ANY TOOL RUNS. In order:
  1. a caller is required                              CALLER_REQUIRED
  2. the caller holds the configured scope (G5)        INSUFFICIENT_SCOPE
  3. the caller may access THIS case -- the existing
     ownership decision, app.security.access.authorize  CASE_NOT_ACCESSIBLE ...
  4. the case's AUTHORITATIVE stage (los.stages.resolve,
     never a caller-supplied value) is allowed (G5)     STAGE_NOT_ALLOWED

Then the application is read through the MCP runtime (application.get) with
the caller's identity, and the context is built from that record only.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit import config
from app.agents.credit.schemas import Party, UnderwritingContext


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def authorize(case_id: str, caller: Any) -> str:
    """Raise Refused unless this caller may underwrite this case now. Returns the stage."""
    from app.security import access

    if caller is None:
        raise Refused("CALLER_REQUIRED", "Underwriting requires an authenticated caller.")
    scopes = set(getattr(caller, "scopes", None) or ())
    if config.required_scope() not in scopes:
        raise Refused("INSUFFICIENT_SCOPE",
                      f"Underwriting requires the {config.required_scope()} scope.")
    try:
        access.authorize(getattr(caller, "subject", None), scopes, case_id=case_id)
    except access.AccessDenied as exc:
        raise Refused(exc.code, exc.message) from exc

    from app.agents.los import stages

    resolved = stages.resolve(case_id)          # no caller-supplied stage
    stage = getattr(getattr(resolved, "stage", None), "value", None)
    if not stage or stage.upper() not in config.allowed_stages():
        raise Refused("STAGE_NOT_ALLOWED",
                      f"Underwriting runs only in stage(s) {list(config.allowed_stages())}; "
                      f"this case is in {stage or 'an unresolved stage'}.")
    return stage.upper()


def bootstrap(case_id: str, stage: str, request_id: str,
              correlation_id: str | None) -> UnderwritingContext:
    """The context before the application is read: enough to call application.get."""
    return UnderwritingContext(case_id=case_id, applicant_id="", stage=stage,
                               request_id=request_id, correlation_id=correlation_id,
                               parties=[])


def from_application(application: dict[str, Any], *, case_id: str, stage: str,
                     request_id: str, correlation_id: str | None) -> UnderwritingContext:
    def text(key: str) -> str | None:
        value = application.get(key)
        return None if value in (None, "") else str(value)

    applicant_id = str(application.get("applicant_id") or "")
    parties = [Party(party_id=applicant_id, role="PRIMARY_APPLICANT")]
    co = text("co_applicant_id")
    if co and co != applicant_id:
        parties.append(Party(party_id=co, role="CO_APPLICANT"))
    return UnderwritingContext(
        case_id=case_id, applicant_id=applicant_id, stage=stage, request_id=request_id,
        correlation_id=correlation_id, parties=parties, product=text("product"),
        loan_amount=text("loan_amount"), tenure_months=text("tenure_months"),
        interest_rate_pct=text("interest_rate_pct"),
        employment_type=(text("employment_type") or "").upper() or None,
        declared_monthly_obligations=text("declared_monthly_obligations"),
        declared_monthly_income=text("declared_monthly_income"))


__all__ = ["Refused", "authorize", "bootstrap", "from_application"]
