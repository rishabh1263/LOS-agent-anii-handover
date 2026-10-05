"""
TWO-ENGINE VOTING FOR IDENTITY FIELDS -- a second OCR opinion, accepted only on evidence.

THE INDUSTRY RULE: when the primary engine leaves a REQUIRED field missing or
invalid, a second engine reads the same (already preprocessed) image, its tokens
go through the SAME field extractors and validators, and a value is accepted only
if it VALIDATES. Where both engines produce valid but DIFFERENT values, neither is
silently chosen: the field is reported as a disagreement for a reviewer.

    primary    RapidOCR (the recogniser's tokens)
    second     Tesseract (TesseractEngine, eng; DOCUMENT_VOTING_LANG to change)

ONLY ON A SHORTFALL. A document whose required fields are all present and valid
pays nothing (DOCUMENT_VOTING_ENABLED, default on). The verdict code downstream is
unchanged: it reads the merged fields exactly as it read the primary ones.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return (os.getenv("DOCUMENT_VOTING_ENABLED", "true") or "true").lower() == "true"


def _norm(value: Any) -> str:
    return "".join(str(value or "").upper().split())


def second_opinion(recognition: Any) -> dict[str, Any]:
    """Fill missing / invalid REQUIRED fields from a second engine, by validation only."""
    from app.agents.document_agent import pipeline
    from app.agents.document_agent import preprocess as PP
    from app.agents.document_agent.schemas import (DocumentStatus, DocumentType, FieldStatus,
                                                   ValidationStatus)

    report: dict[str, Any] = {"ran": False}
    result = getattr(recognition, "result", None)
    if not enabled() or result is None or result.document_type is DocumentType.UNKNOWN \
            or getattr(recognition, "image", None) is None or pipeline._is_complete(result):
        return report

    started = time.perf_counter()
    try:
        from app.agents.document_agent.ocr import TesseractEngine

        tokens, _ = TesseractEngine().read_array(PP.to_array(recognition.image))
        other = pipeline.extract_from_tokens(tokens, ocr_engine="tesseract", image=recognition.image,
                                             force_type=result.document_type)
    except Exception as exc:  # noqa: BLE001 - no second engine: the primary result stands
        logger.info("Second OCR opinion unavailable: %s", exc)
        return {"ran": False, "error": type(exc).__name__}

    filled, disagreements = [], []
    for name, (_validator, _required) in pipeline._spec_for(result.document_type).items():
        mine, theirs = result.fields.get(name), other.fields.get(name)
        if theirs is None or theirs.status is not FieldStatus.EXTRACTED \
                or theirs.validation is not ValidationStatus.VALID:
            continue
        usable = (mine is not None and mine.status is FieldStatus.EXTRACTED
                  and mine.validation is not ValidationStatus.INVALID)
        if not usable:
            theirs.evidence = f"[second engine: tesseract] {theirs.evidence or ''}".strip()
            result.fields[name] = theirs
            filled.append(name)
        elif mine.validation is ValidationStatus.VALID and _norm(mine.value) != _norm(theirs.value):
            disagreements.append(name)

    if filled:
        result.status = DocumentStatus.SUCCESS if pipeline._is_complete(result) else DocumentStatus.PARTIAL
        result.warnings.append("second_engine_filled=" + ",".join(filled))
    if disagreements:
        result.warnings.append("engine_disagreement=" + ",".join(disagreements))
    return {"ran": True, "engine": "tesseract", "filled": filled, "disagreements": disagreements,
            "ms": round((time.perf_counter() - started) * 1000, 1)}


__all__ = ["enabled", "second_opinion"]
