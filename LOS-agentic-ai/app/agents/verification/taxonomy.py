"""
The document taxonomy: every section, every document expected within it.

AUTHORITATIVE AND COMPLETE. These 23 rows are the lender's list of
(DocumentSectionName -> ExpectedDocumentName). Each row stays a row: two rows
that are verified by the same machinery ("Age Proof -> PAN" and "Identity
Proof -> PAN") are still two rows, because a section is a REQUIREMENT and the
document is what satisfies it.

EACH ROW NAMES:

    document_type   what the row resolves to. A real document class where one
                    exists (PAN, PASSPORT, BANK_STATEMENT ...); a GENERIC
                    CATEGORY where the row names a category rather than a
                    document (GOVT_ISSUED_DOCUMENT, ID_PROOF, ADDRESS_PROOF).
                    A generic category is never silently turned into PAN,
                    Aadhaar or a licence.
    generic         True for those categories.

Which issuer, if any, can confirm a document is decided per `document_type`
by the provider registry in issuer.py -- not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TaxonomyEntry:
    number: int
    section: str
    expected_document: str
    document_type: str
    generic: bool = False


#: The 23 rows, in the order the lender lists them.
TAXONOMY: tuple[TaxonomyEntry, ...] = (
    # AGE PROOF
    TaxonomyEntry(1, "Age Proof", "Govt. Issued Document", "GOVT_ISSUED_DOCUMENT", generic=True),
    TaxonomyEntry(2, "Age Proof", "PAN", "PAN"),
    TaxonomyEntry(3, "Age Proof", "Passport", "PASSPORT"),
    TaxonomyEntry(4, "Age Proof", "Aadhar", "AADHAAR"),
    TaxonomyEntry(5, "Age Proof", "Driving License", "DRIVING_LICENCE"),
    TaxonomyEntry(6, "Age Proof", "Mark Sheet", "MARK_SHEET"),
    TaxonomyEntry(7, "Age Proof", "Voter ID", "VOTER_ID"),
    # SIGNATURE VERIFICATION
    TaxonomyEntry(8, "Signature Verification", "Bank Sign Verification", "BANK_SIGNATURE"),
    TaxonomyEntry(9, "Signature Verification", "PAN", "PAN"),
    TaxonomyEntry(10, "Signature Verification", "Driving License", "DRIVING_LICENCE"),
    TaxonomyEntry(11, "Signature Verification", "Passport", "PASSPORT"),
    # IDENTITY PROOF
    TaxonomyEntry(12, "Identity Proof", "Govt. Issued Document", "GOVT_ISSUED_DOCUMENT", generic=True),
    TaxonomyEntry(13, "Identity Proof", "PAN", "PAN"),
    TaxonomyEntry(14, "Identity Proof", "ID Proof", "ID_PROOF", generic=True),
    TaxonomyEntry(15, "Identity Proof", "Address Proof", "ADDRESS_PROOF", generic=True),
    TaxonomyEntry(16, "Identity Proof", "Aadhar", "AADHAAR"),
    TaxonomyEntry(17, "Identity Proof", "Driving License", "DRIVING_LICENCE"),
    # INCOME PROOF
    TaxonomyEntry(18, "Income Proof", "ITR Return Document", "ITR"),
    TaxonomyEntry(19, "Income Proof", "Bank statement", "BANK_STATEMENT"),
    TaxonomyEntry(20, "Income Proof", "Salary Slip", "SALARY_SLIP"),
    # PROPERTY OWNERSHIP PROOF
    TaxonomyEntry(21, "Property Ownership Proof", "Sale Deed", "SALE_DEED"),
    # BUSINESS PHOTOGRAPHS
    TaxonomyEntry(22, "Business Photographs", "Business Proof1", "BUSINESS_PROOF_1"),
    TaxonomyEntry(23, "Business Photographs", "Business Proof2", "BUSINESS_PROOF_2"),
)

#: The generic categories. Each is a requirement some document satisfies,
#: never a document with an issuer of its own.
GENERIC_CATEGORIES = frozenset(e.document_type for e in TAXONOMY if e.generic)


def _key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


_BY_NAME = {(_key(e.section), _key(e.expected_document)): e for e in TAXONOMY}


def resolve(section: str, expected_document: str) -> TaxonomyEntry | None:
    """The row for a (section, expected document) pair, or None."""
    return _BY_NAME.get((_key(section), _key(expected_document)))


def document_types() -> frozenset[str]:
    """Every document type or category the taxonomy resolves to."""
    return frozenset(e.document_type for e in TAXONOMY)


def section_key(section: str) -> str:
    """"Age Proof" -> "AGE_PROOF": how a section is named in configuration."""
    return re.sub(r"[^A-Z0-9]+", "_", str(section or "").upper()).strip("_")


def sections() -> dict[str, tuple[str, ...]]:
    """
    Section -> the document types that can satisfy it, in the lender's order.

    THE TAXONOMY IS NOT A CHECKLIST. This says what COULD evidence a
    section; which sections a case actually needs is decided by policy
    (app/agents/policy/engine.py) from stage, product and case attributes.
    """
    out: dict[str, list[str]] = {}
    for entry in TAXONOMY:
        types = out.setdefault(section_key(entry.section), [])
        if entry.document_type not in types:
            types.append(entry.document_type)
    return {name: tuple(types) for name, types in out.items()}


def accepted_for(section: str) -> tuple[str, ...]:
    """
    The concrete document types that satisfy a section. A GENERIC category
    (GOVT_ISSUED_DOCUMENT, ID_PROOF, ADDRESS_PROOF) is left out: a category
    is not a document anyone can upload.
    """
    return tuple(t for t in sections().get(section_key(section), ())
                 if t not in GENERIC_CATEGORIES)


__all__ = ["GENERIC_CATEGORIES", "TAXONOMY", "TaxonomyEntry", "accepted_for",
           "document_types", "resolve", "section_key", "sections"]
