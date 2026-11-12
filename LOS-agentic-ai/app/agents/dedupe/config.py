"""
Dedupe policy -- loading and validation.

The underwriting policy's conventions (app/agents/credit/policy.py): YAML, every rule reads exactly one signal from a
closed set, operators from the same list, a threshold key on every rule, a confirmation status, and the same sign-off
gate -- an unsigned policy never drives anything in production. One difference, on purpose: a rule's threshold may be
null. That is an UNCONFIGURED business value, and the rule is then NOT_EVALUATED (fail closed), never skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import copy

import yaml

from app.agents.credit.policy import OPERATORS, SEVERITIES
from app.core.exceptions import ConfigurationError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "dedupe_policy.yaml"

#: The closed set of signals a rule may read (computed by app/agents/dedupe/service.py).
SIGNALS = frozenset({
    "identity.other_customer_records",
    "identity.possible_matches",
    "identity.pan_name_conflicts",
    "identity.role_changes",
    "applications.other_cases_same_product",
    "applications.hours_since_previous_same_product",
    "applications.seconds_between_same_product_creates",
    "documents.reused_across_customers",
    "coverage.partial",
})

RECOMMENDATIONS = (
    "CONTINUE_STANDARD_ONBOARDING", "LINK_EXISTING_CUSTOMER", "IDENTITY_CONFLICT_REVIEW",
    "DOCUMENT_VERIFICATION_REQUIRED", "DUPLICATE_APPLICATION", "SIMULTANEOUS_APPLICATION_REVIEW",
    "POSSIBLE_MATCH_REVIEW", "ROLE_CHANGE_REVIEW", "PARTIAL_ANALYSIS", "DATA_UNAVAILABLE",
)
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")
_CONFIDENCE_KEYS = ("strong_parameter", "supporting_only")
_REQUIRED = ("policy_id", "policy_version", "limits", "parameters", "matching", "sources", "rules", "not_implemented",
             "recommendation_precedence", "fraud_dedupe")
_REQUIRED_LIMITS = ("max_scan_cases", "max_scan_documents")
#: the dedupe parameters (owner, 2026-10-22) and the store fields a parameter may map to
PARAMETERS = ("PAN", "MOBILE", "AP_CODE", "PR_CODE")
STRENGTHS = ("STRONG", "SUPPORTING")
#: the store fields a parameter may map to. party_pan = the PAN of the stored profiles (extraction / co_applicants).
#: AP_CODE (customer id) and PR_CODE (loan account number) have NO field in this LOS: they must stay null until a
#: source that holds them (core / LMS) is connected -- a mapping onto an unrelated column would invent a match.
FIELDS = {"PAN": ("party_pan",), "MOBILE": ("applicants.mobile",), "AP_CODE": (), "PR_CODE": ()}
#: the fraud agent's DedupeResult parameter names (app/config/risk_policy.yaml DEDUPE_MATCH.parameters)
FRAUD_PARAMETERS = ("pan", "mobile_no", "customer_id", "loan_account_no")


def policy_path() -> Path:
    raw = (os.getenv("DEDUPE_POLICY_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_PATH


def label_key(label: str) -> str:
    """A form label as a lookup key: "Mobile No" / "MOBILE  NO" / "mobile_no" -> "MOBILE NO"."""
    return " ".join(str(label or "").replace("_", " ").upper().split())


def enabled() -> bool:
    """DEDUPE_ENABLED (default on): off, the LOS flow hands the fraud agent no dedupe -- its data gap, as before."""
    return (os.getenv("DEDUPE_ENABLED") or "true").strip().lower() not in {"false", "0", "no", "off"}


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def _allow_unsigned() -> bool:
    return (os.getenv("ALLOW_UNSIGNED_DEDUPE_POLICY") or "").strip().lower() in {"true", "1", "yes", "on"}


def normalise_rules(rules: Any, policy: dict[str, Any], *, signals: frozenset[str] | None = None,
                    recommendations: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    signals = SIGNALS if signals is None else signals                  # another policy, the same validation
    recommendations = RECOMMENDATIONS if recommendations is None else recommendations
    """Every rule in one canonical shape, or ConfigurationError naming the rule."""
    if not isinstance(rules, list) or not rules:
        raise ConfigurationError("Dedupe policy needs a non-empty `rules` list.")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in rules:
        if not isinstance(raw, dict) or not raw.get("rule_id"):
            raise ConfigurationError("Every dedupe rule needs a rule_id.")
        rule = dict(raw)
        rule_id = str(rule["rule_id"])
        if rule_id in seen:
            raise ConfigurationError(f"Duplicate dedupe rule_id: {rule_id}")
        seen.add(rule_id)
        inputs = rule.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != 1:
            raise ConfigurationError(f"Rule {rule_id}: `inputs` must name exactly one signal.")
        if inputs[0] not in signals:
            raise ConfigurationError(f"Rule {rule_id}: unknown signal {inputs[0]!r}. Allowed: {sorted(signals)}")
        rule["operator"] = str(rule.get("operator", "")).lower()
        rule["severity"] = str(rule.get("severity", "")).upper()
        rule["recommendation"] = str(rule.get("recommendation", "")).upper()
        for key, allowed in (("operator", OPERATORS), ("severity", SEVERITIES),
                             ("recommendation", recommendations)):
            if rule[key] not in allowed:
                raise ConfigurationError(f"Rule {rule_id}: {key} must be one of {allowed}.")
        if "threshold" not in rule:
            raise ConfigurationError(f"Rule {rule_id}: a threshold key is required (null = UNCONFIGURED).")
        threshold = rule["threshold"]
        if threshold is not None:
            if rule["operator"] == "between" and not (isinstance(threshold, list) and len(threshold) == 2):
                raise ConfigurationError(f"Rule {rule_id}: between needs [low, high].")
            if rule["operator"] in ("in", "not_in") and not isinstance(threshold, list):
                raise ConfigurationError(f"Rule {rule_id}: {rule['operator']} needs a list.")
        if not rule.get("confirmation_status"):
            raise ConfigurationError(f"Rule {rule_id}: confirmation_status is required.")
        rule["enabled"] = bool(rule.get("enabled", True))
        rule["policy_version"] = str(rule.get("policy_version") or policy.get("policy_version"))
        rule.setdefault("description", rule_id)
        out.append(rule)
    return out


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """A policy FILE, read and checked (a version about to be imported; tests)."""
    target = Path(path) if path is not None else policy_path()
    if not target.exists():
        raise ConfigurationError(f"Dedupe policy not found: {target}")
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Dedupe policy is not valid YAML: {exc}") from exc
    return parse_policy(data)


def validate_rules(data: Any) -> None:
    """A version of the dedupe policy about to be stored (scripts/rules.py import): checked as it would be used."""
    parse_policy(copy.deepcopy(data))


def parse_policy(data: Any) -> dict[str, Any]:
    """The policy as the agent uses it (checked, normalised) from its content as written. Changes `data`."""
    if not isinstance(data, dict):
        raise ConfigurationError("Dedupe policy root must be a mapping.")
    missing = [k for k in _REQUIRED if k not in data]
    missing += [f"limits.{k}" for k in _REQUIRED_LIMITS if k not in (data.get("limits") or {})]
    if missing:
        raise ConfigurationError(f"Dedupe policy missing: {', '.join(missing)}")
    data["rules"] = normalise_rules(data["rules"], data)
    params = data["parameters"] or {}
    if sorted(params) != sorted(PARAMETERS):
        raise ConfigurationError(f"parameters must be exactly {PARAMETERS}; got {sorted(params)}.")
    for name, spec in params.items():
        spec = dict(spec or {})
        spec["strength"] = str(spec.get("strength", "")).upper()
        if spec["strength"] not in STRENGTHS:
            raise ConfigurationError(f"parameters.{name}.strength must be one of {STRENGTHS}.")
        if "field" not in spec:
            raise ConfigurationError(f"parameters.{name}.field is required (null = not stored by this LOS).")
        if spec["field"] is not None and spec["field"] not in FIELDS[name]:
            raise ConfigurationError(f"parameters.{name}.field must be null or one of {FIELDS[name]} -- a field "
                                     "this LOS actually stores.")
        if spec.get("fraud_parameter") not in FRAUD_PARAMETERS:
            raise ConfigurationError(f"parameters.{name}.fraud_parameter must be one of {FRAUD_PARAMETERS}.")
        params[name] = spec
    data["parameters"] = params
    # the form's dropdown labels -> parameter (upper-cased, spaces collapsed); each label names one parameter
    labels: dict[str, str] = {}
    for name, spec in params.items():
        for label in [name, *(spec.get("labels") or [])]:
            key = label_key(label)
            if labels.get(key, name) != name:
                raise ConfigurationError(f"Dedupe label {label!r} names both {labels[key]} and {name}.")
            labels[key] = name
    data["labels"] = labels
    types = {label_key(k): str(v).upper() for k, v in (data.get("customer_types") or {}).items()}
    if not types or set(types.values()) - {"NEW", "EXISTING"}:
        raise ConfigurationError("customer_types must map the form's labels to NEW / EXISTING.")
    data["customer_types"] = types
    matching = data["matching"] or {}
    confidence = {k: str(v).upper() for k, v in (matching.get("confidence") or {}).items()}
    bad = [k for k in _CONFIDENCE_KEYS if confidence.get(k) not in CONFIDENCES]
    if bad:
        raise ConfigurationError(f"matching.confidence needs {bad} each one of {CONFIDENCES}.")
    matching["confidence"] = confidence
    data["matching"] = matching
    precedence = [str(r).upper() for r in data["recommendation_precedence"] or []]
    unknown = [r for r in precedence if r not in RECOMMENDATIONS]
    if unknown or set(RECOMMENDATIONS) - set(precedence):
        raise ConfigurationError("recommendation_precedence must list every recommendation exactly: "
                                 f"unknown {unknown}, missing {sorted(set(RECOMMENDATIONS) - set(precedence))}")
    data["recommendation_precedence"] = precedence
    rule_ids = {r["rule_id"] for r in data["rules"]}
    if (data["fraud_dedupe"] or {}).get("pan_match_from") not in rule_ids:
        raise ConfigurationError("fraud_dedupe.pan_match_from must name a rule in `rules`.")
    for entry in data["not_implemented"] or []:
        if not isinstance(entry, dict) or not entry.get("rule_id") or not entry.get("needs"):
            raise ConfigurationError("Every not_implemented entry needs rule_id and needs.")
    if _production() and not data.get("signed_off", False) and not _allow_unsigned():
        raise ConfigurationError(
            "Dedupe policy is not signed off (signed_off: false) and ENVIRONMENT is production. Obtain policy "
            "sign-off, or set ALLOW_UNSIGNED_DEDUPE_POLICY=true to override deliberately.")
    return data


RULE_SET = "dedupe_policy"
_PARSED: dict[str, dict[str, Any]] = {}


def get_policy() -> dict[str, Any]:
    """The policy in force (2026-11-02: the rules live in the database -- rule set dedupe_policy, app/agent_db/
    rule_store.py; dedupe_policy.yaml is where a version comes from), checked and normalised once per version."""
    from app.agent_db import rule_store

    try:
        digest, content = rule_store.load_with_hash(RULE_SET)
    except rule_store.RulesUnavailable as exc:      # the agent's own error, as before the rules lived in the DB
        raise ConfigurationError(f"Dedupe policy unavailable: {exc}") from None
    parsed = _PARSED.get(digest)
    if parsed is None:
        parsed = _PARSED[digest] = parse_policy(copy.deepcopy(content))
    return parsed


def rule_set_hash() -> str:
    """The hash of the policy in force (stored with every decision's audit row)."""
    from app.agent_db import rule_store

    return rule_store.load_with_hash(RULE_SET)[0]


def reload() -> None:
    from app.agent_db import rule_store

    _PARSED.clear()
    rule_store.forget()


__all__ = ["RECOMMENDATIONS", "SIGNALS", "get_policy", "load_policy", "normalise_rules", "policy_path", "reload"]
