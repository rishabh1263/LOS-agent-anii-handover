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

#: A field as a reader names it ("PAN number", not "pan number").
_FIELD_LABELS = {"pan_number": "PAN number", "pan": "PAN", "dl_number": "Licence number",
                 "epic_number": "Voter ID number", "passport_number": "Passport number",
                 "aadhaar_number": "Aadhaar number", "account_number": "Account number", "ifsc": "IFSC",
                 "name": "Name", "father_name": "Father's name", "date_of_birth": "Date of birth",
                 "dob": "Date of birth", "address": "Address", "gender": "Gender", "valid_till": "Valid till",
                 "date_of_expiry": "Expiry date", "date_of_issue": "Issue date", "nationality": "Nationality",
                 # added 2026-10-06 with the printed financial fields
                 "guardian_name": "Guardian's name", "relation_name": "Relative's name", "relation_type": "Relation",
                 "pin_code": "PIN code", "issue_date": "Issue date",
                 "employer_name": "Employer", "net_pay": "Net pay", "gross_pay": "Gross pay",
                 "pay_period": "Pay period", "total_deductions": "Total deductions",
                 "assessment_year": "Assessment year", "form_number": "ITR form",
                 "acknowledgement_number": "Acknowledgement number", "filing_date": "Filing date",
                 "total_income": "Total income", "taxes_paid": "Taxes paid",
                 "account_holder_name": "Account holder", "account_number_masked": "Account number",
                 "period_start": "Statement from", "period_end": "Statement to", "closing_balance": "Closing balance",
                 "article_type": "Article", "registration_reference": "Registration reference"}

#: amounts shown as rupees, Indian-grouped (₹1,23,456)
_MONEY = {"net_pay", "gross_pay", "total_deductions", "total_income", "taxes_paid", "closing_balance"}
#: the same value already shown under a clearer name
_REDUNDANT = {"BANK_STATEMENT": {"name"}}


def field_label(key: str) -> str:
    return _FIELD_LABELS.get(str(key), str(key).replace("_", " ").capitalize())


def _rupees(value: Any) -> str:
    """₹ with Indian digit grouping; the value as recorded when it is not a number."""
    try:
        amount = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value)
    whole, paise = f"{abs(amount):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(([head] if head else []) + groups + [tail]) if head or groups else tail
    return f"{'-' if amount < 0 else ''}₹{grouped}" + (f".{paise}" if paise != "00" else "")


def format_field(key: str, value: Any) -> str:
    return _rupees(value) if key in _MONEY else str(value)

#: Spoken document names -> stored document_type. Longest forms first so
#: "bank statement" is not read as "bank".
_DOCUMENTS: tuple[tuple[str, str], ...] = (
    (r"salary\s*slip|pay\s*slip|payslip", "SALARY_SLIP"),
    (r"bank\s*statement|bank\s*account|account\s*holder|account\s*statement",
     "BANK_STATEMENT"),
    (r"driving\s*licen[cs]e|\bdl\b", "DRIVING_LICENCE"),
    (r"voter\s*id|\bvoter\b|\bepic\b", "VOTER_ID"),
    (r"passport", "PASSPORT"),
    (r"aadhaa?r", "AADHAAR"),
    (r"\bitr\b|income\s*tax\s*return", "ITR"),
    (r"form\s*16", "FORM_16"),
    (r"sale\s*deed", "SALE_DEED"),
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
    "ITR": "ITR",
    "FORM_16": "Form 16",
    "SALE_DEED": "sale deed",
}


def document_asked(message: str) -> str | None:
    """The document type the question names, or None."""
    text = message or ""
    for pattern, document_type in _DOCUMENTS:
        if re.search(pattern, text, re.IGNORECASE):
            return document_type
    return None


#: "what details were extracted", "kya kya nikla" -- the whole document, not one field
_ALL_FIELDS = re.compile(r"\b(details?|fields?|information|info|data|kya\s+kya|values?)\b[^?]{0,40}"
                         r"\b(extracted|read|captured|pulled|found|nikla|nikle|nikali)\b"
                         r"|\b(kya\s+kya|what)\b[^?]{0,20}\b(nikla|nikle|extracted|read)\b", re.I)


