from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.constants import CONFIG_DIR
from app.core.exceptions import ConfigurationError

DEFAULT_POLICY_FILENAME = "risk_policy.yaml"


# ---------------------------------------------------------------------------
# AGENT TOGGLES (mirrors app/agents/document_verification/config.py)
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.getenv("FRAUD_RISK_AGENT_ENABLED", "true").lower() == "true"


def version() -> str:
    return os.getenv("FRAUD_RISK_AGENT_VERSION", "1.0.0")


def llm_summary_enabled() -> bool:
    return os.getenv("FRAUD_RISK_LLM_SUMMARY_ENABLED", "true").lower() == "true"


def llm_timeout_seconds() -> float:
    return float(os.getenv("FRAUD_RISK_LLM_TIMEOUT_SECONDS", "20"))


def environment() -> str:
    return os.getenv("ENVIRONMENT", "development").lower()


def is_production() -> bool:
    return environment() in {"production", "prod"}


def allow_unsigned_policy() -> bool:
    """Escape hatch for staging. Never set this true in production."""
    return os.getenv("ALLOW_UNSIGNED_RISK_POLICY", "false").lower() == "true"


def policy_path() -> Path:
    override = os.getenv("RISK_POLICY_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_POLICY_FILENAME


# ---------------------------------------------------------------------------
# POLICY LOADING
# ---------------------------------------------------------------------------


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load the risk policy YAML.

    Raises ConfigurationError (an existing project exception) on a missing or
    malformed file, rather than silently falling back to defaults. A risk
    engine must never run on an unknown policy.
    """
    target = Path(path) if path is not None else policy_path()

    if not target.exists():
        raise ConfigurationError(f"Risk policy not found: {target}")

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Risk policy is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError("Risk policy root must be a mapping.")

    for required in ("scoring", "risk_categories", "outcomes", "rules"):
        if required not in data:
            raise ConfigurationError(f"Risk policy missing section: {required}")

    # An unsigned policy contains placeholder thresholds. It must never
    # silently drive real credit decisions.
    if is_production() and not data.get("signed_off", False) and not allow_unsigned_policy():
        raise ConfigurationError(
            "Risk policy is not signed off (signed_off: false) and ENVIRONMENT is "
            "production. Obtain credit-policy sign-off, or set "
            "ALLOW_UNSIGNED_RISK_POLICY=true to override deliberately."
        )

    return data


@lru_cache(maxsize=4)
def _cached_policy(resolved: str) -> dict[str, Any]:
    return load_policy(resolved)


def get_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Cached policy accessor. Use load_policy() to force a fresh read."""
    target = Path(path) if path is not None else policy_path()
    return _cached_policy(str(target))


def clear_policy_cache() -> None:
    _cached_policy.cache_clear()


__all__ = [
    "enabled",
    "environment",
    "is_production",
    "allow_unsigned_policy",
    "version",
    "llm_summary_enabled",
    "llm_timeout_seconds",
    "policy_path",
    "load_policy",
    "get_policy",
    "clear_policy_cache",
]
