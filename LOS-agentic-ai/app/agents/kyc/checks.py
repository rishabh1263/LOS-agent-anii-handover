"""
The individual KYC consistency checks.

Each one takes the documents that carry its field, compares every pair, and
returns a verdict with the evidence behind it. Common shape:

  fewer than two sources  -> SKIPPED, because there was nothing to cross-check
  every pair agrees       -> PASS
  a pair is uncertain     -> REVIEW, for a human
  a pair plainly disagrees-> FAIL

No check ever fills in a missing value from another document. A field absent
from a document stays absent: inferring it would manufacture the very
agreement the check exists to test.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from itertools import combinations

from app.agents.kyc import address as address_lib
from app.agents.kyc import config
from app.agents.kyc.schemas import (
    CheckResult,
    CheckStatus,
    Evidence,
    KycCheck,
    KycDocumentType,
    PairComparison,
    ReasonCode,
    SourceDocument,
)
from app.agents.document_agent.validate import validate_pan
from app.agents.document_agent.schemas import ValidationStatus
from app.services.name_match import match_names


def _skipped(check: KycCheck, reason: ReasonCode, detail: str) -> CheckResult:
    return CheckResult(
        check=check, status=CheckStatus.SKIPPED,
        reason_codes=[reason], detail=detail,
    )


def _disabled(check: KycCheck) -> CheckResult:
    return _skipped(
        check, ReasonCode.CHECK_DISABLED,
        "Switched off in the KYC policy.",
    )


def _evidence(documents, field: str, getter) -> list[Evidence]:
    return [
        Evidence(
            source_id=d.source_id,
            document_type=d.document_type,
            field=field,
            value=getter(d),
        )
        for d in documents
    ]


# ---------------------------------------------------------------------------
# Name
# ---------------------------------------------------------------------------

def check_name(documents: list[SourceDocument]) -> CheckResult:
    """
    Do these documents name the same person?

    Delegates the comparison itself to the deterministic matcher in
    app/services/name_match.py, which already handles case, punctuation,
    titles, token reordering, initials and OCR damage, and reports WHICH rule
    decided rather than a bare score.
    """
    if not config.check_enabled("name"):
        return _disabled(KycCheck.NAME)

    match_threshold = config.threshold("name", "match_threshold", 0.85)
    review_threshold = config.threshold("name", "review_threshold", 0.70)

    named = [d for d in documents if d.name]

    if not named:
        return _skipped(
            KycCheck.NAME, ReasonCode.NAME_MISSING,
            "No document carried a name.",
        )
    if len(named) < 2:
        return _skipped(
            KycCheck.NAME, ReasonCode.NAME_SINGLE_SOURCE,
            f"Only {named[0].source_id} carried a name; nothing to compare it to.",
        )

    comparisons: list[PairComparison] = []
    reasons: set[ReasonCode] = set()
    worst = CheckStatus.PASS
    lowest = 1.0

    for left, right in combinations(named, 2):
        verdict = match_names(left.name, right.name, threshold=match_threshold)
        lowest = min(lowest, verdict.score)

        if verdict.match:
            agreed = True
        elif verdict.score >= review_threshold:
            agreed = False
            reasons.add(ReasonCode.NAME_PARTIAL_MATCH)
            worst = _worse(worst, CheckStatus.REVIEW)
        else:
            agreed = False
            reasons.add(ReasonCode.NAME_MISMATCH)
            worst = CheckStatus.FAIL

        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=left.name, right_value=right.name,
            agreed=agreed, score=verdict.score,
            detail=f"{verdict.method.value}: {verdict.reason}",
        ))

    return CheckResult(
        check=KycCheck.NAME,
        status=worst,
        score=round(lowest, 4),
        reason_codes=sorted(reasons, key=lambda r: r.value),
        detail=(
            f"{len(comparisons)} name pair(s) compared; weakest match {lowest:.2f} "
            f"against a {match_threshold} threshold."
        ),
        evidence=_evidence(named, "name", lambda d: d.name),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# Father's name
# ---------------------------------------------------------------------------

def check_father_name(documents: list[SourceDocument]) -> CheckResult:
    """
    Do these documents name the same father?

    Uses the same deterministic matcher as the applicant's name, with its own
    thresholds: a father's name is corroborating evidence rather than the
    identity itself, and it is printed on fewer documents and read less
    reliably, so it carries its own policy section and its own weight.

    Where two documents disagree the finding is reported in full. It is not,
    on its own, grounds to refuse anyone -- which is what `blocking: false`
    in policy expresses.
    """
    if not config.check_enabled("father_name"):
        return _disabled(KycCheck.FATHER_NAME)

    match_threshold = config.threshold("father_name", "match_threshold", 0.85)
    review_threshold = config.threshold("father_name", "review_threshold", 0.70)

    named = [d for d in documents if d.father_name]

    if not named:
        return _skipped(
            KycCheck.FATHER_NAME, ReasonCode.FATHER_NAME_MISSING,
            "No document carried a father's name.",
        )
    if len(named) < 2:
        return _skipped(
            KycCheck.FATHER_NAME, ReasonCode.FATHER_NAME_SINGLE_SOURCE,
            f"Only {named[0].source_id} carried a father's name; "
            "nothing to compare it to.",
        )

    comparisons: list[PairComparison] = []
    reasons: set[ReasonCode] = set()
    worst = CheckStatus.PASS
    lowest = 1.0

    for left, right in combinations(named, 2):
        verdict = match_names(left.father_name, right.father_name,
                              threshold=match_threshold)
        lowest = min(lowest, verdict.score)

        if verdict.match:
            agreed = True
        elif verdict.score >= review_threshold:
            agreed = False
            reasons.add(ReasonCode.FATHER_NAME_PARTIAL_MATCH)
            worst = _worse(worst, CheckStatus.REVIEW)
        else:
            agreed = False
            reasons.add(ReasonCode.FATHER_NAME_MISMATCH)
            worst = _worse(worst, CheckStatus.FAIL)

        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=left.father_name, right_value=right.father_name,
            agreed=agreed, score=verdict.score,
            detail=f"{verdict.method.value}: {verdict.reason}",
        ))

    return CheckResult(
        check=KycCheck.FATHER_NAME,
        status=worst,
        score=round(lowest, 4),
        reason_codes=sorted(reasons, key=lambda r: r.value),
        detail=(
            f"{len(comparisons)} father-name pair(s) compared; weakest match "
            f"{lowest:.2f} against a {match_threshold} threshold."
        ),
        evidence=_evidence(named, "father_name", lambda d: d.father_name),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# Date of birth
# ---------------------------------------------------------------------------

def check_dob(documents: list[SourceDocument]) -> CheckResult:
    """
    Compared exactly after normalisation.

    A date of birth has no near-misses: two documents either state the same day
    or they state different days, and a difference is a hard stop. A missing
    date is reported missing and never inferred from another document.
    """
    if not config.check_enabled("dob"):
        return _disabled(KycCheck.DOB)

    dated = [d for d in documents if d.date_of_birth is not None]

    if not dated:
        return _skipped(
            KycCheck.DOB, ReasonCode.DOB_MISSING,
            "No document carried a date of birth.",
        )
    if len(dated) < 2:
        return _skipped(
            KycCheck.DOB, ReasonCode.DOB_SINGLE_SOURCE,
            f"Only {dated[0].source_id} carried a date of birth.",
        )

    comparisons: list[PairComparison] = []
    mismatched = False

    for left, right in combinations(dated, 2):
        agreed = left.date_of_birth == right.date_of_birth
        mismatched = mismatched or not agreed
        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=left.date_of_birth.isoformat(),
            right_value=right.date_of_birth.isoformat(),
            agreed=agreed,
            score=1.0 if agreed else 0.0,
            detail="identical after normalisation" if agreed
                   else "different dates of birth",
        ))

    status = CheckStatus.FAIL if mismatched else CheckStatus.PASS

    return CheckResult(
        check=KycCheck.DOB,
        status=status,
        score=0.0 if mismatched else 1.0,
        reason_codes=[ReasonCode.DOB_MISMATCH] if mismatched else [],
        detail=(
            f"{len(comparisons)} date pair(s) compared"
            + ("; at least one disagrees." if mismatched else "; all identical.")
        ),
        evidence=_evidence(dated, "date_of_birth",
                           lambda d: d.date_of_birth.isoformat()),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------

def check_address(documents: list[SourceDocument]) -> CheckResult:
    if not config.check_enabled("address"):
        return _disabled(KycCheck.ADDRESS)

    pass_score = config.threshold("address", "pass_score", 0.80)
    review_score = config.threshold("address", "review_score", 0.55)
    component_threshold = config.threshold("address", "component_threshold", 0.85)
    weights = config.section("address").get("weights") or {}

    addressed = [
        d for d in documents
        if d.address is not None and not d.address.is_empty()
    ]

    if not addressed:
        return _skipped(
            KycCheck.ADDRESS, ReasonCode.ADDRESS_MISSING,
            "No document carried an address.",
        )
    if len(addressed) < 2:
        return _skipped(
            KycCheck.ADDRESS, ReasonCode.ADDRESS_SINGLE_SOURCE,
            f"Only {addressed[0].source_id} carried an address.",
        )

    comparisons: list[PairComparison] = []
    reasons: set[ReasonCode] = set()
    worst = CheckStatus.PASS
    lowest = 1.0
    any_comparable = False

    for left, right in combinations(addressed, 2):
        score, verdicts, comparable = address_lib.compare(
            left.address, right.address, weights, component_threshold
        )

        if not comparable:
            reasons.add(ReasonCode.ADDRESS_NOT_COMPARABLE)
            comparisons.append(PairComparison(
                left_source_id=left.source_id, right_source_id=right.source_id,
                agreed=False, score=None,
                detail="no component present on both documents",
            ))
            continue

        any_comparable = True
        lowest = min(lowest, score)

        if score >= pass_score:
            agreed = True
        elif score >= review_score:
            agreed = False
            reasons.add(ReasonCode.ADDRESS_PARTIAL_MATCH)
            worst = _worse(worst, CheckStatus.REVIEW)
        else:
            agreed = False
            reasons.add(ReasonCode.ADDRESS_MISMATCH)
            worst = _worse(worst, CheckStatus.FAIL)

        matched = [k for k, v in verdicts.items() if v == "MATCH"]
        differed = [k for k, v in verdicts.items() if v == "MISMATCH"]

        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=address_lib.parse(left.address),
            right_value=address_lib.parse(right.address),
            agreed=agreed, score=score,
            detail=(
                f"matched {matched or 'none'}; differed {differed or 'none'}; "
                f"compared {comparable}"
            ),
        ))

    if not any_comparable:
        return _skipped(
            KycCheck.ADDRESS, ReasonCode.ADDRESS_NOT_COMPARABLE,
            "Addresses shared no component that could be compared.",
        )

    return CheckResult(
        check=KycCheck.ADDRESS,
        status=worst,
        score=round(lowest, 4),
        reason_codes=sorted(reasons, key=lambda r: r.value),
        detail=(
            f"{len(comparisons)} address pair(s) compared component by component; "
            f"weakest weighted score {lowest:.2f} against a {pass_score} threshold."
        ),
        evidence=_evidence(addressed, "address",
                           lambda d: address_lib.parse(d.address)),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------

def check_pan(documents: list[SourceDocument]) -> CheckResult:
    """
    Validate the format, then check every document agrees on the number.

    Values are compared as supplied, upper-cased and stripped only. Nothing is
    repaired here: the Document Agent already applies its own constrained
    repair at extraction time, and re-guessing at a number downstream of that
    would be inventing identity data.
    """
    if not config.check_enabled("pan"):
        return _disabled(KycCheck.PAN)

    carried = [d for d in documents if d.pan]

    if not carried:
        return _skipped(
            KycCheck.PAN, ReasonCode.PAN_MISSING,
            "No document carried a PAN.",
        )

    reasons: set[ReasonCode] = set()
    status = CheckStatus.PASS

    normalised: list[tuple[SourceDocument, str]] = []
    invalid_notes: list[str] = []

    for document in carried:
        value = str(document.pan).strip().upper()
        verdict, note = validate_pan(value)
        if verdict is ValidationStatus.INVALID:
            reasons.add(ReasonCode.PAN_INVALID_FORMAT)
            status = CheckStatus.FAIL
            # Carry the validator's own explanation through. It distinguishes
            # a wrong SHAPE from a well-shaped number whose 4th character is
            # not an assigned holder type -- "ABCDE1234P" looks perfectly
            # valid until you know that D is not one. Reporting only the
            # reason code left a caller unable to tell those apart.
            invalid_notes.append(
                f"{document.source_id}={value}: {note or 'failed PAN validation'}"
            )
        normalised.append((document, value))

    comparisons: list[PairComparison] = []
    for (left, left_value), (right, right_value) in combinations(normalised, 2):
        agreed = left_value == right_value
        if not agreed:
            reasons.add(ReasonCode.PAN_MISMATCH)
            status = CheckStatus.FAIL
        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=left_value, right_value=right_value,
            agreed=agreed, score=1.0 if agreed else 0.0,
            detail="identical" if agreed else "different PAN numbers",
        ))

    if not comparisons and status is CheckStatus.PASS:
        # One document, valid format, nothing to cross-check it against.
        return CheckResult(
            check=KycCheck.PAN,
            status=CheckStatus.SKIPPED,
            reason_codes=[ReasonCode.PAN_SINGLE_SOURCE],
            detail=(
                f"Only {carried[0].source_id} carried a PAN; format is valid but "
                "there is nothing to compare it to."
            ),
            evidence=_evidence(carried, "pan", lambda d: str(d.pan).strip().upper()),
        )

    if status is CheckStatus.PASS:
        detail = f"{len(normalised)} PAN value(s); all agree and validate."
    else:
        parts = []
        if invalid_notes:
            parts.append("rejected by PAN validation -- " + "; ".join(invalid_notes))
        if ReasonCode.PAN_MISMATCH in reasons:
            parts.append("documents carry different PAN numbers")
        detail = f"{len(normalised)} PAN value(s); " + "; ".join(parts)

    return CheckResult(
        check=KycCheck.PAN,
        status=status,
        score=1.0 if status is CheckStatus.PASS else 0.0,
        reason_codes=sorted(reasons, key=lambda r: r.value),
        detail=detail,
        evidence=_evidence(carried, "pan", lambda d: str(d.pan).strip().upper()),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------

_BASIS = (
    "Monthly figures are annualised at {months} months. Bank credits are NOT "
    "guaranteed salary: an average monthly credit includes transfers, refunds "
    "and reimbursements, so it is expected to sit ABOVE declared salary rather "
    "than equal it. Read a positive variance against the bank figure as "
    "consistent, not as undeclared income."
)


def _annualised(document: SourceDocument, months: int) -> list[tuple[str, Decimal]]:
    """Every annual figure this document supports, with how it was derived."""
    income = document.income
    if income is None:
        return []

    out: list[tuple[str, Decimal]] = []
    factor = Decimal(months)

    try:
        if income.declared_annual_income is not None:
            out.append(("declared_annual_income", Decimal(income.declared_annual_income)))
        if income.monthly_net_salary is not None:
            out.append(("monthly_net_salary x %d" % months,
                        Decimal(income.monthly_net_salary) * factor))
        elif income.monthly_gross_salary is not None:
            out.append(("monthly_gross_salary x %d" % months,
                        Decimal(income.monthly_gross_salary) * factor))
        if income.average_monthly_credit is not None:
            out.append(("average_monthly_credit x %d" % months,
                        Decimal(income.average_monthly_credit) * factor))
    except (InvalidOperation, TypeError, ValueError):
        return []

    return out


def check_income(documents: list[SourceDocument]) -> CheckResult:
    """
    Compare salary, bank inflow and declared income on one annual basis.

    The three are not like for like, which is the whole difficulty: a salary
    slip states contractual pay, a bank statement states everything that
    arrived, and an ITR states assessed income for a different period. The
    comparison basis is reported alongside the verdict so the number is read
    correctly.
    """
    if not config.check_enabled("income"):
        return _disabled(KycCheck.INCOME)

    months = int(config.threshold("income", "months_per_year", 12.0))
    pass_pct = config.threshold("income", "pass_variance_pct", 25.0)
    review_pct = config.threshold("income", "review_variance_pct", 50.0)
    floor = Decimal(str(config.threshold("income", "min_comparable_annual", 1000.0)))

    basis = _BASIS.format(months=months)

    figures: list[tuple[SourceDocument, str, Decimal]] = []
    for document in documents:
        for label, value in _annualised(document, months):
            if value >= floor:
                figures.append((document, label, value))

    if not figures:
        return CheckResult(
            check=KycCheck.INCOME, status=CheckStatus.SKIPPED,
            reason_codes=[ReasonCode.INCOME_MISSING],
            detail="No document carried a comparable income figure.",
            basis=basis,
        )

    # Two figures from the SAME document are not a cross-check.
    sources = {id(document) for document, _, _ in figures}
    if len(sources) < 2:
        return CheckResult(
            check=KycCheck.INCOME, status=CheckStatus.SKIPPED,
            reason_codes=[ReasonCode.INCOME_SINGLE_SOURCE],
            detail=(
                f"Only {figures[0][0].source_id} carried income; nothing to "
                "cross-check it against."
            ),
            evidence=[
                Evidence(source_id=d.source_id, document_type=d.document_type,
                         field=label, value=str(value))
                for d, label, value in figures
            ],
            basis=basis,
        )

    comparisons: list[PairComparison] = []
    reasons: set[ReasonCode] = set()
    worst = CheckStatus.PASS
    highest_variance = 0.0

    for (left, left_label, left_value), (right, right_label, right_value) in combinations(figures, 2):
        if left.source_id == right.source_id:
            continue

        larger = max(left_value, right_value)
        if larger <= 0:
            continue

        variance = float(abs(left_value - right_value) / larger * 100)
        highest_variance = max(highest_variance, variance)

        if variance <= pass_pct:
            agreed = True
        elif variance <= review_pct:
            agreed = False
            reasons.add(ReasonCode.INCOME_VARIANCE_HIGH)
            worst = _worse(worst, CheckStatus.REVIEW)
        else:
            agreed = False
            reasons.add(ReasonCode.INCOME_INCONSISTENT)
            worst = _worse(worst, CheckStatus.FAIL)

        comparisons.append(PairComparison(
            left_source_id=left.source_id, right_source_id=right.source_id,
            left_value=f"{left_label}={left_value}",
            right_value=f"{right_label}={right_value}",
            agreed=agreed, score=round(max(0.0, 1.0 - variance / 100.0), 4),
            detail=f"{variance:.1f}% apart on an annual basis",
        ))

    if not comparisons:
        return CheckResult(
            check=KycCheck.INCOME, status=CheckStatus.SKIPPED,
            reason_codes=[ReasonCode.INCOME_SINGLE_SOURCE],
            detail="No cross-document income pair to compare.",
            basis=basis,
        )

    return CheckResult(
        check=KycCheck.INCOME,
        status=worst,
        score=round(max(0.0, 1.0 - highest_variance / 100.0), 4),
        reason_codes=sorted(reasons, key=lambda r: r.value),
        detail=(
            f"{len(comparisons)} income pair(s) compared; widest gap "
            f"{highest_variance:.1f}% against a {pass_pct}% pass threshold."
        ),
        evidence=[
            Evidence(source_id=d.source_id, document_type=d.document_type,
                     field=label, value=str(value))
            for d, label, value in figures
        ],
        comparisons=comparisons,
        basis=basis,
    )


# ---------------------------------------------------------------------------

_ORDER = {
    CheckStatus.PASS: 0,
    CheckStatus.SKIPPED: 0,
    CheckStatus.REVIEW: 1,
    CheckStatus.FAIL: 2,
}


def _worse(left: CheckStatus, right: CheckStatus) -> CheckStatus:
    return left if _ORDER[left] >= _ORDER[right] else right


__all__ = [
    "check_name", "check_father_name", "check_dob", "check_address",
    "check_pan", "check_income",
]
