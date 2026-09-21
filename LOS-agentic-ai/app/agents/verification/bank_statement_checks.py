"""
A bank statement's verification evidence, bank-independent.

WHAT THIS IS FOR. Turning what the parser found into named checks the shared
scoring layer can weigh, and — the part that was missing — into REASON CODES.

The financial verification path published `{"status": ..., "checks": {...}}`
and no reason codes at all, so every bank statement that did not pass came
back as:

    verification: REVIEW
    reason_codes: []

A field officer was told a document needed review and nothing about what to
do with it. The verdict was usually right; it was simply unexplained.

BASIC DOCUMENT VERIFICATION ONLY. Every check here asks whether the document
is a readable, structurally coherent, internally consistent bank statement.
None of them looks at what the money means. There is no income estimate, no
affordability, no spending pattern and no risk — those belong to the credit
stage, and a FOS response that carried them would be that stage's answer
given by a desk with no authority to give it.
"""

from __future__ import annotations

from typing import Any

from app.agents.verification.scoring import Check, Outcome

#: Reason codes this verifier can emit. Kept in one place so the taxonomy
#: can be read without grepping, and so duplicates are visible.
UNREADABLE = "DOCUMENT_UNREADABLE"
STRUCTURE_UNCLEAR = "BANK_STATEMENT_STRUCTURE_UNCLEAR"
PARSE_INCOMPLETE = "BANK_STATEMENT_PARSE_INCOMPLETE"
RECONCILIATION_INCONCLUSIVE = "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE"
RECONCILIATION_FAILED = "BANK_STATEMENT_RECONCILIATION_FAILED"
TIMEOUT = "VERIFICATION_TIMEOUT"
REQUIRES_OCR = "DOCUMENT_REQUIRES_OCR"
NO_TRANSACTIONS = "BANK_STATEMENT_NO_TRANSACTIONS"
PERIOD_MISSING = "REQUIRED_FIELD_MISSING"


def _truncated(result: Any) -> bool:
    text = " ".join(getattr(result, "warnings", None) or [])
    return ("time budget" in text
            or "Stopped after page" in text
            or "completeness could not be established" in text)


