"""
Every sentence the rules write, the rules' names and the error messages: config/reasons.yaml.

    say("missing", what="PAN", who="the applicant", accepted="PAN")   -> a reason
    error("case_not_found")                                             -> an error message
    rule_name("RULE-06") / rule_ids() / who("CO_APPLICANT")

The file is checked once, on first use: every rule, reason and error the code asks for must be there, and every
template must name only fields the engine fills -- a broken file stops the service by name, never mid-evaluation.
"""

from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parent / "reasons.yaml"

# the fields each template may use (what the engine passes); `Who` is `who` with a capital first letter
FIELDS: dict[str, dict[str, set[str]]] = {
    "reasons": {
        "product_list_found": {"product", "count"}, "product_conflict": {"slot", "detail"},
        "coverage_unknown": {"who", "Who"}, "condition_unknown": {"details", "needs"},
        **{k: {"what", "who", "Who"} for k in ("reused", "collected_at_stage", "verified", "stale_for_stage",
                                                "pending_verification", "under_review", "mismatch", "unknown_state",
                                                "stage_entry_unknown")},
        "waived": {"what", "who", "Who", "code"}, "exception_pending": {"what", "who", "Who", "code"},
        "not_accepted": {"what", "who", "Who", "given", "accepted"}, "missing": {"what", "who", "Who", "accepted"},
        "rejected": {"what", "who", "Who", "accepted"},
        "no_min_months": {"name"}, "no_min_count": {"name"}, "no_expiry_field": {"name"},
        "period_unreadable": {"name", "who", "Who", "months"},
        "period_short": {"name", "who", "Who", "start", "end", "months"},
        "count_short": {"name", "who", "Who", "have", "count"},
        "date_unreadable": {"name", "who", "Who", "field"},
        "too_old": {"name", "who", "Who", "when", "age", "max_age"},
        "expiry_unreadable": {"name", "who", "Who"}, "expired": {"name", "who", "Who", "expiry"},
        "period_ok": {"name", "who", "Who"},
        "supporting_ok": {"what", "need", "who", "Who"}, "supporting_pending": {"what", "need", "who", "Who"},
        "supporting_missing": {"what", "need", "who", "Who"},
        "file_not_stored": {"name", "who", "Who"}, "file_problems": {"name", "who", "Who", "problems"},
        "file_wrong_format": {"format", "allowed"}, "file_too_large": {"size_mb", "max_mb"},
        "file_too_small": {"size_kb", "min_kb"}, "file_ok": {"name", "format", "size"},
        "size_mb": {"mb"}, "size_kb": {"kb"},
        "audit_recorded": {"evaluation_id"}, "audit_not_recorded": set(), "audit_event": {"status", "open"},
    },
    "errors": {
        "no_product_requirements": {"product"}, "policy_dir_missing": set(), "policy_invalid": {"file", "detail"},
        "no_product": set(), "no_policy": {"product", "stage"}, "policy_ambiguous": {"names"},
        "policy_not_approved": {"policy", "environment"}, "case_not_found": set(), "store_unavailable": {"error"},
        "timeout": set(), "audit_unavailable": {"error"}, "insufficient_scope": {"scopes"},
    },
}
RULE_IDS = tuple(f"RULE-{n:02d}" for n in range(1, 12))


class ReasonsConfigError(RuntimeError):
    pass


def _check(data: dict) -> dict:
    names = data.get("rule_names") or {}
    missing = [r for r in RULE_IDS if not str(names.get(r) or "").strip()]
    if missing:
        raise ReasonsConfigError(f"{PATH.name}: rule_names is missing {', '.join(missing)}")
    for section, wanted in FIELDS.items():
        have = data.get(section) or {}
        for key, allowed in wanted.items():
            text = have.get(key)
            if not isinstance(text, str) or not text.strip():
                raise ReasonsConfigError(f"{PATH.name}: {section}.{key} is missing")
            used = {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(text) if f}
            if used - allowed:
                raise ReasonsConfigError(f"{PATH.name}: {section}.{key} uses unknown field(s) "
                                         f"{', '.join(sorted(used - allowed))}; allowed: {', '.join(sorted(allowed))}")
    return data


@lru_cache(maxsize=1)
def get() -> dict[str, Any]:
    try:
        data = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ReasonsConfigError(f"{PATH.name} cannot be read: {exc}") from None
    return _check(data)


def _fill(section: str, key: str, fields: dict[str, Any]) -> str:
    if "who" in fields and "Who" not in fields:
        fields = {**fields, "Who": str(fields["who"])[:1].upper() + str(fields["who"])[1:]}
    return get()[section][key].format(**fields)


def say(key: str, **fields: Any) -> str:
    return _fill("reasons", key, fields)


def error(key: str, **fields: Any) -> str:
    return _fill("errors", key, fields)


def rule_name(rule_id: str) -> str:
    return get()["rule_names"][rule_id]


def rule_ids() -> tuple[str, ...]:
    return RULE_IDS


def who(role: str) -> str:
    return (get().get("parties") or {}).get(role) or f"the {role.replace('_', '-').lower()}"


__all__ = ["ReasonsConfigError", "error", "get", "rule_ids", "rule_name", "say", "who"]
