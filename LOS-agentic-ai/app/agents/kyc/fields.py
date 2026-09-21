"""
The field-level view of a KYC assessment.

WHAT THIS IS. The operational shape a field officer reads: one row per
comparable field, with a status, a match score, a confidence, the documents
that contributed, and a sentence saying what happened.

WHAT IT IS NOT. A second opinion. Every row here is DERIVED from the
CheckResult the matching logic already produced -- the status, the score and
the reason codes all come from there. Nothing is re-compared and no verdict
is recomputed, so the field view and the check view cannot drift apart. What
this module adds is presentation: a 0-100 scale, a confidence figure, source
attribution, and plain English.

PRESENTATION ONLY, DELIBERATELY. The temptation with a screen-facing layer is
to let it "improve" a verdict it finds unhelpful. It must not: the moment this
module can change an outcome, the outcome has two authors and an auditor has
to read both.
"""

from __future__ import annotations

from typing import Any

from app.agents.kyc import config, confidence as confidence_lib
from app.agents.kyc.schemas import (
    CheckResult,
    CheckStatus,
    FieldResult,
    FieldSource,
    FieldStatus,
    KycCheck,
    KycField,
    ReasonCode,
    SourceDocument,
)

#: Internal check -> published field name, and the policy section that
#: governs it. One table, so the three things that must agree cannot drift.
CHECK_TO_FIELD: dict[KycCheck, tuple[KycField, str]] = {
    KycCheck.NAME: (KycField.NAME, "name"),
    KycCheck.DOB: (KycField.DATE_OF_BIRTH, "dob"),
    KycCheck.PAN: (KycField.PAN_NUMBER, "pan"),
    KycCheck.FATHER_NAME: (KycField.FATHER_NAME, "father_name"),
    KycCheck.ADDRESS: (KycField.ADDRESS, "address"),
    KycCheck.INCOME: (KycField.INCOME, "income"),
}

#: Which source attribute each field reads, and how it is normalised for
#: comparison. The normaliser is the SAME one the check used -- a displayed
#: normalized_value that differs from the compared one would be a lie.
_FIELD_ATTR: dict[KycField, str] = {
    KycField.NAME: "name",
    KycField.FATHER_NAME: "father_name",
    KycField.DATE_OF_BIRTH: "date_of_birth",
    KycField.PAN_NUMBER: "pan",
    KycField.ADDRESS: "address",
    KycField.INCOME: "income",
}

#: Extractor field names behind each published field, for reading the
#: per-field extraction quality a document supplied.
_QUALITY_KEYS: dict[KycField, tuple[str, ...]] = {
    KycField.NAME: ("name", "full_name", "holder_name"),
    KycField.FATHER_NAME: ("father_name", "guardian_name"),
    KycField.DATE_OF_BIRTH: ("date_of_birth", "dob"),
    KycField.PAN_NUMBER: ("pan_number", "pan"),
    KycField.ADDRESS: ("address",),
    KycField.INCOME: (),
}

#: Reason codes that mean "close, but not the same".
_PARTIAL_CODES = {
    ReasonCode.NAME_PARTIAL_MATCH,
    ReasonCode.FATHER_NAME_PARTIAL_MATCH,
    ReasonCode.ADDRESS_PARTIAL_MATCH,
}

#: Reason codes that mean "there was nothing to compare".
_NO_SOURCE_CODES = {
    ReasonCode.NAME_SINGLE_SOURCE,
    ReasonCode.FATHER_NAME_SINGLE_SOURCE,
    ReasonCode.DOB_SINGLE_SOURCE,
    ReasonCode.PAN_SINGLE_SOURCE,
    ReasonCode.ADDRESS_SINGLE_SOURCE,
    ReasonCode.INCOME_SINGLE_SOURCE,
}

_MISSING_CODES = {
    ReasonCode.NAME_MISSING,
    ReasonCode.FATHER_NAME_MISSING,
    ReasonCode.DOB_MISSING,
    ReasonCode.PAN_MISSING,
    ReasonCode.ADDRESS_MISSING,
    ReasonCode.INCOME_MISSING,
}

#: Comparison method per field, where the field does not report one itself.
#: A date and a PAN are canonical identifiers: they are equal or they are not.
_CANONICAL_FIELDS = {KycField.DATE_OF_BIRTH, KycField.PAN_NUMBER}


# ==========================================================================
# STATUS
# ==========================================================================

def _status_for(check: CheckResult, field: KycField) -> FieldStatus:
    """
    The published status. Derived, never decided.

    The only translation is REVIEW -> PARTIAL where the reason codes say the
    values were close rather than merely uncertain. Everything else maps
    across unchanged.
    """
    codes = set(check.reason_codes)

    if check.status is CheckStatus.SKIPPED:
        return FieldStatus.SKIPPED
    if check.status is CheckStatus.PASS:
        return FieldStatus.PASS
    if check.status is CheckStatus.FAIL:
        return FieldStatus.FAIL
    # REVIEW
    return FieldStatus.PARTIAL if codes & _PARTIAL_CODES else FieldStatus.REVIEW


