"""
THE IMPACT ENGINE -- what a recorded finding MEANS for the case, by rule.

    evidence item / pending item ──> rule (impact_rules.yaml) ──> impact
                                        │
                                        └── blocking: from the config that
                                            decides it (kyc_policies.yaml,
                                            readiness rules), never restated

DETERMINISTIC AND DECLARED. An impact is looked up, never composed: the rule
table is app/config/impact_rules.yaml, which restates behaviour the code
already enforces and is marked unconfirmed. A finding code with no rule gets
`NOT_CONFIGURED` -- the Copilot then says the issue is recorded and that no
impact rule is configured for it. No model is asked what anything means.

ONE IMPACT PER PROBLEM, THE PROBLEM'S SUBJECT. The subject is copied from the
evidence item (Slice 6): a co-applicant's NAME_MISMATCH and the primary
applicant's are two impacts, and a case-level finding's impact is the case's.

LANGUAGE-NEUTRAL. The impact is codes and parameters; `text()` renders it
from the phrasebook in the requested language (English today). Same codes,
same truth, in every language added later.

PROVENANCE, KEPT INTERNAL. `source_rule`, `source_finding`, `source_record`
and `observed_at` let a later slice explain an impact; the public views drop
the rule reference, as they drop record ids.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parents[2] / "config" / "impact_rules.yaml"

NOT_CONFIGURED = "NOT_CONFIGURED"


@lru_cache(maxsize=1)
def rules() -> dict[str, Any]:
    """The rule table. Empty -- every impact NOT_CONFIGURED -- if unreadable."""
    try:
        with _PATH.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        logger.error("Impact rules could not be read: %r", exc)
        return {}


def reload() -> None:
    rules.cache_clear()


def policy_status() -> str:
    table = rules()
    status = str(table.get("status") or "UNKNOWN")
    return status if table.get("confirmed") else f"{status}_UNCONFIRMED"


def blocking(rule: dict[str, Any], stage: str | None) -> bool | None:
    """
    Whether this rule's finding blocks the current stage's handoff -- read
    from the configuration that decides it. None when that is not
    configured for this stage: never a guess.
    """
    if "blocking" in rule:
        return bool(rule["blocking"])
    source = str(rule.get("blocking_from") or "")
    family, _, key = source.partition(".")
    try:
        if family == "kyc_policy" and key:
            from app.agents.kyc import config as kyc_config

            return bool(kyc_config.check_blocking(key))
        if family == "readiness" and key:
            # READINESS IS CONFIGURED FOR THE FOS STAGE ONLY.
            if stage not in (None, "FOS"):
                return None
            from app.agents.applicant import config

            value = config.readiness_rules().get(key)
            return None if value is None else bool(value)
    except Exception as exc:
        logger.warning("Blocking lookup failed for %s: %r", source, exc)
    return None


def impact_for(code: str, *, scope: str, party_id: str | None = None,
               party_role: str | None = None, document: str | None = None,
               stage: str | None = None, source_finding: str | None = None,
               source_record: str | None = None,
               observed_at: str | None = None) -> dict[str, Any]:
    """The impact of one finding or pending-item code, for its subject."""
    rule = (rules().get("rules") or {}).get(str(code or "").upper())
    base = {"finding_code": code, "scope": scope, "party_id": party_id,
            "party_role": party_role, "document": document,
            "source_finding": source_finding, "source_record": source_record,
            "observed_at": observed_at}
    if not isinstance(rule, dict):
        return {**base, "impact_code": NOT_CONFIGURED, "effect": None,
                "blocking": None, "action_code": None, "source_rule": None,
                "policy_status": None}
    return {**base,
            "impact_code": rule.get("impact") or NOT_CONFIGURED,
            "effect": rule.get("effect"),
            "blocking": blocking(rule, stage),
            "action_code": rule.get("action"),
            "source_rule": f"impact_rules:{str(code).upper()}",
            "policy_status": policy_status()}


def for_problem(problem: dict[str, Any], stage: str | None) -> dict[str, Any]:
    """The impact of one Slice 6 evidence item."""
    source = problem.get("source") or {}
    return impact_for(problem.get("type"), scope=problem.get("scope") or "CASE",
                      party_id=problem.get("party_id"),
                      party_role=problem.get("party_role"),
                      document=problem.get("document"), stage=stage,
                      source_finding=problem.get("finding"),
                      source_record=source.get("record_id"),
                      observed_at=problem.get("observed_at"))


def for_pending(items: list[dict[str, Any]] | None,
                stage: str | None) -> list[dict[str, Any]]:
    """
    The impact of each pending item the FOS workflow computed. CASE-level:
    the checklist behind them is the application's, not a person's.
    """
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        out.append(impact_for(
            item["code"], scope="CASE",
            document=item.get("slot") or item.get("document_type"),
            stage=stage, source_finding="PENDING_ITEM"))
    return out


def _document_words(value: Any) -> str:
    from app.agents.applicant.subjects import _type

    return _type(value) if value else "document"


def text(impact: dict[str, Any], language: str = "en") -> str:
    """The impact as a clause, in `language` (English when not available)."""
    phrases = ((rules().get("phrases") or {}).get(language)
               or (rules().get("phrases") or {}).get("en") or {})
    template = (phrases.get("impact") or {}).get(impact.get("impact_code")) \
        or (phrases.get("impact") or {}).get(NOT_CONFIGURED) \
        or "the system has recorded this issue"
    return template.format(document=_document_words(impact.get("document")))


def public(impact: dict[str, Any] | None) -> dict[str, Any] | None:
    """An impact as a response may carry it: codes, effect, blocking, words.
    No rule reference, no record id."""
    if not impact:
        return None
    return {k: v for k, v in {
        "impact_code": impact.get("impact_code"),
        "effect": impact.get("effect"),
        "blocking": impact.get("blocking"),
        "action_code": impact.get("action_code"),
        "text": text(impact),
    }.items() if v is not None or k == "blocking"}


__all__ = ["NOT_CONFIGURED", "blocking", "for_pending", "for_problem",
           "impact_for", "policy_status", "public", "reload", "rules", "text"]
