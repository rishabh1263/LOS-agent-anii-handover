"""
Eligibility: can this applicant afford the loan they asked for?

THE STAGE BETWEEN EVIDENCE AND RISK. Document verification asks whether a
document is real, KYC whether the documents describe one person, income
whether they agree about what arrives each month. This asks the first
question that is about the LOAN: given a verified monthly income and a
requested amount, does the affordability policy allow it.

IT DECIDES NOTHING ABOUT THE LOAN. The result is one stage verdict --
PASS, REVIEW, FAIL or SKIPPED -- with the reasons behind it. Approving
and rejecting belong to a decision layer this package is not part of.
"""

from app.agents.eligibility.schemas import (
    EligibilityResult,
    EligibilityStatus,
    ReasonCode,
)

__all__ = ["EligibilityResult", "EligibilityStatus", "ReasonCode"]