# ==========================================================================
# REASON
# ==========================================================================

def _method_for(check: CheckResult, field: KycField,
                status: FieldStatus) -> str:
    """
    The comparison method, for the confidence model.

    Taken from what the matcher actually reported where it reported one --
    the pair comparisons carry it -- and from the nature of the field
    otherwise.
    """
    if field is KycField.ADDRESS:
        return "COMPONENT"
    if field in _CANONICAL_FIELDS:
        return "CANONICAL"

    # The name matcher names its own method in each comparison's detail, as
    # "METHOD: reason". Read the weakest comparison, since that is the one
    # the verdict rests on.
    weakest: str | None = None
    lowest = 2.0
    for comparison in check.comparisons:
        if comparison.score is not None and comparison.score < lowest:
            lowest = comparison.score
            weakest = comparison.detail

    if weakest and ":" in weakest:
        return weakest.split(":", 1)[0].strip().upper()
    return "UNKNOWN"


def _reason_code(check: CheckResult, field: KycField,
                 status: FieldStatus) -> str:
    """One code, for a screen. The full set stays on the check."""
    if check.reason_codes:
        # The most severe finding is the one worth showing. Codes are already
        # sorted; a mismatch outranks a partial match.
        for preferred in (ReasonCode.NAME_MISMATCH, ReasonCode.DOB_MISMATCH,
                          ReasonCode.PAN_MISMATCH,
                          ReasonCode.FATHER_NAME_MISMATCH,
                          ReasonCode.ADDRESS_MISMATCH,
                          ReasonCode.PAN_INVALID_FORMAT):
            if preferred in check.reason_codes:
                return preferred.value
        return check.reason_codes[0].value

    if status is FieldStatus.PASS:
        if check.score is not None and check.score >= 0.999:
            return ReasonCode.EXACT_MATCH.value
        return ReasonCode.NORMALISED_MATCH.value
    return ""


def _label(field: KycField) -> str:
    return {
        KycField.NAME: "Name",
        KycField.FATHER_NAME: "Father's name",
        KycField.DATE_OF_BIRTH: "Date of birth",
        KycField.PAN_NUMBER: "PAN",
        KycField.ADDRESS: "Address",
        KycField.INCOME: "Income",
    }[field]


#: How each document type is written on screen. `.title()` alone produces
#: "Pan" and "Voter Id", which is how nobody refers to either.
_DOCUMENT_LABELS = {
    "PAN": "PAN",
    "AADHAAR": "Aadhaar",
    "VOTER_ID": "Voter ID",
    "DRIVING_LICENCE": "Driving Licence",
    "PASSPORT": "Passport",
    "BANK_STATEMENT": "Bank Statement",
    "SALARY_SLIP": "Salary Slip",
    "ITR": "ITR",
    "APPLICATION": "Application",
}


def _readable(document_type: Any) -> str:
    key = str(getattr(document_type, "value", document_type)).upper()
    return _DOCUMENT_LABELS.get(key, key.replace("_", " ").title())


def _reason(check: CheckResult, field: KycField, status: FieldStatus,
            sources: list[FieldSource]) -> str:
    """
    A sentence an operator can act on, naming the documents involved.

    Deterministic. A language model may later rephrase this for tone; it may
    never produce it, because the sentence has to agree with the verdict and
    a model cannot be held to that.
    """
    label = _label(field)
    names = [_readable(s.document_type) for s in sources]

    if status is FieldStatus.SKIPPED:
        codes = set(check.reason_codes)
        if codes & _MISSING_CODES:
            return f"{label} was not read from any document."
        if codes & _NO_SOURCE_CODES:
            only = names[0] if names else "one document"
            return (f"{label} was read from {only} only, so there was nothing "
                    "to compare it against.")
        if ReasonCode.CHECK_DISABLED in codes:
            return f"{label} comparison is switched off in policy."
        return f"{label} could not be compared."

    where = " and ".join(names) if len(names) <= 2 else (
        ", ".join(names[:-1]) + " and " + names[-1])

    if status is FieldStatus.PASS:
        return f"{label} matches across {where}."
    if status is FieldStatus.PARTIAL:
        if field is KycField.ADDRESS:
            return f"Core address components overlap across {where}, but the "\
                   "addresses are not identical."
        return f"{label} is close but not identical across {where}."
    if status is FieldStatus.FAIL:
        return f"{label} differs across {where}."
    return f"{label} could not be resolved with confidence across {where}."


# ==========================================================================
# SOURCES
# ==========================================================================

def _normalized(field: KycField, value: Any, document: SourceDocument) -> Any:
    """
    What was actually compared, using the same normaliser the check used.

    Imported lazily and per field so this module does not pull the whole
    matching stack in for a field nobody asked about.
    """
    if value is None:
        return None

    if field in (KycField.NAME, KycField.FATHER_NAME):
        from app.services.name_match import normalize
        return normalize(str(value))

    if field is KycField.DATE_OF_BIRTH:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    if field is KycField.PAN_NUMBER:
        return str(value).strip().upper()

    if field is KycField.ADDRESS:
        from app.agents.kyc import address as address_lib
        parsed = address_lib.parse(value)
        # Only the components that carry something, so a screen is not filled
        # with empty keys.
        return {k: v for k, v in parsed.items() if v} or None

    return str(value)


