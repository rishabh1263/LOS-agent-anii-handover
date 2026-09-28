"""
kyc.get and income.get -- the RECORDED KYC and income-consistency results.

KYC is the KYC agent's; income consistency is the slip-vs-bank check's
(los/flow._income_consistency_for). Both are read back from the case store
exactly as ingest wrote them. Neither is re-run, re-scored or re-compared.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.agents.credit.adapters import current_findings, observed_at, record_type
from app.agents.credit.schemas import EvidenceRef, EvidenceSource, Observation, Quality


def evidence_ref(source: EvidenceSource, finding: Any, field: str | None,
                 value_summary: Any = None, *, party_id: str | None = None,
                 is_demo: bool = False) -> EvidenceRef:
    """One evidence reference to one field of one recorded finding."""
    record_id = getattr(finding, "finding_id", None) if finding is not None else None
    digest = hashlib.sha1(
        f"{source.value}|{record_id}|{field}|{party_id}".encode()).hexdigest()[:12]
    return EvidenceRef(
        ref_id=f"ev_{digest}",
        source=source,
        record_type=record_type(finding) if finding is not None else source.value,
        record_id=record_id,
        field=field,
        value_summary=None if value_summary is None else str(value_summary),
        observed_at=observed_at(finding) if finding is not None else None,
        party_id=party_id,
        is_demo=is_demo,
    )


# ---------------------------------------------------------------------------
# kyc.get
# ---------------------------------------------------------------------------

def kyc_get(case_id: str, parties: list[str], primary_id: str) -> Observation:
    """
    The recorded KYC verdict per party.

    A single-applicant case records ONE case-level KYC block (party_id None);
    it is the primary applicant's. A party with no KYC record is listed in
    `missing_parties` -- not given a verdict.
    """
    findings = current_findings(case_id, kind="KYC")
    by_party: dict[str, Any] = {}
    for finding in findings:
        owner = getattr(finding, "party_id", None) or primary_id
        by_party[owner] = finding          # current_findings: latest wins

    data: dict[str, Any] = {"parties": {}, "missing_parties": []}
    evidence: list[EvidenceRef] = []
    for party_id in parties:
        finding = by_party.get(party_id)
        if finding is None:
            data["missing_parties"].append(party_id)
            continue
        payload = getattr(finding, "payload", None) or {}
        data["parties"][party_id] = {
            "status": finding.status,
            "score": finding.score,
            "confidence": finding.confidence,
            "reason_codes": list(finding.reason_codes or []),
            "fields": [
                {k: f.get(k) for k in ("field", "status", "match_score", "reason_code")
                 if k in f}
                for f in (payload.get("fields") or []) if isinstance(f, dict)
            ],
        }
        evidence.append(evidence_ref(EvidenceSource.KYC, finding, "status",
                                     finding.status, party_id=party_id))

    if not data["parties"]:
        quality = Quality.MISSING
    elif data["missing_parties"]:
        quality = Quality.LOW_CONFIDENCE
    else:
        quality = Quality.PRESENT
    return Observation(tool="kyc.get", category="kyc", quality=quality,
                       data=data, evidence=evidence)


# ---------------------------------------------------------------------------
# income.get
# ---------------------------------------------------------------------------

_BANK_KEYS = ("type", "estimated_monthly_amount", "months_observed",
              "recurring_credit_count", "confidence")
_SLIP_KEYS = ("figure", "amount", "pay_period")


def income_get(case_id: str) -> Observation:
    """
    The recorded slip-vs-bank income-consistency result.

    `documented_monthly_income` is the salary slip's RECORDED amount, as the
    consistency check read it -- the figure a declared income is compared
    with. Absent when the check recorded no slip figure; never estimated.
    """
    findings = [f for f in current_findings(case_id, kind="FINANCIAL")
                if getattr(f, "source_type", None) == "INCOME_CONSISTENCY"]
    if not findings:
        return Observation(tool="income.get", category="income",
                           quality=Quality.MISSING, data={})

    finding = findings[-1]
    payload = getattr(finding, "payload", None) or {}
    bank = payload.get("bank_statement") if isinstance(payload.get("bank_statement"), dict) else {}
    slip = payload.get("salary_slip") if isinstance(payload.get("salary_slip"), dict) else {}

    data = {
        "consistency_status": finding.status or payload.get("status"),
        "reason_codes": list(finding.reason_codes or payload.get("reason_codes") or []),
        "bank_statement": {k: bank.get(k) for k in _BANK_KEYS if bank.get(k) is not None},
        "salary_slip": {k: slip.get(k) for k in _SLIP_KEYS if slip.get(k) is not None},
        "documented_monthly_income": slip.get("amount"),
        "source": "SALARY_SLIP" if slip.get("amount") is not None else None,
    }
    evidence = [evidence_ref(EvidenceSource.INCOME, finding, "status",
                             data["consistency_status"])]
    if slip.get("amount") is not None:
        evidence.append(evidence_ref(EvidenceSource.INCOME, finding,
                                     "salary_slip.amount", slip.get("amount")))
    if bank.get("estimated_monthly_amount") is not None:
        evidence.append(evidence_ref(EvidenceSource.INCOME, finding,
                                     "bank_statement.estimated_monthly_amount",
                                     bank.get("estimated_monthly_amount")))

    quality = Quality.PRESENT if data["consistency_status"] else Quality.LOW_CONFIDENCE
    return Observation(tool="income.get", category="income", quality=quality,
                       data=data, evidence=evidence)


__all__ = ["evidence_ref", "income_get", "kyc_get"]
