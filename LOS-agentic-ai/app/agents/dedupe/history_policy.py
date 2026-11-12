"""
CUSTOMER HISTORY POLICY -- the verified loan history (loan_history.py) evaluated rule by rule
(app/config/customer_history_policy.yaml), with the dedupe / credit rule machinery: the same validation
(config.normalise_rules), the same comparison (credit.policy.matches), the same sign-off gate.

OUTCOME = the credit agent's vocabulary, precedence DATA_INSUFFICIENT > REVIEW_REQUIRED > READY_FOR_DECISION.
  - a rule that fires                         -> its own outcome (reason code + next action)
  - a rule whose data is not verified          -> NOT_EVALUATED / DATA_UNAVAILABLE -> DATA_INSUFFICIENT
  - a rule whose business limit is not set     -> NOT_EVALUATED / THRESHOLD_UNCONFIGURED -> REVIEW_REQUIRED
  - a value that cannot be compared            -> EVALUATION_FAILED -> DATA_INSUFFICIENT
READY_FOR_DECISION only when every rule evaluated and none fired. It is never a loan approval.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from app.agents.credit.policy import NotComparable, matches
from app.agents.dedupe import config as C
from app.core.exceptions import ConfigurationError

PATH = Path(__file__).resolve().parents[2] / "config" / "customer_history_policy.yaml"
OUTCOMES = ("READY_FOR_DECISION", "REVIEW_REQUIRED", "DATA_INSUFFICIENT")
PRECEDENCE = ("DATA_INSUFFICIENT", "REVIEW_REQUIRED", "READY_FOR_DECISION")
SIGNALS = frozenset({
    "source.mifin_unavailable", "match.unresolved", "history.incomplete", "history.conflicting_records",
    "history.unknown_status_loans", "history.adverse_events", "history.active_loans", "exposure.with_requested",
    "history.monthly_emi", "history.worst_dpd_12m",
    "history.topup_ineligible_loans", "history.emi_bounces_12m", "rcu.adverse_records", "collateral.shared_properties",
})
TRIGGERED, NOT_TRIGGERED, NOT_EVALUATED, FAILED = "TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUATED", "EVALUATION_FAILED"


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def load(path: str | Path | None = None) -> dict[str, Any]:
    """A policy FILE, read and checked (a version about to be imported; tests)."""
    target = Path(path or os.getenv("CUSTOMER_HISTORY_POLICY_PATH") or PATH)
    if not target.exists():
        raise ConfigurationError(f"Customer history policy not found: {target}")
    return parse(yaml.safe_load(target.read_text(encoding="utf-8")) or {})


def validate_rules(data: Any) -> None:
    """A version about to be stored (scripts/rules.py import): checked as it would be used."""
    import copy

    parse(copy.deepcopy(data))


def parse(data: Any) -> dict[str, Any]:
    """The policy as the agent uses it (checked, normalised) from its content as written. Changes `data`."""
    if not isinstance(data, dict):
        raise ConfigurationError("Customer history policy must be a mapping.")
    for key in ("policy_id", "policy_version", "rules"):
        if key not in data:
            raise ConfigurationError(f"Customer history policy missing: {key}")
    data["rules"] = C.normalise_rules(data["rules"], data, signals=SIGNALS, recommendations=OUTCOMES)
    for rule in data["rules"]:
        if not rule.get("reason_code") or not rule.get("next_action"):
            raise ConfigurationError(f"Rule {rule['rule_id']}: reason_code and next_action are required.")
    if _production() and not data.get("signed_off") and (
            os.getenv("ALLOW_UNSIGNED_CUSTOMER_HISTORY_POLICY") or "").strip().lower() not in {"true", "1", "yes"}:
        raise ConfigurationError("Customer history policy is not signed off and ENVIRONMENT is production.")
    return data


RULE_SET = "customer_history_policy"
_PARSED: dict[str, dict[str, Any]] = {}


def policy() -> dict[str, Any]:
    """The policy in force (2026-11-02: the rules live in the database -- rule set customer_history_policy,
    app/agent_db/rule_store.py), checked and normalised once per version."""
    import copy

    from app.agent_db import rule_store

    try:
        digest, content = rule_store.load_with_hash(RULE_SET)
    except rule_store.RulesUnavailable as exc:      # the agent's own error, as before the rules lived in the DB
        raise ConfigurationError(f"Customer history policy unavailable: {exc}") from None
    parsed = _PARSED.get(digest)
    if parsed is None:
        parsed = _PARSED[digest] = parse(copy.deepcopy(content))
    return parsed


def reload() -> None:
    from app.agent_db import rule_store

    _PARSED.clear()
    rule_store.forget()


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def signals(h: dict[str, Any], requested_amount: Any = None) -> dict[str, Any]:
    """The closed signal set from a verified history. None = not verified (the rule cannot pass on it)."""
    status = h["history_status"]
    authoritative_none = status in ("NOT_APPLICABLE", "NO_PREVIOUS_LOANS")      # no customer / an empty loan list
    known = status in ("COMPLETE", "PARTIAL", "MIFIN_ONLY") or authoritative_none
    a = h.get("aggregates") or {}
    complete = status == "COMPLETE" or authoritative_none

    def when(cond: bool, value: Any) -> Any:
        return value if cond else None

    exposure = a.get("active_exposure") or {}
    emi = a.get("monthly_emi_burden") or {}
    dpd = a.get("worst_dpd_12m") or {}
    adverse = a.get("adverse_events") or {}
    adverse_count = int(adverse.get("settled") or 0) + int(adverse.get("written_off") or 0)
    live_exposure = (Decimal(0) if authoritative_none or exposure.get("status") == "NOT_APPLICABLE" and complete
                     else _dec(exposure.get("value")) if exposure.get("status") == "COMPLETE" else None)
    requested = _dec(requested_amount)
    return {
        "source.mifin_unavailable": status == "UNAVAILABLE",
        "match.unresolved": when(status != "UNAVAILABLE", h["customer"]["match"] in ("POTENTIAL_MATCH", "AMBIGUOUS")),
        "history.incomplete": when(known, status in ("PARTIAL", "MIFIN_ONLY")),
        "history.conflicting_records": when(known, 0 if authoritative_none else int(a.get("conflicting_duplicates") or 0)),
        "history.unknown_status_loans": when(known, 0 if authoritative_none else int(a.get("unknown_status_loans") or 0)),
        # adverse events: a found event counts on any history; "none" only on a complete one
        "history.adverse_events": adverse_count if adverse_count else when(complete, 0),
        "history.active_loans": when(complete, 0 if authoritative_none else int(a.get("active_loans") or 0)),
        "exposure.with_requested": (live_exposure + requested) if live_exposure is not None and requested is not None
        else None,
        # no running loan on a complete history owes no EMI (as the exposure above): 0, not "unknown"
        "history.monthly_emi": Decimal(0) if authoritative_none or (emi.get("status") == "NOT_APPLICABLE" and complete)
        else (_dec(emi.get("value")) if emi.get("status") == "COMPLETE" else None),
        "history.worst_dpd_12m": 0 if authoritative_none or dpd.get("status") == "NOT_APPLICABLE" else (
            dpd.get("value") if dpd.get("status") == "COMPLETE" else None),
        **_more_signals(h, authoritative_none, complete),
    }


def _more_signals(h: dict[str, Any], authoritative_none: bool, complete: bool) -> dict[str, Any]:
    """Top-up (H11), EMI bounces (H12), RCU (H13), collateral (H14). A finding counts on any history; "none" needs
    every input known."""
    loans = h.get("loans") or []
    live = [x for x in loans if x.get("effective_status") in ("ACTIVE", "DISBURSED") and not x.get("conflicting_duplicate")]
    topup = [(x.get("topup") or {}).get("status") for x in live]
    not_ok = sum(s == "NOT_ELIGIBLE" for s in topup)
    topup_sig = 0 if authoritative_none else (not_ok if not_ok else (0 if complete and all(s == "ELIGIBLE" for s in topup) else None))
    found = [x for x in loans if x.get("lms_status") == "FOUND"]
    bounces = [(x.get("lms") or {}).get("emi_bounces_12m") for x in found]
    worst = max((b for b in bounces if b is not None), default=None)
    bounce_sig = 0 if authoritative_none and not loans else (
        worst if (worst is not None and (complete and None not in bounces or worst > 0)) else (0 if complete and not found else None))

    def source(key: str, field: str) -> Any:
        if authoritative_none:
            return 0                              # no SBFC customer: nothing to hold against them
        src = h.get(key) or {}
        return src.get(field) if src.get("status") == "AVAILABLE" else None

    return {"history.topup_ineligible_loans": topup_sig, "history.emi_bounces_12m": bounce_sig,
            "rcu.adverse_records": source("rcu", "adverse"), "collateral.shared_properties": source("collateral", "shared")}


def evaluate(h: dict[str, Any], requested_amount: Any = None) -> dict[str, Any]:
    p = policy()
    sig = signals(h, requested_amount)
    rules, contributions = [], []
    for rule in p["rules"]:
        value = sig.get(rule["inputs"][0])
        row = {"rule_id": rule["rule_id"], "policy_version": rule["policy_version"], "description": rule["description"],
               "signal": rule["inputs"][0], "value": str(value) if isinstance(value, Decimal) else value,
               "severity": rule["severity"], "confirmation_status": rule["confirmation_status"],
               "evidence": {"source": "loan_history", "history_status": h["history_status"]}}
        if not rule["enabled"]:
            row.update(outcome=NOT_EVALUATED, reason="DISABLED")
        elif rule["threshold"] is None:
            row.update(outcome=NOT_EVALUATED, reason="THRESHOLD_UNCONFIGURED", reason_code="POLICY_NOT_CONFIGURED",
                       next_action=rule["next_action"])
            contributions.append(("REVIEW_REQUIRED", f"POLICY_NOT_CONFIGURED:{rule['rule_id']}", rule["next_action"]))
        elif value is None:
            row.update(outcome=NOT_EVALUATED, reason="DATA_UNAVAILABLE", reason_code="DATA_UNAVAILABLE")
            contributions.append(("DATA_INSUFFICIENT", f"DATA_UNAVAILABLE:{rule['rule_id']}", None))
        else:
            try:
                fired = matches(rule, value)
            except NotComparable:
                row.update(outcome=FAILED, reason="VALUE_NOT_COMPARABLE")
                contributions.append(("DATA_INSUFFICIENT", f"EVALUATION_FAILED:{rule['rule_id']}", None))
            else:
                row.update(outcome=TRIGGERED if fired else NOT_TRIGGERED, reason=None)
                if fired:
                    row.update(reason_code=rule["reason_code"], next_action=rule["next_action"])
                    contributions.append((rule["recommendation"], rule["reason_code"], rule["next_action"]))
        rules.append(row)
    outcome = next((o for o in PRECEDENCE if any(c[0] == o for c in contributions)), "READY_FOR_DECISION")
    reasons = list(dict.fromkeys(c[1] for c in contributions))
    actions = list(dict.fromkeys(c[2] for c in contributions if c[2]))
    if not actions:
        actions = ["Proceed to the credit decision with this verified history." if outcome == "READY_FOR_DECISION"
                   else "Complete the missing information listed in the reasons."]
    counts = {o: sum(r["outcome"] == o for r in rules) for o in (TRIGGERED, NOT_TRIGGERED, NOT_EVALUATED, FAILED)}
    return {"policy": {"policy_id": p["policy_id"], "policy_version": p["policy_version"],
                       "signed_off": bool(p.get("signed_off"))},
            "outcome": outcome, "reason_codes": reasons, "next_actions": actions, "rules": rules,
            "rule_counts": counts, "complete": counts[NOT_EVALUATED] == 0 and counts[FAILED] == 0}


__all__ = ["OUTCOMES", "SIGNALS", "evaluate", "load", "policy", "reload", "signals"]
