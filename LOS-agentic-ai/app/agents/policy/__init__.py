"""
The FOS document policy engine.

WHAT THIS OWNS. Which documents a case needs, and why. It answers that from
configuration -- a policy file per product -- and never from a language model,
never from a hardcoded amount band in Python, and never from an assumption
about what lenders usually want.

WHAT IT DOES NOT OWN. Whether a document is genuine (verification), whether
the applicant is good for the money (credit), or whether the identity holds
up (KYC). It says what to collect. Nothing else.

THE TWO THINGS A CALLER GETS BACK. A list of requirements, and the provenance
of every one of them: which rule produced it, which policy version that rule
came from, and which case attributes made it apply. A requirement a field
officer cannot get an explanation for is a requirement they will argue with.
"""

from app.agents.policy.engine import (
    CONDITIONAL,
    NOT_APPLICABLE,
    OPTIONAL,
    REQUIRED,
    PolicyResolution,
    Requirement,
    resolve,
)
from app.agents.policy.loader import (
    known_products,
    policy_for,
    reload,
)

__all__ = [
    "CONDITIONAL", "NOT_APPLICABLE", "OPTIONAL", "REQUIRED",
    "PolicyResolution", "Requirement", "known_products", "policy_for",
    "reload", "resolve",
]
