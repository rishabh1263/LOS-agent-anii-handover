"""
Schemas for Signature Verification.

ONE service covers every document that carries a signature. The documents
differ only in where the signature sits and whether a reference exists, which
is configuration, not four implementations.

THE CENTRAL RULE, and the reason the vocabulary below is as wordy as it is:

    A signature being PRESENT says nothing about whether it is GENUINE.

Presence is ink in the right place. Genuineness needs a known-good reference
to compare against, and even then a competent forgery defeats it. So the
result separates three questions that are easy to blur together:

    signature_present   -- is there ink where a signature belongs?
    quality             -- is the image good enough to judge anything?
    comparison          -- does it match a reference, IF one exists?

A verdict never upgrades past what those three support. With no reference the
answer is NOT_COMPARABLE and the verdict is REVIEW, however clear the ink is.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignatureDocumentType(str, Enum):
    """Which document the signature was taken from."""

    BANK_SIGNATURE = "BANK_SIGNATURE"
    PAN_SIGNATURE = "PAN_SIGNATURE"
    DRIVING_LICENSE_SIGNATURE = "DRIVING_LICENSE_SIGNATURE"
    PASSPORT_SIGNATURE = "PASSPORT_SIGNATURE"

    # An applicant uploading a signature image on its own, with no
    # surrounding document. Same service, different intake: there is no card
    # to locate a signature band within, so the whole image is the subject.
    STANDALONE_SIGNATURE = "STANDALONE_SIGNATURE"


class InputMode(str, Enum):
    """
    How the signature arrived.

    The distinction is real and worth a type: an embedded signature sits in a
    known region of a known document and the rest of the frame is context,
    whereas a standalone upload IS the subject and an empty frame means the
    applicant uploaded nothing usable.
    """

    DOCUMENT_SIGNATURE = "DOCUMENT_SIGNATURE"
    STANDALONE_SIGNATURE = "STANDALONE_SIGNATURE"


class RiskLevel(str, Enum):
    """
    A risk signal's reading.

    UNKNOWN is not a failure and not a pass: it is what an honest heuristic
    says when it was switched off, or could not measure anything. It must
    never be collapsed into LOW.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SignatureStatus(str, Enum):
    """How the call ended, separate from the verdict."""

    OK = "OK"
    INVALID_FILE = "INVALID_FILE"
    UNSUPPORTED_DOCUMENT = "UNSUPPORTED_DOCUMENT"
    FAILED = "FAILED"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    INDETERMINATE = "INDETERMINATE"


class QualityStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"


class ComparisonStatus(str, Enum):
    """
    The outcome of comparing against a reference.

    NOT_COMPARABLE is the default and the honest answer in most production
    cases: banks hold reference signatures, this service usually is not given
    one, and nothing may be inferred from its absence.
    """

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    NO_REFERENCE = "NO_REFERENCE"


