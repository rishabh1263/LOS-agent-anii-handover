"""
FIELD STATE -- what the authoritative source says about ONE requested field.

Every field a person can ask for (a form value, an identifier read from a
document) resolves to a STRUCTURED STATE before any sentence is written:

    PRESENT        the source holds a value (said per the disclosure policy)
    NOT_PROVIDED   the applicant has not given it (form field not filled,
                   document not uploaded)
    NOT_AVAILABLE  given, but no value could be read from it yet (a document
                   uploaded whose extraction carries no such field)
    RESTRICTED     held, but the disclosure policy withholds the value
    UNKNOWN        the source could not be consulted right now

The state comes from the records only -- never from one tool's silence
(a form field the applicant record does not list as missing is NOT_AVAILABLE,
not "missing"), never from a model. The response layer maps the state to a
sentence in the caller's language (languages.yaml templates `field_*`).
Generic for every field: nothing here knows Aadhaar from a tenure.
"""

from __future__ import annotations

from contextvars import ContextVar

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class FieldState(str, Enum):
    PRESENT = "PRESENT"
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    RESTRICTED = "RESTRICTED"
    UNKNOWN = "UNKNOWN"


#: ANSWER-LEVEL EVIDENCE for this request: every field an answer resolved --
#: the field, its state, the source that decided it. Codes only, never a
#: value. Opened per request by the Copilot agent; a no-op when not open.
EVIDENCE: "ContextVar[list[dict[str, str]] | None]" = ContextVar("copilot_answer_evidence", default=None)


def record(field: str, state: str, source: str | None, *, party: str | None = None) -> None:
    """Note that the answer rests on `field` in `state`, from `source`."""
    log = EVIDENCE.get()
    if log is None:
        return
    entry = {"field": str(field).upper(), "state": str(state), "source": str(source or "unknown")}
    if party:
        entry["party"] = party
        # the same field already noted without its party: this is its party
        for known in log:
            if known["field"] == entry["field"] and "party" not in known:
                known["party"] = party
                return
    if entry not in log:
        log.append(entry)


@dataclass(frozen=True)
class Resolved:
    field: str
    state: FieldState
    value: Any = None          # the raw recorded value, PRESENT only
    source: str | None = None  # applicant.get | application.get | document:<TYPE>


#: Identifiers read from a document: field -> (document type, extracted key).
IDENTITY_FIELDS: dict[str, tuple[str, str]] = {
    "aadhaar_number": ("AADHAAR", "aadhaar_number"),
    "pan_number": ("PAN", "pan_number"),
    "bank_account_number": ("BANK_STATEMENT", "account_number"),
}


def resolve(field: str, results: dict[str, Any], *, case_id: str | None = None,
            party_id: str | None = None) -> Resolved:
    """The state of `field` for this case, from its authoritative source."""
    from app.agents.applicant.copilot.routing import capabilities
    from app.security import sensitivity

    if field in IDENTITY_FIELDS:
        resolved = _from_documents(field, case_id, party_id)
    elif field in capabilities.APPLICANT_FIELDS:
        resolved = _from_record(field, results, "applicant.get", "applicant")
    elif field in capabilities.APPLICATION_FIELDS:
        resolved = _from_record(field, results, "application.get", "application")
    else:
        return Resolved(field, FieldState.UNKNOWN)
    if resolved.state is FieldState.PRESENT and sensitivity.disclosure(field) == "withhold" \
            and field not in capabilities.IDENTIFIER_FIELDS:
        resolved = Resolved(field, FieldState.RESTRICTED, source=resolved.source)
    record(field, resolved.state.value, resolved.source)
    return resolved


def _from_record(field: str, results: dict[str, Any], tool: str, key: str) -> Resolved:
    from app.agents.applicant.copilot.answering.answer import _get

    payload = results.get(tool)
    if not isinstance(payload, dict):
        return Resolved(field, FieldState.UNKNOWN, source=tool)       # the tool did not answer
    record = _get(results, tool, key) or {}
    value = record.get(field)
    if value not in (None, ""):
        return Resolved(field, FieldState.PRESENT, value=value, source=tool)
    if field in (record.get("missing_fields") or []):
        return Resolved(field, FieldState.NOT_PROVIDED, source=tool)
    return Resolved(field, FieldState.NOT_AVAILABLE, source=tool)


