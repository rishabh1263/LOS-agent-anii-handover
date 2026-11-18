"""
THE ELIGIBILITY API (2026-10-13): the decision for a case, from its STRUCTURED applicant data -- no documents.

    POST /api/v1/eligibility/check       {case_id} -> provider.get -> engine.evaluate -> saved run -> the decision
    GET  /api/v1/eligibility/runs/{id}   one saved check
    GET  /api/v1/eligibility/{case_id}   the decision RECORDED on a LOS case (read-only), with `stale` when the data moved

THE DATA COMES FROM THE DataProvider (app/agents/eligibility/providers.py, config `data_provider`): the Postgres
`eligibility_cases` table today, the LOS database once connected. The decision is the deterministic engine's
(engine.py, five config-driven rules); no model decides anything.

AUTHORISATION. /check and /runs need the `agents:execute` scope; every check is audited. GET /{case_id} reads through
the `eligibility.get` capability (its scope, ownership and audit).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from enum import Enum

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.applicant import audit, permissions
from app.agents.eligibility.domain.requests import (ID_PATTERN, MAX_RATE_PERCENT, MAX_RUPEES, MAX_TENURE_MONTHS, Collateral,
                                                    IncomeInput, Occupancy, TopupInput, upper_or_none)
from app.agents.applicant.permissions import Caller, PermissionDenied
from app.security.auth import require_jwt, require_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/eligibility", tags=["Eligibility"])

_CAPABILITY = "eligibility.get"

#: agent error -> (HTTP status, message): never a decision about the applicant
_ERRORS = {"CASE_ID_REQUIRED": (422, "Send a case_id."),
           "CASE_NOT_FOUND": (404, "No applicant data for this case."),
           "DATA_SOURCE_UNAVAILABLE": (503, "The eligibility data source cannot be read right now. Try again."),
           "CONFIG_NOT_APPROVED": (503, "The eligibility rules are not approved for this environment.")}


#: Swagger "Try it out" examples (2026-10-14): one clean body per scenario -- never every field of every block filled
#: with its maximum (a BT top-up carrying the internal top-up's fields is a 422)
_CHECK_EXAMPLES = {
    "case_only": {"summary": "Case data only",
                  "description": "Everything from the case record.",
                  "value": {"case_id": "CASE-E11D00000010"}},
    "itr_income": {"summary": "ITR income (annual)",
                   "description": "Income as per ITR - tax + other income, divided by 12 for FOIR.",
                   "value": {"case_id": "CASE-E11D00000010",
                             "income": {"method": "ITR", "income_as_per_itr": 1500000, "tax": 150000,
                                        "other_income": 60000}}},
    "salaried_income": {"summary": "Salaried income (monthly)",
                        "value": {"case_id": "CASE-E11D00000009",
                                  "income": {"method": "SALARIED", "gross_salary": 85000, "pf_deduction": 5100,
                                             "other_deduction": 2000, "other_income": 0}}},
    "cash_profit_income": {"summary": "Cash profit (business, annual)",
                           "value": {"case_id": "CASE-E11D00000012",
                                     "income": {"method": "CASH_PROFIT", "profit_after_tax": 1200000,
                                                "depreciation": 200000, "interest_to_partners": 100000,
                                                "remuneration_to_partners": 240000, "remuneration_to_directors": 0,
                                                "other_income": 0}}},
    "gross_profit_income": {"summary": "Gross profit (business, annual)",
                            "description": "Lower of 15% of turnover and the gross margin, plus rental income.",
                            "value": {"case_id": "CASE-E11D00000012",
                                      "income": {"method": "GROSS_PROFIT", "turnover": 12000000,
                                                 "gross_margin": 2100000, "rental_income": 240000}}},
    "bt_topup": {"summary": "Balance transfer + top-up",
                 "description": "BT_TOPUP takes ONLY pos (outstanding taken over) and mob (months on book).",
                 "value": {"case_id": "CASE-E11D00000011",
                           "income": {"method": "ITR", "income_as_per_itr": 900000, "tax": 60000, "other_income": 0},
                           "topup": {"type": "BT_TOPUP", "pos": 1200000, "mob": 30},
                           "collateral_type": "RESIDENTIAL", "occupancy_type": "SELF_OCCUPIED"}},
    "internal_topup": {"summary": "Internal top-up on our existing loan",
                       "description": "INTERNAL_TOPUP takes previous_sanction, pos, mob, original_tenor_months.",
                       "value": {"case_id": "CASE-E11D00000012",
                                 "topup": {"type": "INTERNAL_TOPUP", "previous_sanction": 1500000, "pos": 900000,
                                           "mob": 30, "original_tenor_months": 120},
                                 "collateral_type": "RESIDENTIAL", "occupancy_type": "RENTED"}},
    "existing_loan_on_property": {"summary": "LAP with another loan already on the property",
                                  "value": {"case_id": "CASE-E11D00000012", "existing_exposure": 500000,
                                            "collateral_type": "COMMERCIAL", "occupancy_type": "SELF_OCCUPIED"}},
    "other_product": {"summary": "Same case, another product",
                      "description": "The case is a HOME_LOAN; check it as a LOAN_AGAINST_PROPERTY.",
                      "value": {"case_id": "CASE-E11D00000011", "product": "LOAN_AGAINST_PROPERTY",
                                "collateral_type": "RESIDENTIAL", "occupancy_type": "SELF_OCCUPIED"}},
    "with_explanation": {"summary": "With a one-line explanation (local model)",
                         "value": {"case_id": "CASE-E11D00000010", "explain": True, "language": "en"}},
}


class PopupInputs(BaseModel):
    """THE OFFICER'S ENTRIES for a case (eligibility screen / Swagger): all optional; absent = the case's own data.
    The same model validates a check and a saved draft (PUT /cases/{case_id}/inputs), so both refuse the same
    mistakes with the same 422."""

    # SWAGGER SHOWS A REALISTIC PLACEHOLDER, never every field filled with its schema maximum (owner 2026-10-14)
    model_config = ConfigDict(json_schema_extra={"example": {
        "requested_amount": 3000000, "tenure_months": 240, "rate_percent": 9.5,
        "income": {"method": "ITR", "income_as_per_itr": 1500000, "tax": 150000, "other_income": 0},
        "collateral_type": "RESIDENTIAL", "occupancy_type": "SELF_OCCUPIED"}})

    # editable base figures (workbook: "Tenor" / "Eligibility ROI %" editable; existing EMIs are NOT -- bureau / case)
    requested_amount: float | None = Field(None, gt=0, le=MAX_RUPEES, allow_inf_nan=False,
                                           description="Loan amount to check (Rs); absent = the case's.")
    tenure_months: int | None = Field(None, ge=1, le=MAX_TENURE_MONTHS, description="Tenure in months; absent = the case's.")
    rate_percent: float | None = Field(None, gt=0, le=MAX_RATE_PERCENT, allow_inf_nan=False,
                                       description="Rate % a year; absent = the case's, else the product's.")
    product: str | None = Field(None, max_length=48, description=(
        "Check the case for ANOTHER product (e.g. LOAN_AGAINST_PROPERTY instead of the case's HOME_LOAN). Absent = "
        "the case's own product. One of the configured products (eligibility_config.yaml `products`), else 422."))
    # THE OFFICER'S POP-UP (business "Calculations File.xlsx", 2026-10-14) -- all optional; absent = the case's data
    income: IncomeInput | None = Field(None, description=(
        "Income working: {\"method\": SALARIED | ITR | CASH_PROFIT | GROSS_PROFIT, ...fields}. SALARIED: gross_salary, "
        "other_income, other_deduction, pf_deduction (monthly). ITR: income_as_per_itr, tax, other_income (annual). "
        "CASH_PROFIT: profit_after_tax, depreciation, interest_to_partners, remuneration_to_partners, "
        "remuneration_to_directors, other_income (annual). GROSS_PROFIT: turnover, gross_margin, rental_income "
        "(annual). The computed MONTHLY income replaces the declared one. A field of another method, a negative or "
        "non-numeric figure, or a missing required figure -> 422."),
)
    topup: TopupInput | None = Field(None, description=(
        "Top-up: {\"type\": BT_TOPUP, pos, mob} or {\"type\": INTERNAL_TOPUP, previous_sanction, pos, mob, "
        "original_tenor_months}. Caps the loan (limit TOPUP); an internal top-up's outstanding counts in LTV."))
    collateral_type: Collateral | None = Field(None, description="RESIDENTIAL / COMMERCIAL / INDUSTRIAL")
    occupancy_type: Occupancy | None = Field(None, description="SELF_OCCUPIED / RENTED / VACANT")
    existing_exposure: float | None = Field(None, ge=0, le=MAX_RUPEES, allow_inf_nan=False,
                                            description="Other loans already on the same property (Rs)")

    @field_validator("collateral_type", "occupancy_type", mode="before")
    @classmethod
    def _upper(cls, value):
        return upper_or_none(value)

    @field_validator("product", mode="before")
    @classmethod
    def _configured_product(cls, value):
        from app.agents.eligibility import config as _elig_config

        product = upper_or_none(value)
        known = sorted(_elig_config.products())
        if product is not None and product not in known:
            raise ValueError(f"unknown product {product}; configured: {', '.join(known)}")
        return product

    def overrides(self) -> dict[str, Any]:
        """The entries as engine overrides (only what was entered)."""
        out = {"product": self.product, "requested_amount": self.requested_amount,
               "tenure_months": self.tenure_months, "rate_percent": self.rate_percent,
               "income_details": self.income.model_dump(exclude_none=True) if self.income else None,
               "topup_details": self.topup.model_dump(exclude_none=True) if self.topup else None,
               "collateral_type": self.collateral_type, "occupancy_type": self.occupancy_type,
               "existing_exposure": self.existing_exposure}
        return {k: v for k, v in out.items() if v is not None}

    def entered(self) -> bool:
        return bool(self.overrides())


#: Swagger "Try it out" bodies for SAVE (PUT /cases/{case_id}/inputs) -- one clean body per situation
_SAVE_EXAMPLES = {
    "itr_income": {"summary": "ITR income + loan figures",
                   "value": {"requested_amount": 3000000, "tenure_months": 240, "rate_percent": 9.5,
                             "income": {"method": "ITR", "income_as_per_itr": 1500000, "tax": 150000}}},
    "salaried": {"summary": "Salaried income",
                 "value": {"income": {"method": "SALARIED", "gross_salary": 85000, "pf_deduction": 5100,
                                      "other_deduction": 2000}}},
    "bt_topup": {"summary": "Balance transfer + top-up (pos, mob only)",
                 "value": {"topup": {"type": "BT_TOPUP", "pos": 1200000, "mob": 30},
                           "collateral_type": "RESIDENTIAL", "occupancy_type": "SELF_OCCUPIED"}},
    "internal_topup": {"summary": "Internal top-up (4 fields)",
                       "value": {"topup": {"type": "INTERNAL_TOPUP", "previous_sanction": 1500000, "pos": 900000,
                                           "mob": 30, "original_tenor_months": 120}}},
    "property": {"summary": "Property: collateral, occupancy, other loans on it",
                 "value": {"collateral_type": "COMMERCIAL", "occupancy_type": "RENTED", "existing_exposure": 500000}},
}


class CheckRequest(PopupInputs):
    model_config = ConfigDict(json_schema_extra={"example": {"case_id": "CASE-E11D00000010"}})

    case_id: str = Field(..., min_length=1, max_length=64, pattern=ID_PATTERN, examples=["CASE-E11D00000011"])
    detail: bool = Field(False, description="The FULL working (loan, ratios, rules, income lines, exposure, "
                                            "reasons); default: the compact answer -- decision, summary, offer, "
                                            "checks, what it would take, next steps, one-line working.")
    use_saved: bool = Field(True, description="When no entries are sent, use the entries SAVED for the case "
                                              "(eligibility screen: Save, then Trigger eligibility).")
    explain: bool = Field(False, description="Add a one-or-two-sentence `explanation` written by the local model "
                                            "(never changes the decision; absent when the model is unavailable).")
    language: str = Field("en", pattern="^(en|hi)$", description="The explanation's language: en / hi (Hinglish).")


async def _evaluated(body: CheckRequest) -> dict[str, Any]:
    """The case evaluated exactly as /check does (entries, else the saved ones) -- the final response shape, with NO
    run saved and nothing audited: for the summary only."""
    from app.agents.eligibility.output import response as _response
    from app.agents.eligibility.agent import assess_payload

    case_id = body.case_id.strip()
    overrides = body.overrides()
    if not overrides and body.use_saved:
        from app.agents.eligibility.data import inputs_store as _saved

        try:
            overrides = await asyncio.to_thread(_saved.overrides_for, case_id)
        except _saved.InputsUnavailable:
            overrides = {}
    out = await asyncio.to_thread(assess_payload, {"case_id": case_id, "overrides": overrides})
    if out.get("error"):
        status, message = _ERRORS.get(out["error"], (500, "Eligibility could not be checked."))
        raise HTTPException(status, detail={"error": out["error"], "message": message})
    return _response.build(out["eligibility"], None)


@router.post(
    "/explain/stream",
    summary="The AI summary as it is written: NDJSON lines {\"delta\": words} ... then {\"final\": text | null}",
    description=(
        "The same body as /check; no run saved. The words stream while the local model writes (first words in about "
        "a second on CPU); the LAST line is the checked sentence -- `final: null` means the text failed the figure "
        "check or the model is unavailable, and the screen withdraws what it showed."),
)
async def explain_stream(body: CheckRequest, claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    import json
    import queue
    import threading

    from fastapi.responses import StreamingResponse

    from app.agents.eligibility.output import summary as _explain

    response = await _evaluated(body)
    lines: queue.Queue = queue.Queue()

    def produce():                                   # the model call blocks: a thread feeds the response
        try:
            for kind, value in _explain.stream(response, body.language):
                lines.put(json.dumps({kind: value}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - never a broken stream: the screen gets its final line
            lines.put(json.dumps({"final": None}) + "\n")
        finally:
            lines.put(None)

    threading.Thread(target=produce, name="eligibility-explain-stream", daemon=True).start()

    async def body_lines():
        while True:
            line = await asyncio.to_thread(lines.get)
            if line is None:
                return
            yield line

    return StreamingResponse(body_lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post(
    "/explain",
    summary="The AI summary of a check, on its own: the same body as /check -> {explanation} (no run saved)",
    description=(
        "For the eligibility screen: the decision comes from /check at once, the summary from here alongside it. The "
        "case is evaluated exactly as /check does (same body, same engine), but NO run is saved and nothing is "
        "audited as a decision -- the summary never creates a second record. `explanation` is absent when the model "
        "is unavailable or its text fails the figure check; identical facts answer from a cache."),
)
async def explain_check(body: CheckRequest, claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    import time as _time

    from app.agents.eligibility.output import summary as _explain

    started = _time.perf_counter()
    case_id = body.case_id.strip()
    response = await _evaluated(body)
    text = await asyncio.to_thread(_explain.explain, response, body.language)
    payload: dict[str, Any] = {"case_id": case_id, "decision": response.get("decision"),
                               "ms": round((_time.perf_counter() - started) * 1000)}
    if text:
        payload["explanation"] = text
    return payload


@router.post(
    "/check",
    summary="Check a case's eligibility: data from the provider -> 5 rules -> decision (saved as a run)",
    description=(
        "1. **provider.get(case_id)** -- the case's applicant data from the configured provider (config "
        "`data_provider`: the Postgres `eligibility_cases` table today, the LOS database later).\n"
        "2. **engine.evaluate** -- age, income, bureau score (knock-outs), FOIR and LTV (loan sizing); limits from "
        "`eligibility_config.yaml`. No documents, no model.\n"
        "3. **saved** as a run in `eligibility_runs` (`run_id`; read it back with GET /eligibility/runs/{run_id}).\n\n"
        "**Response**: `case_id`, `run_id`, `decision` (APPROVED / CONDITIONAL / REFER / REJECTED / DATA_MISSING), "
        "`summary` (one line), `loan` {product, requested, eligible, emi, tenure_months, rate_percent}, `ratios` "
        "{foir, ltv: {on_requested, on_eligible, limit}} (LTV for secured products), `limiting_factor`, `rules` "
        "[{rule, label, result: pass / fail / missing / not_applicable, value, limit}], `next_steps` and `reasons`. Empty fields "
        "are left out; no `run_id` means the run could not be saved (the decision is still the decision). The request "
        "id is in the `X-Request-ID` header.\n\n"
        "**`explain: true`** adds `explanation`: one or two sentences by the local model (`language`: en / hi), "
        "checked against the result's own figures. It never changes the decision, and is absent when the model is "
        "unavailable or its text fails the check.\n\n"
        "Errors are never decisions: 404 no such case, 503 data source / bureau down or rules not approved here."),
)
async def check(body: CheckRequest = Body(..., openapi_examples=_CHECK_EXAMPLES),
                claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.data import runs
    from app.agents.eligibility.agent import assess_payload
    from app.security.auth import get_subject

    request_id = f"elig_{uuid.uuid4().hex}"
    case_id = body.case_id.strip()
    subject = str(get_subject(claims) or "")
    overrides = body.overrides()
    if not overrides and body.use_saved:
        # NOTHING ENTERED IN THIS CALL: the entries the officer SAVED for the case (eligibility screen)
        from app.agents.eligibility.data import inputs_store as _saved

        try:
            overrides = await asyncio.to_thread(_saved.overrides_for, case_id)
        except _saved.InputsUnavailable as exc:
            logger.warning("eligibility: saved inputs not read for %s (%s)", case_id, exc)
            overrides = {}
    out = await asyncio.to_thread(assess_payload, {"case_id": case_id, "overrides": overrides})  # provider -> engine
    if out.get("error"):
        status, message = _ERRORS.get(out["error"], (500, "Eligibility could not be checked."))
        audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id,
                     intent="ELIGIBILITY_CHECK", tools=["eligibility_agent"], status="FAILED", detail=out["error"])
        raise HTTPException(status, detail={"request_id": request_id, "error": out["error"], "message": message})
    result = out["eligibility"]
    run_id = None
    if runs.enabled():                                                          # 3: the run, saved
        try:
            run_id = await asyncio.to_thread(runs.save, result, subject)
        except runs.RunStoreUnavailable as exc:
            logger.warning("eligibility: run not saved for %s (%s)", case_id, exc)
    audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id,
                 intent="ELIGIBILITY_CHECK", tools=["eligibility_agent"], write=run_id is not None, status="OK",
                 detail=f"decision={result.get('decision')} run={run_id}")
    from app.agents.eligibility.output import response as _response

    response = _response.build(result, run_id)
    if body.explain:
        # THE MODEL EXPLAINS THE FINISHED RESULT -- it never decides; a rejected / unavailable explanation is absent
        from app.agents.eligibility.output import summary as _explain

        text = await asyncio.to_thread(_explain.explain, response, body.language)
        if text:
            response["explanation"] = text
    if not body.detail:
        response = _response.compact(response)       # the clean default; the full shape is saved in the run
    return JSONResponse(response, headers={"X-Request-ID": request_id})


# ---------------------------------------------------------------------------------------------------------------------
# THE SAME CHECK AS A FORM (owner 2026-10-14: "/check me fields enter karne ke liye chahiye, dropdown chahiye"): every
# figure its own input box, the options as dropdowns. Swagger cannot hide / show fields when a dropdown changes (the
# eligibility screen /ui does), so each option's fields are prefixed (itr_*, salaried_*, cash_*, gross_*, bt_*,
# internal_*): fill the group the dropdown names, leave the rest empty. The SAME validation and engine as /check.
# ---------------------------------------------------------------------------------------------------------------------

#: THE FOUR SHARED INCOME BOXES -> each method's own field (None: the method does not use that box)
_FORM_INCOME = {
    "SALARIED": {"income_amount": "gross_salary", "income_adjustment": "pf_deduction",
                 "other_deduction": "other_deduction", "other_income": "other_income"},
    "ITR": {"income_amount": "income_as_per_itr", "income_adjustment": "tax", "other_deduction": None,
            "other_income": "other_income"},
    # cash profit: the three add-backs entered as one total (depreciation + partner interest + remuneration)
    "CASH_PROFIT": {"income_amount": "profit_after_tax", "income_adjustment": "depreciation",
                    "other_deduction": None, "other_income": "other_income"},
    "GROSS_PROFIT": {"income_amount": "turnover", "income_adjustment": "gross_margin", "other_deduction": None,
                     "other_income": "rental_income"},
}
#: THE SHARED TOP-UP BOXES -> each type's own field
_FORM_TOPUP = {
    "BT_TOPUP": {"topup_pos": "pos", "topup_mob": "mob", "previous_sanction": None, "original_tenure_months": None},
    "INTERNAL_TOPUP": {"topup_pos": "pos", "topup_mob": "mob", "previous_sanction": "previous_sanction",
                       "original_tenure_months": "original_tenor_months"},
}

# THE DROPDOWNS, FROM CONFIG (2026-10-15, owner: "kuch bhi hardcode nahi"): products, top-up types, collateral and
# occupancy are the configured ones; an income method appears here when it has a box mapping above (the full screen
# and POST /check take every configured method)
from app.agents.eligibility import config as _elig_cfg  # noqa: E402


def _choices(name: str, first: tuple[str, str], values) -> type[Enum]:
    return Enum(name, {first[0]: first[1], **{str(v): str(v) for v in values}}, type=str)


IncomeMethod = _choices("IncomeMethod", ("DECLARED", "DECLARED (use the case's declared income)"),
                        [m for m in _elig_cfg.setting("income_methods") if m in _FORM_INCOME])
TopupType = _choices("TopupType", ("NONE", "NONE"), [t for t in _elig_cfg.setting("topup") if t in _FORM_TOPUP])
ProductChoice = _choices("ProductChoice", ("CASE", "CASE (the case's own product)"), _elig_cfg.setting("products"))
CollateralChoice = _choices("CollateralChoice", ("DEFAULT", "DEFAULT (product limit)"),
                            _elig_cfg.setting("form", "collateral_types"))
OccupancyChoice = _choices("OccupancyChoice", ("DEFAULT", "DEFAULT (product limit)"),
                           _elig_cfg.setting("form", "occupancy_types"))

_INCOME_HELP = {
    "income_amount": "SALARIED gross salary (Rs/month) | ITR income as per ITR (Rs/year) | CASH_PROFIT profit "
                     "after tax (Rs/year) | GROSS_PROFIT turnover (Rs/year)",
    "income_adjustment": "SALARIED PF deduction, added back | ITR tax, subtracted | CASH_PROFIT depreciation + "
                         "partner interest + remuneration, added | GROSS_PROFIT gross margin as per financials",
    "other_deduction": "SALARIED only: other deductions (Rs/month)",
    "other_income": "Other income -- GROSS_PROFIT: rental income",
}


@router.post(
    "/check-form",
    summary="The same check as a FORM: a few boxes + dropdowns",
    description=(
        "Pick **income_method** and **topup_type** from the dropdowns; the shared boxes mean what that option needs "
        "(each box's description lists it per option). Empty or 0 in a box the option does not use is ignored; a "
        "non-zero value there is refused (422). Empty = the case's own data. Same validation, engine and response "
        "as POST /check (compact; `detail` = the full working)."))
async def check_form(
    claims: dict[str, Any] = Depends(require_scope("agents:execute")),
    case_id: str = Form(..., pattern=ID_PATTERN, description="e.g. CASE-E11D00000010"),
    product: ProductChoice = Form(ProductChoice.CASE),
    requested_amount: float | None = Form(None, description="Loan amount (Rs); empty = the case's"),
    tenure_months: int | None = Form(None, description="Tenure in months; empty = the case's"),
    rate_percent: float | None = Form(None, description="Rate % a year; empty = the case's"),
    income_method: IncomeMethod = Form(IncomeMethod.DECLARED),
    income_amount: float | None = Form(None, description=_INCOME_HELP["income_amount"]),
    income_adjustment: float | None = Form(None, description=_INCOME_HELP["income_adjustment"]),
    other_deduction: float | None = Form(None, description=_INCOME_HELP["other_deduction"]),
    other_income: float | None = Form(None, description=_INCOME_HELP["other_income"]),
    topup_type: TopupType = Form(TopupType.NONE),
    topup_pos: float | None = Form(None, description="BT: outstanding taken over | INTERNAL: current outstanding (Rs)"),
    topup_mob: int | None = Form(None, description="Months on book of the existing loan"),
    previous_sanction: float | None = Form(None, description="INTERNAL only: previous sanction (Rs)"),
    original_tenure_months: int | None = Form(None, description="INTERNAL only: original tenure (months)"),
    collateral: CollateralChoice = Form(CollateralChoice.DEFAULT),
    occupancy: OccupancyChoice = Form(OccupancyChoice.DEFAULT),
    existing_exposure: float | None = Form(None, description="Other loans already on this property (Rs)"),
    detail: bool = Form(False, description="The full working instead of the compact answer"),
):
    from fastapi.exceptions import RequestValidationError
    from pydantic import ValidationError

    boxes = {"income_amount": income_amount, "income_adjustment": income_adjustment,
             "other_deduction": other_deduction, "other_income": other_income, "topup_pos": topup_pos,
             "topup_mob": topup_mob, "previous_sanction": previous_sanction,
             "original_tenure_months": original_tenure_months}

    def mapped(table: dict, chosen: str, key: str) -> dict[str, Any] | None:
        names = {box for spec in table.values() for box in spec}
        if chosen not in table:                                   # DECLARED / NONE: every box of this group unused
            used = {}
        else:
            used = {target: boxes[box] for box, target in table[chosen].items()
                    if target is not None and boxes[box] not in (None, "")}
        unused = [box for box in names if (chosen not in table or table[chosen].get(box) is None)
                  and boxes[box] not in (None, "", 0, 0.0)]
        if unused:
            raise RequestValidationError([{"type": "value_error", "loc": ("body", unused[0]),
                                           "msg": f"{', '.join(sorted(unused))} is not used by {chosen} -- leave it "
                                                  f"empty or change the dropdown", "input": None}])
        return {key: chosen, **used} if chosen in table else None

    body = {"case_id": case_id, "requested_amount": requested_amount, "tenure_months": tenure_months,
            "rate_percent": rate_percent, "existing_exposure": existing_exposure or None, "detail": detail,
            "product": None if product is ProductChoice.CASE else product.value,
            "collateral_type": None if collateral is CollateralChoice.DEFAULT else collateral.value,
            "occupancy_type": None if occupancy is OccupancyChoice.DEFAULT else occupancy.value,
            "income": mapped(_FORM_INCOME, income_method.name, "method"),
            "topup": mapped(_FORM_TOPUP, topup_type.name, "type")}
    try:
        request_body = CheckRequest.model_validate({k: v for k, v in body.items() if v is not None})
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    return await check(body=request_body, claims=claims)


@router.get("/runs/{run_id}", summary="One saved eligibility check (the full result as it was returned)")
async def get_run(run_id: str = Path(..., pattern=ID_PATTERN), claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.data import runs

    request_id = f"elig_{uuid.uuid4().hex}"
    try:
        saved = await asyncio.to_thread(runs.get, run_id.strip())
    except runs.RunStoreUnavailable:
        raise HTTPException(503, detail={"request_id": request_id, "error": "DATA_SOURCE_UNAVAILABLE",
                                         "message": "The eligibility runs cannot be read right now."}) from None
    if saved is None:
        raise HTTPException(404, detail={"request_id": request_id, "error": "RUN_NOT_FOUND",
                                         "message": "No such eligibility run."})
    from app.agents.eligibility.output import response as _response

    return JSONResponse({**_response.build(saved["result"] or {}, saved["run_id"]), "checked_at": saved["created_at"]},
                        headers={"X-Request-ID": request_id})


# ---------------------------------------------------------------------------------------------------------------------
# THE ELIGIBILITY SCREEN (2026-10-14): pick a case (fetched), enter the figures, Save, Trigger eligibility.
# Registered BEFORE "/{case_id}" so "/form", "/cases" and "/ui" are never read as a case id.
# ---------------------------------------------------------------------------------------------------------------------

@router.get("/ui", include_in_schema=False)
async def eligibility_ui():
    """The eligibility screen (static page; every figure it shows comes from the APIs below)."""
    from pathlib import Path

    from fastapi.responses import HTMLResponse

    page = Path(__file__).resolve().parents[2] / "static" / "eligibility_ui.html"
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/form", summary="The eligibility screen's fields (config `form`): case fields, editable figures, the "
                             "fields of each income method / top-up type, collateral and occupancy options")
async def eligibility_form(claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility import config as _cfg
    from app.agents.eligibility.calc import income as _income
    from app.agents.eligibility.calc import topup as _topup

    form = dict(_cfg.document().get("form") or {})
    required = {m: list((spec or {}).get("required") or []) for m, spec in _income.methods().items()}
    for method, spec in (form.get("income_methods") or {}).items():
        for f in spec.get("fields") or []:
            f["required"] = f["key"] in required.get(method, [])
    for kind, spec in (form.get("topup_types") or {}).items():
        for f in spec.get("fields") or []:
            f["required"] = f["key"] in _topup.REQUIRED.get(kind, ())
    form["products"] = sorted(_cfg.products())
    # which products take a property (LTV applies): from config products.*.ltv_limit_pct -- the screen shows its
    # Property section for these only (never a list of names in the page)
    form["secured_products"] = sorted(k for k, v in _cfg.products().items() if (v or {}).get("ltv_limit_pct") is not None)
    # the screen marks demo rules ("DEMO_NON_PRODUCTION") so nobody mistakes them for approved policy
    form["config_status"] = _cfg.status()
    form["config_version"] = _cfg.version()
    return form


@router.post("/preview", summary="The income / top-up working for figures being typed (no case, no rules, no run "
                                 "saved): {income?, topup?} -> the same computed blocks /check returns")
async def eligibility_preview(body: dict[str, Any], claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.calc import income as _income
    from app.agents.eligibility.calc import topup as _topup

    # partial figures are normal while typing: compute() reports `missing` instead of refusing; non-numbers -> None
    income = body.get("income") if isinstance(body.get("income"), dict) else None
    topup = body.get("topup") if isinstance(body.get("topup"), dict) else None
    return {"income": _income.compute(income), "topup": _topup.compute(topup)}


@router.get("/cases", summary="The cases the screen can pick from [{case_id, label, product}]")
async def eligibility_cases(claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.data.providers import ProviderUnavailable, get_provider

    try:
        return {"cases": await asyncio.to_thread(get_provider().case_options)}
    except ProviderUnavailable:
        raise HTTPException(503, detail={"error": "DATA_SOURCE_UNAVAILABLE",
                                         "message": _ERRORS["DATA_SOURCE_UNAVAILABLE"][1]}) from None


@router.get("/cases/{case_id}", summary="One case for the screen: its data (read-only + editable figures) and the "
                                        "entries saved for it")
async def eligibility_case(case_id: str = Path(..., pattern=ID_PATTERN), claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.data import inputs_store
    from app.agents.eligibility.data.providers import CaseNotFound, ProviderUnavailable, get_provider

    case_id = case_id.strip()
    try:
        data = await asyncio.to_thread(get_provider().get, case_id)
    except CaseNotFound:
        raise HTTPException(404, detail={"error": "CASE_NOT_FOUND", "message": _ERRORS["CASE_NOT_FOUND"][1]}) from None
    except ProviderUnavailable:
        raise HTTPException(503, detail={"error": "DATA_SOURCE_UNAVAILABLE",
                                         "message": _ERRORS["DATA_SOURCE_UNAVAILABLE"][1]}) from None
    keep = ("product", "age", "net_monthly_income", "existing_emi", "bureau_score", "max_dpd_12m", "write_offs",
            "settlements", "property_value", "requested_amount", "tenure_months", "rate_percent")
    case = {k: getattr(data, k, None) for k in keep}
    try:
        saved = await asyncio.to_thread(inputs_store.load, case_id)
    except inputs_store.InputsUnavailable as exc:
        logger.warning("eligibility: saved inputs not read for %s (%s)", case_id, exc)
        saved = None
    return {"case_id": case_id, "case": case, "saved": saved}


@router.put("/cases/{case_id}/inputs", summary="Save the officer's entries for a case (validated like /check); "
                                               "Trigger eligibility then runs on them")
async def save_case_inputs(case_id: str = Path(..., pattern=ID_PATTERN), body: PopupInputs = Body(..., openapi_examples=_SAVE_EXAMPLES),
                           claims: dict[str, Any] = Depends(require_scope("agents:execute"))):
    from app.agents.eligibility.data import inputs_store
    from app.agents.eligibility.data.providers import CaseNotFound, ProviderUnavailable, get_provider
    from app.security.auth import get_subject

    case_id = case_id.strip()
    try:
        await asyncio.to_thread(get_provider().get, case_id)          # only an existing case gets entries
    except CaseNotFound:
        raise HTTPException(404, detail={"error": "CASE_NOT_FOUND", "message": _ERRORS["CASE_NOT_FOUND"][1]}) from None
    except ProviderUnavailable:
        raise HTTPException(503, detail={"error": "DATA_SOURCE_UNAVAILABLE",
                                         "message": _ERRORS["DATA_SOURCE_UNAVAILABLE"][1]}) from None
    entries = body.model_dump(exclude_none=True)
    subject = str(get_subject(claims) or "")
    try:
        saved = await asyncio.to_thread(inputs_store.save, case_id, entries, subject)
    except inputs_store.InputsUnavailable:
        raise HTTPException(503, detail={"error": "INPUTS_NOT_SAVED",
                                         "message": "The entries could not be saved right now. Try again."}) from None
    audit.record(request_id=f"elig_{uuid.uuid4().hex}", subject=subject, applicant_id=None, case_id=case_id,
                 intent="ELIGIBILITY_INPUTS_SAVED", tools=["eligibility_inputs"], write=True, status="OK",
                 detail=",".join(sorted(entries)))
    return saved


@router.get(
    "/{case_id}",
    summary="The eligibility decision recorded on a LOS case",
    description=(
        "The decision the LOS pipeline (or a terms edit) RECORDED for this case -- read-only, nothing "
        "is computed. `recorded: false`: never assessed. `stale: true`: the data source no longer holds the values the "
        "recorded decision was computed from (`stale_fields`) -- run POST /check again."),
)
async def get_eligibility(
    case_id: str = Path(..., pattern=ID_PATTERN),
    applicant_id: str = Query(..., description="The applicant the case belongs to. Checked against the store."),
    claims: dict[str, Any] = Depends(require_jwt),
):
    from app.mcp import applicant as tools

    request_id = f"elig_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)
    try:
        permissions.check_tool(caller, _CAPABILITY)
        permissions.check_ownership(applicant_id, case_id, caller=caller)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject, applicant_id=applicant_id, case_id=case_id,
                     intent="ELIGIBILITY", tools=[], status="DENIED", detail=exc.code)
        raise HTTPException(403, detail={"request_id": request_id, "error": exc.code,
                                         "message": exc.message}) from exc
    envelope = await tools.eligibility_get(case_id)
    audit.record(request_id=request_id, subject=caller.subject, applicant_id=applicant_id, case_id=case_id,
                 intent="ELIGIBILITY", tools=[_CAPABILITY], status="OK" if envelope.ok else "FAILED")
    if not envelope.ok:
        raise HTTPException(502, detail={"request_id": request_id,
                                         "error": envelope.error.code if envelope.error else "FAILED",
                                         "message": "The recorded eligibility result could not be read."})
    return {"request_id": request_id, "case_id": case_id, "applicant_id": applicant_id,
            "recorded": envelope.result.get("recorded", False), "eligibility": envelope.result.get("eligibility"),
            "stale": bool(envelope.result.get("stale", False)),
            "stale_fields": list(envelope.result.get("stale_fields") or [])}


__all__ = ["router"]
