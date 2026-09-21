"""
One party's declared profile, against that party's own documents.

WHAT THIS IS, AND WHAT KYC ALREADY IS. The existing KYC layer compares
DOCUMENTS WITH EACH OTHER — do the PAN and the licence describe one person?
This compares a DECLARED PROFILE WITH DOCUMENTS — does the person the
application says this is match the person the documents show? Two different
questions; both worth asking; neither replaces the other. KYC is untouched.

NOT A SECOND MATCHING ENGINE. Every comparison below delegates to the
deterministic matcher that already exists:

    NAME, FATHER_NAME  ->  app/services/name_match.match_names
    DATE_OF_BIRTH      ->  document_agent.normalize.normalize_date
    PAN_NUMBER         ->  document_agent.normalize.normalize_pan
    ADDRESS            ->  app/agents/kyc/address.compare
    confidence         ->  app/agents/kyc/confidence.score
    thresholds         ->  app/agents/kyc/config  (kyc_policies.yaml)

Writing a second normaliser is how two parts of one service end up
disagreeing about whether "R. SHARMA" and "RAJESH SHARMA" are the same
person.

PARTY ISOLATION IS THE POINT. The caller filters the case's documents
down to one party's -- with `parties.owned_by`, the one ownership filter
the response sections and KYC also use -- before anything is compared.
The primary applicant's profile is never held up against the
co-applicant's PAN, and the reverse. On one case both parties routinely
upload `pan.jpg`; without the filter, whichever was processed last would
answer for both.

THE GATE IS OBEYED, NOT RE-DECIDED. Only fields the verification gate
released are matched. The caller applies it -- `flow._released_for_matching`
runs the same `response.released_extraction` the response boundary uses --
because matching runs on the internal envelope, which carries extraction
whatever the verdict.

IT CHANGES NO VERDICT. This is an evidence layer. It cannot alter a
document's verification status, its reason codes or its score. A profile
mismatch is reported as profile evidence — the document is still whatever
verification found it to be.

NOTHING IS INVENTED. A profile field the caller did not supply is SKIPPED.
A document field the gate did not release is SKIPPED. Absence is never a
mismatch, which is why `fields_compared` is reported alongside the score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from app.agents.kyc.schemas import FieldStatus, KycField

logger = logging.getLogger(__name__)

#: The fields a profile can be matched on, in the order a reviewer reads
#: them. INCOME is deliberately absent: it is not an identity field and
#: comparing a declared income against a payslip is credit's question.
MATCHABLE = (
    KycField.NAME,
    KycField.DATE_OF_BIRTH,
    KycField.PAN_NUMBER,
    KycField.FATHER_NAME,
    KycField.ADDRESS,
)

#: Profile attribute -> the extracted document field(s) that can answer it.
#:
#: Several document types spell the same thing differently, so the first
#: key present wins. Resolved here rather than per document type, because
#: a per-type table is a per-type table to keep in step.
_DOCUMENT_FIELDS: dict[KycField, tuple[str, ...]] = {
    KycField.NAME: ("name",),
    KycField.DATE_OF_BIRTH: ("date_of_birth",),
    KycField.PAN_NUMBER: ("pan_number", "pan"),
    KycField.FATHER_NAME: ("father_name", "guardian_name", "relation_name"),
    KycField.ADDRESS: ("address",),
}

#: Reason codes. Reused from the existing vocabulary where one fits, so a
#: queue routing on reason codes does not need a second table.
MATCH = "PROFILE_MATCH"
PARTIAL = "PROFILE_PARTIAL_MATCH"
MISMATCH = "PROFILE_MISMATCH"
NO_PROFILE_VALUE = "PROFILE_VALUE_NOT_SUPPLIED"
NO_DOCUMENT_VALUE = "PROFILE_NOT_COMPARABLE"
FORMAT_INVALID = "FIELD_FORMAT_INVALID"


@dataclass
class Profile:
    """What the application says this person is."""

    name: str | None = None
    date_of_birth: str | None = None
    pan_number: str | None = None
    father_name: str | None = None
    address: str | None = None

    def value_for(self, field: KycField) -> str | None:
        raw = getattr(self, _ATTRIBUTE[field], None)
        text = str(raw).strip() if raw is not None else ""
        return text or None

    def is_empty(self) -> bool:
        return not any(self.value_for(f) for f in MATCHABLE)


_ATTRIBUTE = {
    KycField.NAME: "name",
    KycField.DATE_OF_BIRTH: "date_of_birth",
    KycField.PAN_NUMBER: "pan_number",
    KycField.FATHER_NAME: "father_name",
    KycField.ADDRESS: "address",
}


def from_request(**supplied: Any) -> Profile:
    """A profile from the caller's own fields."""
    return Profile(**{
        name: (str(value).strip() or None) if value is not None else None
        for name, value in supplied.items()
        if name in {f.value.lower() for f in MATCHABLE} or name in _ATTRIBUTE.values()
    })


