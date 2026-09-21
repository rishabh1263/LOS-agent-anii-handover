"""
Verification Agent.

Single owner of "is this document acceptable?" for every document class.

Verification was previously spread across three places: a stub package that
contained no checks, the extraction agent, and per-document API routes that
each re-implemented the decision. A caller could not tell which rules applied
to which document, and adding a class meant editing several files. This agent
holds the decision logic; the routes only carry HTTP.

Two depths are offered, and they answer different questions:

    QUICK  -- is this a legible document of the claimed class, with a
              well-formed identifier? One OCR pass, no field extraction.

    FULL   -- everything QUICK checks, plus every field extracted and
              validated. Slower, and the only depth that can report a field
              as invalid rather than merely absent.

NEITHER establishes authenticity. Both answer whether a document is readable
and well-formed, not whether it is genuine; a competent forgery passes both.
Tamper detection needs photo-level analysis and is out of scope here.
"""

from __future__ import annotations

import logging
import os
import time
from enum import Enum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class VerificationDepth(str, Enum):
    QUICK = "QUICK"
    FULL = "FULL"


class VerificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    SKIPPED = "SKIPPED"


class Check(BaseModel):
    name: str
    passed: bool
    detail: str | None = None


class VerificationResult(BaseModel):
    document_type: str
    requested_type: str | None = None
    status: VerificationStatus
    depth: VerificationDepth

    enabled: bool = True
    confidence: float = 0.0
    checks: list[Check] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    identifier: str | None = None

    # Always false. Present so a PASS can never be read as proof the document
    # is genuine.
    authenticity_checked: bool = False

    processing_ms: float = 0.0
    request_id: str = ""
    hint: str | None = None


