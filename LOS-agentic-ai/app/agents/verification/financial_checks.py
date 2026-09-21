"""
Verification evidence for a financial document that is not a bank statement.

WHAT WAS MISSING. `bank_statement_checks.py` turns a parsed statement into
named checks the shared scoring layer can weigh. ITR returns and salary
slips had no equivalent, so `_serialise_financial_verification` fell back to
a bare status and neither a score nor a confidence was produced. A passing
ITR came back with `verification_score: null`, exactly as identity
documents did before Phase 1.

DOCUMENT-AGNOSTIC BY CONSTRUCTION. Nothing here knows what an ITR looks
like or what a payslip contains. It reads the NORMALISED result every
financial extractor already produces -- status, identity fields, period,
and the tri-state `verified` flag -- so a financial type added later is
scored without anyone writing a new module for it. The alternative, a
checks module per document type, is how bank statements ended up with
reason codes and ITRs ended up without.

THE TRI-STATE IS THE WHOLE POINT, and it is the distinction the brief calls
out as PHASE 2B:

    verified = True    the integrity check ran and passed
    verified = False   the integrity check ran and FAILED -- conclusive
    verified = None    the check could not run at all -- INCONCLUSIVE

None is not False. A salary slip whose deductions could not be read has not
failed its arithmetic; nobody did the arithmetic. Reporting that as a
failure turns "we could not check" into an accusation about the customer's
payslip, which is the same defect the bank-statement path was fixed for
earlier in this hardening pass.

NO HARD GATES ARE SET HERE. The verdict is already decided by the caller
from `verified`, unchanged. These checks produce the two numbers and the
reason codes that explain them; they do not decide anything.
"""

from __future__ import annotations

from typing import Any

from app.agents.verification.scoring import Check, Outcome

# Reason codes this builder can emit. Kept in one place so the taxonomy is
# readable without grepping, and so duplicates are visible.
UNREADABLE = "DOCUMENT_UNREADABLE"
REQUIRES_OCR = "DOCUMENT_REQUIRES_OCR"
UNSUPPORTED = "INVALID_DOCUMENT"
IDENTITY_MISSING = "REQUIRED_FIELD_NOT_FOUND"
PERIOD_MISSING = "REQUIRED_FIELD_NOT_FOUND"
INTEGRITY_FAILED = "VERIFICATION_INTEGRITY_FAILED"
INTEGRITY_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"

#: Which normalised fields each financial type's extractor actually
#: populates.
#:
#: STRUCTURAL, NOT POLICY. It says what the extractor produces, not what a
#: lender requires. A type absent from here is scored on the checks that
#: apply to everything, which is why adding one does not break it.
#:
#: SCORING A FIELD NOBODY POPULATES IS THE VOTER_ID DEFECT AGAIN. That bug
#: -- configuration requiring `voter_id` while the extractor emitted
#: `epic_number` -- sent every voter ID to REVIEW. The same shape appeared
#: here the moment this module was first run: `period` was scored on every
#: financial document, and neither the ITR nor the salary-slip extractor
#: populates `period_start` or `period_end`, so both lost ten points for a
#: field that was never going to arrive. `period` is now opt-in per type,
#: and tests/agents/test_financial_scoring.py asserts every scored field is
#: one the extractor can actually fill.
_EXPECTED: dict[str, tuple[str, ...]] = {
    "ITR": ("pan", "name"),
    "SALARY_SLIP": ("name", "employer_name"),
    "BANK_STATEMENT": ("account_number_masked",),
    "SALE_DEED": (),
}

#: Types whose extractor populates a statement period.
#:
#: A bank statement is meaningless without one. An ITR acknowledgement and
#: a payslip carry dates, but the normalised result does not surface them,
#: so asking for one is asking for something that cannot be given.
_HAS_PERIOD = {"BANK_STATEMENT"}


def _status_of(result: Any) -> str:
    return str(getattr(getattr(result, "status", None), "value", "") or "")