def merged(request: Profile | None, stored: Profile | None) -> Profile:
    """
    The profile to match against: REQUEST FIRST, store fills the gaps.

    The caller is describing the person in front of them right now; the
    store is describing whoever was captured earlier. Where they differ
    the live one wins. Where the caller said nothing, the stored value is
    better than no comparison at all.

    NOTHING IS INVENTED. A field absent from both stays absent and its
    comparison is SKIPPED.
    """
    request = request or Profile()
    stored = stored or Profile()

    return Profile(**{
        attribute: (request.value_for(field) or stored.value_for(field))
        for field, attribute in _ATTRIBUTE.items()
    })


def stored_profile(applicant: Any) -> Profile:
    """
    A profile from a persisted applicant record.

    The store holds a name, a date of birth and an address. It holds no
    PAN and no father's name, so those two simply stay absent — they are
    matched only when the request supplies them. Designed so that adding
    the columns later needs no change here beyond reading them.
    """
    if applicant is None:
        return Profile()
    return Profile(
        name=getattr(applicant, "full_name", None),
        date_of_birth=getattr(applicant, "date_of_birth", None),
        address=getattr(applicant, "address", None),
        pan_number=getattr(applicant, "pan_number", None),
        father_name=getattr(applicant, "father_name", None),
    )


# ==========================================================================
# THE COMPARISONS -- each delegates to the existing matcher
# ==========================================================================


@dataclass
class Comparison:
    """One field compared, or the reason it was not."""

    field: KycField
    status: FieldStatus
    match_score: int = 0
    confidence: int = 0
    reason_code: str | None = None
    reason: str = ""
    source_id: str | None = None
    document_type: str | None = None
    #: The matcher that decided, for the confidence model. Internal.
    method: str = "UNKNOWN"
    #: The extractor's own reading of the document field, where it gave one.
    quality: float | None = None

    def public(self) -> dict[str, Any]:
        """
        What a caller sees. An allowlist, not a filter.

        No OCR text, no candidate list, no matcher internals: `method` and
        `quality` fed the confidence figure and describe how this service
        is built.
        """
        row: dict[str, Any] = {
            "field": self.field.value,
            "status": self.status.value,
            "match_score": self.match_score,
            "confidence": self.confidence,
        }
        if self.reason_code:
            row["reason_code"] = self.reason_code
        if self.reason:
            row["reason"] = self.reason
        if self.source_id:
            row["source"] = {
                "source_id": self.source_id,
                "document_type": self.document_type,
            }
        return row


def _skipped(field: KycField, code: str, reason: str) -> Comparison:
    """
    Nothing was compared.

    match_score 0 on a SKIPPED field means NOTHING WAS COMPARED, not that
    nothing matched — the same convention the KYC field results already
    use, and the reason `fields_compared` is published beside the score.
    """
    return Comparison(field=field, status=FieldStatus.SKIPPED,
                      reason_code=code, reason=reason)


