"""
POST /api/v1/credit/dedupe -- THE ONE DEDUPE API (owner, 2026-10-22: "one API for everything"), under the credit
underwriting agent, called from the FOS stage with the application form's dedupe block.

One call answers everything about who the applicant is and what they already hold:
  los_dedupe   the LOS's own dedupe: the supplied value searched across LOS cases, and this case's assessment (role
               swaps, file reuse, simultaneous applications)  -- app/agents/dedupe/service.py
  history      MIFIN match + every prospect / loan + LMS detail -- app/agents/dedupe/loan_history.py
  evaluation   the customer-history policy, rule by rule      -- app/agents/dedupe/history_policy.py
  outcome      READY_FOR_DECISION | REVIEW_REQUIRED | DATA_INSUFFICIENT (credit vocabulary; NEVER an approval),
               with reason codes and next actions from both the policy and the LOS dedupe
  explanation  the model's wording of the verified result, validated, or the deterministic text

AUTHENTICATED (require_jwt on the router) and SCOPED: los.dedupe.read or the credit underwriting scope, or the
service read scope; the case must be the caller's (one refusal whether or not it exists).

ISOLATION / MASKING: a matched case or customer the caller cannot open is REDACTED; no response, log or audit line
carries a PAN, mobile, name, DOB, full AP / PR code, account number or HMAC token; the searched value is never echoed.

STATUS CODES
  200  assessed (any outcome, DATA_INSUFFICIENT included)
  401  no authenticated caller
  403  missing scope, or the caller may not access this case
  404  a service principal named a case with no application record
  422  malformed body, an unknown dedupe parameter / customer type, or a value that is not a valid PAN / mobile /
       AP code / PR code
  500  the dedupe / customer-history policy is invalid (named in the log)
  503  the case store is unavailable
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.security.access import require_any_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credit", tags=["Credit Underwriting"])
SCOPE = "los.dedupe.read"


def _scopes():
    """los.dedupe.read, or the credit underwriting scope (app/agents/credit/config.required_scope)."""
    from app.agents.credit import config as credit_config

    return require_any_scope(SCOPE, credit_config.required_scope())


class DedupeRequest(BaseModel):
    """The FOS form's dedupe block for a case (Customer Type, Dedupe Parameter, Dedupe Parameter Value), plus what is
    being asked for (optional)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str = Field(..., min_length=1, max_length=128, examples=["CASE-3D51FFAC6342"])
    parameter: str = Field(..., min_length=1, max_length=40,
                           validation_alias=AliasChoices("dedupe_parameter", "parameter"),
                           description="The dropdown: PAN | Mobile No | AP Code (customer id) | PR Code (loan account "
                                       "number). Labels come from dedupe_policy.yaml parameters.*.labels.")
    value: str = Field(..., min_length=1, max_length=64,
                       validation_alias=AliasChoices("dedupe_parameter_value", "value"),
                       description="Never echoed back or logged.")
    customer_type: str | None = Field(None, max_length=20, description="New | Existing (the form's Customer Type).")
    party_role: str | None = Field(None, max_length=20, validation_alias=AliasChoices("party_role", "check_for"),
                                   description="Who the check is for: Applicant (default) | Co-Applicant "
                                               "(dedupe_policy.yaml party_roles).")
    confirm_match: Literal["SAME_CUSTOMER", "NOT_SAME_CUSTOMER"] | None = Field(
        None, description="The officer's decision on a POSSIBLE match (history.customer.confirmation.required): needs "
                          "`candidate` from that answer and a scope from dedupe_policy.yaml officer_confirmation.")
    candidate: str | None = Field(None, max_length=128,
                                  description="history.customer.confirmation.candidate of the answer being decided.")
    requested_product: str | None = Field(None, max_length=60)
    requested_amount: float | None = Field(None, gt=0, le=1_000_000_000, description="Rupees.")


