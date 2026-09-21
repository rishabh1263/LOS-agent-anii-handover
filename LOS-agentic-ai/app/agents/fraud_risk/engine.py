"""
Deterministic risk rule engine.

Runs every enabled rule, aggregates flags, computes the risk score, maps it to
a risk category, and derives this agent's final outcome.

No LLM is reachable from this module. Given the same input and the same policy,
this engine always produces the same score, category and outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.fraud_risk.rules import RULE_REGISTRY
from app.agents.fraud_risk.schemas import (
    AgentOutcome,
    DataGap,
    FraudRiskRequest,
    RiskCategory,
    RiskFlag,
    Severity,
)


@dataclass
class RiskAssessment:
    """Complete deterministic output. Produced before the LLM is ever called."""

    risk_score: int
    risk_category: RiskCategory
    final_outcome: AgentOutcome
    flags: list[RiskFlag] = field(default_factory=list)
    data_gaps: list[DataGap] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class RiskRuleEngine:
    """Evaluates the policy's rules against a request."""

    def __init__(self, policy: dict[str, Any]) -> None:
        self.policy = policy

    # -- scoring ----------------------------------------------------------

    def _severity_weight(self, severity: Severity) -> int:
        weights = self.policy["scoring"].get("severity_weights", {})
        return int(weights.get(severity.value, 0))

    def _categorise(self, score: int) -> RiskCategory:
        categories = self.policy["risk_categories"]
        for name in ("HIGH", "MEDIUM", "LOW"):
            band = categories.get(name)
            if band is None:
                continue
            low = band.get("min_score", 0)
            high = band.get("max_score")
            if score >= low and (high is None or score <= high):
                return RiskCategory(name)
        # Score above every configured band -> most severe category.
        return RiskCategory.HIGH

    def _escalate_for_critical(
        self, category: RiskCategory, flags: list[RiskFlag]
    ) -> RiskCategory:
        """
        Raise the category when a CRITICAL flag is present.

        Without this a single CRITICAL finding can land in the MEDIUM band on
        score alone, producing a MEDIUM category alongside a FAIL outcome --
        the two disagreeing about the same application.

        Only ever escalates, never downgrades.
        """
        target = self.policy.get("critical_forces_category")
        if not target:
            return category
        if not any(f.severity == Severity.CRITICAL for f in flags):
            return category

        rank = {RiskCategory.LOW: 0, RiskCategory.MEDIUM: 1, RiskCategory.HIGH: 2}
        forced = RiskCategory(target)
        return forced if rank[forced] > rank[category] else category

    def _outcome(
        self,
        category: RiskCategory,
        flags: list[RiskFlag],
        insufficient_data: bool,
    ) -> AgentOutcome:
        outcomes = self.policy["outcomes"]

        # A CRITICAL flag overrides the score-derived outcome.
        if any(f.severity == Severity.CRITICAL for f in flags):
            return AgentOutcome(outcomes["on_critical"])

        base = AgentOutcome(outcomes[category.value])

        # Insufficient data may only make the outcome stricter, never weaker.
        if insufficient_data:
            fallback = AgentOutcome(outcomes["on_insufficient_data"])
            order = {AgentOutcome.PASS: 0, AgentOutcome.REVIEW: 1, AgentOutcome.FAIL: 2}
            if order[fallback] > order[base]:
                return fallback

        return base

    # -- required inputs --------------------------------------------------

    def _missing_required(self, req: FraudRiskRequest) -> list[str]:
        required = self.policy.get("required_for_complete_assessment", [])
        missing: list[str] = []
        for name in required:
            if name == "dedupe_result":
                if req.dedupe is None:
                    missing.append(name)
            elif name == "declared_income":
                if req.income.declared_income is None:
                    missing.append(name)
            elif name == "gross_income":
                from app.agents.fraud_risk.income import verified_income

                if verified_income(req.income) is None:
                    missing.append(name)
        return missing

    # -- main -------------------------------------------------------------

    def evaluate(self, req: FraudRiskRequest) -> RiskAssessment:
        rules_cfg = self.policy.get("rules", {})
        flags: list[RiskFlag] = []
        gaps: list[DataGap] = []

        for rule_name, rule_cfg in rules_cfg.items():
            if not rule_cfg.get("enabled", False):
                continue
            func = RULE_REGISTRY.get(rule_name)
            if func is None:
                continue

            raw_flags, raw_gaps = func(req, rule_cfg)
            gaps.extend(raw_gaps)

            for raw in raw_flags:
                severity = raw["severity"]
                flags.append(
                    RiskFlag(
                        rule=raw["rule"],
                        severity=severity,
                        score_contribution=self._severity_weight(severity),
                        message=raw["message"],
                        evidence=raw["evidence"],
                    )
                )

        max_score = int(self.policy["scoring"].get("max_score", 100))
        raw_score = sum(f.score_contribution for f in flags)
        score = min(raw_score, max_score)

        category = self._categorise(score)
        category_before_escalation = category
        category = self._escalate_for_critical(category, flags)

        missing_required = self._missing_required(req)
        outcome = self._outcome(category, flags, bool(missing_required))

        # Highest severity first, then by score contribution, then by rule name
        # so ordering is stable across runs.
        severity_rank = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        flags.sort(key=lambda f: (severity_rank[f.severity], -f.score_contribution, f.rule))

        evidence = {
            "raw_score_before_cap": raw_score,
            "max_score": max_score,
            "triggered_rule_count": len(flags),
            "missing_required_inputs": missing_required,
            "category_from_score": category_before_escalation.value,
            "category_escalated_by_critical": category is not category_before_escalation,
            "rules_evaluated": [
                name for name, cfg in rules_cfg.items() if cfg.get("enabled", False)
            ],
        }

        return RiskAssessment(
            risk_score=score,
            risk_category=category,
            final_outcome=outcome,
            flags=flags,
            data_gaps=gaps,
            evidence=evidence,
        )


__all__ = ["RiskRuleEngine", "RiskAssessment"]