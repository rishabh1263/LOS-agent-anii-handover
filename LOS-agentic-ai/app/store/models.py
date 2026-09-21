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


__all__ = [
    "ACTIONABLE_STATUSES",
    "Applicant",
    "Application",
    "ApplicationStatus",
    "Document",
    "DocumentStatus",
    "SATISFYING_STATUSES",
    "VERDICT_TO_STATUS",
    "status_for_verdict",
    "utcnow",
]