def _visibility(claims: dict[str, Any]):
    """Whether this caller may open a matched case: a service principal sees every case; anyone else only the cases
    (or customers) they were granted."""
    from app.security import access
    from app.security.auth import get_scopes, get_subject
    from app.store import get_repository

    scopes, subject = set(get_scopes(claims)), get_subject(claims)
    if access.is_service(scopes, write=False):
        return lambda _cid: True
    repository = get_repository()

    def visible(case_id: str) -> bool:
        try:
            access.authorize(subject, scopes, case_id=case_id)
            return True
        except access.AccessDenied:
            return False

    return visible if subject and repository is not None else (lambda _cid: False)


@router.post("/dedupe",
             summary="Dedupe: LOS dedupe + MIFIN match + loan history + policy outcome + explanation (one call)",
             response_description="los_dedupe, history (masked, loan by loan), rule-wise evaluation, outcome "
                                  "(READY_FOR_DECISION | REVIEW_REQUIRED | DATA_INSUFFICIENT -- never an approval), "
                                  "reason codes, next actions, completeness, explanation, audit.")
async def dedupe(request: DedupeRequest, claims: dict[str, Any] = Depends(_scopes())) -> dict[str, Any]:
    from app.agents.dedupe import customer_history as history
    from app.agents.dedupe import service
    from app.core.exceptions import ConfigurationError
    from app.security import access
    from app.store import RepositoryError

    request_id = f"ddp_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=request.case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None
    except RepositoryError as exc:                  # the store is down during the access check: a 503, not a 500
        logger.warning("dedupe %s: access check: %r", request_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "CASE_STORE_UNAVAILABLE", "message": "The case store is unavailable.",
                                    "request_id": request_id}) from None
    if request.confirm_match is not None:
        from app.agents.dedupe import config as dedupe_config
        from app.security.auth import get_scopes

        allowed = set((dedupe_config.get_policy().get("officer_confirmation") or {}).get("scopes") or [])
        if not allowed & set(get_scopes(claims)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "CONFIRMATION_NOT_ALLOWED", "request_id": request_id,
                                        "message": "Your role may not confirm a customer match."})
    from app.agents.dedupe import match_decision
    from app.security.auth import get_subject

    try:
        # in a worker thread: MIFIN / LMS calls and the per-case advisory lock never block the event loop
        result = await asyncio.to_thread(
            history.assess, request.case_id, request.parameter, request.value, customer_type=request.customer_type,
            requested_product=request.requested_product, requested_amount=request.requested_amount,
            request_id=request_id, can_view_case=_visibility(claims), party_role=request.party_role,
            confirm_match=request.confirm_match, candidate=request.candidate, decided_by=get_subject(claims))
    except match_decision.NothingToConfirm as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": "NOTHING_TO_CONFIRM", "message": str(exc),
                                    "request_id": request_id}) from None
    except service.InvalidParameter as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "INVALID_PARAMETER_VALUE", "message": str(exc),
                                    "request_id": request_id}) from None
    except history.CaseNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "CASE_NOT_FOUND", "message": "No application record for this case.",
                                    "request_id": request_id}) from None
    except ConfigurationError as exc:
        logger.error("dedupe policy / config invalid: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail={"code": "POLICY_INVALID", "message": "The dedupe policy is invalid.",
                                    "request_id": request_id}) from None
    except RepositoryError as exc:
        logger.warning("dedupe %s: %r", request_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "CASE_STORE_UNAVAILABLE", "message": "The case store is unavailable.",
                                    "request_id": request_id}) from None
    if (result.get("explanation") or {}).get("llm_reason_pending"):
        from app.agents.dedupe import history_summary

        history_summary.remember(result, get_subject(claims))      # the page fetches the model's 3 points next
    return result


