"""
Sale Deed capability.

Wraps the proven e-Stamp extractor in the common evidence contract the rest
of the platform speaks -- status, checks, reason codes, evidence refs -- and
turns its extraction status into a conservative PASS / REVIEW / FAIL verdict.

WHAT THIS CAN ESTABLISH, measured on the four real samples in the repository:

  sale_deed_test.pdf   e-Stamp cover on page 1  -> reference, date, article,
                       stamp duty, first party
  sale_deed_clean.pdf  e-Stamp cover on page 2  -> reference, date
  sale_deed_small.pdf  no cover page            -> nothing
  sale_deed2.pdf       corrupt scan             -> nothing

WHAT IT CANNOT ESTABLISH. The deed BODY -- property schedule, survey number,
area, full address, witnesses -- was not readable on any real sample: they
are photographs of handwritten Devanagari forms, several taken through glass
with the flash reflecting off it. Those fields are therefore absent from this
capability rather than guessed at, and a caller that needs them must send the
deed for manual review.

Above all: a readable e-Stamp certificate shows that duty was paid on a
registered instrument. It does NOT establish that the seller owns the
property, that the buyer now does, or that the deed is genuine. No verdict
here may be read as a legal ownership claim.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.sale_deed.extract import extract_sale_deed
from app.agents.sale_deed.schemas import (
    DeedSubtype,
    SaleDeedResult,
    SaleDeedStatus,
    TemplateKind,
)

CAPABILITY = "sale_deed"

# IN-UP04991166365567V and SUBIN-... are the two issuer formats seen. Kept
# deliberately tolerant on length: an over-tight pattern would reject a valid
# reference from a state whose numbering this repository has never seen, and
# a rejected-but-valid reference sends a good deed to manual review.
# Imported rather than restated: the extractor decides what it is willing to
# report, and the verdict has to judge references by the same rule. Two copies
# would drift, and a reference accepted by one and rejected by the other is a
# document that extracts a value it can never pass on.
from app.agents.sale_deed.extract import reference_is_plausible


class Decision(str, Enum):
    """The deterministic verdict. Never produced or altered by a model."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class ReasonCode(str, Enum):
    FILE_UNREADABLE = "FILE_UNREADABLE"
    ESTAMP_PAGE_NOT_FOUND = "ESTAMP_PAGE_NOT_FOUND"
    REGISTRATION_REFERENCE_MISSING = "REGISTRATION_REFERENCE_MISSING"
    REGISTRATION_REFERENCE_MALFORMED = "REGISTRATION_REFERENCE_MALFORMED"
    PARTY_MISSING = "PARTY_MISSING"
    CONSIDERATION_MISSING = "CONSIDERATION_MISSING"
    DEED_BODY_NOT_READABLE = "DEED_BODY_NOT_READABLE"
    OWNERSHIP_NOT_ESTABLISHED = "OWNERSHIP_NOT_ESTABLISHED"

    # The instrument is not the one that was asked for -- most often a gift
    # deed offered where a sale deed is required. Same stationery, different
    # transaction.
    DEED_SUBTYPE_MISMATCH = "DEED_SUBTYPE_MISMATCH"
    DEED_SUBTYPE_UNKNOWN = "DEED_SUBTYPE_UNKNOWN"

    # Cross-field checks.
    DATE_ORDER_INVALID = "DATE_ORDER_INVALID"
    AMOUNT_INVALID = "AMOUNT_INVALID"
    PIN_CODE_INVALID = "PIN_CODE_INVALID"
    PARTY_CONTAINS_LABEL = "PARTY_CONTAINS_LABEL"


class Check(BaseModel):
    """One deterministic check and what it looked at."""

    name: str
    passed: bool
    detail: str = ""


class EvidenceRef(BaseModel):
    """Where a value came from, so a reviewer can go and look."""

    source_id: str
    locator: str
    detail: str = ""