def _compare_name(declared: str, evidence: "Evidence") -> tuple[FieldStatus, int, str]:
    from app.agents.kyc import config as kyc_config
    from app.services.name_match import match_names

    match_threshold = kyc_config.threshold("name", "match_threshold", 0.85)
    review_threshold = kyc_config.threshold("name", "review_threshold", 0.70)

    verdict = match_names(declared, str(evidence.value), threshold=match_threshold)
    score = int(round(verdict.score * 100))

    if verdict.match:
        return FieldStatus.PASS, score, verdict.method.value
    if verdict.score >= review_threshold:
        return FieldStatus.PARTIAL, score, verdict.method.value
    return FieldStatus.FAIL, score, verdict.method.value


def _compare_date(declared: str, evidence: "Evidence") -> tuple[FieldStatus, int, str]:
    """
    Exact or nothing.

    A date is either the same day or a different one. Scoring "close"
    dates would mean a transposed year reads as a near match, and a near
    match on a date of birth is a different person.
    """
    from app.agents.document_agent.normalize import normalize_date

    found = evidence.value
    left = normalize_date(str(declared))
    right = (found.isoformat() if hasattr(found, "isoformat")
             else normalize_date(str(found)))

    if not left or not right:
        return FieldStatus.SKIPPED, 0, "UNPARSEABLE"
    return (FieldStatus.PASS, 100, "EXACT") if left == right else (
        FieldStatus.FAIL, 0, "EXACT")


def _compare_pan(declared: str, evidence: "Evidence") -> tuple[FieldStatus, int, str]:
    """
    Exact after normalisation. NEVER fuzzy.

    Two PANs differing by one character are two different taxpayers. A
    similarity score here would eventually approve one.
    """
    from app.agents.document_agent.normalize import normalize_pan

    left = normalize_pan(str(declared))
    right = normalize_pan(str(evidence.value))

    if not left:
        return FieldStatus.SKIPPED, 0, "FORMAT_INVALID"
    if not right:
        return FieldStatus.SKIPPED, 0, "UNPARSEABLE"
    return (FieldStatus.PASS, 100, "EXACT") if left == right else (
        FieldStatus.FAIL, 0, "EXACT")


def _compare_address(declared: str, evidence: "Evidence") -> tuple[FieldStatus, int, str]:
    """
    Component by component, never as two strings.

    `kyc.address.compare` takes PARSED ADDRESSES, not text. Passing it the
    raw strings raised inside the comparator on every single call, and the
    defensive handler above turned that into SKIPPED -- so the address was
    silently never checked, on every application, while the response said
    only "could not be compared". Nothing failed loudly enough to notice.

    The document side is built from the whole released field map rather
    than the one `address` value, because the pincode -- the strongest
    single component -- is frequently extracted into its own field.
    """
    from app.agents.kyc import address as address_lib
    from app.agents.kyc import config as kyc_config
    from app.agents.kyc.schemas import AddressInput
    from app.agents.los.mapping import address_input

    section = kyc_config.section("address")
    weights = dict(section.get("weights") or {})
    component_threshold = float(section.get("component_threshold", 0.85))
    pass_score = float(section.get("pass_score", 0.80))
    review_score = float(section.get("review_score", 0.55))

    on_document = address_input(evidence.fields or {"address": evidence.value})
    if on_document is None:
        return FieldStatus.SKIPPED, 0, "NOT_COMPARABLE"

    raw, _verdicts, comparable = address_lib.compare(
        AddressInput(raw=declared), on_document, weights, component_threshold,
    )

    if not comparable:
        return FieldStatus.SKIPPED, 0, "NOT_COMPARABLE"

    score = int(round(raw * 100))
    if raw >= pass_score:
        return FieldStatus.PASS, score, "COMPONENT"
    if raw >= review_score:
        return FieldStatus.PARTIAL, score, "COMPONENT"
    return FieldStatus.FAIL, score, "COMPONENT"


_COMPARATORS = {
    KycField.NAME: _compare_name,
    KycField.FATHER_NAME: _compare_name,
    KycField.DATE_OF_BIRTH: _compare_date,
    KycField.PAN_NUMBER: _compare_pan,
    KycField.ADDRESS: _compare_address,
}

_HUMAN = {
    KycField.NAME: "name",
    KycField.DATE_OF_BIRTH: "date of birth",
    KycField.PAN_NUMBER: "PAN",
    KycField.FATHER_NAME: "father's name",
    KycField.ADDRESS: "address",
}


