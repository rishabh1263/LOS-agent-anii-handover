"""Verification Agent: the single owner of document verification."""

from app.agents.verification.agent import (
    ALL_CLASSES, Check, FINANCIAL_CLASSES, IDENTITY_CLASSES, OTHER_CLASSES,
    VerificationDepth, VerificationResult, VerificationStatus,
    configuration, enabled, enabled_for, verify_extraction_result,
    verify_financial_result, verify_quick, version,
)
from app.agents.verification.basic import DocumentClass, quick_verify

__all__ = [
    "verify_quick", "verify_extraction_result", "verify_financial_result",
    "enabled", "enabled_for", "version", "configuration",
    "VerificationResult", "VerificationStatus", "VerificationDepth", "Check",
    "ALL_CLASSES", "IDENTITY_CLASSES", "FINANCIAL_CLASSES", "OTHER_CLASSES",
    "DocumentClass", "quick_verify",
]