# Every document class this agent can verify, and which depths apply.
IDENTITY_CLASSES = ("PAN", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT", "AADHAAR")
FINANCIAL_CLASSES = ("BANK_STATEMENT", "ITR", "SALARY_SLIP")
OTHER_CLASSES = ("MARK_SHEET", "SALE_DEED")

ALL_CLASSES = IDENTITY_CLASSES + FINANCIAL_CLASSES + OTHER_CLASSES


def enabled() -> bool:
    """
    Whether the verification agent is switched on at all.

    Separate from enabled_for(): this is the agent itself, that is a single
    document class. Kept because deployment configuration and existing tests
    refer to it.
    """
    return (
        os.getenv("DOCUMENT_AGENT_ENABLED", "true") or "true"
    ).strip().lower() == "true"


def version() -> str:
    return os.getenv("DOCUMENT_AGENT_VERSION", "1.0.0")


def enabled_for(document_type: str) -> bool:
    """
    Whether verification runs for this class.

    A per-document setting overrides the global one, so a single risky class
    can be gated without switching the check on everywhere.
    """
    specific = os.getenv(f"VERIFICATION_ENABLED_{document_type.upper()}")
    if specific is not None and specific.strip():
        return specific.strip().lower() == "true"
    return (os.getenv("VERIFICATION_ENABLED", "true") or "true").strip().lower() == "true"


def _skipped(document_type: str, depth: VerificationDepth, request_id: str,
             started: float) -> VerificationResult:
    return VerificationResult(
        document_type=document_type,
        status=VerificationStatus.SKIPPED,
        depth=depth,
        enabled=False,
        reason_codes=["VERIFICATION_DISABLED"],
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
        request_id=request_id,
        hint="Verification is switched off for this document type.",
    )


def verify_quick(image_or_path, requested_type: str | None,
                 request_id: str = "") -> VerificationResult:
    """Structural check only. One OCR pass, no field extraction."""
    started = time.perf_counter()
    doc_type = (requested_type or "AUTO").upper().replace("-", "_")

    if requested_type and not enabled_for(doc_type):
        return _skipped(doc_type, VerificationDepth.QUICK, request_id, started)

    from app.agents.verification.basic import quick_verify

    verdict = quick_verify(image_or_path, requested_type)

    return VerificationResult(
        document_type=verdict.document_class.value,
        requested_type=requested_type,
        status=VerificationStatus(verdict.status),
        depth=VerificationDepth.QUICK,
        confidence=verdict.confidence,
        checks=[Check(name=c.name, passed=c.passed, detail=c.detail)
                for c in verdict.checks],
        reason_codes=verdict.reason_codes,
        identifier=verdict.identifier_found,
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
        request_id=request_id,
        hint=verdict.retry_hint,
    )


def verify_extraction_result(result, requested_type: str | None,
                             request_id: str = "",
                             started: float | None = None) -> VerificationResult:
    """
    Turn a completed extraction into a verification verdict.

    Reusing the extraction avoids running OCR twice for one decision: it
    already establishes whether the class was identified, whether the
    identifiers validate, and which fields were legible.
    """
    started = started if started is not None else time.perf_counter()

    from app.agents.document_agent.schemas import (
        DocumentStatus, DocumentType, FieldStatus, ValidationStatus,
    )

    checks: list[Check] = []
    reasons: list[str] = []

    identified = result.document_type is not DocumentType.UNKNOWN
    checks.append(Check(name="document_identified", passed=identified,
                        detail=result.document_type.value))
    if not identified:
        reasons.append("DOC_TYPE_UNRECOGNISED")

    readable = result.status is not DocumentStatus.FAILED
    checks.append(Check(name="document_readable", passed=readable,
                        detail=result.status.value))
    if not readable:
        reasons.append("DOC_UNREADABLE")

    invalid = [n for n, f in result.fields.items()
               if f.validation is ValidationStatus.INVALID]
    checks.append(Check(name="identifiers_valid", passed=not invalid,
                        detail=", ".join(invalid) or "all validated fields passed"))
    if invalid:
        reasons.append("IDENTIFIER_INVALID")

    missing = [n for n, f in result.fields.items()
               if f.status is FieldStatus.MISSING]
    complete = result.status is DocumentStatus.SUCCESS
    checks.append(Check(name="required_fields_present", passed=complete,
                        detail=f"{len(missing)} field(s) missing" if missing else "complete"))
    if not complete:
        reasons.append("FIELDS_INCOMPLETE")

    if requested_type:
        wanted = requested_type.upper().replace("-", "_")
        if wanted not in {"AUTO", "ANY"}:
            matched = result.document_type.value == wanted
            checks.append(Check(
                name="matches_requested_type", passed=matched,
                detail=f"requested {wanted}, found {result.document_type.value}",
            ))
            if not matched:
                reasons.append("DOC_TYPE_MISMATCH")

    confidence = round(sum(1 for c in checks if c.passed) / max(1, len(checks)), 4)
    by_name = {c.name: c for c in checks}

    # Unidentifiable, unreadable, wrong type, or a malformed identifier are
    # hard failures: each is positive evidence of a problem. Missing fields
    # are a REVIEW, because a genuine document scanned badly should reach a
    # human rather than be rejected.
    hard = ("document_identified", "document_readable", "identifiers_valid",
            "matches_requested_type")
    if any(not by_name[n].passed for n in hard if n in by_name):
        status = VerificationStatus.FAIL
    elif not by_name["required_fields_present"].passed:
        status = VerificationStatus.REVIEW
    else:
        status = VerificationStatus.PASS

    return VerificationResult(
        document_type=result.document_type.value,
        requested_type=requested_type,
        status=status,
        depth=VerificationDepth.FULL,
        confidence=confidence,
        checks=checks,
        reason_codes=reasons,
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
        request_id=request_id,
    )


def verify_financial_result(result, request_id: str = "",
                            started: float | None = None) -> VerificationResult:
    """
    Verify a financial document.

    The decisive check here is the document's own arithmetic rather than a
    format rule: a bank statement whose balance reconciles and an ITR carrying
    a valid acknowledgement are self-evidencing. Where that cannot be
    established the answer is REVIEW -- never PASS, because an unverifiable
    figure must not be mistaken for a verified one.
    """
    started = started if started is not None else time.perf_counter()

    from app.agents.financial.schemas import FinancialDocumentType, FinancialStatus

    checks = [
        Check(name="document_identified",
              passed=result.document_type is not FinancialDocumentType.UNKNOWN,
              detail=result.document_type.value),
        Check(name="document_readable",
              passed=result.status is not FinancialStatus.FAILED,
              detail=result.status.value),
        Check(name="figures_self_verify",
              passed=result.verified is True,
              detail=result.verification_note or "self-check passed"),
    ]

    reasons: list[str] = []
    if not checks[0].passed:
        reasons.append("FIN_DOC_UNRECOGNISED")
    if not checks[1].passed:
        reasons.append("FIN_DOC_UNREADABLE")
    if result.verified is False:
        reasons.append("FIN_FIGURES_INCONSISTENT")
    elif result.verified is None:
        reasons.append("FIN_FIGURES_UNVERIFIED")

    confidence = round(sum(1 for c in checks if c.passed) / len(checks), 4)

    if not checks[0].passed or not checks[1].passed or result.verified is False:
        status = VerificationStatus.FAIL
    elif result.verified is None:
        status = VerificationStatus.REVIEW
    else:
        status = VerificationStatus.PASS

    return VerificationResult(
        document_type=result.document_type.value,
        status=status,
        depth=VerificationDepth.FULL,
        confidence=confidence,
        checks=checks,
        reason_codes=reasons,
        processing_ms=round((time.perf_counter() - started) * 1000, 2),
        request_id=request_id,
    )


def configuration() -> dict:
    """What this agent verifies, and how it is switched."""
    return {
        "classes": {
            "identity": list(IDENTITY_CLASSES),
            "financial": list(FINANCIAL_CLASSES),
            "other": list(OTHER_CLASSES),
        },
        "enabled": {c: enabled_for(c) for c in ALL_CLASSES},
        "global_switch": "VERIFICATION_ENABLED",
        "per_document_switch": "VERIFICATION_ENABLED_<CLASS>",
        "depths": {
            "QUICK": "class, legibility and identifier format. One OCR pass.",
            "FULL": "QUICK plus every field extracted and validated.",
        },
        "statuses": {
            "PASS": "acceptable, may proceed",
            "FAIL": "unidentifiable, unreadable, wrong type, or a malformed identifier",
            "REVIEW": "readable but incomplete or unverifiable; a human decides",
            "SKIPPED": "verification is switched off for this class",
        },
        "authenticity_checked": False,
        "authenticity_note": (
            "No depth detects tampering or forgery. Both answer whether a "
            "document is readable and well-formed, not whether it is genuine."
        ),
    }


__all__ = [
    "enabled", "version",
    "verify_quick", "verify_extraction_result", "verify_financial_result",
    "enabled_for", "configuration",
    "VerificationResult", "VerificationStatus", "VerificationDepth", "Check",
    "ALL_CLASSES", "IDENTITY_CLASSES", "FINANCIAL_CLASSES", "OTHER_CLASSES",
]
