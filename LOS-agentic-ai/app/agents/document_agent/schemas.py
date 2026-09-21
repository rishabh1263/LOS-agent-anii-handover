"""Structured extraction contract for the Document Agent."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    PAN = "PAN"
    DRIVING_LICENCE = "DRIVING_LICENCE"
    VOTER_ID = "VOTER_ID"
    PASSPORT = "PASSPORT"
    UNKNOWN = "UNKNOWN"


class FieldStatus(str, Enum):
    EXTRACTED = "EXTRACTED"
    MISSING = "MISSING"
    UNCERTAIN = "UNCERTAIN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    NOT_VALIDATED = "NOT_VALIDATED"


class DocumentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class OCRToken(BaseModel):
    """
    One OCR line with spatial information.

    Engine-agnostic: any OCR that yields text, confidence and a box can feed
    this pipeline. Extraction never touches an image.
    """

    model_config = ConfigDict(extra="allow")

    text: str
    confidence: float = 0.0
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


class ExtractedField(BaseModel):
    """One field with its evidence and validation verdict."""

    value: Any = None
    confidence: float = 0.0
    status: FieldStatus = FieldStatus.MISSING
    validation: ValidationStatus = ValidationStatus.NOT_VALIDATED
    validation_error: str | None = None
    evidence: str | None = None      # raw OCR text the value came from
    ocr_confidence: float | None = None

    # Comparison key for name fields. The OCR engine sometimes emits a name
    # with no spaces ("LAXMISANTOSHGUPTA"); splitting it would require a
    # dictionary and was measured to CORRUPT names when attempted, so the
    # value is kept as read and a space-insensitive key is supplied for
    # matching against LOS records.
    match_key: str | None = None


class ProcessingTimes(BaseModel):
    ocr_ms: float = 0.0
    classification_ms: float = 0.0
    extraction_ms: float = 0.0
    normalization_ms: float = 0.0
    validation_ms: float = 0.0
    name_spacing_ms: float = 0.0
    name_spacing_calls: int = 0
    total_ms: float = 0.0

    # Telemetry: what the engine actually saw, and on which thread. Without
    # this it is impossible to tell a slow image from a slow code path.
    source_width: int = 0
    source_height: int = 0
    processed_width: int = 0
    processed_height: int = 0
    ocr_thread: str = ""
    ocr_passes: int = 0


class DocumentExtractionResult(BaseModel):
    """Structured result returned to the LOS."""

    document_type: DocumentType
    status: DocumentStatus
    classification_confidence: float = 0.0
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    processing: ProcessingTimes = Field(default_factory=ProcessingTimes)
    ocr_engine: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def value(self, name: str) -> Any:
        f = self.fields.get(name)
        return f.value if f else None


__all__ = [
    "DocumentType", "FieldStatus", "ValidationStatus", "DocumentStatus",
    "OCRToken", "ExtractedField", "ProcessingTimes", "DocumentExtractionResult",
]