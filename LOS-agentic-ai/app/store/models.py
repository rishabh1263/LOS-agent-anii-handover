"""
The entities the FOS stage works with.

DELIBERATELY SMALL. An applicant, an application and the documents attached to
it -- nothing else. Credit, risk, RCU and underwriting keep their own state
downstream, and putting placeholder columns here for them would invite someone
to fill those columns in from the wrong place.

Plain dataclasses rather than an ORM. The repository interface is what the
rest of the service depends on, so the storage technology stays swappable; a
model class carrying session state would nail it to one.

Every status value here is set from a DETERMINISTIC source: the document
pipeline's own verdict, or a transition the FOS explicitly asked for through
an authorised tool. Nothing in this module infers a status, and no language
model ever writes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    """One clock, in UTC, so stored timestamps compare across machines."""
    return datetime.now(timezone.utc)


# ==========================================================================
# STATES
#
# Kept to the few the FOS stage actually distinguishes. A state nobody routes
# on is a state that drifts out of date silently.
# ==========================================================================

class ApplicationStatus(str, Enum):
    """Where an application sits in the FOS stage."""

    APPLICATION_CREATED = "APPLICATION_CREATED"
    DOCUMENT_COLLECTION = "DOCUMENT_COLLECTION"
    BASIC_DOCUMENT_VERIFICATION = "BASIC_DOCUMENT_VERIFICATION"
    READY_FOR_CPA = "READY_FOR_CPA"


class DocumentStatus(str, Enum):
    """
    What has happened to one document.

    MISSING is not stored -- it is the absence of a row, derived against the
    product's required list. Storing it would mean two places could disagree
    about whether a document exists.
    """

    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    VERIFIED = "VERIFIED"
    REVIEW = "REVIEW"
    REJECTED = "REJECTED"


#: Document statuses that satisfy a required-document slot.
SATISFYING_STATUSES = frozenset({DocumentStatus.VERIFIED})

#: Document statuses that need the FOS to do something.
ACTIONABLE_STATUSES = frozenset({DocumentStatus.REVIEW, DocumentStatus.REJECTED})


# ==========================================================================
# ENTITIES
# ==========================================================================

@dataclass
class Applicant:
    """A person applying. Basic FOS-captured information only."""

    applicant_id: str
    full_name: str | None = None
    mobile: str | None = None
    email: str | None = None
    date_of_birth: str | None = None
    address: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    #: Fields the readiness gate expects before a case leaves the FOS stage.
    #: Named here rather than in the gate so one edit changes both the
    #: completeness answer and the thing that explains it.
    REQUIRED_FIELDS = ("full_name", "mobile", "date_of_birth", "address")

    def missing_fields(self) -> list[str]:
        """Which required fields are still blank."""
        return [
            name for name in self.REQUIRED_FIELDS
            if not (getattr(self, name) or "").strip()
        ]

    def is_complete(self) -> bool:
        return not self.missing_fields()


@dataclass
class Application:
    """One loan application, belonging to one applicant."""

    case_id: str
    applicant_id: str
    status: ApplicationStatus = ApplicationStatus.APPLICATION_CREATED
    product: str | None = None
    loan_amount: str | None = None
    #: An applicant attribute the document policy may key on. Kept here
    #: rather than on the applicant because it is captured per application
    #: and can differ between two applications by the same person.
    #:
    #: NOT DEFAULTED. An uncaptured employment type means the rules that
    #: depend on it are reported as unevaluated, which is the honest
    #: outcome; guessing SALARIED would produce a checklist that looks
    #: complete and is not.
    employment_type: str | None = None

    # -- the loan terms, for affordability --------------------------------
    #
    # WHY THESE LIVE HERE AND NOT ON A REQUEST. FOIR needs a monthly
    # instalment, and an instalment needs an amount, a tenure and a rate.
    # `loan_amount` was already captured; the other two were not captured
    # anywhere, so the FOIR that already existed in the risk rules could
    # only ever be computed from figures a caller typed in by hand.
    #
    # ALL THREE OPTIONAL, AND NOT DEFAULTED. An application created
    # without a tenure is one the affordability check reports as
    # unassessable, naming the missing input. A default tenure would
    # produce an EMI that looks calculated and was invented, which is the
    # one thing this stage must never publish.
    #
    # STORED AS TEXT, like `loan_amount`, so the store keeps what was
    # captured and the parsing and validating happen in one place at the
    # point of use.
    tenure_months: str | None = None
    interest_rate_pct: str | None = None

    #: What the applicant says they already repay each month.
    #:
    #: DECLARED, AND ONLY EVER TREATED AS DECLARED. Eligibility reads it
    #: through a policy allowlist that is empty by default, so capturing
    #: it does not by itself put it into a FOIR -- see
    #: eligibility_policy.yaml.
    declared_monthly_obligations: str | None = None

    #: The property's value, for a secured product LTV applies to.
    #:
    #: DECLARED, and used only through the eligibility policy's accepted
    #: sources. Never derived from a sale deed's consideration price.
    property_value: str | None = None

    #: The second party on this case, when there is one.
    #:
    #: Held on the APPLICATION rather than the applicant because a person
    #: can be a co-applicant on one case and a primary applicant on
    #: another; the relationship belongs to the case, not to either party.
    co_applicant_id: str | None = None

    # -- the policy this case was assessed under --------------------------
    #
    # PINNED ON FIRST ASSESSMENT AND NOT MOVED. A policy file is edited
    # while applications are in flight; without a pin, an applicant who was
    # told on Monday that three documents were needed is told on Wednesday
    # that it is five, with no record of why. The pin is what makes a
    # checklist reproducible after the fact, and it is what an audit reads.
    policy_id: str | None = None
    policy_version: str | None = None
    policy_pinned_at: datetime | None = None

    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    REQUIRED_FIELDS = ("product",)

    def policy_attributes(self) -> dict[str, str]:
        """
        What the policy engine may key on, with absent values left absent.

        A key is present only when it holds something. The engine
        distinguishes "not captured" from "captured as empty", and passing
        an empty string would collapse that distinction here.
        """
        attributes: dict[str, str] = {}
        if (self.employment_type or "").strip():
            attributes["employment_type"] = self.employment_type.strip().upper()
        return attributes

    def missing_fields(self) -> list[str]:
        return [
            name for name in self.REQUIRED_FIELDS
            if not (getattr(self, name) or "").strip()
        ]


@dataclass
class Document:
    """
    One document attached to a case.

    `verification_status` and `reason_codes` are COPIED from the document
    pipeline's verdict, never recomputed here. This store records what
    verification concluded; it does not participate in concluding it.
    """

    document_id: str
    case_id: str
    applicant_id: str
    document_type: str
    # WHICH PERSON ON THE CASE THIS BELONGS TO.
    #
    # `applicant_id` alone was ambiguous the moment a case could carry two
    # people: it named the case's primary applicant whoever had actually
    # uploaded the file. `party_id` names the OWNER and `party_role` says
    # which seat they occupy.
    #
    # Defaults keep every existing row correct: a document stored before
    # co-applicants existed belongs to the primary applicant, which is what
    # `applicant_id` already meant.
    party_id: str | None = None
    party_role: str = "PRIMARY_APPLICANT"
    status: DocumentStatus = DocumentStatus.UPLOADED
    source_id: str | None = None
    verification_status: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    uploaded_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def needs_attention(self) -> bool:
        return self.status in ACTIONABLE_STATUSES

    @property
    def owner_id(self) -> str:
        """
        The party this document belongs to.

        Falls back to `applicant_id` for a row written before documents
        carried a party, where the two were the same thing.
        """
        return (self.party_id or "").strip() or self.applicant_id

    def belongs_to(self, party_id: str) -> bool:
        """
        Whether this document is this party's.

        THE ONE CHECK THAT STOPS CROSS-PARTY CONTAMINATION. Every read
        that is scoped to a person goes through it, so "whose document is
        this?" is answered in one place rather than re-derived by each
        caller from whichever field looked right.
        """
        wanted = str(party_id or "").strip()
        return bool(wanted) and self.owner_id == wanted


#: Maps a document pipeline verdict onto a stored document status.
#:
#: The pipeline answers PASS / REVIEW / FAIL / SKIPPED about verification. A
#: SKIPPED check established nothing, so the document stays UPLOADED rather
#: than being recorded as though it had been looked at.
VERDICT_TO_STATUS = {
    "PASS": DocumentStatus.VERIFIED,
    "REVIEW": DocumentStatus.REVIEW,
    "FAIL": DocumentStatus.REJECTED,
    "SKIPPED": DocumentStatus.UPLOADED,
}


def status_for_verdict(verdict: str | None) -> DocumentStatus:
    """Translate a pipeline verification verdict into a stored status."""
    return VERDICT_TO_STATUS.get(
        str(verdict or "").strip().upper(), DocumentStatus.UPLOADED
    )



# ==========================================================================
# CASE MEMORY
#
# WHAT THIS IS FOR. Everything above survives a request because the FOS stage
# needs it later. Everything the pipeline CONCLUDES -- what verification
# found, what KYC decided, what the bank statement showed, what risk scored --
# was computed, returned in one response and then lost. A question like "why
# did KYC go to review?" could not be answered five minutes later, because
# nothing had written the answer down.
#
# ONE TABLE FOR FINDINGS, NOT FIVE. Verification, KYC, financial, risk and
# RCU findings have the same shape: they belong to a party on a case, they
# carry a status, a score, a confidence, reason codes and some structured
# detail. Five near-identical tables would be five places to add a column and
# five migrations to keep in step. `finding_kind` discriminates instead.
#
# STRUCTURED FINDINGS ONLY. `payload` is for values the pipeline already
# publishes -- never OCR tokens, bounding boxes, prompts, model reasoning,
# raw tool payloads, credentials or filesystem paths. What is not worth
# putting in a response is not worth keeping in a database.
# ==========================================================================

class FindingKind(str, Enum):
    """
    What produced a finding.

    A closed vocabulary rather than a free string: a typo'd kind would
    write a row that no reader ever asks for, and it would look like
    successful persistence.
    """

    VERIFICATION = "VERIFICATION"
    EXTRACTION = "EXTRACTION"
    KYC = "KYC"
    PROFILE_MATCH = "PROFILE_MATCH"
    FINANCIAL = "FINANCIAL"
    RISK = "RISK"
    RCU = "RCU"


@dataclass
class CaseFinding:
    """
    One conclusion the pipeline reached, kept.

    OWNERSHIP IS NOT OPTIONAL. `case_id` is required and `party_id` is
    carried wherever the finding belongs to one person rather than to the
    case as a whole -- a KYC result is a party's, a cross-document check is
    the case's. A finding with no case cannot be scoped, so it is never
    written.
    """

    finding_id: str
    case_id: str
    finding_kind: FindingKind
    party_id: str | None = None
    stage: str | None = None
    status: str | None = None
    score: int | None = None
    confidence: int | None = None
    reason_codes: list[str] = field(default_factory=list)
    #: Structured detail, already public. Never raw OCR or model output.
    payload: dict[str, Any] = field(default_factory=dict)
    source_type: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    #: Bumped when the same logical finding is recorded again.
    version: int = 1
    #: When this row was LAST written. `created_at` is when it was first
    #: written and never moves; a re-run that reached a conclusion recorded
    #: before updates the old row, so only this says which is current.
    #: None on a row written before the column existed.
    updated_at: datetime | None = None
    #: Identifies an unchanged re-run, so a repeated request does not
    #: accumulate duplicate rows saying the same thing.
    content_hash: str | None = None


@dataclass
class DocumentVersion:
    """
    One upload of one document.

    A re-uploaded document is a NEW VERSION, not an overwrite. The current
    `documents` row keeps answering "what is the state of this document";
    this keeps "what was sent, and when" -- which is the question a reviewer
    asks when a document changed after a rejection.
    """

    document_version_id: str
    document_id: str
    case_id: str
    version: int = 1
    party_id: str | None = None
    source_id: str | None = None
    content_hash: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class CaseDecision:
    """
    A decision the pipeline RECORDED. Never one this layer made.

    Stored with the policy it was taken under, because a decision without
    its policy version cannot be explained six months later -- the rules
    will have moved and nothing will say which ones applied.
    """

    decision_id: str
    case_id: str
    decision: str | None = None
    next_action: str | None = None
    status: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    policy_id: str | None = None
    policy_version: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class CaseEvent:
    """
    One thing that happened on a case, in order.

    `sequence` exists because timestamps are not enough: several events are
    written inside one request and can share a millisecond, and a timeline
    that reorders itself between reads is worse than no timeline.
    """

    event_id: str
    case_id: str
    event_type: str
    party_id: str | None = None
    stage: str | None = None
    summary: str | None = None
    #: The finding, document or decision this event is about.
    ref_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    sequence: int = 0


__all__ = [
    "ACTIONABLE_STATUSES",
    "Applicant",
    "Application",
    "ApplicationStatus",
    "CaseDecision",
    "CaseEvent",
    "CaseFinding",
    "Document",
    "DocumentStatus",
    "DocumentVersion",
    "FindingKind",
    "SATISFYING_STATUSES",
    "VERDICT_TO_STATUS",
    "status_for_verdict",
    "utcnow",
]
