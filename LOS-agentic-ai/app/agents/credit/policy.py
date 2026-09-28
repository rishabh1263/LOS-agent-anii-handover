"""
Underwriting policy -- loading and validation.

Slice 2 needs only the EVIDENCE PLAN from the policy file; the rule engine
that turns observations into findings is added in Slice 3.

The same sign-off gate as the risk policy (fraud_risk/config.load_policy): an
unsigned policy holds placeholder thresholds and must never silently drive
underwriting in production.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agents.credit import config
from app.core.exceptions import ConfigurationError

_SCOPES = {"case", "party"}


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else config.policy_path()
    if not target.exists():
        raise ConfigurationError(f"Underwriting policy not found: {target}")
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Underwriting policy is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("Underwriting policy root must be a mapping.")
    for required in ("policy_id", "policy_version", "evidence_plan", "rules"):
        if required not in data:
            raise ConfigurationError(f"Underwriting policy missing section: {required}")
    validate_evidence_plan(data["evidence_plan"])
    data["rules"] = normalise_rules(data["rules"], data)

    if config.is_production() and not data.get("signed_off", False) \
            and not config.allow_unsigned_policy():
        raise ConfigurationError(
            "Underwriting policy is not signed off (signed_off: false) and "
            "ENVIRONMENT is production. Obtain credit-policy sign-off, or set "
            "ALLOW_UNSIGNED_UNDERWRITING_POLICY=true to override deliberately.")
    return data


def validate_evidence_plan(plan: Any) -> None:
    """Shape only. Whether each source is ALLOWED is the planner's check."""
    if not isinstance(plan, list) or not plan:
        raise ConfigurationError("evidence_plan must be a non-empty list.")
    seen: set[str] = set()
    for step in plan:
        if not isinstance(step, dict) or not step.get("category"):
            raise ConfigurationError("Each evidence_plan step needs a category.")
        if step["category"] in seen:
            raise ConfigurationError(f"Duplicate evidence_plan category: {step['category']}")
        seen.add(step["category"])
        if str(step.get("scope", "case")) not in _SCOPES:
            raise ConfigurationError(f"evidence_plan scope must be one of {sorted(_SCOPES)}.")
        if not isinstance(step.get("sources"), list) or not step["sources"]:
            raise ConfigurationError(
                f"evidence_plan step {step['category']!r} needs at least one source.")


# ---------------------------------------------------------------------------
# RULES -- shape, and the deterministic evaluator
# ---------------------------------------------------------------------------

OPERATORS = ("lt", "lte", "gt", "gte", "eq", "ne", "in", "not_in", "between")
SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH")
FINDING_STATUSES = ("POSITIVE", "NEGATIVE", "REVIEW")
CATEGORIES = ("CREDIT_PROFILE", "REPAYMENT", "BANKING", "INCOME_CONSISTENCY", "ADVERSE",
              "CROSS_SOURCE")


def normalise_rules(rules: Any, policy: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Every rule in one canonical shape, or ConfigurationError.

    Accepts the older single `input` / `status` keys and rewrites them to
    `inputs` / `finding_status`. A rule may read only a signal from the
    closed set (signals.SIGNALS) -- never an arbitrary field.
    """
    from app.agents.credit.signals import SIGNALS

    if not isinstance(rules, list) or not rules:
        raise ConfigurationError("Underwriting policy needs a non-empty `rules` list.")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in rules:
        if not isinstance(raw, dict) or not raw.get("rule_id"):
            raise ConfigurationError("Every underwriting rule needs a rule_id.")
        rule = dict(raw)
        rule_id = str(rule["rule_id"])
        if rule_id in seen:
            raise ConfigurationError(f"Duplicate underwriting rule_id: {rule_id}")
        seen.add(rule_id)

        inputs = rule.get("inputs", [rule["input"]] if rule.get("input") else None)
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list) or len(inputs) != 1:
            raise ConfigurationError(f"Rule {rule_id}: `inputs` must name exactly one signal.")
        if inputs[0] not in SIGNALS:
            raise ConfigurationError(f"Rule {rule_id}: unknown signal {inputs[0]!r}. "
                                     f"Allowed: {sorted(SIGNALS)}")
        rule["inputs"] = list(inputs)
        rule.pop("input", None)

        rule["finding_status"] = str(rule.get("finding_status") or rule.pop("status", "")).upper()
        rule.pop("status", None)
        rule["severity"] = str(rule.get("severity", "")).upper()
        rule["category"] = str(rule.get("category", "")).upper()
        rule["operator"] = str(rule.get("operator", "")).lower()
        for key, allowed in (("finding_status", FINDING_STATUSES), ("severity", SEVERITIES),
                             ("category", CATEGORIES), ("operator", OPERATORS)):
            if rule[key] not in allowed:
                raise ConfigurationError(f"Rule {rule_id}: {key} must be one of {allowed}.")
        if "threshold" not in rule:
            raise ConfigurationError(f"Rule {rule_id}: a threshold is required.")
        if rule["operator"] == "between" and not (
                isinstance(rule["threshold"], list) and len(rule["threshold"]) == 2):
            raise ConfigurationError(f"Rule {rule_id}: between needs [low, high].")
        if rule["operator"] in ("in", "not_in") and not isinstance(rule["threshold"], list):
            raise ConfigurationError(f"Rule {rule_id}: {rule['operator']} needs a list.")
        if not rule.get("confirmation_status"):
            raise ConfigurationError(f"Rule {rule_id}: confirmation_status is required.")
        rule["enabled"] = bool(rule.get("enabled", True))
        rule["policy_version"] = str(rule.get("policy_version") or policy.get("policy_version"))
        rule.setdefault("description", rule_id)
        out.append(rule)
    return out


class NotComparable(Exception):
    """The value cannot be compared with the threshold (wrong type)."""


def _num(value: Any) -> float:
    if isinstance(value, bool):
        raise NotComparable(value)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NotComparable(value) from exc


def _norm(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value


def matches(rule: dict[str, Any], value: Any) -> bool:
    """Whether `value` meets the rule's condition. Raises NotComparable."""
    op, threshold = rule["operator"], rule["threshold"]
    if op in ("in", "not_in"):
        hit = _norm(value) in {_norm(t) for t in threshold}
        return hit if op == "in" else not hit
    if op in ("eq", "ne"):
        if isinstance(threshold, bool) or isinstance(value, bool):
            if not isinstance(value, bool) or not isinstance(threshold, bool):
                raise NotComparable(value)
            hit = value is threshold
        elif isinstance(threshold, (int, float)):
            hit = _num(value) == float(threshold)
        else:
            hit = _norm(value) == _norm(threshold)
        return hit if op == "eq" else not hit
    number = _num(value)
    if op == "between":
        low, high = (_num(t) for t in threshold)
        return low <= number <= high
    limit = _num(threshold)
    return {"lt": number < limit, "lte": number <= limit,
            "gt": number > limit, "gte": number >= limit}[op]


@lru_cache(maxsize=4)
def _cached(resolved: str) -> dict[str, Any]:
    return load_policy(resolved)


def get_policy() -> dict[str, Any]:
    return _cached(str(config.policy_path()))


def reload() -> None:
    _cached.cache_clear()


__all__ = ["NotComparable", "get_policy", "load_policy", "matches", "normalise_rules", "reload",
           "validate_evidence_plan"]
