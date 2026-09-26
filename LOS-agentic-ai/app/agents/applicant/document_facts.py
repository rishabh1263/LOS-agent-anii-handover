"""
What a document on this case says: "what is the name on my PAN?"

THE QUESTION WAS NOT ANSWERED FROM THE CASE AT ALL. It matched no case
pattern, fell through to the knowledge base, and came back with a
handbook paragraph about PAN cards not being address proof. The answer
is a recorded value: the released extraction of that applicant's PAN.

THE SOURCE OF TRUTH, in order:

    1. the CURRENT released extraction of that document type, for that
       party, on that case -- what the verification gate let through
    2. the value the CURRENT KYC comparison recorded for that same
       document, where the released extraction does not carry the field
       (a bank statement's holder name is recorded only there)

and never anything else. Not a superseded run, not another party's
document, and NEVER ANOTHER DOCUMENT TYPE: asked for the name on a PAN
when only a salary slip was uploaded, the answer is that there is no
PAN -- a salary slip's name offered in its place would be exactly the
substitution a reviewer cannot detect.

A DOCUMENT THAT DID NOT PASS IS NOT QUOTED. Its verification state is
reported instead, because a value from a document the gate refused is
not a fact about the applicant.

DETERMINISTIC. No model on this path at any setting; the value is quoted
from the record, and a paraphrase of a name is a different name.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Spoken document names -> stored document_type. Longest forms first so
#: "bank statement" is not read as "bank".
_DOCUMENTS: tuple[tuple[str, str], ...] = (
    (r"salary\s*slip|pay\s*slip|payslip", "SALARY_SLIP"),
    (r"bank\s*statement|bank\s*account|account\s*holder|account\s*statement",
     "BANK_STATEMENT"),
    (r"driving\s*licen[cs]e", "DRIVING_LICENCE"),
    (r"voter\s*id", "VOTER_ID"),
    (r"passport", "PASSPORT"),
    (r"aadhaa?r", "AADHAAR"),
    (r"\bpan\b", "PAN"),
)

#: Spoken field names -> (extracted key, KYC comparison field, how to say it).
#: Father's name before name, so "father's name" is not read as "name".
_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (r"father'?s?\s*name|\bfather\b", "father_name", "FATHER_NAME",
     "father's name"),
    (r"date\s+of\s+birth|\bdob\b|birth\s*date", "date_of_birth",
     "DATE_OF_BIRTH", "date of birth"),
    (r"pan\s*(number|no\b)", "pan_number", "PAN_NUMBER", "PAN number"),
    (r"employer", "employer_name", "", "employer name"),
    (r"\bname\b|account\s*holder|\bwho\b", "name", "NAME", "name"),
)

_WORDS = {
    "PAN": "PAN",
    "SALARY_SLIP": "salary slip",
    "BANK_STATEMENT": "bank statement",
    "DRIVING_LICENCE": "driving licence",
    "VOTER_ID": "voter ID",
    "PASSPORT": "passport",
    "AADHAAR": "Aadhaar",
}


def document_asked(message: str) -> str | None:
    """The document type the question names, or None."""
    text = message or ""
    for pattern, document_type in _DOCUMENTS:
        if re.search(pattern, text, re.IGNORECASE):
            return document_type
    return None


def field_asked(message: str) -> tuple[str, str, str]:
    """(extracted key, KYC field, label) for the field the question names."""
    text = message or ""
    for pattern, key, kyc_field, label in _FIELDS:
        if re.search(pattern, text, re.IGNORECASE):
            return key, kyc_field, label
    return "name", "NAME", "name"


def _repository():
    from app.store import get_repository

    return get_repository()


def _kind(finding: Any) -> str:
    kind = getattr(finding, "finding_kind", "")
    return str(getattr(kind, "value", kind) or "")


def answer(
    case_id: str,
    party_id: str | None,
    message: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    The recorded value, and the records it was read from.

    Returns (sentence, sources). `sources` is empty exactly when nothing
    recorded supports an answer -- no such document, or none that passed
    -- which is how the caller's grounding rule tells the two apart.
    """
    document_type = document_asked(message)
    key, kyc_field, label = field_asked(message)
    if document_type is None:
        return ("Please name the document -- for example the PAN or the "
                "salary slip -- whose details you want.", [])
    words = _WORDS.get(document_type, document_type.replace("_", " ").lower())

    try:
        findings = _repository().get_current_findings(case_id, party_id=party_id)
    except Exception as exc:
        logger.warning("Document facts unavailable for %s: %r", case_id, exc)
        return (f"The {words} details for this case could not be read right "
                "now.", [])

    # THE DOCUMENT, BY ITS OWN RECORDED TYPE. The latest-written one when
    # the same party uploaded two -- the findings arrive in write order.
    verified = [
        f for f in findings
        if _kind(f) == "VERIFICATION"
        and str((f.payload or {}).get("type") or "").upper() == document_type
    ]
    if not verified:
        # No document, so no value of any field: said without naming a field
        # the question may not have asked for.
        return (f"No {words} has been recorded for this case yet, so I "
                f"don't have its details.", [])

    verification = verified[-1]
    status = str(verification.status or "").upper() or "UNKNOWN"
    verification_source = _source(verification, document_type)

    if status != "PASS":
        # THE RECORDED VERDICT AND REASONS, IN WORDS. The status and the
        # reason codes stay in `sources`; the sentence says what they mean
        # -- "under review", and the catalogue's text for each code --
        # rather than printing REVIEW (REQUIRED_FIELD_MISSING).
        from app.agents.applicant.answer import _explained

        held = {"REVIEW": "is under review", "FAIL": "did not pass verification"}
        verb = held.get(status, f"was recorded as {status.lower()}")
        reasons = " ".join(_explained(code)
                           for code in (verification.reason_codes or [])[:2])
        return (f"Your {words} {verb}."
                + (f" {reasons}" if reasons else "")
                + f" Its details are not confirmed, so no {label} from it "
                  f"is reported.",
                [verification_source])

    extraction = next(
        (f for f in reversed(findings)
         if _kind(f) == "EXTRACTION"
         and f.source_id == verification.source_id
         and f.party_id == verification.party_id),
        None,
    )
    value = (extraction.payload or {}).get(key) if extraction else None
    if value:
        return (f"The {label} on your {words} is {value}.",
                [_source(extraction, document_type, field=key),
                 verification_source])

    # THE SAME DOCUMENT'S VALUE AS KYC RECORDED IT, where the released
    # extraction does not carry the field. Matched on the document's own
    # source id, so another document's value cannot answer.
    kyc_value, kyc = _kyc_value(findings, kyc_field, document_type,
                                verification.source_id)
    if kyc_value:
        return (f"The {label} on your {words} is {kyc_value}.",
                [_source(kyc, document_type, field=key), verification_source])

    return (f"Your {words} passed verification, but no {label} was read "
            "from it.", [verification_source])


def _kyc_value(findings, kyc_field, document_type, source_id):
    if not kyc_field:
        return None, None
    for finding in reversed(findings):
        if _kind(finding) != "KYC":
            continue
        for field in (finding.payload or {}).get("fields") or []:
            if str(field.get("field") or "").upper() != kyc_field:
                continue
            for source in field.get("sources") or []:
                if (str(source.get("document_type") or "").upper() == document_type
                        and source.get("source_id") == source_id
                        and source.get("value")):
                    return source["value"], finding
    return None, None


def _source(finding: Any, document_type: str,
            field: str | None = None) -> dict[str, Any]:
    """A pointer to the record. Never the value itself."""
    entry: dict[str, Any] = {
        "kind": "case_finding",
        "finding_kind": _kind(finding),
        "document_type": document_type,
    }
    if field:
        entry["field"] = field
    if finding.status:
        entry["status"] = finding.status
    for name in ("document_id", "source_id", "party_id"):
        value = getattr(finding, name, None)
        if value:
            entry[name] = value
    return entry


__all__ = ["answer", "document_asked", "field_asked"]
