"""
Which Credit document policy applies to a case -- resolved, never guessed.

Policies are the `*.credit.json` files in the policy directory (settings.policy_dir); `example_policy.json` documents
the format and is never loaded. For a case's product, scheme, stage and evaluation date exactly ONE policy in force
must match:

    none                      -> POLICY_NOT_CONFIGURED   (never "nothing required")
    more than one             -> POLICY_AMBIGUOUS        (never "the first one")
    a file that does not parse / validate -> POLICY_INVALID (named; a broken file could be the right one)
    a SAMPLE policy outside development  -> POLICY_NOT_APPROVED

Every error is an EvaluationError the API maps to a status; none becomes a result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.config import settings as S
from app.agents.docs_verify_credit_agent.models.policy import CreditDocumentPolicy


class EvaluationError(Exception):
    """An evaluation that cannot produce a trustworthy result (code, message, http status)."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class LoadedPolicy:
    policy: CreditDocumentPolicy
    file: str
    sha256: str


def _strip_comments(value):
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def load_all(folder: Path) -> list[LoadedPolicy]:
    """Every policy file, validated. Raises POLICY_INVALID naming the first broken file."""
    if not folder.is_dir():
        raise EvaluationError("POLICY_NOT_CONFIGURED", T.error("policy_dir_missing"), 422)
    out = []
    for path in sorted(folder.glob("*.credit.json")):
        raw = path.read_bytes()
        try:
            policy = CreditDocumentPolicy.model_validate(_strip_comments(json.loads(raw)))
        except (ValueError, ValidationError) as exc:
            detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            raise EvaluationError("POLICY_INVALID", T.error("policy_invalid", file=path.name, detail=detail),
                                  422) from None
        out.append(LoadedPolicy(policy, path.name, hashlib.sha256(raw).hexdigest()))
    return out


def validate_rules(content: Any) -> None:
    """A policy version about to be stored (scripts/rules.py import): it must load as the desk loads it."""
    CreditDocumentPolicy.model_validate(_strip_comments(content))


_VALIDATED: dict[str, CreditDocumentPolicy] = {}


def load_in_force() -> list[LoadedPolicy]:
    """Every policy IN FORCE (2026-11-02: the rules live in the database -- rule set docs_policy, one version per
    policy file, app/agent_db/rule_store.py; the policies folder is where versions come from). `sha256` is the
    version's hash (its rule_set_hash). Raises POLICY_NOT_CONFIGURED / POLICY_INVALID like load_all."""
    from app.agent_db import rule_sets, rule_store

    try:
        family = rule_store.load_family("docs_policy")
    except rule_store.RulesUnavailable:
        raise EvaluationError("POLICY_NOT_CONFIGURED", T.error("policy_dir_missing"), 422) from None
    if not family:
        raise EvaluationError("POLICY_NOT_CONFIGURED", T.error("policy_dir_missing"), 422)
    out = []
    for member, content in family.items():
        digest = rule_sets.fingerprint(content)
        policy = _VALIDATED.get(digest)
        if policy is None:
            file = member.split(":", 1)[-1]
            try:
                policy = CreditDocumentPolicy.model_validate(_strip_comments(content))
            except (ValueError, ValidationError) as exc:
                detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                raise EvaluationError("POLICY_INVALID", T.error("policy_invalid", file=file, detail=detail),
                                      422) from None
            _VALIDATED[digest] = policy
        out.append(LoadedPolicy(policy, member.split(":", 1)[-1], digest))
    return out


def resolve(product: str | None, *, scheme: str | None, stage: str, on: date) -> LoadedPolicy:
    """The one policy in force for this product / scheme / stage on this date."""
    if not product:
        raise EvaluationError("CONTEXT_INCOMPLETE", T.error("no_product"), 422)
    settings = S.get()
    product, stage = product.strip().upper(), stage.strip().upper()
    matches = [p for p in load_in_force()
               if p.policy.status != "RETIRED"
               and product in {x.upper() for x in p.policy.applies_to.products}
               and p.policy.applies_to.stage.upper() == stage
               and (not p.policy.applies_to.schemes or (scheme or "").upper()
                    in {x.upper() for x in p.policy.applies_to.schemes})
               and p.policy.in_force(on)]
    if not matches:
        raise EvaluationError("POLICY_NOT_CONFIGURED", T.error("no_policy", product=product, stage=stage), 422)
    if len(matches) > 1:
        names = ", ".join(f"{m.policy.policy_id}@{m.policy.version}" for m in matches)
        raise EvaluationError("POLICY_AMBIGUOUS", T.error("policy_ambiguous", names=names), 422)
    chosen = matches[0]
    if chosen.policy.status == "SAMPLE" and not settings.allow_sample_policy:
        raise EvaluationError("POLICY_NOT_APPROVED",
                              T.error("policy_not_approved", policy=f"{chosen.policy.policy_id}@{chosen.policy.version}",
                                      environment=settings.environment), 422)
    return chosen


__all__ = ["EvaluationError", "LoadedPolicy", "load_all", "resolve"]