def checks_for(result: Any) -> list[Check]:
    """
    The evidence, as named checks.

    Read defensively throughout: an extractor that returned something
    unexpected must produce an inconclusive score, never an exception on
    the upload path.
    """
    status = _status_of(result)
    document_type = str(getattr(getattr(result, "document_type", None),
                                "value", "") or "")
    checks: list[Check] = []

    # -- could it be read at all? -------------------------------------
    #
    # THREE OUTCOMES, NOT TWO, and they must stay apart. A scan awaiting
    # OCR is not a corrupt file, and neither is a file type this service
    # does not handle. Each sends the document somewhere different.
    if status == "REQUIRES_OCR":
        checks.append(Check(
            name="readable", outcome=Outcome.UNKNOWN, weight=3.0,
            reason_code=REQUIRES_OCR,
            reason=("This document is a scan that could not be read "
                    "automatically. Re-upload it as a clearer scan, or have "
                    "it reviewed."),
        ))
        return checks

    if status == "UNSUPPORTED":
        checks.append(Check(
            name="readable", outcome=Outcome.BAD, weight=3.0,
            reason_code=UNSUPPORTED,
            reason="This file is not a document this service can read.",
        ))
        return checks

    checks.append(Check(
        name="readable",
        outcome=Outcome.BAD if status == "FAILED" else Outcome.OK,
        weight=3.0,
        reason_code=UNREADABLE,
        reason=("This document could not be read. Re-upload it in a clearer "
                "form."),
    ))

    # -- whose document is it? -----------------------------------------
    expected = _EXPECTED.get(document_type)
    if expected is None:
        # A type nobody has described here is scored on the universal
        # checks alone rather than being penalised for fields this module
        # has no opinion about.
        expected = ()

    for field_name in expected:
        present = str(getattr(result, field_name, None) or "").strip()
        checks.append(Check(
            name=f"field:{field_name}",
            outcome=Outcome.OK if present else Outcome.BAD,
            weight=1.5,
            reason_code=IDENTITY_MISSING,
            reason=(f"{field_name.replace('_', ' ')} could not be read from "
                    f"this document."),
        ))

    # -- what period does it cover? ------------------------------------
    #
    # Only asked of the types whose extractor populates one. Scoring it
    # everywhere docked every ITR and every payslip for a field their
    # extractors do not produce -- a deduction nothing could ever clear.
    if document_type in _HAS_PERIOD:
        period = (getattr(result, "period_start", None)
                  or getattr(result, "period_end", None))
        checks.append(Check(
            name="period",
            outcome=Outcome.OK if period else Outcome.UNKNOWN,
            weight=1.0,
            reason_code=PERIOD_MISSING,
            reason="The period this document covers could not be read.",
        ))

    # -- INTEGRITY, and the tri-state that matters ----------------------
    verified = getattr(result, "verified", None)
    note = str(getattr(result, "verification_note", None) or "").strip()

    if verified is True:
        checks.append(Check(name="integrity", outcome=Outcome.OK, weight=3.0))
    elif verified is False:
        # CONCLUSIVE. The check ran and the document did not satisfy it.
        checks.append(Check(
            name="integrity", outcome=Outcome.BAD, weight=3.0,
            reason_code=INTEGRITY_FAILED,
            reason=(note or "This document did not pass its integrity check."),
        ))
    else:
        # INCONCLUSIVE, which is a different thing entirely. Nobody did the
        # arithmetic; the document did not fail it.
        checks.append(Check(
            name="integrity", outcome=Outcome.UNKNOWN, weight=3.0,
            reason_code=INTEGRITY_INCONCLUSIVE,
            reason=(note or "This document needs review because its "
                            "verification could not be completed."),
        ))

    return checks


def confidence_for(base_confidence: int, result: Any) -> int:
    """
    Temper the scoring module's conclusiveness with the extractor's own.

    WHY THIS IS NEEDED AND NOT DECORATION. `scoring.assess` computes
    confidence as the share of the applicable weight that was decided
    either way. On a document where nothing failed, that is arithmetically
    the same number as the score -- an ITR came back score 90, confidence
    90, and two figures that always agree are one figure reported twice.

    They answer different questions and must be able to diverge. The score
    says how much was established; the confidence says how far to trust
    it. The extractor's own confidence is exactly that second thing: a
    parser that read a payslip cleanly and one that guessed at it can
    produce the same fields and should not produce the same certainty.

    A FLOOR, as everywhere else. Extractor confidence is not calibrated
    across document types, and letting a conservative parser drive the
    number to nothing would make it useless.
    """
    raw = getattr(result, "confidence", None)
    if raw is None:
        return max(0, min(100, int(base_confidence)))

    try:
        extractor = float(raw)
    except (TypeError, ValueError):
        return max(0, min(100, int(base_confidence)))

    # Extractors report 0-1; a stray percentage is normalised rather than
    # allowed to multiply the confidence by fifty.
    if extractor > 1.0:
        extractor = extractor / 100.0

    factor = max(0.6, min(1.0, extractor))
    return max(0, min(100, int(round(base_confidence * factor))))


__all__ = [
    "IDENTITY_MISSING", "INTEGRITY_FAILED", "INTEGRITY_INCONCLUSIVE",
    "PERIOD_MISSING", "REQUIRES_OCR", "UNREADABLE", "UNSUPPORTED",
    "checks_for", "confidence_for",
]