#: "pan details", "give me the licence info" -- the document asked about as a
#: whole. Counts as every field only when no single field is named
#: ("what is the name on my PAN" stays one field).
_GENERIC_DETAILS = re.compile(r"\b(details?|info(rmation)?|data|fields?)\b", re.I)


def wants_every_field(message: str) -> bool:
    """
    EVERY FIELD, unless the question names ONE ("what is the name on my PAN?").
    "PAN me kya likha hai?", "voter id ki jaankari", "salary slip ka data" all
    want the whole document -- answering them with just the name (the old
    default) hid everything else that was read (user report, 2026-10-06).
    """
    text = message or ""
    if _ALL_FIELDS.search(text):
        return True
    return not any(re.search(pattern, text, re.IGNORECASE) for pattern, *_ in _FIELDS)


#: "saare documents ki details", "all document details", "sab docs ka data"
ALL_DOCUMENTS = re.compile(
    r"\b(all|saare|sare|saari|sari|sab|sabhi|every|each|sagl[ei]|sarv[ae]?)\b[^?]{0,15}"
    r"\b(documents?|docs?|kagaz\w*|kagadpatr\w*|dastavez\w*)\b", re.I)


#: no single document named, but documents in general ("documents ki details")
_ANY_DOCUMENTS = re.compile(r"\b(documents?|docs?|kagaz\w*|kagadpatr\w*|dastavez\w*)\b", re.I)


def _lines(document_type: str, fields: dict[str, Any]) -> list[str]:
    """One '- Label: value' line per field: masked identifiers, ₹ amounts, nothing redundant."""
    from app.security import sensitivity

    shown = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (fields or {}).items()
             if not str(k).startswith("_") and k not in _REDUNDANT.get(document_type, set())}
    shown = {k: v for k, v in shown.items() if v not in (None, "", [], {}) and not isinstance(v, (dict, list))}
    masked = sensitivity.mask_payload(shown)
    for k in list(masked):
        # ALREADY MASKED means it STARTS with the mask -- "an X in the first four
        # characters" let a voter ID like ZAX0399947 through unmasked (2026-10-06)
        if k in ("pan_number", "pan", "dl_number", "epic_number", "passport_number", "account_number",
                 "aadhaar_number") and not str(masked[k]).startswith("XX"):
            masked[k] = sensitivity.mask(str(masked[k]))
    return [f"- {field_label(k)}: {format_field(k, v)}" for k, v in masked.items()]


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


