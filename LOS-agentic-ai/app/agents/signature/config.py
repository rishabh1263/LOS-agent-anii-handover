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


def readable_name_verified() -> bool:
    """A readable cursive name (joined letters) on an uploaded / captured signature is VERIFIED (documents.yaml
    signature.readable_name_verified); off: any readable name goes to review."""
    return _flag("readable_name_verified", "SIGNATURE_READABLE_NAME_VERIFIED", default=False)


def name_max_chars() -> int:
    """The most letters / digits a signature's readable name may have (documents.yaml signature.name_max_chars);
    more text than this is a document, not a signature."""
    try:
        return int(_section().get("name_max_chars", 40))
    except (TypeError, ValueError):
        return 40


def picture_limits() -> tuple[float, int, float, float]:
    """(colour share, hue groups, mixed share, minimum paper share) that tell a picture from a signature
    (documents.yaml signature.picture_*)."""
    s = _section()
    try:
        return (float(s.get("picture_colour_share", 0.25)), int(s.get("picture_hue_groups", 3)),
                float(s.get("picture_mixed_share", 0.10)), float(s.get("picture_min_paper", 0.45)))
    except (TypeError, ValueError):
        return 0.25, 3, 0.10, 0.45


def padding_limits() -> tuple[bool, float, float, float]:
    """(trim on, flat-line std max, minimum difference from the paper, minimum share kept) for the bands a gallery /
    screenshot puts around a signature photo (documents.yaml signature.padding_*)."""
    s = _section()
    try:
        return (bool(s.get("padding_trim", True)), float(s.get("padding_line_std_max", 4.0)),
                float(s.get("padding_min_difference", 20)), float(s.get("padding_min_keep", 0.3)))
    except (TypeError, ValueError):
        return True, 4.0, 20.0, 0.3


def white_balance_max() -> float:
    """How far the picture test may neutralise a camera's colour cast, per channel (documents.yaml
    signature.picture_white_balance_max; 1.0 = off)."""
    try:
        return max(1.0, float(_section().get("picture_white_balance_max", 1.5)))
    except (TypeError, ValueError):
        return 1.5


def faint_limits() -> tuple[int, float]:
    """(the lighter ink line, the least share of the image under it) that tell a FAINT signature from a blank page
    (documents.yaml signature.faint_ink_level / faint_min_share)."""
    s = _section()
    try:
        return int(s.get("faint_ink_level", 200)), float(s.get("faint_min_share", 0.001))
    except (TypeError, ValueError):
        return 200, 0.001


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
