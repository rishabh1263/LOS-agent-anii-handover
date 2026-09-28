"""
financial_documents.get -- the salary-slip and ITR figures the pipeline RECORDED.

The Financial Agent (app/agents/financial) normalises each financial document
into IncomeSignals -- `monthly_net_salary` / `monthly_gross_salary` off a
salary slip, `declared_annual_income` (the return's total income) off an ITR
-- and the LOS flow releases them in the document's EXTRACTION only when its
verification gate allowed it. Ingest stores both the VERIFICATION verdict and
that released EXTRACTION.

This reads them back, per document: its recorded verdict and, where released,
its recorded figures. It never re-reads a document, re-extracts a field, or
turns an annual figure into a monthly one.

A document whose recorded verdict is not PASS is listed with its verdict and
contributes no figure the income step may rely on.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.adapters import current_findings
from app.agents.credit.adapters.case_memory import evidence_ref
from app.agents.credit.schemas import EvidenceSource, Observation, Quality

TYPES = ("SALARY_SLIP", "ITR")
_SIGNAL_KEYS = {
    "SALARY_SLIP": ("monthly_net_salary", "monthly_gross_salary"),
    "ITR": ("declared_annual_income",),
}


def _type(finding: Any) -> str:
    return str((getattr(finding, "payload", None) or {}).get("type") or "").upper()


def financial_documents_get(case_id: str, primary_id: str) -> Observation:
    verifications = [f for f in current_findings(case_id, kind="VERIFICATION")
                     if _type(f) in TYPES]
    if not verifications:
        return Observation(tool="financial_documents.get", category="income",
                           quality=Quality.MISSING, data={"documents": []})
    extractions = {(getattr(f, "source_id", None), getattr(f, "party_id", None)): f
                   for f in current_findings(case_id, kind="EXTRACTION")}

    documents, evidence = [], []
    for verification in verifications:
        doc_type = _type(verification)
        party_id = verification.party_id or primary_id
        verdict = str(verification.status or "").upper() or None
        row: dict[str, Any] = {"document_type": doc_type, "party_id": party_id,
                               "verification": verdict, "figures": {}}
        evidence.append(evidence_ref(EvidenceSource.VERIFICATION, verification,
                                     f"{doc_type}.verification", verdict or "NOT_RECORDED",
                                     party_id=party_id))
        extraction = extractions.get((verification.source_id, verification.party_id))
        fields = ((getattr(extraction, "payload", None) or {}).get("fields") or {}) \
            if extraction is not None else {}
        signals = fields.get("signals") if isinstance(fields.get("signals"), dict) else {}
        if verdict == "PASS":
            for key in _SIGNAL_KEYS[doc_type]:
                if signals.get(key) is not None:
                    row["figures"][key] = signals[key]
                    evidence.append(evidence_ref(EvidenceSource.INCOME, extraction,
                                                 f"signals.{key}", signals[key],
                                                 party_id=party_id))
        documents.append(row)

    usable = [d for d in documents if d["figures"]]
    quality = Quality.PRESENT if usable else Quality.LOW_CONFIDENCE
    return Observation(
        tool="financial_documents.get", category="income", quality=quality,
        data={"documents": documents,
              "verified_salary_slip": next((d["figures"] for d in usable
                                            if d["document_type"] == "SALARY_SLIP"), None),
              "verified_itr": next((d["figures"] for d in usable
                                    if d["document_type"] == "ITR"), None)},
        evidence=evidence)


__all__ = ["TYPES", "financial_documents_get"]
