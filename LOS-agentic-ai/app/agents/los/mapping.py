"""
Turn a finished Document Agent response into a KYC source document.

This is the seam that keeps KYC free of extraction logic. Everything here is a
field lookup on an already-normalised result -- no OCR, no parsing, no
re-reading of anything. If a value is absent from the extraction it stays
absent: KYC must see the same gaps the extractor reported, because a gap it
cannot see is a gap it will silently treat as agreement.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.kyc.schemas import (
    AddressInput,
    IncomeInput,
    KycDocumentType,
    SourceDocument,
)

logger = logging.getLogger(__name__)

# The Document Agent and Financial Agent both report a document type string.
# Anything not listed is not a KYC source -- an unidentified document has
# nothing to cross-check with.
_TYPES = {
    "PAN": KycDocumentType.PAN,
    "DRIVING_LICENCE": KycDocumentType.DRIVING_LICENCE,
    "VOTER_ID": KycDocumentType.VOTER_ID,
    "PASSPORT": KycDocumentType.PASSPORT,
    "SALARY_SLIP": KycDocumentType.SALARY_SLIP,
    "BANK_STATEMENT": KycDocumentType.BANK_STATEMENT,
    "ITR": KycDocumentType.ITR,
}

# Field names that carry a PAN, in preference order. A PAN card prints it as
# pan_number; the financial extractors report it as pan.
_PAN_FIELDS = ("pan_number", "pan")

# Where each document type keeps the person's own name. Relation names
# (father, guardian, spouse) are deliberately NOT included: matching an
# applicant against their own father's name would pass the wrong person.
_NAME_FIELDS = ("name", "employee_name")

# The father's name, which IS compared -- against the other documents'
# father's name, never against the applicant's own. Kept in its own tuple for
# exactly that reason: the moment the two lists merge, an applicant matches
# their own father and the check passes the wrong person.
_FATHER_NAME_FIELDS = ("father_name", "guardian_name")


def _first(fields: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = fields.get(name)
        if value not in (None, "", []):
            return value
    return None


def address_input(fields: dict[str, Any]) -> AddressInput | None:
    """
    An address assembled from extracted fields.

    Public because profile matching builds the same thing from the same
    fields. A second builder would be a second answer to "which keys hold
    an address", and the two would drift.
    """
    raw = fields.get("address")
    pincode = fields.get("pin_code") or fields.get("pincode")

    if not raw and not pincode:
        return None

    return AddressInput(
        raw=str(raw) if raw else None,
        pincode=str(pincode) if pincode else None,
    )


def _quality(response: dict[str, Any]) -> dict[str, float] | None:
    """
    The extractor's own per-field confidence, where it reported any.

    NOT an OCR internal and never published: it is folded into the KYC
    confidence figure and the raw numbers stay here. It is also not invented
    -- absent means absent, and the confidence model deducts for the unknown
    rather than assuming the field was read well.
    """
    extraction = response.get("extraction")
    if not isinstance(extraction, dict):
        return None

    quality = extraction.get("field_quality")
    if not isinstance(quality, dict) or not quality:
        return None

    out: dict[str, float] = {}
    for name, value in quality.items():
        try:
            out[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return out or None


def _income(fields: dict[str, Any]) -> IncomeInput | None:
    """
    Read the Financial Agent's normalised income signals.

    `signals` is emitted by the Financial Agent exactly as IncomeSignals, and
    IncomeInput mirrors that model field for field, so this is a direct
    hand-off rather than a translation that could drift.
    """
    signals = fields.get("signals")
    if not isinstance(signals, dict):
        return None

    income = IncomeInput(
        monthly_net_salary=signals.get("monthly_net_salary"),
        monthly_gross_salary=signals.get("monthly_gross_salary"),
        average_monthly_credit=signals.get("average_monthly_credit"),
        declared_annual_income=signals.get("declared_annual_income"),
    )

    if not any(
        (income.monthly_net_salary, income.monthly_gross_salary,
         income.average_monthly_credit, income.declared_annual_income)
    ):
        return None

    return income


def to_kyc_source(
    response: dict[str, Any],
    source_id: str,
) -> SourceDocument | None:
    """
    Build a KYC source from a Document Agent response, or None.

    None is returned when the document was not identified, or when extraction
    was withheld -- which is the normal case for VERIFY, and for an EXTRACT
    that did not clear the verification gate. A document whose fields were
    never released must not reach KYC, or the gate would be worth nothing.
    """
    document = response.get("document") or {}
    document_type = _TYPES.get(str(document.get("type") or "").upper())

    if document_type is None:
        return None

    extraction = response.get("extraction")
    if not isinstance(extraction, dict):
        return None

    fields = extraction.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None

    try:
        return SourceDocument(
            source_id=source_id,
            document_type=document_type,
            name=_first(fields, _NAME_FIELDS),
            father_name=_first(fields, _FATHER_NAME_FIELDS),
            date_of_birth=fields.get("date_of_birth"),
            pan=_first(fields, _PAN_FIELDS),
            address=address_input(fields),
            income=_income(fields),
            field_quality=_quality(response),
        )
    except Exception as exc:
        # A value the extractor released but KYC cannot model (an unparseable
        # date, say) must not take the whole application down. The document is
        # dropped from the cross-check and the caller is told, rather than the
        # value being coerced into something plausible.
        logger.warning(
            "Document %s could not be mapped for KYC (%s: %s); excluded.",
            source_id, type(exc).__name__, exc,
        )
        return None


__all__ = ["to_kyc_source", "address_input"]