# ==========================================================================
# EVIDENCE -- only what the verification gate released
# ==========================================================================


@dataclass
class Evidence:
    """One released document field, and where it came from."""

    value: Any
    source_id: str
    document_type: str
    quality: float | None = None
    #: Every field this document released. An address is assembled from
    #: several of them -- the printed line and a separately extracted
    #: pincode -- so one value is not enough to compare it properly.
    fields: dict[str, Any] = dataclass_field(default_factory=dict)


def released_fields(documents: list[dict[str, Any]]) -> dict[str, list[Evidence]]:
    """
    The extracted fields this party's documents actually released.

    THE VERIFICATION GATE IS NOT RE-IMPLEMENTED HERE. `extraction` is
    present on a document result only when the gate released it — a FAIL,
    a REJECTED, a REVIEW whose fields were withdrawn, and a run with
    extraction disabled all arrive with no extraction at all. Reading the
    key is reading the gate's decision; second-guessing it would be a
    second gate.
    """
    found: dict[str, list[Evidence]] = {}

    for document in documents:
        extraction = document.get("extraction")
        if not isinstance(extraction, dict) or not extraction:
            continue

        # TWO SHAPES, ONE READER. Internally the flow carries
        # `extraction = {"fields": {...}, "field_quality": {...}}`; the
        # public response flattens it to `extraction = {...fields...}`.
        # Handling only the flat one silently found nothing when called
        # from inside the flow -- every field reported SKIPPED on a
        # document that had extracted perfectly.
        nested = extraction.get("fields")
        if isinstance(nested, dict):
            fields = nested
            quality = extraction.get("field_quality") or {}
        else:
            fields = extraction
            quality = document.get("field_quality") or {}

        if not fields:
            continue

        source_id = str(document.get("source_id") or "")
        # Same two shapes again: the internal result nests the class
        # under `document.type`, the public one hoists it to `type`.
        document_type = str(
            document.get("type")
            or (document.get("document") or {}).get("type")
            or "UNKNOWN"
        )

        for name, value in fields.items():
            if value is None or str(value).strip() == "":
                continue
            found.setdefault(name, []).append(Evidence(
                value=value,
                source_id=source_id,
                document_type=document_type,
                quality=(float(quality[name])
                         if isinstance(quality, dict) and name in quality
                         else None),
                fields=dict(fields),
            ))

    return found


def _evidence_for(
    field: KycField, released: dict[str, list[Evidence]],
) -> Evidence | None:
    for name in _DOCUMENT_FIELDS.get(field, ()):
        candidates = released.get(name)
        if candidates:
            return candidates[0]
    return None


# ==========================================================================
# MATCHING ONE PARTY
# ==========================================================================


@dataclass
class PartyMatch:
    """One party's profile, matched against their own documents."""

    party_id: str
    party_role: str
    comparisons: list[Comparison] = dataclass_field(default_factory=list)

    # -- coverage, so a gap is never read as a disagreement -------------
    fields_expected: int = 0
    fields_extracted: int = 0
    fields_compared: int = 0

    score: int = 0
    confidence: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "party_id": self.party_id,
            "party_role": self.party_role,
            "score": self.score,
            "confidence": self.confidence,
            "fields_expected": self.fields_expected,
            "fields_extracted": self.fields_extracted,
            "fields_compared": self.fields_compared,
            "fields": [c.public() for c in self.comparisons],
        }


def _weight(field: KycField) -> float:
    """The field's weight in the overall score, from KYC configuration."""
    from app.agents.kyc import fields as kyc_fields

    try:
        return float(kyc_fields.weight(field))
    except Exception:  # pragma: no cover - configuration failure
        return 1.0


def _confidence_for(comparison: Comparison, compared: int, total: int) -> int:
    """Delegates to the KYC confidence model. No second implementation."""
    from app.agents.kyc import confidence as kyc_confidence

    try:
        value, _factors = kyc_confidence.score(
            method=comparison.method,
            compared_sources=compared,
            total_sources=max(total, compared),
            raw_score=comparison.match_score / 100.0,
            threshold=None,
            qualities=([comparison.quality]
                       if comparison.quality is not None else None),
        )
        return int(value)
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Profile-match confidence failed")
        return 0


