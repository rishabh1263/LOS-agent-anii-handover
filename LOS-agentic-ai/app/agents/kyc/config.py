"""
KYC policy loading.

Thresholds live in app/config/kyc_policies.yaml, never in the matching logic,
so changing credit policy is a reviewable config change rather than a code
change. Follows the same shape as the fraud & risk policy loader.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.constants import CONFIG_DIR
from app.core.exceptions import ConfigurationError

DEFAULT_POLICY_FILENAME = "kyc_policies.yaml"


def enabled() -> bool:
    return os.getenv("KYC_AGENT_ENABLED", "true").strip().lower() == "true"


def version() -> str:
    return os.getenv("KYC_AGENT_VERSION", "1.0.0")


def policy_path() -> Path:
    override = os.getenv("KYC_POLICY_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_POLICY_FILENAME


@lru_cache(maxsize=1)
def _load(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        raise ConfigurationError(f"KYC policy file not found: {path}")

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"KYC policy file is not valid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigurationError("KYC policy file must contain a mapping")

    return loaded


def get_policy() -> dict[str, Any]:
    """The whole policy document."""
    return _load(str(policy_path()))


def reset_policy_cache() -> None:
    """Drop the cached policy. For tests and controlled reloads."""
    _load.cache_clear()


def section(name: str) -> dict[str, Any]:
    """One policy section, or an empty mapping when it is absent."""
    value = get_policy().get(name)
    return value if isinstance(value, dict) else {}


def policy_version() -> str:
    return str(get_policy().get("policy_version", "unknown"))


def check_enabled(name: str) -> bool:
    return bool(section(name).get("enabled", True))


def check_blocking(name: str) -> bool:
    """
    Whether this check may drive the overall verdict to FAIL on its own.

    A non-blocking check can still report FAIL for itself -- the finding is not
    suppressed -- but it caps the overall result at REVIEW rather than
    rejecting an applicant outright. Address and income are non-blocking by
    default: documents legitimately disagree on both.
    """
    return bool(section(name).get("blocking", True))


def threshold(name: str, key: str, default: float) -> float:
    raw = section(name).get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


__all__ = [
    "enabled", "version", "policy_path", "get_policy", "reset_policy_cache",
    "section", "policy_version", "check_enabled", "check_blocking", "threshold",
]