def checks_for(result: Any) -> list[Check]:
    """
    The evidence, as named checks.

    `result` is a BankStatementResult. Read defensively: a parser that
    returned something unexpected must produce an inconclusive verdict, not
    an exception on the upload path.
    """
    status = str(getattr(getattr(result, "status", None), "value", "") or "")
    source = str(getattr(getattr(result, "source_kind", None), "value", "")
                 or "")
    transactions = list(getattr(result, "transactions", None) or [])
    reconciles = getattr(result, "balance_reconciles", None)
    opening = getattr(result, "opening_balance", None)
    closing = getattr(result, "closing_balance", None)
    period = getattr(result, "period", None)
    pages = getattr(result, "pages", 0) or 0
    pages_with_text = getattr(result, "pages_with_text", 0) or 0
    truncated = _truncated(result)

    checks: list[Check] = []

    # -- readable at all --------------------------------------------------
    #
    # A HARD GATE, resolving to REVIEW rather than FAIL. A scan this build
    # cannot read is a limit of this service, not a finding against the
    # document, and it routes to a person or to the OCR queue.
    if status == "REQUIRES_OCR":
        checks.append(Check(
            name="readable", outcome=Outcome.UNKNOWN, weight=3.0,
            hard_gate=True, gate_verdict="REVIEW",
            reason_code=REQUIRES_OCR,
            reason=("This bank statement is a scan that could not be read "
                    "automatically. It needs to be re-uploaded as a clearer "
                    "scan or reviewed manually."),
        ))
        return checks

    checks.append(Check(
        name="readable",
        outcome=(Outcome.OK if pages_with_text or transactions
                 else Outcome.BAD),
        weight=3.0, hard_gate=True, gate_verdict="REVIEW",
        reason_code=UNREADABLE,
        reason=("This bank statement could not be read. It needs to be "
                "re-uploaded in a clearer form."),
    ))

    # -- recognisable as a statement --------------------------------------
    checks.append(Check(
        name="statement_structure",
        outcome=Outcome.OK if transactions else Outcome.BAD,
        weight=2.5,
        reason_code=NO_TRANSACTIONS,
        reason=("No transactions could be read from this bank statement, so "
                "its structure could not be confirmed."),
    ))

    # -- the parse ran to the end -----------------------------------------
    checks.append(Check(
        name="parse_complete",
        outcome=Outcome.UNKNOWN if truncated else Outcome.OK,
        weight=2.0,
        reason_code=TIMEOUT if truncated else PARSE_INCOMPLETE,
        reason=("This bank statement is long and could not be read all the "
                "way through within the time allowed, so it needs review. "
                "This is a limit of the service, not a problem with the "
                "document."),
    ))

    # -- the period the statement covers ----------------------------------
    has_period = bool(
        period and (getattr(period, "start", None)
                    or getattr(period, "end", None))
    )
    checks.append(Check(
        name="statement_period",
        outcome=Outcome.OK if has_period else Outcome.UNKNOWN,
        weight=1.0,
        reason_code=PERIOD_MISSING,
        reason="The statement period could not be read from this document.",
    ))

    # -- balances ----------------------------------------------------------
    checks.append(Check(
        name="balances_present",
        outcome=(Outcome.OK if opening is not None and closing is not None
                 else Outcome.UNKNOWN),
        weight=1.5,
        reason_code=STRUCTURE_UNCLEAR,
        reason=("The opening and closing balances could not both be read, "
                "so this statement needs review."),
    ))

    # -- INTEGRITY, and the distinction that matters -----------------------
    #
    # True  the rows explain the balance -- conclusive, and good
    # False the rows do not -- conclusive, and bad
    # None  it could not be established -- NOT a failure
    #
    # Reporting None as a failure is how a parser limitation becomes an
    # accusation about the customer's statement.
    if reconciles is True:
        integrity = Check(name="integrity", outcome=Outcome.OK, weight=3.0)
    elif reconciles is False:
        # CONCLUSIVE arithmetic inconsistency, on complete evidence. The
        # existing integrity policy stands: this is a hard gate and it fails.
        integrity = Check(
            name="integrity", outcome=Outcome.BAD, weight=3.0,
            hard_gate=True, gate_verdict="FAIL",
            reason_code=RECONCILIATION_FAILED,
            reason=("The transactions on this statement do not add up to the "
                    "balance it shows."),
        )
    elif truncated:
        # INCONCLUSIVE, AND WE KNOW WHY: the parse did not reach the end of
        # the statement, so there is an unread tail the rows could be
        # missing from.
        #
        # WHY THIS GETS ITS OWN CODE. The gate decides the verdict, and the
        # scoring layer publishes the deciding gate's code alone -- so
        # whatever this check says is the ONLY thing the queue sees. Sending
        # "integrity could not be established" for a statement we simply ran
        # out of time on routes a service capacity problem to a human
        # reviewer, who opens the document, finds nothing wrong, and closes
        # it. The operator action is to raise the budget or queue the
        # document for a full parse, and only this code says so.
        integrity = Check(
            name="integrity", outcome=Outcome.UNKNOWN, weight=3.0,
            hard_gate=True, gate_verdict="FAIL",
            reason_code=TIMEOUT,
            reason=("This bank statement could not be read all the way "
                    "through within the time allowed, so its transaction "
                    "integrity could not be established. This is a limit of "
                    "the service, not a finding about the document."),
        )
    else:
        # INCONCLUSIVE, which is a different thing entirely. The same gate,
        # but an UNKNOWN on a gate resolves to REVIEW: a check that could not
        # be evaluated is not a check the document failed, and treating a
        # parser limitation as an integrity failure turns our shortcoming
        # into an accusation about the customer's statement.
        integrity = Check(
            name="integrity", outcome=Outcome.UNKNOWN, weight=3.0,
            hard_gate=True, gate_verdict="FAIL",
            reason_code=RECONCILIATION_INCONCLUSIVE,
            reason=("This bank statement needs review because its "
                    "transaction integrity could not be established "
                    "confidently."),
        )
    checks.append(integrity)

    return checks


__all__ = [
    "NO_TRANSACTIONS", "PARSE_INCOMPLETE", "PERIOD_MISSING",
    "RECONCILIATION_FAILED", "RECONCILIATION_INCONCLUSIVE", "REQUIRES_OCR",
    "STRUCTURE_UNCLEAR", "TIMEOUT", "UNREADABLE", "checks_for",
]
