"""
Deterministic risk rules.

Each rule is a pure function:

    (request, rule_config) -> (list[RiskFlag-like dicts], list[DataGap])

No LLM is involved. No rule reads global state. Thresholds arrive from the
policy config, never from literals in this file.

Document handling principle:
    Raw document names are normalised to canonical business document
    categories before mandatory-document rules are evaluated.

Example:
    PAN / AADHAAR / PASSPORT -> Identity Proof

This keeps document extraction/classification separate from deterministic
risk-policy evaluation.
"""

from __future__ import annotations

from typing import Any

from app.agents.fraud_risk import income as inc
from app.agents.fraud_risk.schemas import (
    DataGap,
    FraudRiskRequest,
    ReferenceStatus,
    Severity,
    VerificationStatus,
)

RuleResult = tuple[list[dict[str, Any]], list[DataGap]]

# Ratios are rounded to this many decimal places BEFORE band comparison.
#
# Without this, a value intended to sit exactly on a policy threshold can be
# represented as e.g. 14.999999999999986 and fall into the wrong band. Policy
# thresholds are expressed to two decimal places, so ratios are quantised to
# match before any comparison is made. This keeps banding reproducible and
# auditable.
RATIO_PRECISION = 2


def _quantise(value: float) -> float:
    return round(value, RATIO_PRECISION)


def _flag(
    rule: str,
    severity: str,
    message: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": Severity(severity),
        "message": message,
        "evidence": evidence,
    }


def _band_severity(
    value: float,
    bands: list[dict[str, Any]],
    key: str,
) -> str | None:
    """
    Select a severity band for `value`.

    Bands are half-open [min, max): a value exactly at a band's minimum falls
    INTO that band; a value exactly at its maximum falls into the NEXT band.
    A null max means unbounded.
    """
    for band in bands:
        low = band[f"min_{key}"]
        high = band[f"max_{key}"]

        if value >= low and (high is None or value < high):
            return band["severity"]

    return None


# ---------------------------------------------------------------------------
# DEDUPE_MATCH   [EXCEL] Sourcing sheet
# ---------------------------------------------------------------------------