class SaleDeedOutcome(BaseModel):
    """
    One Sale Deed assessment, in the common capability contract.

    `status` is how the call ended; `decision` is the verdict. A call that
    could not run has no decision rather than a defaulted one.
    """

    request_id: str = ""
    source_id: str = ""
    capability: str = CAPABILITY
    document_type: str = "SALE_DEED"

    #: What the instrument actually is. A gift deed and a sale deed arrive on
    #: identical stationery, so the caller is told which one this is rather
    #: than having it inferred from the capability's name.
    subtype: DeedSubtype = DeedSubtype.UNKNOWN
    template: TemplateKind = TemplateKind.UNKNOWN

    status: SaleDeedStatus
    decision: Decision | None = None

    fields: dict[str, Any] = Field(default_factory=dict)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    confidence: float = 0.0

    reason_codes: list[ReasonCode] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    language: str | None = None
    pages: int = 0
    processing_ms: float = 0.0

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # A registered instrument is not a title search.
    ownership_verified: bool = False

    def as_tool_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def valid_reference(value: str | None) -> bool:
    """
    Whether an e-Stamp reference has a usable issuer format.

    Shape alone is not enough: an OCR-mangled reference keeps the shape. The
    body has to be mostly digits, the way a real one is.
    """
    return reference_is_plausible(value)


def _decide(
    result: SaleDeedResult,
    checks: list[Check],
    reasons: list[ReasonCode],
) -> Decision:
    """
    Map extraction evidence onto a verdict.

    PASS requires a genuinely complete e-Stamp certificate: a well-formed
    reference, both parties named, and a money figure. On the samples in this
    repository nothing reaches that bar, which is the correct outcome rather
    than a threshold to lower until something passes.
    """
    if result.status is SaleDeedStatus.FAILED:
        return Decision.FAIL

    if result.status is SaleDeedStatus.UNSUPPORTED:
        return Decision.REVIEW

    has_reference = valid_reference(result.registration_reference)
    has_parties = bool(result.first_party) and bool(result.second_party)
    has_money = (
        result.consideration_price is not None
        or result.stamp_duty_amount is not None
    )

    if has_reference and has_parties and has_money:
        return Decision.PASS

    return Decision.REVIEW


def _final_decision(
    result: SaleDeedResult,
    checks: list[Check],
    reasons: list[ReasonCode],
    subtype_matches: bool,
) -> Decision:
    """
    The verdict, after the instrument's own nature is taken into account.

    A gift deed can extract perfectly and still must not PASS as a sale deed:
    the extraction was right and the document is the wrong one. It goes to
    REVIEW rather than FAIL, because the document is valid -- it simply is not
    what was asked for, and that is a human's call, not a rejection.
    """
    decision = _decide(result, checks, reasons)

    if decision is Decision.PASS and not subtype_matches:
        return Decision.REVIEW

    # A consistency failure means fields disagree with each other, which is
    # never good enough to pass on.
    blocking = {
        ReasonCode.DATE_ORDER_INVALID,
        ReasonCode.AMOUNT_INVALID,
        ReasonCode.PIN_CODE_INVALID,
        ReasonCode.PARTY_CONTAINS_LABEL,
    }
    if decision is Decision.PASS and blocking.intersection(reasons):
        return Decision.REVIEW

    return decision


_PIN_RE = re.compile(r"^[1-9]\d{5}$")

# Words that mean a caption leaked into a value.
_LABEL_LEAKAGE = ("FIRST PARTY", "SECOND PARTY", "STAMP DUTY", "CONSIDERATION")


def cross_field_checks(result: SaleDeedResult) -> tuple[list[Check], list[ReasonCode]]:
    """
    Deterministic consistency checks across extracted fields.

    These catch the mistakes that look most like success: a date that parsed
    but sits in the wrong order, an amount that is negative, a party name
    that is actually the next caption.
    """
    checks: list[Check] = []
    reasons: list[ReasonCode] = []

    # -- dates ----------------------------------------------------------
    if result.registration_date and result.document_date:
        ordered = _dates_ordered(result.document_date, result.registration_date)
        checks.append(
            Check(
                name="date_order_plausible",
                passed=ordered is not False,
                detail=(
                    f"issued {result.document_date}, "
                    f"registered {result.registration_date}"
                ),
            )
        )
        if ordered is False:
            reasons.append(ReasonCode.DATE_ORDER_INVALID)

    # -- amounts --------------------------------------------------------
    for name, amount in (
        ("stamp_duty_amount", result.stamp_duty_amount),
        ("consideration_price", result.consideration_price),
        ("registration_fee", result.registration_fee),
    ):
        if amount is None:
            continue
        valid = amount >= 0
        checks.append(
            Check(name=f"{name}_non_negative", passed=valid, detail=str(amount))
        )
        if not valid:
            reasons.append(ReasonCode.AMOUNT_INVALID)

    # -- pin ------------------------------------------------------------
    if result.pin_code:
        valid = bool(_PIN_RE.match(result.pin_code.strip()))
        checks.append(
            Check(name="pin_code_valid", passed=valid, detail=result.pin_code)
        )
        if not valid:
            reasons.append(ReasonCode.PIN_CODE_INVALID)

    # -- field boundaries -----------------------------------------------
    for name, value in (
        ("first_party", result.first_party),
        ("second_party", result.second_party),
    ):
        if not value:
            continue
        upper = value.upper()
        leaked = any(label in upper for label in _LABEL_LEAKAGE)
        checks.append(
            Check(
                name=f"{name}_is_not_a_label",
                passed=not leaked,
                detail=value[:60],
            )
        )
        if leaked:
            reasons.append(ReasonCode.PARTY_CONTAINS_LABEL)

    return checks, reasons