class Decision(str, Enum):
    """The deterministic verdict. Never produced or altered by a model."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class ReasonCode(str, Enum):
    FILE_UNREADABLE = "FILE_UNREADABLE"
    UNSUPPORTED_DOCUMENT_TYPE = "UNSUPPORTED_DOCUMENT_TYPE"

    SIGNATURE_PRESENT = "SIGNATURE_PRESENT"
    SIGNATURE_ABSENT = "SIGNATURE_ABSENT"
    SIGNATURE_BLANK = "SIGNATURE_BLANK"
    SIGNATURE_NOT_FOUND = "SIGNATURE_NOT_FOUND"
    SIGNATURE_STRIP_BLANK = "SIGNATURE_STRIP_BLANK"
    SIGNATURE_REGION_NOT_FOUND = "SIGNATURE_REGION_NOT_FOUND"

    # The subject looks like printed matter -- typed text, a logo, a form
    # border -- rather than a handwritten mark.
    SIGNATURE_NOT_HANDWRITTEN = "SIGNATURE_NOT_HANDWRITTEN"

    IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
    IMAGE_LOW_CONTRAST = "IMAGE_LOW_CONTRAST"
    IMAGE_BLURRED = "IMAGE_BLURRED"
    IMAGE_CLIPPED = "IMAGE_CLIPPED"
    IMAGE_OVER_COMPRESSED = "IMAGE_OVER_COMPRESSED"
    SIGNATURE_CROPPED = "SIGNATURE_CROPPED"
    SIGNATURE_LOW_QUALITY = "SIGNATURE_LOW_QUALITY"
    QUALITY_INSUFFICIENT_FOR_COMPARISON = "QUALITY_INSUFFICIENT_FOR_COMPARISON"

    SIGNATURE_MANIPULATION_SUSPECTED = "SIGNATURE_MANIPULATION_SUSPECTED"
    SIGNATURE_SYNTHETIC_RISK = "SIGNATURE_SYNTHETIC_RISK"
    RISK_SIGNALS_UNAVAILABLE = "RISK_SIGNALS_UNAVAILABLE"

    REFERENCE_UNAVAILABLE = "REFERENCE_UNAVAILABLE"
    REFERENCE_MISSING = "SIGNATURE_REFERENCE_MISSING"
    REFERENCE_UNREADABLE = "REFERENCE_UNREADABLE"
    NOT_COMPARABLE = "SIGNATURE_NOT_COMPARABLE"
    COMPARISON_MATCH = "COMPARISON_MATCH"
    COMPARISON_MISMATCH = "COMPARISON_MISMATCH"
    COMPARISON_INCONCLUSIVE = "COMPARISON_INCONCLUSIVE"

    VERIFICATION_DISABLED = "SIGNATURE_VERIFICATION_DISABLED"

    # Attached to every outcome, including a comparison MATCH.
    AUTHENTICITY_NOT_ESTABLISHED = "AUTHENTICITY_NOT_ESTABLISHED"


class Region(BaseModel):
    """Where in the image the signature was found."""

    x0: int
    y0: int
    x1: int
    y1: int
    source: str = ""

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class EvidenceRef(BaseModel):
    source_id: str
    locator: str
    detail: str = ""


class SignatureOutcome(BaseModel):
    """One signature assessment."""

    request_id: str = ""
    source_id: str = ""
    capability: str = "signature_verification"
    document_type: SignatureDocumentType | None = None

    status: SignatureStatus
    decision: Decision | None = None

    input_mode: InputMode = InputMode.DOCUMENT_SIGNATURE

    presence: PresenceStatus = PresenceStatus.INDETERMINATE
    quality: QualityStatus = QualityStatus.INSUFFICIENT
    comparison: ComparisonStatus = ComparisonStatus.NO_REFERENCE

    #: Heuristic risk signals. UNKNOWN when disabled or unmeasurable. These
    #: can send a signature to review; they never clear one.
    synthetic_risk: RiskLevel = RiskLevel.UNKNOWN
    manipulation_risk: RiskLevel = RiskLevel.UNKNOWN

    #: Only set when a comparison actually ran against a readable reference.
    comparison_score: float | None = None
    reference_available: bool = False

    region: Region | None = None
    ink_density: float | None = None

    fields: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    confidence: float = 0.0

    reason_codes: list[ReasonCode] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    processing_ms: float = 0.0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    #: Never true. No amount of pixel comparison establishes authenticity.
    authenticity_verified: bool = False

    def as_tool_payload(self) -> dict[str, Any]:
        """
        The wire form.

        Carries a nested `signature` block alongside the flat fields, because
        a caller asking "is this signature usable" wants the four answers --
        present, quality, comparison, risk -- in one place rather than
        assembled from across the envelope.
        """
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["signature"] = {
            "present": self.presence is PresenceStatus.PRESENT,
            "presence": self.presence.value,
            "quality": self.quality.value,
            "comparison": self.comparison.value,
            "match_score": self.comparison_score,
            "synthetic_risk": self.synthetic_risk.value,
            "manipulation_risk": self.manipulation_risk.value,
        }
        return payload


__all__ = [
    "Check",
    "ComparisonStatus",
    "Decision",
    "EvidenceRef",
    "InputMode",
    "PresenceStatus",
    "QualityStatus",
    "ReasonCode",
    "Region",
    "RiskLevel",
    "SignatureDocumentType",
    "SignatureOutcome",
    "SignatureStatus",
]
