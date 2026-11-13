"""The eight bank verification rules. R1-R7 read the Evidence; R8 sums up their results."""

from __future__ import annotations

from app.agents.credit.bank_verification.rules import (account_ownership, balance_validation, bank_details,
                                                       duplicate_detection, risk_assessment, source_crosscheck,
                                                       statement_integrity, transaction_matching)
from app.agents.credit.bank_verification.rules.base import Evidence, PriorDocument
from app.agents.credit.bank_verification.schemas import RuleResult

#: R1-R7, in order
CHECKS = (bank_details, transaction_matching, statement_integrity, account_ownership, balance_validation,
          source_crosscheck, duplicate_detection)


def run_all(evidence: Evidence) -> list[RuleResult]:
    """All eight results, R1 to R8."""
    results = [rule.run(evidence) for rule in CHECKS]
    return results + [risk_assessment.run(results)]


__all__ = ["CHECKS", "Evidence", "PriorDocument", "run_all"]
