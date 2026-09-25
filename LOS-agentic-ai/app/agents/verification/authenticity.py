"""
What a verification verdict establishes -- and what it does not.

TWO DIFFERENT QUESTIONS, and only one of them is answered here.

    DOCUMENT INTEGRITY   is this a legible document of the claimed type,
                         structurally valid, internally consistent? A PAN's
                         format and field consistency; a bank statement's
                         running balance reconciling; a payslip's arithmetic.
                         THIS SERVICE CHECKS THIS.

    ISSUER AUTHENTICITY  did the government actually issue this PAN? did the
                         bank actually produce this statement? Only the
                         issuer can say. There is no issuer lookup, no bank
                         API and no issuer signature check anywhere in this
                         build. THIS SERVICE DOES NOT CHECK THIS.

A PASS is a verdict about the first question. Every verified document
therefore carries, beside its verdict:

    verification_scope   which checks the verdict rests on
    authenticity         NOT_ESTABLISHED, always, until an issuer source exists
    issuer_verified      false, always, until an issuer source exists

so a caller reading "PASS" cannot mistake it for "genuine".

WHERE AUTHENTICITY EVIDENCE IS REQUIRED, the `REQUIRE_EXTERNAL` policy
(documents.yaml, or VERIFICATION_AUTHENTICITY_POLICY) caps every identity
AND financial document at REVIEW with AUTHENTICITY_NOT_ESTABLISHED: with no
issuer source, a human has to look. Which products or stages require it is
the lender's decision; the default remains STRUCTURAL_PASS.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any

NOT_ESTABLISHED = "NOT_ESTABLISHED"
REVIEW_CODE = "AUTHENTICITY_NOT_ESTABLISHED"

#: No issuer, bank or government verification source is wired up.
ISSUER_VERIFICATION_AVAILABLE = False

IDENTITY_CLASSES = frozenset({"PAN", "DRIVING_LICENCE", "VOTER_ID",
                              "PASSPORT", "AADHAAR"})
FINANCIAL_CLASSES = frozenset({"BANK_STATEMENT", "SALARY_SLIP", "ITR"})

#: What each verdict rests on. Integrity checks only -- never issuance.
_SCOPES = {
    **{c: "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY" for c in IDENTITY_CLASSES},
    "BANK_STATEMENT": "DOCUMENT_STRUCTURE_AND_BALANCE_RECONCILIATION",
    "SALARY_SLIP": "DOCUMENT_STRUCTURE_AND_PAY_ARITHMETIC",
    "ITR": "DOCUMENT_STRUCTURE_AND_FIELD_CONSISTENCY",
}


def applies_to(document_class: Any) -> bool:
    """Identity and financial documents -- the ones an issuer produces."""
    return str(document_class or "").upper() in _SCOPES


def scope(document_class: Any) -> dict[str, Any]:
    """The three fields every verified identity or financial document carries."""
    key = str(document_class or "").upper()
    return {
        "verification_scope": _SCOPES.get(key, "DOCUMENT_STRUCTURE"),
        "authenticity": NOT_ESTABLISHED,
        "issuer_verified": False,
    }


#: Set by the LOS flow around the Document Agent. There the ISSUER LAYER
#: applies the authenticity hold -- after asking the issuer -- so the agent
#: must not apply it first: its hold discards the extracted fields, leaving a
#: provider nothing to verify and a confirmation nothing to release. Direct
#: callers of the agent never set this and keep the hold exactly as before.
_DEFERRED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "authenticity_deferred", default=False)


@contextmanager
def deferred():
    """Leave the authenticity hold to the issuer layer, for this context only."""
    token = _DEFERRED.set(True)
    try:
        yield
    finally:
        _DEFERRED.reset(token)


def is_deferred() -> bool:
    return _DEFERRED.get()


def required() -> bool:
    """Whether authenticity evidence is required for a PASS."""
    from app.agents.verification.rules import authenticity_policy

    return authenticity_policy() == "REQUIRE_EXTERNAL"


def cap(document_class: Any, status: str,
        reason_codes: list[str]) -> tuple[str, list[str]]:
    """
    A PASS that needs authenticity evidence it cannot have becomes REVIEW.

    Only ever downgrades, and only a PASS. With an issuer source configured
    this would ask it; there is none, so the answer is always "not
    established".
    """
    codes = list(reason_codes or [])
    if is_deferred():
        return status, codes
    if (applies_to(document_class) and required()
            and not ISSUER_VERIFICATION_AVAILABLE
            and str(status).upper() == "PASS"):
        if REVIEW_CODE not in codes:
            codes.append(REVIEW_CODE)
        return "REVIEW", codes
    return status, codes


__all__ = ["FINANCIAL_CLASSES", "IDENTITY_CLASSES", "NOT_ESTABLISHED",
           "REVIEW_CODE", "applies_to", "cap", "required", "scope"]
