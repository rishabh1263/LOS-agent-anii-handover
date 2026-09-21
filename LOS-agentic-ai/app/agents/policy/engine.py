"""
Resolve a case's document requirements from configured policy.

THE INPUT is a product, a loan amount and whatever applicant attributes the
store actually holds. THE OUTPUT is a list of requirements, each one carrying
the rule that produced it.

FOUR RULES THIS MODULE FOLLOWS, and the reasons they exist:

1.  A REQUIREMENT IS NEVER INVENTED. Every slot comes from a rule in a policy
    file. There is no amount band, no month count and no document matrix in
    this file.

2.  A RULE THAT CANNOT BE EVALUATED DOES NOT FIRE, AND IS NOT DISCARDED. If a
    rule keys on employment type and the case has not captured one, the
    requirement is not imposed -- asking for a document on a guess wastes a
    customer's afternoon -- and the rule is reported in `unevaluated_rules`
    so the gap is visible. Silence would be the worst of both.

3.  RULES ADD; THEY DO NOT REMOVE. A high-value band can ask for more than
    the base. Nothing can drop a base requirement, so no combination of
    attributes can quietly produce a case that needs less identity evidence
    than every other case.

4.  A CONFLICT IS REPORTED, NOT RESOLVED SILENTLY. Two rules that narrow the
    same slot to disjoint document types describe an unsatisfiable
    requirement. The engine widens rather than blocking the case, and says
    in `conflicts` that the policy file needs a human.

WHAT THE ENGINE DOES NOT DECIDE. Whether a collected document is any good.
That is verification's answer and it arrives separately; this module is asked
only what should be there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.agents.policy import loader

logger = logging.getLogger(__name__)

# -- how strongly a slot is required ----------------------------------------
#: Required for this case, whatever its attributes.
REQUIRED = "REQUIRED"
#: Required BECAUSE of something about this case. Carries the condition.
CONDITIONAL = "CONDITIONAL"
#: Offered. Never blocks a handoff.
OPTIONAL = "OPTIONAL"
#: Declared by policy but excluded for this case.
NOT_APPLICABLE = "NOT_APPLICABLE"

#: Strongest wins when several rules name the same slot.
_STRENGTH = {NOT_APPLICABLE: 0, OPTIONAL: 1, CONDITIONAL: 2, REQUIRED: 3}

#: The policy source when a product has no policy file. Not a version number
#: -- it is the absence of one, said out loud.
LEGACY_SOURCE = "applicant_agent.yaml"
LEGACY_POLICY_ID = "APPLICANT_AGENT_CHECKLIST"
LEGACY_VERSION = "unversioned"


# ==========================================================================
# WHAT COMES BACK
# ==========================================================================


@dataclass(frozen=True)
class Requirement:
    """One slot the case needs filled, and why."""

    slot: str
    accepts: tuple[str, ...]
    requirement: str
    rule_ids: tuple[str, ...] = ()
    reason: str = ""
    #: The case attributes that made a CONDITIONAL requirement apply, as
    #: "employment_type=SELF_EMPLOYED". Empty for an unconditional one.
    applicable_conditions: tuple[str, ...] = ()
    #: UNCONFIRMED while the policy file says its numbers are placeholders.
    policy_status: str = "UNCONFIRMED"

    @property
    def mandatory(self) -> bool:
        """
        Whether a missing document here blocks the handoff.

        A CONDITIONAL requirement that has FIRED is as binding as a base one
        -- the condition already decided. `requirement` keeps the
        distinction for display; this collapses it for the readiness check.
        """
        return self.requirement in (REQUIRED, CONDITIONAL)

    def as_checklist_entry(self) -> dict[str, Any]:
        """The shape the existing checklist code already consumes, widened."""
        entry: dict[str, Any] = {
            "slot": self.slot,
            "accepts": list(self.accepts),
            "mandatory": self.mandatory,
            "requirement": self.requirement,
            "rule_ids": list(self.rule_ids),
            "reason": self.reason,
            "policy_status": self.policy_status,
        }
        if self.applicable_conditions:
            entry["applicable_conditions"] = list(self.applicable_conditions)
        return entry


@dataclass(frozen=True)
class UnevaluatedRule:
    """A rule that could not be decided because the case lacks an input."""

    rule_id: str
    missing_attributes: tuple[str, ...]
    reason: str
    #: What the rule would have asked for. Shown so a FOS can see what is at
    #: stake in capturing the attribute -- not imposed.
    would_require: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "missing_attributes": list(self.missing_attributes),
            "reason": self.reason,
            "would_require": list(self.would_require),
        }


@dataclass(frozen=True)
class PolicyConflict:
    rule_ids: tuple[str, ...]
    slot: str
    detail: str

    def public(self) -> dict[str, Any]:
        return {"slot": self.slot, "rule_ids": list(self.rule_ids),
                "detail": self.detail}


@dataclass(frozen=True)
class PolicyResolution:
    """Everything policy has to say about one case."""

    product: str | None
    policy_id: str
    policy_version: str
    policy_status: str
    source: str
    requirements: tuple[Requirement, ...] = ()
    applied_rules: tuple[str, ...] = ()
    unevaluated_rules: tuple[UnevaluatedRule, ...] = ()
    conflicts: tuple[PolicyConflict, ...] = ()
    loan_amount: Decimal | None = None
    #: Document-level evidence requirements, keyed by document type.
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def checklist(self) -> list[dict[str, Any]]:
        return [r.as_checklist_entry() for r in self.requirements]

    def requirement_for(self, slot: str) -> Requirement | None:
        key = str(slot or "").upper()
        return next((r for r in self.requirements if r.slot == key), None)

    def evidence_for(self, document_type: str) -> dict[str, Any]:
        """What must be readable on a document of this type."""
        return dict(self.evidence.get(str(document_type or "").upper(), {}) or {})

    def provenance(self) -> dict[str, Any]:
        """
        The block published alongside a checklist.

        `status` travels with it deliberately. A caller rendering a
        requirement derived from placeholder thresholds should be able to
        say so, and cannot if the resolution does not carry it.
        """
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "status": self.policy_status,
            "source": self.source,
            # The product this was resolved FOR. Carried because a narrow
            # answer -- a checklist question plans one tool -- never
            # fetches the application record, and a header that could not
            # name the product sat above a checklist that was entirely
            # determined by it.
            "product": self.product,
            "applied_rules": list(self.applied_rules),
            "unevaluated_rules": [u.public() for u in self.unevaluated_rules],
            "conflicts": [c.public() for c in self.conflicts],
        }

    def explain(self, slot: str) -> dict[str, Any] | None:
        """Why this case needs this slot. The whole answer, in one object."""
        requirement = self.requirement_for(slot)
        if requirement is None:
            return None
        return {
            "slot": requirement.slot,
            "requirement": requirement.requirement,
            "accepts": list(requirement.accepts),
            "reason": requirement.reason,
            "rule_id": requirement.rule_ids[0] if requirement.rule_ids else None,
            "rule_ids": list(requirement.rule_ids),
            "applicable_conditions": list(requirement.applicable_conditions),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_status": self.policy_status,
        }


# ==========================================================================
# INPUTS
# ==========================================================================

_AMOUNT_JUNK = re.compile(r"[^0-9.]")
_CURRENCY_WORDS = re.compile(r"(?i)\b(inr|rs|rupees)\b|[^\W\d_]")


def parse_amount(value: Any) -> Decimal | None:
    """
    A loan amount, or None when there is not one.

    None is returned for anything that is not unambiguously a number:
    unset, blank, "approx 5 lakh", a negative. An amount the engine is not
    sure of must not select a band, because selecting the wrong band asks a
    customer for documents they do not owe.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        return amount if amount >= 0 else None

    text = str(value).strip()
    if not text:
        return None

    # A currency mark is ordinary. A WORD is not: "approx 500000" and
    # "5 lakh" would both clean down to a number and quietly select a band.
    stripped = re.sub(r"(?i)\b(inr|rs\.?|rupees)\b", " ", text)
    if re.search(r"[^\W\d_]", stripped):
        return None

    # Indian grouping ("5,00,000") is ordinary here.
    cleaned = _AMOUNT_JUNK.sub("", stripped.replace(",", ""))
    if not cleaned or cleaned.count(".") > 1 or cleaned == ".":
        return None
    if "-" in text:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _matches(expected: Any, actual: Any) -> bool:
    """One `when` clause. A list means any of."""
    if actual is None:
        return False
    observed = str(actual).strip().upper()
    if isinstance(expected, (list, tuple, set)):
        return any(str(e).strip().upper() == observed for e in expected)
    return str(expected).strip().upper() == observed