def _dates_ordered(earlier: str | None, later: str | None) -> bool | None:
    """
    Whether two dates are in a plausible order.

    None when either cannot be parsed -- unknown is not a failure, and a date
    this extractor could not read must not become a consistency error.
    """
    from datetime import date

    def parse(value: str | None) -> date | None:
        if not value:
            return None
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                from datetime import datetime

                return datetime.strptime(value.strip()[:11], fmt).date()
            except ValueError:
                continue
        return None

    first, second = parse(earlier), parse(later)
    if first is None or second is None:
        return None

    return first <= second


def assess(
    result: SaleDeedResult,
    *,
    source_id: str = "",
    request_id: str = "",
    expected_subtype: DeedSubtype | str | None = None,
) -> SaleDeedOutcome:
    """
    Turn an extraction into the common contract. Pure, so it is testable
    without OCR: every status can be exercised by constructing a result.
    """
    checks: list[Check] = []
    reasons: list[ReasonCode] = []

    if result.status is SaleDeedStatus.FAILED:
        reasons.append(ReasonCode.FILE_UNREADABLE)

    if result.status is SaleDeedStatus.UNSUPPORTED:
        reasons.append(ReasonCode.ESTAMP_PAGE_NOT_FOUND)

    found = result.status not in (SaleDeedStatus.FAILED, SaleDeedStatus.UNSUPPORTED)
    checks.append(
        Check(
            name="estamp_page_found",
            passed=found,
            detail=(
                f"e-Stamp cover recognised on page {result.estamp_page}"
                if found
                else "no e-Stamp certificate page recognised"
            ),
        )
    )

    reference_ok = valid_reference(result.registration_reference)
    if found:
        checks.append(
            Check(
                name="registration_reference_valid",
                passed=reference_ok,
                detail=(
                    f"{result.registration_reference}"
                    if reference_ok
                    else f"missing or malformed: {result.registration_reference!r}"
                ),
            )
        )
        if not result.registration_reference:
            reasons.append(ReasonCode.REGISTRATION_REFERENCE_MISSING)
        elif not reference_ok:
            reasons.append(ReasonCode.REGISTRATION_REFERENCE_MALFORMED)

        parties_ok = bool(result.first_party) and bool(result.second_party)
        checks.append(
            Check(
                name="both_parties_named",
                passed=parties_ok,
                detail=f"first={result.first_party!r} second={result.second_party!r}",
            )
        )
        if not parties_ok:
            reasons.append(ReasonCode.PARTY_MISSING)

        money_ok = (
            result.consideration_price is not None
            or result.stamp_duty_amount is not None
        )
        checks.append(
            Check(
                name="consideration_or_duty_present",
                passed=money_ok,
                detail=(
                    f"consideration={result.consideration_price} "
                    f"duty={result.stamp_duty_amount}"
                ),
            )
        )
        if not money_ok:
            reasons.append(ReasonCode.CONSIDERATION_MISSING)

    # --- subtype -------------------------------------------------------
    #
    # A gift deed is printed on the same e-Stamp stationery as a conveyance.
    # If the caller asked for a sale and this is a gift, no amount of clean
    # extraction makes it a sale, so it cannot pass as one.
    subtype_matches = True
    if expected_subtype is not None:
        try:
            wanted = DeedSubtype(expected_subtype)
        except ValueError:
            wanted = DeedSubtype.UNKNOWN

        subtype_matches = result.subtype is wanted
        checks.append(
            Check(
                name="deed_subtype_matches_request",
                passed=subtype_matches,
                detail=f"requested {wanted.value}, found {result.subtype.value}",
            )
        )
        if not subtype_matches:
            reasons.append(ReasonCode.DEED_SUBTYPE_MISMATCH)

    if result.subtype is DeedSubtype.UNKNOWN and found:
        reasons.append(ReasonCode.DEED_SUBTYPE_UNKNOWN)

    # --- cross-field consistency ---------------------------------------
    consistency_checks, consistency_reasons = cross_field_checks(result)
    checks.extend(consistency_checks)
    reasons.extend(consistency_reasons)

    # Stated on every outcome, including a PASS: the deed body was never read,
    # so nobody downstream can mistake a duty-paid certificate for a title.
    reasons.append(ReasonCode.DEED_BODY_NOT_READABLE)
    reasons.append(ReasonCode.OWNERSHIP_NOT_ESTABLISHED)

    fields = {
        k: v
        for k, v in {
            "article_type": result.article_type,
            "first_party": result.first_party,
            "second_party": result.second_party,
            "stamp_duty_paid_by": result.stamp_duty_paid_by,
            "stamp_duty_amount": (
                str(result.stamp_duty_amount)
                if result.stamp_duty_amount is not None
                else None
            ),
            "consideration_price": (
                str(result.consideration_price)
                if result.consideration_price is not None
                else None
            ),
            "property_description": result.property_description,
            "registration_reference": result.registration_reference,
            "document_date": result.document_date,
            # Endorsement-summary evidence.
            "document_number": result.document_number,
            "book_number": result.book_number,
            "volume_number": result.volume_number,
            "registration_date": result.registration_date,
            "registration_fee": (
                str(result.registration_fee)
                if result.registration_fee is not None
                else None
            ),
            "registration_office": result.registration_office,
            "presented_by": result.presented_by,
            # Registration-form evidence.
            "property_type": result.property_type,
            "village": result.village,
            "district": result.district,
            "pin_code": result.pin_code,
            "area": result.area,
            "plot_number": result.plot_number,
            "khasra_number": result.khasra_number,
        }.items()
        if v is not None
    }

    evidence: list[EvidenceRef] = []
    if found and result.estamp_page:
        evidence = [
            EvidenceRef(
                source_id=source_id,
                locator=f"page={result.estamp_page}",
                detail=f"e-Stamp certificate, OCR language {result.language}",
            )
        ]

    # A field read off the recognised cover carries that page's confidence;
    # nothing here is more certain than the page it came from.
    field_confidence = {name: result.confidence for name in fields}

    return SaleDeedOutcome(
        request_id=request_id,
        source_id=source_id,
        subtype=result.subtype,
        template=result.template,
        status=result.status,
        decision=_final_decision(result, checks, reasons, subtype_matches),
        fields=fields,
        field_confidence=field_confidence,
        checks=checks,
        confidence=result.confidence,
        reason_codes=reasons,
        evidence_refs=evidence,
        language=result.language,
        pages=result.pages,
        processing_ms=result.processing_ms,
        errors=list(result.errors),
        warnings=list(result.warnings),
    )


def analyze_sale_deed(
    file_path: str,
    *,
    source_id: str = "",
    request_id: str = "",
) -> SaleDeedOutcome:
    """
    Extract and assess one Sale Deed.

    Synchronous and OCR-bound; async callers hand it to the OCR executor the
    way every other document path in this codebase does.
    """
    started = time.perf_counter()
    result = extract_sale_deed(file_path)
    outcome = assess(result, source_id=source_id, request_id=request_id)

    if not outcome.processing_ms:
        outcome.processing_ms = round((time.perf_counter() - started) * 1000, 2)

    return outcome


__all__ = [
    "CAPABILITY",
    "Check",
    "Decision",
    "EvidenceRef",
    "ReasonCode",
    "SaleDeedOutcome",
    "analyze_sale_deed",
    "assess",
    "valid_reference",
]