class DedupeAllRequest(BaseModel):
    """Dedupe on EVERY parameter at once (sir, 2026-10-24). Each value is optional: a PAN / mobile not entered is
    taken from the case record; a parameter with no value at all is reported as missing, never guessed."""
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1, max_length=128, examples=["CASE-3D51FFAC6342"])
    pan: str | None = Field(None, max_length=20, description="Never echoed back or logged.")
    mobile: str | None = Field(None, max_length=20, description="Never echoed back or logged.")
    ap_code: str | None = Field(None, max_length=20, description="Customer ID.")
    pr_code: str | None = Field(None, max_length=20, description="Loan account number.")
    customer_type: str | None = Field(None, max_length=20)
    party_role: str | None = Field(None, max_length=20, validation_alias=AliasChoices("party_role", "check_for"))
    requested_product: str | None = Field(None, max_length=60)
    requested_amount: float | None = Field(None, gt=0, le=1_000_000_000)


@router.post("/dedupe/all",
             summary="Dedupe on ALL parameters at once: PAN, mobile, customer ID, loan account -- side by side",
             response_description="rows (one per parameter, with its full answer), missing, consistency (do they all "
                                  "name the same MIFIN customer?), combined recommendation -- never an approval.")
async def dedupe_all(request: DedupeAllRequest, claims: dict[str, Any] = Depends(_scopes())) -> dict[str, Any]:
    from app.agents.dedupe import all_parameters
    from app.agents.dedupe import customer_history as history
    from app.agents.dedupe import service
    from app.core.exceptions import ConfigurationError
    from app.security import access
    from app.security.auth import get_subject
    from app.store import RepositoryError

    request_id = f"dda_{uuid.uuid4().hex}"
    try:
        access.authorize_claims(claims, case_id=request.case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None
    except RepositoryError as exc:
        logger.warning("dedupe-all %s: access check: %r", request_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "CASE_STORE_UNAVAILABLE", "message": "The case store is unavailable.",
                                    "request_id": request_id}) from None
    values = {"PAN": request.pan, "MOBILE": request.mobile, "AP_CODE": request.ap_code, "PR_CODE": request.pr_code}
    try:
        result = await asyncio.to_thread(
            all_parameters.assess_all, request.case_id, values, party_role=request.party_role, request_id=request_id,
            customer_type=request.customer_type, requested_product=request.requested_product,
            requested_amount=request.requested_amount, can_view_case=_visibility(claims),
            decided_by=get_subject(claims))
    except all_parameters.NothingToCheck as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "NOTHING_TO_CHECK", "message": str(exc), "request_id": request_id}) from None
    except service.InvalidParameter as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "INVALID_PARAMETER_VALUE", "message": str(exc),
                                    "request_id": request_id}) from None
    except history.CaseNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "CASE_NOT_FOUND", "message": "No application record for this case.",
                                    "request_id": request_id}) from None
    except ConfigurationError as exc:
        logger.error("dedupe policy / config invalid: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail={"code": "POLICY_INVALID", "message": "The dedupe policy is invalid.",
                                    "request_id": request_id}) from None
    except RepositoryError as exc:
        logger.warning("dedupe-all %s: %r", request_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "CASE_STORE_UNAVAILABLE", "message": "The case store is unavailable.",
                                    "request_id": request_id}) from None
    from app.agents.dedupe import history_summary

    for row in result["rows"]:                       # each row's model reason, fetched by the page like a single check
        if ((row.get("result") or {}).get("explanation") or {}).get("llm_reason_pending"):
            history_summary.remember(row["result"], get_subject(claims))
    return result


@router.get("/dedupe/{request_id}/reason",
            summary="The model's 3-point reason for one earlier dedupe check (validated; else the deterministic points)",
            response_description="points (exactly summary.reason_points), source LLM | DETERMINISTIC, fallback_reason")
async def dedupe_reason(request_id: str, claims: dict[str, Any] = Depends(_scopes())) -> dict[str, Any]:
    from app.agents.dedupe import history_summary
    from app.security.auth import get_subject

    try:
        # off the event loop: the local model may take tens of seconds on a CPU
        return await asyncio.to_thread(history_summary.reason, request_id, get_subject(claims))
    except history_summary.ReasonNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "REASON_NOT_FOUND", "request_id": request_id,
                                    "message": "No dedupe check with this id for you (it may have expired)."}) from None
