"""
Income policy loading.

Thresholds live in app/config/income_policy.yaml, never in the evidence or
comparison logic, so changing what "consistent" means is a reviewable
config change rather than a code change. Same shape as the KYC policy
loader, deliberately: one way to read a policy file in this project.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.constants import CONFIG_DIR

DEFAULT_POLICY_FILENAME = "income_policy.yaml"

#: What each threshold is when the file does not say.
#:
#: WHY DEFAULTS EXIST HERE AND NOT IN THE CALLERS. A missing policy file
#: must not change a verdict silently from one deployment to the next, and
#: a default spelled out beside its neighbours can be read as a policy.
#: The comparison logic itself still contains no number.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "income_evidence": {
        "recurring_amount_tolerance": 0.10,
        "minimum_recurring_months": 2,
        "maximum_credits_per_month": 2,
        "weak_evidence_confidence": 0.5,
    },
    "income_consistency": {
        "enabled": True,
        "compare_against": "NET_PAY",
        "amount_tolerance": 0.10,
        "minimum_months_observed": 2,
    },
}


def policy_path() -> Path:
    override = os.getenv("INCOME_POLICY_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_POLICY_FILENAME


@lru_cache(maxsize=1)
def _load(path_text: str) -> dict[str, Any]:
    """
    The policy document, or the built-in defaults.

    NEVER RAISES. An unreadable policy file must not take the whole
    pipeline down: income consistency is one signal among many, and a
    case that cannot be compared is reported as not comparable.
    """
    path = Path(path_text)
    if not path.exists():
        return {}

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def reset_policy_cache() -> None:
    """Drop the cached policy. For tests and controlled reloads."""
    _load.cache_clear()


def section(name: str) -> dict[str, Any]:
    """One policy section, with anything the file omits filled in."""
    values = dict(_DEFAULTS.get(name) or {})
    loaded = _load(str(policy_path())).get(name)
    if isinstance(loaded, dict):
        values.update(loaded)
    return values


def _number(name: str, key: str) -> float:
    try:
        return float(section(name)[key])
    except (TypeError, ValueError, KeyError):
        return float(_DEFAULTS[name][key])


def recurring_amount_tolerance() -> float:
    return _number("income_evidence", "recurring_amount_tolerance")


def minimum_recurring_months() -> int:
    return int(_number("income_evidence", "minimum_recurring_months"))


def maximum_credits_per_month() -> float:
    return _number("income_evidence", "maximum_credits_per_month")


def weak_evidence_confidence() -> float:
    return _number("income_evidence", "weak_evidence_confidence")


def consistency_enabled() -> bool:
    return bool(section("income_consistency").get("enabled", True))


def compare_against() -> str:
    return str(section("income_consistency").get("compare_against") or "NET_PAY").upper()


def amount_tolerance() -> float:
    return _number("income_consistency", "amount_tolerance")


def minimum_months_observed() -> float:
    return _number("income_consistency", "minimum_months_observed")


__all__ = [
    "amount_tolerance", "compare_against", "consistency_enabled",
    "maximum_credits_per_month", "minimum_months_observed",
    "minimum_recurring_months", "policy_path",
    "recurring_amount_tolerance", "reset_policy_cache", "section",
    "weak_evidence_confidence",
]
