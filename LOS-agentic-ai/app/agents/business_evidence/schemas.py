"""
Schemas for Business Evidence (Business Proof 1 and Business Proof 2).

ONE capability serves both LOS sections. The real dataset shows why: both are
field-visit photographs of business premises -- a shop front, an interior, the
proprietor standing in the doorway -- captured on a phone, many carrying a
"GPS Map Camera" overlay burned into the image with coordinates, an address
and a timestamp. They are the same evidence class appearing twice in the
application form, so they get one handler and differ only by which slot the
file arrived in.

WHAT THIS EVIDENCE CAN ESTABLISH

  * that a photograph was taken, and when, where and on what device, WHEN
    that metadata survives
  * that the image is of usable quality
  * whether several photos in a set agree with each other

WHAT IT CANNOT ESTABLISH, and must never claim:

  * that the business legally exists
  * that the applicant owns or operates it
  * that the premises are the applicant's
  * that the photograph is untampered

A photo of a shop is a photo of a shop. Ownership and existence come from
registration documents and the external on-site verification service, not
from pixels.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BusinessProofSlot(str, Enum):
    """Which LOS form slot the evidence arrived in."""

    BUSINESS_PROOF_1 = "BUSINESS_PROOF_1"
    BUSINESS_PROOF_2 = "BUSINESS_PROOF_2"


class EvidenceKind(str, Enum):
    """What the file turned out to be."""

    PHOTOGRAPH = "PHOTOGRAPH"
    PHOTO_COLLAGE = "PHOTO_COLLAGE"
    DOCUMENT_SCAN = "DOCUMENT_SCAN"
    UNKNOWN = "UNKNOWN"


class MetadataSource(str, Enum):
    """Where the location/time evidence came from."""

    EXIF = "EXIF"
    OVERLAY_OCR = "OVERLAY_OCR"
    NONE = "NONE"


class BusinessEvidenceStatus(str, Enum):
    """How the call ended, separate from the verdict."""

    OK = "OK"
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    INVALID_FILE = "INVALID_FILE"
    FAILED = "FAILED"


class Decision(str, Enum):
    """The deterministic verdict. Never produced or altered by a model."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class ReasonCode(str, Enum):
    FILE_UNREADABLE = "FILE_UNREADABLE"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"

    IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
    IMAGE_BLURRED = "IMAGE_BLURRED"
    IMAGE_TOO_DARK = "IMAGE_TOO_DARK"

    GEOTAG_PRESENT = "GEOTAG_PRESENT"
    GEOTAG_MISSING = "GEOTAG_MISSING"
    GEOTAG_INVALID = "GEOTAG_INVALID"

    TIMESTAMP_PRESENT = "TIMESTAMP_PRESENT"
    TIMESTAMP_MISSING = "TIMESTAMP_MISSING"

    METADATA_STRIPPED = "METADATA_STRIPPED"
    DEVICE_METADATA_PRESENT = "DEVICE_METADATA_PRESENT"

    # Attached to every outcome, including PASS.
    BUSINESS_EXISTENCE_NOT_ESTABLISHED = "BUSINESS_EXISTENCE_NOT_ESTABLISHED"
    OWNERSHIP_NOT_ESTABLISHED = "OWNERSHIP_NOT_ESTABLISHED"


class GeoPoint(BaseModel):
    latitude: float
    longitude: float

    def valid(self) -> bool:
        return -90.0 <= self.latitude <= 90.0 and -180.0 <= self.longitude <= 180.0


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class EvidenceRef(BaseModel):
    source_id: str
    locator: str
    detail: str = ""


class BusinessEvidenceOutcome(BaseModel):
    """One piece of business evidence, assessed."""

    request_id: str = ""
    source_id: str = ""
    capability: str = "business_evidence"
    document_type: BusinessProofSlot | None = None

    status: BusinessEvidenceStatus
    decision: Decision | None = None

    evidence_kind: EvidenceKind = EvidenceKind.UNKNOWN
    metadata_source: MetadataSource = MetadataSource.NONE

    fields: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    confidence: float = 0.0

    reason_codes: list[ReasonCode] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    processing_ms: float = 0.0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # Never true. Present so no caller has to infer it from absence.
    business_existence_verified: bool = False
    ownership_verified: bool = False

    def as_tool_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = [
    "BusinessEvidenceOutcome",
    "BusinessEvidenceStatus",
    "BusinessProofSlot",
    "Check",
    "Decision",
    "EvidenceKind",
    "EvidenceRef",
    "GeoPoint",
    "MetadataSource",
    "ReasonCode",
]
