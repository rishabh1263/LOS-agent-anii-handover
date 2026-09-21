"""
Stage switches for POST /api/v1/los/process.

Reads the `los:` section of documents.yaml, with a per-flag environment
override. Same shape as app/services/verification_config.py and
app/agents/signature/config.py -- one configuration mechanism, three
consumers, no second system.

EVERY OFF STATE IS REPORTED. Turning a stage off does not make it quietly
absent from the response: verification off makes verification SKIPPED with a
reason code and extraction null, a disabled specialist says so on the
document it would have handled, and a disabled summary falls back to the
deterministic one. A stage that silently stops running is indistinguishable
from a stage that keeps passing, and that is the failure this avoids.
"""

from __future__ import annotations

import os

from app.services.verification_config import _load, reload  # noqa: F401


def _section() -> dict:
    return _load().get("los", {}) or {}


def _flag(name: str, default: bool = True) -> bool:
    """Environment wins over YAML; YAML wins over the built-in default."""
    override = (os.getenv(f"LOS_{name.upper()}") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return bool(_section().get(name, default))


def classification_enabled() -> bool:
    return _flag("classification_enabled")


def verification_enabled() -> bool:
    return _flag("verification_enabled")


def extraction_enabled() -> bool:
    return _flag("extraction_enabled")


def financial_enabled() -> bool:
    return _flag("financial_enabled")


def kyc_enabled() -> bool:
    return _flag("kyc_enabled")


def conflict_detection_enabled() -> bool:
    """Whether cross-document disagreements are reported."""
    return _flag("conflict_detection_enabled")


def signature_enabled() -> bool:
    return _flag("signature_enabled")


def business_evidence_enabled() -> bool:
    return _flag("business_evidence_enabled")


def sale_deed_enabled() -> bool:
    return _flag("sale_deed_enabled")


def collateral_customer_enabled() -> bool:
    """Off by default: no contract exists for the external service."""
    return _flag("collateral_customer_enabled", default=False)


def collateral_property_enabled() -> bool:
    """Off by default: no contract exists for the external service."""
    return _flag("collateral_property_enabled", default=False)


def llm_summary_enabled() -> bool:
    return _flag("llm_summary_enabled")


#: Specialist agent id -> the flag that governs it, so the router can ask
#: one question instead of carrying a branch per capability.
SPECIALIST_FLAGS = {
    "signature_verification": signature_enabled,
    "business_evidence": business_evidence_enabled,
    "sale_deed": sale_deed_enabled,
}


def specialist_enabled(agent_id: str) -> bool:
    check = SPECIALIST_FLAGS.get(agent_id)
    return check() if check else True


def snapshot() -> dict[str, bool]:
    """Every flag as currently resolved. Reported, not guessed at."""
    return {
        "classification_enabled": classification_enabled(),
        "verification_enabled": verification_enabled(),
        "extraction_enabled": extraction_enabled(),
        "financial_enabled": financial_enabled(),
        "kyc_enabled": kyc_enabled(),
        "conflict_detection_enabled": conflict_detection_enabled(),
        "signature_enabled": signature_enabled(),
        "business_evidence_enabled": business_evidence_enabled(),
        "sale_deed_enabled": sale_deed_enabled(),
        "collateral_customer_enabled": collateral_customer_enabled(),
        "collateral_property_enabled": collateral_property_enabled(),
        "llm_summary_enabled": llm_summary_enabled(),
    }


__all__ = [
    "SPECIALIST_FLAGS",
    "business_evidence_enabled",
    "classification_enabled",
    "conflict_detection_enabled",
    "collateral_customer_enabled",
    "collateral_property_enabled",
    "extraction_enabled",
    "financial_enabled",
    "kyc_enabled",
    "llm_summary_enabled",
    "reload",
    "sale_deed_enabled",
    "signature_enabled",
    "snapshot",
    "specialist_enabled",
    "verification_enabled",
]
