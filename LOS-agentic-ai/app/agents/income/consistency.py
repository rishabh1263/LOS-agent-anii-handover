"""
Does the bank statement support the salary the slip states?

ONE SIGNAL, AND ONLY ONE. This compares two figures that were read off two
documents and says whether they agree within a configured tolerance. It is
not a fraud check, not a credit decision and not part of identity KYC. A
difference means a person should look, which is what REVIEW means
everywhere else in this pipeline.

WHY A DIFFERENCE IS NOT AN ACCUSATION. Salary credits and payslip figures
routinely differ for reasons nobody did anything wrong to cause: a
mid-month joining, an employer paying reimbursements separately, a loan
instalment deducted at source, a revision between the slip's month and the
statement's, or a second account that is the real salary account. The
check cannot tell those from a fabricated slip, so it reports the
difference and names it, and a human decides.

THE AUTHORITATIVE FIGURE IS THE SLIP'S. A salary slip STATES a salary; a
bank statement EVIDENCES credits arriving. Where both exist the stated
figure is the salary and the credits are evidence about it -- never the
other way round, and the bank statement alone never produces a salary
figure at all.

NOTHING HERE IS HARDCODED. Which slip figure is compared, how far apart
the two may sit, and how much statement must be observed are all read from
income_policy.yaml.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.agents.income import config as income_config

# ---------------------------------------------------------------------------
# Verdicts. The same three words the rest of the pipeline uses.
# ---------------------------------------------------------------------------
PASS = "PASS"
REVIEW = "REVIEW"
SKIPPED = "SKIPPED"

# ---------------------------------------------------------------------------
# Reason codes. Deterministic: each is produced by exactly one condition.
# ---------------------------------------------------------------------------
#: The stated figure and the observed credits agree within tolerance.
INCOME_CONSISTENT = "INCOME_CONSISTENT"
#: They do not. The gap is reported; what it means is a reviewer's call.
INCOME_AMOUNT_MISMATCH = "INCOME_AMOUNT_MISMATCH"
#: Too little statement to compare against -- not a disagreement.
INCOME_INSUFFICIENT_HISTORY = "INCOME_INSUFFICIENT_HISTORY"
#: Credits recur, but nothing in the statement says they are salary.
SALARY_CREDIT_NOT_IDENTIFIED = "SALARY_CREDIT_NOT_IDENTIFIED"
#: No salary slip was uploaded, so nothing states a salary.
SALARY_SLIP_MISSING = "SALARY_SLIP_MISSING"
#: No usable recurring credit evidence in the statement.
BANK_INCOME_EVIDENCE_MISSING = "BANK_INCOME_EVIDENCE_MISSING"
#: One side is present but carries no figure to compare.
INCOME_NOT_COMPARABLE = "INCOME_NOT_COMPARABLE"


def _amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def _skipped(*codes: str, **sections: Any) -> dict[str, Any]:
    return {"status": SKIPPED, "reason_codes": list(codes), **sections}


def check(
    *,
    bank_evidence: dict[str, Any] | None,
    salary_slip: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Compare stated salary against observed recurring credits.

    `bank_evidence` is `bank_statement.income.income_evidence` output.
    `salary_slip` is the slip's released extraction -- `net_pay` and
    `gross_earnings` as the extractor names them.

    Returns a structure that always carries `status` and `reason_codes`,
    and carries `bank_statement` and `salary_slip` sub-objects describing
    what each side contributed. SKIPPED whenever a side is missing: an
    absent document is not a failed comparison, and reporting it as one
    would penalise a case for a document it was never asked for.
    """
    if not income_config.consistency_enabled():
        return _skipped()

    stated_key, stated = _stated_salary(salary_slip)
    observed = _amount((bank_evidence or {}).get("estimated_monthly_amount"))

    slip_side: dict[str, Any] = {}
    if stated is not None:
        slip_side = {
            "figure": stated_key,
            "amount": str(stated),
            "pay_period": (salary_slip or {}).get("pay_period"),
        }

    bank_side: dict[str, Any] = {}
    if bank_evidence:
        bank_side = {
            "type": bank_evidence.get("type"),
            "estimated_monthly_amount": str(observed) if observed is not None else None,
            "months_observed": bank_evidence.get("months_observed"),
            "recurring_credit_count": bank_evidence.get("recurring_credit_count"),
            "confidence": bank_evidence.get("confidence"),
        }

    # -- one side or the other is not there ---------------------------------
    if not salary_slip and not bank_evidence:
        return _skipped(SALARY_SLIP_MISSING, BANK_INCOME_EVIDENCE_MISSING)
    if not salary_slip:
        return _skipped(SALARY_SLIP_MISSING, bank_statement=bank_side)
    if not bank_evidence or observed is None:
        return _skipped(BANK_INCOME_EVIDENCE_MISSING, salary_slip=slip_side)
    if stated is None:
        # The slip is here and unreadable on the one figure that matters.
        return _skipped(INCOME_NOT_COMPARABLE,
                        bank_statement=bank_side, salary_slip=slip_side)

    sides = {"bank_statement": bank_side, "salary_slip": slip_side}

    # -- the evidence is too thin to compare against ------------------------
    #
    # CHECKED BEFORE THE AMOUNTS, because a close match over one month is
    # a coincidence and a distant one is not yet a disagreement. Either
    # way the honest answer is that there is not enough statement.
    months = float(bank_evidence.get("months_observed") or 0)
    confidence = float(bank_evidence.get("confidence") or 0)

    if (months < income_config.minimum_months_observed()
            or confidence < income_config.weak_evidence_confidence()):
        return {"status": REVIEW,
                "reason_codes": [INCOME_INSUFFICIENT_HISTORY], **sides}

    # -- the comparison -----------------------------------------------------
    tolerance = Decimal(str(income_config.amount_tolerance()))
    difference = abs(observed - stated)
    within = difference <= (tolerance * stated)

    # Strings for money, floats for ratios: the structure is written to
    # case memory and to the response, and both are JSON.
    sides["difference"] = str(difference.quantize(Decimal("0.01")))
    sides["tolerance"] = float(tolerance)

    codes: list[str] = []

    # NAMED WHETHER IT PASSES OR NOT. Recurring credits that the bank did
    # not label as payroll are weaker evidence even when the amount
    # agrees, and a reviewer reading a PASS is entitled to know the
    # agreement rests on an unlabelled credit.
    if bank_evidence.get("type") != "SALARY_CREDIT":
        codes.append(SALARY_CREDIT_NOT_IDENTIFIED)

    if within:
        return {"status": PASS,
                "reason_codes": [INCOME_CONSISTENT] + codes, **sides}

    return {"status": REVIEW,
            "reason_codes": [INCOME_AMOUNT_MISMATCH] + codes, **sides}