def _whose(finding: Any) -> str:
    """'your', or "the co-applicant's" when the document's own record says so."""
    try:
        document = _repository().get_document(finding.document_id) if getattr(finding, "document_id", None) else None
    except Exception:  # noqa: BLE001 - an unreadable record keeps the default wording
        document = None
    role = str(getattr(document, "party_role", "") or "").upper()
    return "the co-applicant's" if role == "CO_APPLICANT" else "your"


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
    if document_type is None and (ALL_DOCUMENTS.search(message or "") or _ANY_DOCUMENTS.search(message or "")):
        return answer_all(case_id, party_id)
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
        # NEVER "NO PAN" WHILE A PAN IS ON THE CASE (FOS plan 6.6): the store's own document row is checked
        # before saying it is absent -- a rejected / unverified one is named with its state, its values unreleased
        try:
            stored = [d for d in _repository().list_documents(case_id) or []
                      if str(getattr(d, "document_type", "")).upper() == document_type
                      and str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper()
                      != "SUPERSEDED" and (party_id is None or getattr(d, "party_id", None) in (party_id, None))]
        except Exception:  # noqa: BLE001 - unreadable: the plain "not recorded" below stands
            stored = []
        if stored:
            state = str(getattr(getattr(stored[-1], "status", ""), "value", getattr(stored[-1], "status", ""))).lower()
            return (f"The {words} is on this case but it is {state}, so its details have not been released. "
                    f"Upload a clear, correct {words} to get them read.", [])
        # No document, so no value of any field: said without naming a field
        # the question may not have asked for.
        return (f"No {words} has been recorded for this case yet, so I "
                f"don't have its details.", [])

    verification = verified[-1]
    status = str(verification.status or "").upper() or "UNKNOWN"
    verification_source = _source(verification, document_type)
    # WHOSE DOCUMENT, from its own record: a co-applicant's PAN is never "your PAN"
    whose = _whose(verification)
    if whose != "your":
        words = f"{whose} {words}"
        your = ""
    else:
        your = "your "

    if status != "PASS":
        # THE RECORDED VERDICT AND REASONS, IN WORDS. The status and the
        # reason codes stay in `sources`; the sentence says what they mean
        # -- "under review", and the catalogue's text for each code --
        # rather than printing REVIEW (REQUIRED_FIELD_MISSING).
        from app.agents.applicant.copilot.answering.answer import _explained

        held = {"REVIEW": "is under review", "FAIL": "did not pass verification"}
        verb = held.get(status, f"was recorded as {status.lower()}")
        reasons = " ".join(_explained(code)
                           for code in (verification.reason_codes or [])[:2])
        return (f"{(your + words)[:1].upper() + (your + words)[1:]} {verb}."
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
    # EVERY RELEASED FIELD, when the question asks what was read -- not one field
    if extraction and wants_every_field(message):
        payload = dict(extraction.payload or {})
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        lines = _lines(document_type, fields)
        if lines:
            return (f"Details read from {your}{words} (it passed verification):\n" + "\n".join(lines),
                    [_source(extraction, document_type), verification_source])
    value = (extraction.payload or {}).get(key) if extraction else None
    if value:
        return (f"The {label} on {your}{words} is {value}.",
                [_source(extraction, document_type, field=key),
                 verification_source])

    # THE SAME DOCUMENT'S VALUE AS KYC RECORDED IT, where the released
    # extraction does not carry the field. Matched on the document's own
    # source id, so another document's value cannot answer.
    kyc_value, kyc = _kyc_value(findings, kyc_field, document_type,
                                verification.source_id)
    if kyc_value:
        return (f"The {label} on {your}{words} is {kyc_value}.",
                [_source(kyc, document_type, field=key), verification_source])

    owner = your + words
    return (f"{owner[:1].upper() + owner[1:]} passed verification, but no {label} was read "
            "from it.", [verification_source])


def answer_all(case_id: str, party_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    """
    EVERY UPLOADED DOCUMENT'S DETAILS, one section each (user request, 2026-10-06):
    a verified document lists every field read from it; one that did not pass says
    its state and why -- its values are not confirmed, so none is quoted.
    """
    from app.agents.applicant.copilot.answering.answer import _explained

    try:
        findings = _repository().get_current_findings(case_id, party_id=party_id)
    except Exception as exc:
        logger.warning("Document facts unavailable for %s: %r", case_id, exc)
        return ("The document details for this case could not be read right now.", [])
    verifications = [f for f in findings if _kind(f) == "VERIFICATION"]
    if not verifications:
        return ("No documents have been recorded for this case yet.", [])
    latest: dict[tuple, Any] = {}
    for v in verifications:                       # newest per document: findings arrive in write order
        latest[(v.party_id, v.source_id)] = v
    sections, sources = [], []
    for v in latest.values():
        document_type = str((v.payload or {}).get("type") or "").upper() or "UNKNOWN"
        words = _WORDS.get(document_type, document_type.replace("_", " ").title())
        heading = words[:1].upper() + words[1:]
        status = str(v.status or "").upper()
        if status != "PASS":
            reason = " ".join(_explained(c) for c in (v.reason_codes or [])[:1])
            state = "needs a review" if status == "REVIEW" else "did not pass verification"
            sections.append(f"{heading} -- {state}." + (f" {reason}" if reason else ""))
            sources.append(_source(v, document_type))
            continue
        extraction = next((f for f in reversed(findings) if _kind(f) == "EXTRACTION"
                           and f.source_id == v.source_id and f.party_id == v.party_id), None)
        payload = dict(extraction.payload or {}) if extraction else {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        lines = _lines(document_type, fields)
        sections.append(f"{heading} (verified):\n" + ("\n".join(lines) if lines else "- No details were read."))
        sources.append(_source(extraction or v, document_type))
    return ("Details read from the documents on this application:\n\n" + "\n\n".join(sections), sources)


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
