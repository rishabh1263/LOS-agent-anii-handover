"""
Document verification.

Verification answers a different question from extraction. Extraction asks
"what does this document say"; verification asks "is this document acceptable
enough to act on". Keeping them separate matters because a document can be
perfectly readable and still be unacceptable -- a licence that expired last
year extracts cleanly and must still be refused.

Every check here is deterministic and returns a reason code, so a decision can
be explained to a reviewer or an auditor without re-running anything. No
language model participates.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    PASS = "PASS"        # acceptable, extraction may proceed
    REVIEW = "REVIEW"    # a human should look before acting
    FAIL = "FAIL"        # not acceptable, do not proceed


class CheckOutcome(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"   # not applicable to this document


class Check(BaseModel):
    """One verification rule and what it found."""

    name: str
    outcome: CheckOutcome
    reason_code: str
    detail: str | None = None


class VerificationResult(BaseModel):
    document_type: str
    status: VerificationStatus
    verification_enabled: bool
    verification_confidence: float = Field(ge=0.0, le=1.0)
    checks: list[Check] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    processing: dict = Field(default_factory=dict)


# Reason codes are stable identifiers. A caller branches on these; the human
# readable detail beside them may change wording without breaking anything.
RC_OK = "OK"
RC_WRONG_TYPE = "DOC_TYPE_MISMATCH"
RC_UNREADABLE = "DOC_UNREADABLE"
RC_LOW_CONFIDENCE = "LOW_OCR_CONFIDENCE"
RC_REQUIRED_MISSING = "REQUIRED_FIELD_MISSING"
RC_INVALID_FORMAT = "FIELD_FORMAT_INVALID"
RC_EXPIRED = "DOCUMENT_EXPIRED"
RC_EXPIRING_SOON = "DOCUMENT_EXPIRING_SOON"
RC_FUTURE_DATE = "DATE_IN_FUTURE"
RC_UNDERAGE = "HOLDER_UNDERAGE"
RC_NAME_MISMATCH = "NAME_MISMATCH"
RC_MRZ_FAILED = "MRZ_CHECK_DIGIT_FAILED"
RC_NOT_RECONCILED = "BALANCE_NOT_RECONCILED"

# Below this, a field's reading is not trustworthy enough to act on without a
# human looking at it.
LOW_CONFIDENCE = 0.70

# A licence or passport inside this window is still valid but worth flagging,
# because the loan will outlive the document.
EXPIRY_WARN_DAYS = 90


def _check(name, outcome, code, detail=None) -> Check:
    return Check(name=name, outcome=outcome, reason_code=code, detail=detail)


def _parse(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def verify_extraction(
    result,
    *,
    expected_type: str | None = None,
    expected_name: str | None = None,
    name_threshold: float = 0.85,
) -> VerificationResult:
    """
    Run the checks that apply to an already-extracted identity document.

    Verification deliberately runs AFTER extraction here rather than before:
    almost every meaningful check -- expiry, format, age, name agreement --
    needs the field values. What the verification gate controls is whether
    those values are released to the caller, not whether OCR happens.
    """
    checks: list[Check] = []
    doc_type = getattr(result.document_type, "value", str(result.document_type))

    # --- is it the document we were promised? -------------------------
    if expected_type and doc_type != expected_type:
        checks.append(_check(
            "document_type", CheckOutcome.FAIL, RC_WRONG_TYPE,
            f"expected {expected_type}, found {doc_type}",
        ))
    elif doc_type == "UNKNOWN":
        checks.append(_check(
            "document_type", CheckOutcome.FAIL, RC_UNREADABLE,
            "the document type could not be identified",
        ))
    else:
        checks.append(_check("document_type", CheckOutcome.PASS, RC_OK, doc_type))

    # --- are the required fields present and well formed? -------------
    missing = [
        name for name, field in result.fields.items()
        if field.value is None and getattr(field, "required", False)
    ]
    if result.errors:
        checks.append(_check(
            "required_fields", CheckOutcome.FAIL, RC_REQUIRED_MISSING,
            "; ".join(result.errors[:2]),
        ))
    else:
        checks.append(_check("required_fields", CheckOutcome.PASS, RC_OK))

    invalid = [
        name for name, field in result.fields.items()
        if getattr(field.validation, "value", "") == "INVALID"
    ]
    if invalid:
        checks.append(_check(
            "field_formats", CheckOutcome.FAIL, RC_INVALID_FORMAT,
            f"failed validation: {', '.join(invalid)}",
        ))
    else:
        checks.append(_check("field_formats", CheckOutcome.PASS, RC_OK))

    # --- was it read well enough to rely on? --------------------------
    confidences = [
        f.confidence for f in result.fields.values()
        if f.value is not None and f.confidence
    ]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    weak = [
        name for name, f in result.fields.items()
        if f.value is not None and f.confidence and f.confidence < LOW_CONFIDENCE
    ]
    if weak:
        checks.append(_check(
            "ocr_confidence", CheckOutcome.WARN, RC_LOW_CONFIDENCE,
            f"read weakly: {', '.join(weak)}",
        ))
    else:
        checks.append(_check(
            "ocr_confidence", CheckOutcome.PASS, RC_OK,
            f"mean {mean_confidence:.2f}",
        ))

    # --- expiry, where the document has one ---------------------------
    expiry = _parse(result.value("valid_till") or result.value("date_of_expiry"))
    if expiry is None:
        checks.append(_check(
            "expiry", CheckOutcome.SKIPPED, RC_OK,
            "this document type carries no expiry date",
        ))
    else:
        today = date.today()
        if expiry < today:
            checks.append(_check(
                "expiry", CheckOutcome.FAIL, RC_EXPIRED,
                f"expired on {expiry.isoformat()}",
            ))
        elif (expiry - today).days <= EXPIRY_WARN_DAYS:
            checks.append(_check(
                "expiry", CheckOutcome.WARN, RC_EXPIRING_SOON,
                f"expires on {expiry.isoformat()}",
            ))
        else:
            checks.append(_check("expiry", CheckOutcome.PASS, RC_OK, expiry.isoformat()))

    # --- date of birth sanity -----------------------------------------
    dob = _parse(result.value("date_of_birth"))
    if dob is None:
        checks.append(_check("date_of_birth", CheckOutcome.SKIPPED, RC_OK))
    elif dob > date.today():
        checks.append(_check(
            "date_of_birth", CheckOutcome.FAIL, RC_FUTURE_DATE,
            f"{dob.isoformat()} is in the future",
        ))
    else:
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 18:
            checks.append(_check(
                "holder_age", CheckOutcome.FAIL, RC_UNDERAGE,
                f"holder is {age}",
            ))
        else:
            checks.append(_check("holder_age", CheckOutcome.PASS, RC_OK, f"{age} years"))

    # --- MRZ, on a passport -------------------------------------------
    mrz = result.value("mrz_verified")
    if mrz is None:
        checks.append(_check("mrz", CheckOutcome.SKIPPED, RC_OK))
    elif mrz is True:
        checks.append(_check("mrz", CheckOutcome.PASS, RC_OK, "check digits pass"))
    else:
        checks.append(_check(
            "mrz", CheckOutcome.FAIL, RC_MRZ_FAILED,
            "the machine readable zone does not validate",
        ))

    # --- does the holder match the applicant? -------------------------
    if expected_name:
        from app.services.name_match import match_names

        found = result.value("name")
        if not found:
            checks.append(_check(
                "name_match", CheckOutcome.FAIL, RC_NAME_MISMATCH,
                "no name was extracted to compare",
            ))
        else:
            comparison = match_names(expected_name, str(found), name_threshold)
            checks.append(_check(
                "name_match",
                CheckOutcome.PASS if comparison.match else CheckOutcome.FAIL,
                RC_OK if comparison.match else RC_NAME_MISMATCH,
                f"{comparison.method.value} score {comparison.score}",
            ))
    else:
        checks.append(_check("name_match", CheckOutcome.SKIPPED, RC_OK))

    return _summarise(doc_type, checks, mean_confidence)


def verify_bank_statement(result) -> VerificationResult:
    """
    Verify a parsed bank statement.

    The decisive check is reconciliation, because it is the one thing a wrong
    parse cannot survive. An unverified reconciliation is reported as REVIEW
    rather than PASS: it means completeness could not be established, which is
    not the same as the statement being wrong.
    """
    checks: list[Check] = []

    status = getattr(result.status, "value", str(result.status))
    if status in {"FAILED", "UNSUPPORTED"}:
        checks.append(_check(
            "parse", CheckOutcome.FAIL, RC_UNREADABLE,
            "; ".join(result.errors[:1]) or status,
        ))
    elif status == "REQUIRES_OCR":
        checks.append(_check(
            "parse", CheckOutcome.WARN, RC_UNREADABLE,
            "scanned statement routed for asynchronous OCR",
        ))
    else:
        checks.append(_check("parse", CheckOutcome.PASS, RC_OK, status))

    if not result.transactions:
        checks.append(_check(
            "transactions", CheckOutcome.FAIL, RC_REQUIRED_MISSING,
            "no transactions were extracted",
        ))
    else:
        checks.append(_check(
            "transactions", CheckOutcome.PASS, RC_OK,
            f"{result.transaction_count} rows",
        ))

    reconciles = result.balance_reconciles
    if reconciles is True:
        checks.append(_check(
            "reconciliation", CheckOutcome.PASS, RC_OK,
            "movements explain the balance and the rows are complete",
        ))
    elif reconciles is False:
        checks.append(_check(
            "reconciliation", CheckOutcome.FAIL, RC_NOT_RECONCILED,
            "the movements do not explain the closing balance",
        ))
    else:
        checks.append(_check(
            "reconciliation", CheckOutcome.WARN, RC_NOT_RECONCILED,
            "completeness could not be established",
        ))

    months = result.period.months_covered
    if months and months < 5.5:
        checks.append(_check(
            "period", CheckOutcome.WARN, RC_REQUIRED_MISSING,
            f"only {months} months covered",
        ))
    else:
        checks.append(_check("period", CheckOutcome.PASS, RC_OK, f"{months} months"))

    return _summarise("BANK_STATEMENT", checks, 1.0 if reconciles else 0.5)


def _summarise(
    doc_type: str, checks: list[Check], confidence: float
) -> VerificationResult:
    """
    Turn individual check outcomes into one verdict.

    Any FAIL fails the document outright. Warnings alone produce REVIEW rather
    than PASS, because a warning is precisely the case where a person should
    look before the system acts.
    """
    failed = [c for c in checks if c.outcome is CheckOutcome.FAIL]
    warned = [c for c in checks if c.outcome is CheckOutcome.WARN]

    if failed:
        status = VerificationStatus.FAIL
    elif warned:
        status = VerificationStatus.REVIEW
    else:
        status = VerificationStatus.PASS

    codes = [c.reason_code for c in failed + warned if c.reason_code != RC_OK]

    return VerificationResult(
        document_type=doc_type,
        status=status,
        verification_enabled=True,
        verification_confidence=round(
            confidence * (0.6 if failed else 0.85 if warned else 1.0), 4
        ),
        checks=checks,
        reason_codes=codes or [RC_OK],
    )


__all__ = [
    "verify_extraction", "verify_bank_statement", "VerificationResult",
    "VerificationStatus", "CheckOutcome", "Check",
]
