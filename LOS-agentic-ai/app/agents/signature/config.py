"""
Feature switches for Signature Verification.

Reads the `signature:` section of documents.yaml, with an environment
override per switch so an incident can be rolled back without editing a file
in a container image. Same shape as app/services/verification_config.py --
one configuration system, not two.

Every switch here is reported when it is off. A disabled capability returns
SIGNATURE_VERIFICATION_DISABLED; a disabled risk signal reports UNKNOWN
rather than LOW. A check that quietly stops running is indistinguishable
from a check that keeps passing, and that is the failure mode this avoids.
"""

from __future__ import annotations

import os

from app.services.verification_config import _load, reload  # noqa: F401


def _section() -> dict:
    return _load().get("signature", {}) or {}


def _flag(name: str, env: str, default: bool = True) -> bool:
    """Environment wins over YAML; YAML wins over the built-in default."""
    override = (os.getenv(env) or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return bool(_section().get(name, default))


def enabled() -> bool:
    """The master switch. Off means no signature work happens at all."""
    return _flag("enabled", "SIGNATURE_ENABLED")


def standalone_enabled() -> bool:
    """Whether an applicant may upload a signature image on its own."""
    return enabled() and _flag("standalone_enabled", "SIGNATURE_STANDALONE_ENABLED")


def document_enabled() -> bool:
    """Whether signatures embedded in ID documents are assessed."""
    return enabled() and _flag("document_enabled", "SIGNATURE_DOCUMENT_ENABLED")


def reference_comparison_enabled() -> bool:
    """
    Whether a submitted signature may be compared against a specimen.

    Turning this off removes the only route to PASS, which is the intended
    behaviour: with no comparison there is nothing to pass on.
    """
    return enabled() and _flag(
        "reference_comparison_enabled", "SIGNATURE_REFERENCE_COMPARISON_ENABLED"
    )


def synthetic_risk_detection_enabled() -> bool:
    return enabled() and _flag(
        "synthetic_risk_detection_enabled", "SIGNATURE_SYNTHETIC_RISK_ENABLED"
    )


def manipulation_detection_enabled() -> bool:
    return enabled() and _flag(
        "manipulation_detection_enabled", "SIGNATURE_MANIPULATION_DETECTION_ENABLED"
    )


__all__ = [
    "document_enabled",
    "enabled",
    "manipulation_detection_enabled",
    "reference_comparison_enabled",
    "reload",
    "standalone_enabled",
    "synthetic_risk_detection_enabled",
]
