from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ============================================================================
# OCR TEXT ITEM
# ============================================================================


class OCRTextItem(BaseModel):
    """
    One OCR-detected text item.

    Contains:
        - recognized text
        - OCR confidence
        - bounding box
    """

    text: str

    confidence: float = 0.0

    bbox: list[float] = Field(
        default_factory=list,
    )


# ============================================================================
# OCR RESULT
# ============================================================================


class OCRResult(BaseModel):
    """
    Standard output of the OCR layer.

    OCR is responsible only for reading the document.

    It does NOT:
        - verify the document
        - extract business fields
        - decide ACCEPT/REJECT
    """

    text: list[str] = Field(
        default_factory=list,
    )

    raw_text: str = ""

    confidence: float = 0.0

    processing_time_ms: float = 0.0

    items: list[OCRTextItem] = Field(
        default_factory=list,
    )


# ============================================================================
# EXTRACTION RESULT
# ============================================================================


class ExtractionResult(BaseModel):
    """
    Standard output of the Document Extraction Agent.

    Example:

        {
            "document_type": "PAN",
            "status": "SUCCESS",
            "confidence": 0.96,
            "fields": {
                "name": "JAYSHRI AJIT SINGH"
            }
        }
    """

    document_type: str

    status: str

    confidence: float = 0.0

    fields: dict[str, Any] = Field(
        default_factory=dict,
    )


# ============================================================================
# EXPORTS
# ============================================================================


__all__ = [
    "OCRTextItem",
    "OCRResult",
    "ExtractionResult",
]