def _displayable(field: KycField, value: Any) -> Any:
    if value is None:
        return None
    if field is KycField.DATE_OF_BIRTH:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if field is KycField.ADDRESS:
        raw = getattr(value, "raw", None)
        if raw:
            return raw
        parts = [getattr(value, name, None) for name in
                 ("house", "street", "locality", "city", "state", "pincode")]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return str(value)


def _sources(field: KycField, documents: list[SourceDocument]
             ) -> list[FieldSource]:
    """Every document that carried this field, with what it said."""
    attribute = _FIELD_ATTR[field]
    out: list[FieldSource] = []

    for document in documents:
        value = getattr(document, attribute, None)
        if value is None:
            continue
        if field is KycField.ADDRESS and getattr(value, "is_empty", None) \
                and value.is_empty():
            continue
        out.append(FieldSource(
            source_id=document.source_id,
            document_type=document.document_type,
            value=_displayable(field, value),
            normalized_value=_normalized(field, value, document),
        ))
    return out


def _qualities(field: KycField, documents: list[SourceDocument],
               sources: list[FieldSource]) -> list[float] | None:
    """
    The extractor's own quality readings for this field, where supplied.

    None -- not an assumed value -- when no document reported any. The
    confidence model deducts for the unknown rather than pretending it read
    well.
    """
    wanted = {s.source_id for s in sources}
    keys = _QUALITY_KEYS.get(field, ())
    found: list[float] = []

    for document in documents:
        if document.source_id not in wanted or not document.field_quality:
            continue
        for key in keys:
            if key in document.field_quality:
                try:
                    found.append(float(document.field_quality[key]))
                except (TypeError, ValueError):
                    pass
                break

    return found or None


# ==========================================================================
# BUILD
# ==========================================================================

def _threshold_for(field: KycField, section: str) -> float | None:
    """The score a verdict turned on, for the near-threshold check."""
    if field in (KycField.NAME, KycField.FATHER_NAME):
        return config.threshold(section, "match_threshold", 0.85)
    if field is KycField.ADDRESS:
        return config.threshold(section, "pass_score", 0.80)
    return None


def build(check: CheckResult, documents: list[SourceDocument]) -> FieldResult:
    """One published field row, derived from one internal check."""
    field, section = CHECK_TO_FIELD[check.check]
    status = _status_for(check, field)
    sources = _sources(field, documents)

    # A skipped field has no match to report. Zero here means "nothing was
    # compared", which the status already says; it is not a failing score.
    if status is FieldStatus.SKIPPED:
        match_score = 0
    else:
        match_score = int(round(max(0.0, min(1.0, check.score or 0.0)) * 100))

    if status is FieldStatus.SKIPPED:
        # Nothing was compared, so there is no comparison to be confident
        # about. Reported as 0 with the reason named, rather than as a
        # plausible-looking number attached to no evidence.
        confidence, factors = 0, [{
            "factor": "not_compared",
            "detail": "no comparison was made, so there is no confidence to report",
            "adjustment": 0,
        }]
    else:
        confidence, factors = confidence_lib.score(
            method=_method_for(check, field, status),
            compared_sources=len(sources),
            total_sources=len(documents),
            raw_score=check.score,
            threshold=_threshold_for(field, section),
            qualities=_qualities(field, documents, sources),
        )

    return FieldResult(
        field=field,
        status=status,
        match_score=match_score,
        confidence=confidence,
        reason_code=_reason_code(check, field, status),
        reason=_reason(check, field, status, sources),
        sources=sources,
        confidence_factors=factors,
    )


def weight(field: KycField) -> float:
    """
    How much this field counts toward the overall score.

    Read from policy. A field the configuration says nothing about counts for
    nothing rather than for a default somebody has to guess at.
    """
    weights = config.section("fields").get("weights") or {}
    try:
        return float(weights.get(field.value, 0.0))
    except (TypeError, ValueError):
        return 0.0


def roll_up(fields: list[FieldResult]) -> tuple[int, int]:
    """
    Overall score and overall confidence, from the field rows.

    Weighted means over the fields that were ACTUALLY COMPARED. A skipped
    field is excluded and the remaining weights are renormalised, so a
    document bundle that happens not to carry a father's name is neither
    rewarded nor punished for it.

    Returns (0, 0) when nothing was compared. That is not a score of zero in
    the sense of "everything disagreed" -- the status carries that distinction,
    and it will be SKIPPED or REVIEW, never PASS.
    """
    scored = [f for f in fields if f.status is not FieldStatus.SKIPPED]
    total = sum(weight(f.field) for f in scored)

    if not scored or total <= 0:
        return 0, 0

    score = sum(f.match_score * weight(f.field) for f in scored) / total
    conf = sum(f.confidence * weight(f.field) for f in scored) / total
    return int(round(score)), int(round(conf))


__all__ = ["CHECK_TO_FIELD", "build", "roll_up", "weight"]