def match_party(
    *,
    party_id: str,
    party_role: str,
    profile: Profile,
    documents: list[dict[str, Any]],
) -> PartyMatch:
    """
    Match one party's profile against that party's own documents.

    `documents` MUST already be filtered to this party — see
    `documents_for_party`. This function does not know how to tell one
    person's uploads from another's, and giving it a mixed list would
    compare a profile against a stranger's PAN.
    """
    released = released_fields(documents)
    result = PartyMatch(party_id=party_id, party_role=party_role)

    for field in MATCHABLE:
        declared = profile.value_for(field)
        evidence = _evidence_for(field, released)

        if evidence is not None:
            result.fields_extracted += 1

        if declared is None:
            result.comparisons.append(_skipped(
                field, NO_PROFILE_VALUE,
                f"No {_HUMAN[field]} was supplied for this party, so there "
                f"was nothing to compare the documents against.",
            ))
            continue

        result.fields_expected += 1

        if evidence is None:
            result.comparisons.append(_skipped(
                field, NO_DOCUMENT_VALUE,
                f"No verified document released a {_HUMAN[field]} for this "
                f"party, so the supplied value could not be checked.",
            ))
            continue

        try:
            status, score, method = _COMPARATORS[field](declared, evidence)
        except Exception:  # pragma: no cover - a comparator must not crash
            logger.exception("Profile comparison failed for %s", field)
            result.comparisons.append(_skipped(
                field, NO_DOCUMENT_VALUE,
                f"The {_HUMAN[field]} could not be compared.",
            ))
            continue

        comparison = Comparison(
            field=field, status=status, match_score=score,
            source_id=evidence.source_id,
            document_type=evidence.document_type,
            method=method, quality=evidence.quality,
        )

        if status is FieldStatus.SKIPPED:
            comparison.reason_code = (
                FORMAT_INVALID if method == "FORMAT_INVALID"
                else NO_DOCUMENT_VALUE
            )
            comparison.reason = (
                f"The {_HUMAN[field]} could not be read in a comparable "
                f"form."
            )
        else:
            result.fields_compared += 1
            comparison.confidence = _confidence_for(
                comparison, compared=1, total=max(1, len(documents)),
            )
            if status is FieldStatus.PASS:
                comparison.reason_code = MATCH
                comparison.reason = (
                    f"The {_HUMAN[field]} on {evidence.document_type} matches "
                    f"the one supplied for this party."
                )
            elif status is FieldStatus.PARTIAL:
                comparison.reason_code = PARTIAL
                comparison.reason = (
                    f"The {_HUMAN[field]} on {evidence.document_type} partly "
                    f"matches the one supplied for this party."
                )
            else:
                comparison.reason_code = MISMATCH
                comparison.reason = (
                    f"The {_HUMAN[field]} on {evidence.document_type} does "
                    f"not match the one supplied for this party."
                )

        result.comparisons.append(comparison)

    _roll_up(result)
    return result


def _roll_up(result: PartyMatch) -> None:
    """
    The overall figures, over the fields ACTUALLY COMPARED.

    A field nobody could compare contributes nothing — neither a zero nor
    a pass. Averaging a missing field in as zero is how "we could not
    check the address" becomes "the address is wrong", which is the
    single most damaging thing a matching layer can get wrong.
    """
    compared = [c for c in result.comparisons
                if c.status is not FieldStatus.SKIPPED]

    if not compared:
        result.score = 0
        result.confidence = 0
        return

    total_weight = sum(_weight(c.field) for c in compared) or 1.0
    result.score = int(round(
        sum(c.match_score * _weight(c.field) for c in compared) / total_weight
    ))
    result.confidence = int(round(
        sum(c.confidence * _weight(c.field) for c in compared) / total_weight
    ))


__all__ = [
    "Comparison", "Evidence", "MATCHABLE", "PartyMatch", "Profile",
    "from_request", "match_party", "merged",
    "released_fields", "stored_profile",
]
