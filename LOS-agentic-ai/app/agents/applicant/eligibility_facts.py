"""
What affordability concluded, read back and said plainly.

THIS MODULE COMPUTES NOTHING. No FOIR, no instalment, no threshold
comparison, no verdict. Every figure in every sentence here was produced
by the Eligibility stage and written into case memory at the time; this
reads the record and puts it in a sentence, exactly as `income_facts`
does for income.

WHY THAT MATTERS MORE HERE THAN ANYWHERE ELSE. A chat answer that
computes its own FOIR would be computing it from whatever happened to be
readable at the moment somebody asked -- a different moment, possibly a
different application record -- and an officer would have two
percentages for one applicant with no way to tell which was assessed.
The pipeline's answer is the answer.

AND AN ABSENT VERDICT IS SAID, NOT FILLED IN. A case whose affordability
was never assessed is told that it was never assessed.
"""

from __future__ import annotations

from typing import Any

#: The recorded finding that carries affordability.
_FINANCIAL = "FINANCIAL"

#: How each verdict opens. The stage's own word, never re-derived.
_VERDICT = {
    "PASS": "The affordability check passed.",
    "REVIEW": "The affordability check needs review.",
    "FAIL": "The affordability check did not pass.",
    "SKIPPED": "Affordability could not be assessed.",
}

#: What each reason code says to a person. One sentence, no enum names.
#:
#: THE ABSENCES ARE PHRASED AS REQUESTS, because that is what they are:
#: an officer reading "no tenure was captured" knows what to go and do,
#: where "insufficient data" leaves them to guess.
_READABLE = {
    "ELIGIBILITY_WITHIN_POLICY":
        "the instalment and existing obligations sit within the configured "
        "limit for this product",
    "FOIR_ABOVE_THRESHOLD":
        "the instalment and existing obligations together take up more of "
        "the monthly income than policy allows",
    "INCOME_BELOW_MINIMUM":
        "the verified monthly income is below the minimum for this product",
    "OBLIGATIONS_NOT_CAPTURED":
        "nobody has recorded what this applicant already repays each month",
    "EMI_INPUTS_MISSING":
        "the loan tenure or the interest rate has not been captured, so no "
        "instalment could be worked out",
    "LOAN_AMOUNT_MISSING": "no loan amount has been captured",
    "INCOME_EVIDENCE_MISSING":
        "no verified monthly income has been established for this case",
    "INCOME_EVIDENCE_UNDER_REVIEW":
        "the income evidence itself is under review",
    "LTV_NOT_AVAILABLE":
        "no property value has been captured, so loan-to-value could not "
        "be assessed",
    "LTV_ABOVE_MAXIMUM":
        "the loan is above the maximum share of the property value",
    "ELIGIBILITY_NOT_COMPARABLE":
        "the figures available could not be compared",
    "POLICY_THRESHOLD_NOT_CONFIGURED":
        "no approved affordability threshold is configured for this "
        "product, so the ratio was reported rather than assessed",
    "POLICY_UNAVAILABLE":
        "no eligibility policy could be applied to this product",
    "LOAN_AMOUNT_BELOW_MINIMUM":
        "the loan amount is below the minimum for this product",
    "LOAN_AMOUNT_ABOVE_MAXIMUM":
        "the loan amount is above the maximum for this product",
    "TENURE_BELOW_MINIMUM": "the tenure is shorter than this product allows",
    "TENURE_ABOVE_MAXIMUM": "the tenure is longer than this product allows",
    "EMPLOYMENT_TYPE_NOT_ELIGIBLE":
        "the employment type is not one this product accepts",
    "EMPLOYMENT_TYPE_NOT_CAPTURED":
        "the employment type has not been captured, so that criterion "
        "could not be checked",
}

#: Reason codes that describe the check's own scope rather than this
#: case, and are left out of a short answer unless asked for directly.
_BACKGROUND: set[str] = set()

#: How each income provenance reads. Never shortened to "salary".
_INCOME_SOURCE = {
    "SALARY_SLIP_NET": "the net salary stated on the salary slip",
    "SALARY_SLIP_GROSS": "the gross salary stated on the salary slip",
    "BANK_RECURRING_CREDIT":
        "recurring credits observed on the bank statement",
    "DECLARED": "income the applicant declared",
}

NOT_ASSESSED = (
    "Eligibility has not been evaluated for this case yet. It is assessed "
    "once the income evidence and the loan terms are in place."
)


def _money(value: Any) -> str | None:
    try:
        return f"₹{float(value):,.0f}"
    except (TypeError, ValueError):
        return None


def recorded_eligibility(memory: dict[str, Any]) -> dict[str, Any] | None:
    """The affordability verdict this case recorded, or None."""
    for finding in memory.get("findings") or []:
        if finding.get("finding_kind") != _FINANCIAL:
            continue
        recorded = finding.get("eligibility")
        if isinstance(recorded, dict) and recorded:
            return recorded
    return None


