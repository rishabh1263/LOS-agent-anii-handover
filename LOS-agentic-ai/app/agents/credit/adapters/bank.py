"""
bank_behaviour.get -- the bank-statement signals the Financial Agent RECORDED.

The signals (bank_statement/signals.derive) and the income evidence
(bank_statement/income.income_evidence) were computed at extraction time,
only from rows that reconciled against the printed balance, and released
with the extraction. This reads them back. It does not re-parse a statement,
re-count a bounce or re-detect a salary credit.

ABSENCE IS NOT ZERO. signals.derive reports a narration count (returned
transactions, salary credits, mandate debits) only when a row matched. A key
that is absent is passed on as absent -- this reader never writes a 0.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.adapters import current_findings
from app.agents.credit.adapters.case_memory import evidence_ref
from app.agents.credit.schemas import EvidenceSource, Observation, Quality

_SIGNAL_KEYS = (
    "transaction_count", "credit_count", "debit_count", "reconciled",
    "opening_balance_source", "minimum_balance", "maximum_balance",
    "average_transaction_balance", "monthly_credit_count", "credit_volatility",
    "salary_credit_count", "salary_credit_total", "returned_transaction_count",
    "mandate_debit_count", "mandate_debit_total", "cash_withdrawal_count",
)
_INCOME_KEYS = ("type", "estimated_monthly_amount", "months_observed",
                "recurring_credit_count", "confidence")


def _is_bank_statement(finding: Any) -> bool:
    return str((getattr(finding, "payload", None) or {}).get("type") or "").upper() \
        == "BANK_STATEMENT"


def bank_behaviour_get(case_id: str, primary_id: str) -> Observation:
    """
    The recorded signals for each bank statement on the case.

    PRESENT only when a statement's signals are there and reconciled. A
    statement that verified but carried no signals (did not reconcile) is
    LOW_CONFIDENCE; no bank statement at all is MISSING.
    """
    verifications = {
        (getattr(f, "source_id", None), getattr(f, "party_id", None)): f
        for f in current_findings(case_id, kind="VERIFICATION") if _is_bank_statement(f)
    }
    if not verifications:
        return Observation(tool="bank_behaviour.get", category="banking",
                           quality=Quality.MISSING, data={"statements": []})

    extractions = {
        (getattr(f, "source_id", None), getattr(f, "party_id", None)): f
        for f in current_findings(case_id, kind="EXTRACTION")
    }

    statements: list[dict[str, Any]] = []
    evidence = []
    for key, verification in verifications.items():
        party_id = key[1] or primary_id
        extraction = extractions.get(key)
        fields = ((getattr(extraction, "payload", None) or {}).get("fields") or {}) \
            if extraction is not None else {}
        signals = fields.get("evidence") if isinstance(fields.get("evidence"), dict) else {}
        income = fields.get("income_evidence") \
            if isinstance(fields.get("income_evidence"), dict) else {}
        row = {
            "party_id": party_id,
            "verification": verification.status,
            "signals": {k: signals[k] for k in _SIGNAL_KEYS if k in signals},
            "income_evidence": {k: income[k] for k in _INCOME_KEYS if income.get(k) is not None},
        }
        statements.append(row)
        if row["signals"]:
            for name in ("reconciled", "returned_transaction_count", "mandate_debit_count",
                         "salary_credit_count"):
                if name in row["signals"]:
                    evidence.append(evidence_ref(EvidenceSource.BANK, extraction,
                                                 f"evidence.{name}", row["signals"][name],
                                                 party_id=party_id))
        if row["income_evidence"].get("estimated_monthly_amount") is not None:
            evidence.append(evidence_ref(
                EvidenceSource.BANK, extraction, "income_evidence.estimated_monthly_amount",
                row["income_evidence"]["estimated_monthly_amount"], party_id=party_id))

    reconciled = [s for s in statements if s["signals"].get("reconciled") is True]
    quality = Quality.PRESENT if reconciled else Quality.LOW_CONFIDENCE

    # The case-level view the policy reads: the FIRST reconciled statement's
    # recorded signals, verbatim. Not a sum or an average across statements --
    # that would be a new calculation.
    primary = (reconciled or statements)[0]
    data = {
        "statements": statements,
        "reconciled": bool(reconciled),
        **{k: v for k, v in primary["signals"].items() if k != "reconciled"},
        "income_evidence": primary["income_evidence"],
    }
    return Observation(tool="bank_behaviour.get", category="banking", quality=quality,
                       data=data, evidence=evidence)


__all__ = ["bank_behaviour_get"]