def _in_band(amount: Decimal, rule: Mapping[str, Any]) -> bool:
    low = rule.get("min_amount")
    high = rule.get("max_amount")
    try:
        if low is not None and amount < Decimal(str(low)):
            return False
        if high is not None and amount > Decimal(str(high)):
            return False
    except InvalidOperation:
        logger.error("Policy rule %s has a non-numeric band", rule.get("rule_id"))
        return False
    return True


# ==========================================================================
# RESOLUTION
# ==========================================================================


def _entries(rule: Mapping[str, Any], group: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in (rule.get(group) or []):
        if isinstance(item, str):
            out.append({"slot": item.upper(), "accepts": [item.upper()],
                        "reason": ""})
            continue
        if not isinstance(item, Mapping):
            continue
        slot = str(item.get("slot") or "").strip().upper()
        if not slot:
            continue
        accepts = [str(a).strip().upper() for a in (item.get("accepts") or [])]
        out.append({"slot": slot, "accepts": accepts or [slot],
                    "reason": str(item.get("reason") or "").strip()})
    return out


class _Accumulator:
    """Collects requirements slot by slot as rules contribute them."""

    def __init__(self, policy_status: str) -> None:
        self._slots: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._policy_status = policy_status
        self.conflicts: list[PolicyConflict] = []

    def add(self, entry: Mapping[str, Any], *, rule_id: str, strength: str,
            conditions: Sequence[str] = ()) -> None:
        slot = entry["slot"]
        accepts = tuple(entry["accepts"])
        reason = entry.get("reason") or ""

        current = self._slots.get(slot)
        if current is None:
            self._order.append(slot)
            self._slots[slot] = {
                "accepts": accepts,
                "requirement": strength,
                "rule_ids": [rule_id],
                "reason": reason,
                "conditions": list(conditions),
            }
            return

        current["rule_ids"].append(rule_id)
        current["conditions"].extend(c for c in conditions
                                     if c not in current["conditions"])

        if _STRENGTH[strength] > _STRENGTH[current["requirement"]]:
            current["requirement"] = strength
            # The stronger rule's words explain the requirement better than
            # the weaker one's.
            if reason:
                current["reason"] = reason
        elif reason and not current["reason"]:
            current["reason"] = reason

        # A later rule may NARROW what satisfies a slot -- self-employment
        # narrowing income proof to an ITR is the point of having the rule.
        narrowed = tuple(a for a in current["accepts"] if a in accepts)
        if narrowed:
            current["accepts"] = narrowed
        else:
            # Disjoint. Widening keeps the case moving; the conflict is
            # reported so the policy file gets fixed.
            self.conflicts.append(PolicyConflict(
                rule_ids=tuple(current["rule_ids"]),
                slot=slot,
                detail=(f"rules narrow {slot} to document types that do not "
                        f"overlap ({sorted(current['accepts'])} and "
                        f"{sorted(accepts)}); the wider set is used"),
            ))
            current["accepts"] = tuple(
                dict.fromkeys(current["accepts"] + accepts))

    def build(self) -> tuple[Requirement, ...]:
        return tuple(
            Requirement(
                slot=slot,
                accepts=self._slots[slot]["accepts"],
                requirement=self._slots[slot]["requirement"],
                rule_ids=tuple(dict.fromkeys(self._slots[slot]["rule_ids"])),
                reason=self._slots[slot]["reason"],
                applicable_conditions=tuple(self._slots[slot]["conditions"]),
                policy_status=self._policy_status,
            )
            for slot in self._order
        )


def _legacy(product: str | None) -> PolicyResolution:
    """
    No policy file for this product. Use the agent checklist, and say so.

    This is what every product used before the engine existed, so a
    deployment that has not written a policy yet behaves exactly as it did.
    """
    from app.agents.applicant import config as agent_config

    try:
        # The YAML reader directly: `checklist_for` delegates BACK to this
        # engine, and calling it here would recurse.
        entries = agent_config._checklist_from_yaml(product)
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Could not read the agent checklist for %s", product)
        entries = []

    requirements = tuple(
        Requirement(
            slot=str(e["slot"]).upper(),
            accepts=tuple(str(a).upper() for a in e["accepts"]),
            requirement=REQUIRED if e.get("mandatory", True) else OPTIONAL,
            rule_ids=(LEGACY_POLICY_ID,),
            reason="",
            policy_status="UNVERSIONED",
        )
        for e in entries
    )
    return PolicyResolution(
        product=(str(product).upper() if product else None),
        policy_id=LEGACY_POLICY_ID,
        policy_version=LEGACY_VERSION,
        policy_status="UNVERSIONED",
        source=LEGACY_SOURCE,
        requirements=requirements,
        applied_rules=(LEGACY_POLICY_ID,) if requirements else (),
    )


def vocabulary_for(product: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """
    Every slot and document type this product's policy can ever produce.

    (slots, types). NOT the same as a case's checklist, and the difference
    matters. A checklist is what THIS case needs; the vocabulary is what
    the product could need under any amount and any attribute.

    WHY THE GROUNDING VALIDATOR NEEDS IT. The validator rejects an answer
    naming a document type that is not configured -- that is what stops a
    model inventing "AADHAAR_XML" or "FORM_26AS". It was checking against
    the case's resolved checklist, so an answer explaining that a larger
    loan would also need income proof named a slot the case did not have
    and was thrown away as ungrounded. The answer was correct; the
    vocabulary was too narrow.
    """
    policy = loader.policy_for(product)
    if policy is None:
        return frozenset(), frozenset()

    slots: set[str] = set()
    types: set[str] = set(str(k).upper() for k in (policy.get("documents") or {}))

    groups = [policy.get("base") or {}]
    groups += [r for r in (policy.get("amount_rules") or [])
               if isinstance(r, Mapping)]
    groups += [r for r in (policy.get("conditional_rules") or [])
               if isinstance(r, Mapping)]

    for group in groups:
        for key in ("required", "optional"):
            for entry in _entries(group, key):
                slots.add(entry["slot"])
                types.update(entry["accepts"])

    return frozenset(slots), frozenset(types)


def resolve(
    product: str | None,
    *,
    loan_amount: Any = None,
    attributes: Mapping[str, Any] | None = None,
) -> PolicyResolution:
    """
    The document requirements for one case.

    `attributes` is whatever the store holds about the applicant and the
    application -- employment type, residence status, anything a policy file
    keys on. A key that is absent is absent; the engine does not default it.
    """
    policy = loader.policy_for(product)
    if policy is None:
        return _legacy(product)

    attributes = {str(k).lower(): v for k, v in (attributes or {}).items()}
    policy_status = str(policy.get("status") or "UNCONFIRMED").upper()
    accumulator = _Accumulator(policy_status)
    applied: list[str] = []
    unevaluated: list[UnevaluatedRule] = []

    # ---- base: every case, no condition --------------------------------
    base = policy.get("base") or {}
    base_id = str(base.get("rule_id") or "BASE")
    if base:
        applied.append(base_id)
        for entry in _entries(base, "required"):
            accumulator.add(entry, rule_id=base_id, strength=REQUIRED)
        for entry in _entries(base, "optional"):
            accumulator.add(entry, rule_id=base_id, strength=OPTIONAL)

    # ---- amount bands ---------------------------------------------------
    amount = parse_amount(loan_amount)
    amount_rules = [r for r in (policy.get("amount_rules") or [])
                    if isinstance(r, Mapping)]

    if amount is None and amount_rules:
        # No amount, so no band. The base checklist still stands; the case
        # is told the checklist is not final rather than being handed a
        # short list that looks final.
        unevaluated.append(UnevaluatedRule(
            rule_id="AMOUNT_BANDS",
            missing_attributes=("loan_amount",),
            reason=("The loan amount is not captured, so amount-based "
                    "document rules could not be applied."),
            would_require=tuple(dict.fromkeys(
                e["slot"] for r in amount_rules for e in _entries(r, "required")
            )),
        ))
    elif amount is not None:
        for rule in amount_rules:
            rule_id = str(rule.get("rule_id") or "AMOUNT_RULE")
            if not _in_band(amount, rule):
                continue
            applied.append(rule_id)
            for entry in _entries(rule, "required"):
                accumulator.add(entry, rule_id=rule_id, strength=REQUIRED)
            for entry in _entries(rule, "optional"):
                accumulator.add(entry, rule_id=rule_id, strength=OPTIONAL)

    # ---- attribute conditions -------------------------------------------
    for rule in (policy.get("conditional_rules") or []):
        if not isinstance(rule, Mapping):
            continue
        rule_id = str(rule.get("rule_id") or "CONDITIONAL_RULE")
        when = rule.get("when") or {}
        if not isinstance(when, Mapping) or not when:
            continue

        missing = [str(k).lower() for k in when
                   if attributes.get(str(k).lower()) in (None, "")]
        if missing:
            unevaluated.append(UnevaluatedRule(
                rule_id=rule_id,
                missing_attributes=tuple(missing),
                reason=(f"{rule_id} depends on {', '.join(missing)}, which "
                        f"the case has not captured, so the rule was not "
                        f"applied."),
                would_require=tuple(e["slot"]
                                    for e in _entries(rule, "required")),
            ))
            continue

        if not all(_matches(expected, attributes.get(str(key).lower()))
                   for key, expected in when.items()):
            continue

        conditions = tuple(
            f"{str(key).lower()}={attributes.get(str(key).lower())}"
            for key in when
        )
        applied.append(rule_id)
        for entry in _entries(rule, "required"):
            accumulator.add(entry, rule_id=rule_id, strength=CONDITIONAL,
                            conditions=conditions)
        for entry in _entries(rule, "optional"):
            accumulator.add(entry, rule_id=rule_id, strength=OPTIONAL,
                            conditions=conditions)

    evidence = {str(k).upper(): v
                for k, v in (policy.get("documents") or {}).items()
                if isinstance(v, Mapping)}

    return PolicyResolution(
        product=str(product).upper() if product else None,
        policy_id=str(policy.get("policy_id") or "UNNAMED_POLICY"),
        policy_version=str(policy.get("policy_version") or "unversioned"),
        policy_status=policy_status,
        source=str(policy.get("source_file") or "policy"),
        requirements=accumulator.build(),
        applied_rules=tuple(dict.fromkeys(applied)),
        unevaluated_rules=tuple(unevaluated),
        conflicts=tuple(accumulator.conflicts),
        loan_amount=amount,
        evidence=evidence,
    )


__all__ = [
    "CONDITIONAL", "LEGACY_POLICY_ID", "LEGACY_SOURCE", "LEGACY_VERSION",
    "NOT_APPLICABLE", "OPTIONAL", "REQUIRED", "PolicyConflict",
    "PolicyResolution", "Requirement", "UnevaluatedRule", "parse_amount",
    "resolve",
]
