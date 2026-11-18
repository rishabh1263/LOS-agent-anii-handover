"""
Eligibility: is this applicant eligible for the loan asked for, and for how much?

Five config-driven rules over STRUCTURED applicant data (a DataProvider by case id -- never a document), a deterministic
engine (no model) and one of APPROVED / CONDITIONAL / REFER / REJECTED / DATA_MISSING with the eligible amount, the
EMI, the FOIR and every rule's outcome. See engine.py.
"""

from app.agents.eligibility.domain.models import Decision, EligibilityInput, EligibilityResult, RuleResult

__all__ = ["Decision", "EligibilityInput", "EligibilityResult", "RuleResult"]