def rule_dedupe_match(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    if req.dedupe is None:
        return [], [
            DataGap(
                rule="DEDUPE_MATCH",
                missing_fields=["dedupe"],
                reason=(
                    "LOS dedupe result not supplied; "
                    "duplicate risk unassessed."
                ),
            )
        ]

    known = cfg.get("parameters", [])
    sev_map = cfg.get("severity_by_parameter", {})

    flags: list[dict[str, Any]] = []

    for param in req.dedupe.matched_parameters:
        normalised = str(param).strip().lower()

        if normalised not in known:
            continue

        severity = sev_map.get(normalised)

        if severity is None:
            continue

        flags.append(
            _flag(
                "DEDUPE_MATCH",
                severity,
                f"Dedupe match on {normalised}.",
                {
                    "matched_parameter": normalised,
                    "matched_customer_ids": (
                        req.dedupe.matched_customer_ids
                    ),
                },
            )
        )

    return flags, []


# ---------------------------------------------------------------------------
# VERIFICATION_NEGATIVE
# [EXCEL] Valuation & Verification sheet
# ---------------------------------------------------------------------------


def rule_verification_negative(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    sev_map = cfg.get("severity_by_verification", {})

    flags: list[dict[str, Any]] = []
    missing: list[str] = []

    for name, severity in sev_map.items():

        status = getattr(
            req.verifications,
            name,
            None,
        )

        if status is None:
            missing.append(name)
            continue

        if status == VerificationStatus.NEGATIVE:

            flags.append(
                _flag(
                    "VERIFICATION_NEGATIVE",
                    severity,
                    f"{name.upper()} verification returned NEGATIVE.",
                    {
                        "verification": name,
                        "status": status.value,
                    },
                )
            )

    gaps = []

    if missing:

        gaps.append(
            DataGap(
                rule="VERIFICATION_NEGATIVE",
                missing_fields=missing,
                reason=(
                    "Verification(s) not performed; "
                    "treated as unassessed, not as POSITIVE."
                ),
            )
        )

    return flags, gaps


# ---------------------------------------------------------------------------
# PD_STATUS_NEGATIVE
# [EXCEL] Property Visit & Credit Visit sheet
# ---------------------------------------------------------------------------


def rule_pd_status(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    status = req.verifications.pd_status

    if status is None:
        return [], [
            DataGap(
                rule="PD_STATUS_NEGATIVE",
                missing_fields=["pd_status"],
                reason="Personal discussion not performed.",
            )
        ]

    if status == VerificationStatus.NEGATIVE:

        return [
            _flag(
                "PD_STATUS_NEGATIVE",
                cfg["severity"],
                "Personal discussion (PD) status is NEGATIVE.",
                {
                    "pd_status": status.value,
                },
            )
        ], []

    return [], []


# ---------------------------------------------------------------------------
# REFERENCE_NEGATIVE
# [EXCEL] References sheet
# ---------------------------------------------------------------------------


def rule_reference_negative(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    if not req.references:

        return [], [
            DataGap(
                rule="REFERENCE_NEGATIVE",
                missing_fields=["references"],
                reason="No reference checks supplied.",
            )
        ]

    sev_map = cfg.get("severity_by_status", {})

    flags: list[dict[str, Any]] = []

    for ref in req.references:

        if (
            ref.status is None
            or ref.status == ReferenceStatus.POSITIVE
        ):
            continue

        severity = sev_map.get(
            ref.status.value
        )

        if severity is None:
            continue

        flags.append(
            _flag(
                "REFERENCE_NEGATIVE",
                severity,
                f"Reference check returned {ref.status.value}.",
                {
                    "reference_name": ref.name,
                    "relation": ref.relation,
                    "status": ref.status.value,
                },
            )
        )

    return flags, []


# ---------------------------------------------------------------------------
# INCOME_MISMATCH
# Verified income uses the [EXCEL] eligibility formulas.
# Bands are supplied by policy.
# ---------------------------------------------------------------------------


def rule_income_mismatch(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    declared = req.income.declared_income

    missing: list[str] = []

    if declared is None:
        missing.append("declared_income")

    missing.extend(
        inc.missing_income_fields(
            req.income
        )
    )

    verified = inc.verified_income(
        req.income
    )

    if verified is None or declared is None:

        return [], [
            DataGap(
                rule="INCOME_MISMATCH",
                missing_fields=(
                    missing
                    or ["eligibility_type"]
                ),
                reason=(
                    "Cannot compute verified income; "
                    "declared-vs-verified unassessed."
                ),
            )
        ]

    if verified <= 0:

        return [], [
            DataGap(
                rule="INCOME_MISMATCH",
                missing_fields=[],
                reason=(
                    f"Verified income computed as {verified:.2f}; "
                    "variance is undefined against a non-positive base."
                ),
            )
        ]

    difference = declared - verified

    variance_pct = _quantise(
        difference / verified * 100.0
    )

    # Only over-declaration is a fraud signal.
    if variance_pct <= 0:
        return [], []

    severity = _band_severity(
        variance_pct,
        cfg.get("bands", []),
        "variance_pct",
    )

    if severity is None:
        return [], []

    return [
        _flag(
            "INCOME_MISMATCH",
            severity,
            "Declared income exceeds verified income.",
            {
                "declared_income": round(
                    declared,
                    2,
                ),
                "verified_income": round(
                    verified,
                    2,
                ),
                "difference": round(
                    difference,
                    2,
                ),
                "variance_pct": round(
                    variance_pct,
                    2,
                ),
                "eligibility_type": (
                    req.income
                    .eligibility_type
                    .value
                ),
            },
        )
    ], []


# ---------------------------------------------------------------------------
# MOB_TOPUP_INELIGIBLE
# [EXCEL] D39 / D57 IF-ladders
# ---------------------------------------------------------------------------


def _applicable_pct(
    mob: int,
    ladder: list[dict[str, Any]],
) -> float | None:

    for band in ladder:

        low = band["min_mob"]
        high = band["max_mob"]

        if (
            mob >= low
            and (
                high is None
                or mob <= high
            )
        ):
            return float(
                band["applicable_pct"]
            )

    return None


def rule_mob_topup(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    scheme = (
        req.loan.scheme or ""
    ).strip().lower()

    ladders = cfg.get(
        "ladders",
        {},
    )

    ladder_key = None

    if (
        "bt" in scheme
        and "top" in scheme
    ):
        ladder_key = "rtr_bt_topup"

    elif "top" in scheme:
        ladder_key = "topup_internal"

    if ladder_key is None:
        return [], []

    if req.loan.mob_months is None:

        return [], [
            DataGap(
                rule="MOB_TOPUP_INELIGIBLE",
                missing_fields=[
                    "loan.mob_months"
                ],
                reason=(
                    "MOB not supplied; "
                    "top-up eligibility ladder unassessed."
                ),
            )
        ]

    pct = _applicable_pct(
        req.loan.mob_months,
        ladders.get(
            ladder_key,
            [],
        ),
    )

    if pct is None or pct > 0:
        return [], []

    return [
        _flag(
            "MOB_TOPUP_INELIGIBLE",
            cfg["severity"],
            (
                "Months-on-book below the minimum "
                "for any top-up entitlement."
            ),
            {
                "mob_months": req.loan.mob_months,
                "ladder": ladder_key,
                "applicable_pct": pct,
                "scheme": req.loan.scheme,
            },
        )
    ], []


# ---------------------------------------------------------------------------
# FOIR_BREACH
# Field [EXCEL]; formula and caps supplied by policy.
# ---------------------------------------------------------------------------


def rule_foir_breach(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    gross = inc.verified_income(
        req.income
    )

    obligation = (
        req.income.fixed_obligation
    )

    missing: list[str] = []

    if gross is None:
        missing.append(
            "verified_income"
        )

    if obligation is None:
        missing.append(
            "income.fixed_obligation"
        )

    if req.loan.loan_amount is None:
        missing.append(
            "loan.loan_amount"
        )

    if req.loan.tenor_months is None:
        missing.append(
            "loan.tenor_months"
        )

    if req.loan.eligibility_roi_pct is None:
        missing.append(
            "loan.eligibility_roi_pct"
        )

    if missing:

        return [], [
            DataGap(
                rule="FOIR_BREACH",
                missing_fields=missing,
                reason=(
                    "Insufficient inputs to compute FOIR."
                ),
            )
        ]

    proposed_emi = inc.emi(
        req.loan.loan_amount,
        req.loan.eligibility_roi_pct,
        req.loan.tenor_months,
    )

    foir_raw = inc.foir_pct(
        gross,
        obligation,
        proposed_emi,
    )

    foir = (
        _quantise(foir_raw)
        if foir_raw is not None
        else None
    )

    if foir is None:

        return [], [
            DataGap(
                rule="FOIR_BREACH",
                missing_fields=[],
                reason=(
                    "Gross income is non-positive; "
                    "FOIR undefined."
                ),
            )
        ]

    severity = _band_severity(
        foir,
        cfg.get("bands", []),
        "foir_pct",
    )

    if severity is None:
        return [], []

    return [
        _flag(
            "FOIR_BREACH",
            severity,
            (
                "Fixed obligation to income "
                "ratio exceeds policy comfort."
            ),
            {
                "foir_pct": round(
                    foir,
                    2,
                ),
                "gross_income": round(
                    gross,
                    2,
                ),
                "fixed_obligation": round(
                    obligation,
                    2,
                ),
                "proposed_emi": round(
                    proposed_emi,
                    2,
                ),
            },
        )
    ], []


# ---------------------------------------------------------------------------
# LTV_BREACH
# Field [EXCEL]; formula and caps supplied by policy.
# ---------------------------------------------------------------------------


def rule_ltv_breach(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    missing: list[str] = []

    if req.loan.loan_amount is None:
        missing.append(
            "loan.loan_amount"
        )

    if req.loan.property_value is None:
        missing.append(
            "loan.property_value"
        )

    if missing:

        return [], [
            DataGap(
                rule="LTV_BREACH",
                missing_fields=missing,
                reason=(
                    "Insufficient inputs to compute LTV."
                ),
            )
        ]

    ltv_raw = inc.ltv_pct(
        req.loan.loan_amount,
        req.loan.property_value,
    )

    ltv = (
        _quantise(ltv_raw)
        if ltv_raw is not None
        else None
    )

    if ltv is None:

        return [], [
            DataGap(
                rule="LTV_BREACH",
                missing_fields=[],
                reason=(
                    "Property value is non-positive; "
                    "LTV undefined."
                ),
            )
        ]

    severity = _band_severity(
        ltv,
        cfg.get("bands", []),
        "ltv_pct",
    )

    if severity is None:
        return [], []

    return [
        _flag(
            "LTV_BREACH",
            severity,
            (
                "Loan to value ratio exceeds "
                "policy comfort."
            ),
            {
                "ltv_pct": round(
                    ltv,
                    2,
                ),
                "loan_amount": round(
                    req.loan.loan_amount,
                    2,
                ),
                "property_value": round(
                    req.loan.property_value,
                    2,
                ),
            },
        )
    ], []


# ---------------------------------------------------------------------------
# MANDATORY_DOCS_MISSING
# [EXCEL] Documents sheet, Mandatory = Y
#
# IMPORTANT:
# Do NOT compare raw document names directly against business-level
# requirements.
#
# Example:
#
#   PAN
#   AADHAAR
#
# are physical document types, while:
#
#   Identity Proof
#
# is a business requirement.
#
# The policy can define aliases:
#
#   Identity Proof:
#       - PAN
#       - AADHAAR
#       - PASSPORT
#
# This rule resolves the aliases deterministically.
# ---------------------------------------------------------------------------


def _normalise_document_name(
    value: Any,
) -> str:

    return (
        str(value)
        .strip()
        .casefold()
    )


def _build_document_alias_map(
    cfg: dict[str, Any],
) -> dict[str, set[str]]:

    aliases_cfg = cfg.get(
        "document_aliases",
        {},
    )

    alias_map: dict[
        str,
        set[str],
    ] = {}

    if not isinstance(
        aliases_cfg,
        dict,
    ):
        return alias_map

    for canonical, aliases in aliases_cfg.items():

        canonical_key = (
            _normalise_document_name(
                canonical
            )
        )

        if isinstance(
            aliases,
            str,
        ):
            aliases = [aliases]

        if not isinstance(
            aliases,
            (list, tuple, set),
        ):
            continue

        accepted = {
            _normalise_document_name(
                item
            )
            for item in aliases
            if str(item).strip()
        }

        # The canonical name itself should always satisfy the requirement.
        accepted.add(
            canonical_key
        )

        alias_map[
            canonical_key
        ] = accepted

    return alias_map


def rule_mandatory_docs(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:
    """
    Evaluate mandatory document requirements using canonical aliases.

    Raw extracted/classified documents remain untouched.

    Example input:

        sections_present = [
            "PAN",
            "AADHAAR",
            "ITR",
        ]

    Policy:

        Identity Proof:
            - PAN
            - AADHAAR

    Result:

        Identity Proof = PRESENT

    This prevents false missing-document flags caused by comparing a
    business requirement with a physical document name.
    """

    required = cfg.get(
        "required_sections",
        [],
    )

    if not required:
        return [], []

    # ---------------------------------------------------------------
    # Raw document names supplied by LOS / Document Agent.
    # ---------------------------------------------------------------

    present_raw = {
        _normalise_document_name(
            section
        )
        for section in req.documents.sections_present
        if str(section).strip()
    }

    # ---------------------------------------------------------------
    # Build configurable canonical -> accepted-document mapping.
    # ---------------------------------------------------------------

    alias_map = _build_document_alias_map(
        cfg
    )

    missing_sections: list[str] = []

    satisfied_by: dict[
        str,
        str | None,
    ] = {}

    for required_section in required:

        required_key = (
            _normalise_document_name(
                required_section
            )
        )

        # -----------------------------------------------------------
        # Direct match.
        # -----------------------------------------------------------

        if required_key in present_raw:

            satisfied_by[
                str(required_section)
            ] = str(required_section)

            continue

        # -----------------------------------------------------------
        # Alias match.
        # -----------------------------------------------------------

        accepted_documents = alias_map.get(
            required_key,
            set(),
        )

        matched_document = next(
            (
                document
                for document in present_raw
                if document
                in accepted_documents
            ),
            None,
        )

        if matched_document is not None:

            satisfied_by[
                str(required_section)
            ] = matched_document

            continue

        # -----------------------------------------------------------
        # Requirement not satisfied.
        # -----------------------------------------------------------

        missing_sections.append(
            str(required_section)
        )

        satisfied_by[
            str(required_section)
        ] = None

    if not missing_sections:
        return [], []

    return [
        _flag(
            "MANDATORY_DOCS_MISSING",
            cfg["severity"],
            "Mandatory document sections are absent.",
            {
                "missing_sections": missing_sections,
                "required_sections": required,
                "present_documents": sorted(
                    present_raw
                ),
                "satisfied_by": satisfied_by,
            },
        )
    ], []


# ---------------------------------------------------------------------------
# LEGAL_TITLE_RISK
# [EXCEL] Legal Verification Yes/No checklist
# ---------------------------------------------------------------------------


def rule_legal_title(
    req: FraudRiskRequest,
    cfg: dict[str, Any],
) -> RuleResult:

    checklist = (
        req.verifications
        .legal_checklist
    )

    if not checklist:

        return [], [
            DataGap(
                rule="LEGAL_TITLE_RISK",
                missing_fields=[
                    "legal_checklist"
                ],
                reason=(
                    "Legal title checklist "
                    "not supplied."
                ),
            )
        ]

    failed = [
        key
        for key, value in checklist.items()
        if value is False
    ]

    if not failed:
        return [], []

    return [
        _flag(
            "LEGAL_TITLE_RISK",
            cfg["severity"],
            (
                "Legal title diligence has "
                "unresolved items."
            ),
            {
                "failed_checks": failed,
                "failed_count": len(failed),
            },
        )
    ], []


# ---------------------------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------------------------

RULE_REGISTRY = {
    "DEDUPE_MATCH": rule_dedupe_match,
    "VERIFICATION_NEGATIVE": rule_verification_negative,
    "PD_STATUS_NEGATIVE": rule_pd_status,
    "REFERENCE_NEGATIVE": rule_reference_negative,
    "INCOME_MISMATCH": rule_income_mismatch,
    "MOB_TOPUP_INELIGIBLE": rule_mob_topup,
    "FOIR_BREACH": rule_foir_breach,
    "LTV_BREACH": rule_ltv_breach,
    "MANDATORY_DOCS_MISSING": rule_mandatory_docs,
    "LEGAL_TITLE_RISK": rule_legal_title,
}


__all__ = [
    "RULE_REGISTRY",
    "RuleResult",
]