def _stated_salary(salary_slip: dict[str, Any] | None) -> tuple[str, Decimal | None]:
    """
    The slip figure policy says to compare against, and its name.

    NET PAY BY DEFAULT, because net pay is what leaves the employer for
    the employee's account. Gross never arrives anywhere, so comparing
    credits against it would report a mismatch on every correctly paid
    salary.
    """
    if not salary_slip:
        return "NET_PAY", None

    wanted = income_config.compare_against()
    keys = {
        "NET_PAY": ("net_pay", "net_salary"),
        "GROSS": ("gross_earnings", "gross_salary"),
        "GROSS_EARNINGS": ("gross_earnings", "gross_salary"),
    }.get(wanted, ("net_pay", "net_salary"))

    for key in keys:
        amount = _amount(salary_slip.get(key))
        if amount is not None:
            return wanted, amount

    return wanted, None


__all__ = [
    "check", "PASS", "REVIEW", "SKIPPED",
    "INCOME_CONSISTENT", "INCOME_AMOUNT_MISMATCH", "INCOME_INSUFFICIENT_HISTORY",
    "SALARY_CREDIT_NOT_IDENTIFIED", "SALARY_SLIP_MISSING",
    "BANK_INCOME_EVIDENCE_MISSING", "INCOME_NOT_COMPARABLE",
]