def answer(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    One eligibility answer, SHORT AND STRUCTURED, from the recorded verdict.

        Eligibility: PASS -- within policy.
        FOIR 43.27% (limit 50.0%) -- PASS. LTV: not applicable (unsecured loan).
        Based on ₹50,000 net salary (salary slip), ₹10,000 obligations
        (declared), EMI ₹11,634 at 14.0% over 60 months.
        Policy PL_DUMMY_V1 is a demonstration policy, not company lending policy.

    Every figure is read from the record; nothing is computed here.
    """
    recorded = recorded_eligibility(memory)
    if not recorded:
        return NOT_ASSESSED, []

    status = str(recorded.get("status") or "").upper()
    policy = recorded.get("policy") or {}
    sources = [{"kind": _FINANCIAL, "status": status,
                "policy_id": policy.get("id"),
                "policy_version": policy.get("version")}]

    lines = [_headline(status, recorded.get("reason_codes") or [])]
    lines.append(_ratios(recorded.get("foir") or {}, recorded.get("ltv") or {}))

    basis = _basis(recorded.get("inputs") or {})
    if basis:
        lines.append(basis)

    policy_line = _policy_line(policy, status)
    if policy_line:
        lines.append(policy_line)

    return " ".join(line for line in lines if line), sources


def _headline(status: str, codes: list[Any]) -> str:
    """The verdict, and the reason in words -- one sentence."""
    shown = [str(c) for c in codes if str(c) != "ELIGIBILITY_WITHIN_POLICY"]
    if status == "PASS":
        return "Eligibility: PASS -- within policy."

    reasons = [_READABLE.get(c, c.replace("_", " ").lower()) for c in shown]
    if not reasons:
        return f"Eligibility: {status}."
    joined = reasons[0] if len(reasons) == 1 else "; ".join(reasons)
    return f"Eligibility: {status} -- {joined}."


def _ratios(foir: dict[str, Any], ltv: dict[str, Any]) -> str:
    """FOIR and LTV, each with its own outcome."""
    return f"{_ratio('FOIR', foir)} {_ratio('LTV', ltv)}".strip()


def _ratio(name: str, block: dict[str, Any]) -> str:
    status = str(block.get("status") or "").upper()
    if status == "NOT_APPLICABLE":
        return f"{name}: not applicable (unsecured loan)."
    value, limit = block.get("value_pct"), block.get("limit_pct")
    if value is None:
        return f"{name}: not computed."
    against = f" (limit {limit}%)" if limit is not None else ""
    return f"{name} {value}%{against} -- {status}."


def _basis(inputs: dict[str, Any]) -> str:
    """What the ratios were built from, each figure with its source."""
    parts: list[str] = []

    income = _money(inputs.get("monthly_income"))
    if income:
        source = _INCOME_SOURCE_SHORT.get(str(inputs.get("income_source") or ""), "")
        parts.append(f"{income} {source}".strip())

    obligations = inputs.get("monthly_obligations")
    if obligations is not None:
        declared = " (declared)" if inputs.get("obligations_source") == "DECLARED" else ""
        parts.append(f"{_money(obligations)} obligations{declared}")

    emi = _money(inputs.get("proposed_emi"))
    if emi:
        rate, tenure = inputs.get("interest_rate_pct"), inputs.get("tenure_months")
        terms = (f" at {rate}% over {tenure} months"
                 if rate is not None and tenure is not None else "")
        parts.append(f"EMI {emi}{terms}")

    value = _money(inputs.get("property_value"))
    if value:
        declared = " (declared)" if inputs.get("property_value_source") == "DECLARED" else ""
        parts.append(f"property {value}{declared}")

    return ("Based on " + ", ".join(parts) + ".") if parts else ""


def _policy_line(policy: dict[str, Any], status: str) -> str:
    """AN UNAPPROVED LIMIT SAYS SO, every time a limit is quoted."""
    if status not in {"PASS", "REVIEW", "FAIL"}:
        return ""
    policy_status = str(policy.get("status") or "").upper()
    if policy_status == "CONFIRMED":
        return ""
    name = policy.get("id") or "This"
    if policy_status.startswith("DEMO"):
        return f"Policy {name} is a demonstration policy, not company lending policy."
    return f"Policy {name} is not yet signed-off policy."


#: Income provenance in two or three words, never shortened to "salary"
#: when the figure is credits nobody labelled.
_INCOME_SOURCE_SHORT = {
    "SALARY_SLIP_NET": "net salary (salary slip)",
    "SALARY_SLIP_GROSS": "gross salary (salary slip)",
    "BANK_RECURRING_CREDIT": "recurring bank credits",
    "DECLARED": "declared income",
}


__all__ = ["NOT_ASSESSED", "answer", "recorded_eligibility"]