def _from_documents(field: str, case_id: str | None, party_id: str | None) -> Resolved:
    """An identifier read from the party's own document of the right type."""
    document_type, key = IDENTITY_FIELDS[field]
    source = f"document:{document_type}"
    if not case_id:
        return Resolved(field, FieldState.UNKNOWN, source=source)
    try:
        from app.store import get_repository

        findings = get_repository().get_current_findings(case_id, party_id=party_id)
    except Exception as exc:  # noqa: BLE001 - the store, not the field, is unavailable
        logger.warning("Field state unavailable for %s: %r", case_id, exc)
        return Resolved(field, FieldState.UNKNOWN, source=source)

    def kind(f: Any) -> str:
        return str(getattr(getattr(f, "finding_kind", None), "value",
                           getattr(f, "finding_kind", "")) or "").upper()

    # WHICH DOCUMENT EACH FINDING IS ABOUT. The pipeline writes the type on
    # the verification finding and on the document row, not always on the
    # extraction of the same file: a finding's type is its own, or the type
    # recorded for the same file (source / document id) anywhere else.
    documents: list[Any] = []
    try:
        from app.store import request_cache

        documents = list(request_cache.read(get_repository(), "list_documents", case_id) or [])
    except Exception:  # noqa: BLE001 - the findings alone still decide
        documents = []
    if party_id is not None:
        documents = [d for d in documents if getattr(d, "party_id", None) in (None, party_id)]
    type_of_file: dict[str, str] = {}
    for d in documents:
        kind_ = str(getattr(d, "document_type", "") or "").upper()
        for ref in (getattr(d, "source_id", None), getattr(d, "document_id", None)):
            if ref and kind_:
                type_of_file[str(ref)] = kind_
    for f in findings:
        own = str((getattr(f, "payload", None) or {}).get("type") or "").upper()
        if own:
            for ref in (getattr(f, "source_id", None), getattr(f, "document_id", None)):
                if ref:
                    type_of_file.setdefault(str(ref), own)

    def type_of(f: Any) -> str:
        own = str((getattr(f, "payload", None) or {}).get("type") or "").upper()
        if own:
            return own
        for ref in (getattr(f, "source_id", None), getattr(f, "document_id", None)):
            if ref and str(ref) in type_of_file:
                return type_of_file[str(ref)]
        return ""

    of_type = [f for f in findings if type_of(f) == document_type]
    rows_of_type = [d for d in documents
                    if str(getattr(d, "document_type", "") or "").upper() == document_type]
    if not of_type and not rows_of_type:
        return Resolved(field, FieldState.NOT_PROVIDED, source=source)
    for f in reversed(of_type):
        if kind(f) == "EXTRACTION":
            value = (f.payload or {}).get(key)
            if value not in (None, ""):
                return Resolved(field, FieldState.PRESENT, value=value,
                                source=f"{source}:extraction")
    # The document row's own extracted fields, where the pipeline stored them.
    for d in reversed(rows_of_type):
        extracted = getattr(d, "extracted_fields", None)
        if isinstance(extracted, str):
            try:
                import json

                extracted = json.loads(extracted)
            except ValueError:
                extracted = None
        if isinstance(extracted, dict) and extracted.get(key) not in (None, ""):
            return Resolved(field, FieldState.PRESENT, value=extracted[key],
                            source=f"{source}:document_record")
    # The KYC comparison may carry the field the extraction did not.
    for f in reversed(findings):
        if kind(f) == "KYC":
            for entry in (getattr(f, "payload", None) or {}).get("fields") or []:
                if str(entry.get("field") or "").upper() in (key.upper(), document_type):
                    for side in entry.get("sources") or []:
                        if str(side.get("document_type") or "").upper() == document_type \
                                and side.get("value"):
                            return Resolved(field, FieldState.PRESENT, value=side["value"],
                                            source=f"{source}:kyc_comparison")
    return Resolved(field, FieldState.NOT_AVAILABLE, source=source)


_ENGLISH = {
    FieldState.NOT_PROVIDED: "You have not provided your {field} yet.",
    FieldState.NOT_AVAILABLE: "I don't have your {field} recorded on this application yet.",
    FieldState.RESTRICTED: ("Your {field} is on record, but for your security I can't share "
                            "the full value here."),
    FieldState.UNKNOWN: "I can't check your {field} right now. Please try again shortly.",
}


def say(resolved: Resolved, *, label: str, shown: str | None, language: str | None = None) -> str:
    """The state as a sentence -- localized when a template exists."""
    from app.agents.applicant import language as languages
    from app.agents.applicant.copilot.answering import phrasing

    seed = phrasing.current_seed(resolved.field)
    if resolved.state is FieldState.PRESENT:
        varied = phrasing.field_sentence(resolved.field, str(shown), language=language, seed=seed)
        text = varied or f"Your {label} is {shown}."
        return varied or languages.localized("field_present", language or "en", field=label,
                                             value=str(shown)) or text
    varied = phrasing.state_sentence(resolved.state.value, label, language=language, seed=seed)
    if varied:
        return varied
    template_key = f"field_{resolved.state.value.lower()}"
    return (languages.localized(template_key, language or "en", field=label)
            or _ENGLISH[resolved.state].format(field=label))


__all__ = ["FieldState", "IDENTITY_FIELDS", "Resolved", "resolve", "say"]
