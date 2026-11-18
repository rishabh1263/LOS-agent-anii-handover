"""
repayment.get -- grading the credit-stage MCP capability's answer (app/mcp/credit.py): the repayment report built from
the vendor's verified bank analysis and the eligibility case data. Read as it stands, never re-judged: its verdict is
a signal the underwriting policy's rules read.

`recorded: false` (no bank analysis received) -> MISSING; a failed envelope is graded by tools._grade_mcp.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.schemas import EvidenceRef, EvidenceSource, Observation, Quality

RECORD_TYPE = "BANK_ANALYSIS"          # provenance checks the case holds this analysis (provenance.py)


def _ref(case_id: str, field: str, value: Any, is_demo: bool) -> EvidenceRef:
    import hashlib

    digest = hashlib.sha1(f"{RECORD_TYPE}|{case_id}|{field}".encode()).hexdigest()[:12]
    return EvidenceRef(ref_id=f"ev_{digest}", source=EvidenceSource.BANK, record_type=RECORD_TYPE,
                       record_id=case_id, field=field,
                       value_summary=None if value is None else str(value), is_demo=is_demo)


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """What the credit assessment carries of the report (the full answer is POST /credit/assess)."""
    loans = next((v for k, v in (report.get("sections") or {}).items() if isinstance(v, dict) and "loans" in v), {})
    return {
        "verdict": report.get("verdict"), "can_repay": report.get("can_repay"), "headline": report.get("headline"),
        "reasons": list(report.get("reasons") or [])[:8],
        "rules": [{k: r.get(k) for k in ("rule", "result", "value", "limit")} for r in report.get("rules") or []],
        "suggestion": report.get("suggestion"),
        "obligations": {k: loans.get(k) for k in ("monthly_emi_in_bank", "declared_emi", "undeclared_emi",
                                                  "new_loans", "missed_emis", "found_only_by_us")},
        "policy": report.get("policy"), "demo_bank_data": bool((report.get("bank_data") or {}).get("demo")),
    }


def grade(result: dict[str, Any], case_id: str) -> Observation:
    """The MCP result {recorded, report} -> a graded observation with its evidence."""
    report = result.get("report") if result.get("recorded") else None
    if not report:
        return Observation(tool="repayment.get", category="repayment", quality=Quality.MISSING,
                           error=result.get("reason") or "NO_BANK_ANALYSIS", data={"recorded": False})
    data = summary(report)
    is_demo = data["demo_bank_data"] or not (data.get("policy") or {}).get("signed_off")
    evidence = [_ref(case_id, "verdict", data["verdict"], is_demo)]
    evidence += [_ref(case_id, f"rule.{r['rule']}", r["result"], is_demo) for r in data["rules"]]
    return Observation(tool="repayment.get", category="repayment", quality=Quality.PRESENT, data=data,
                       evidence=evidence, is_demo=is_demo)


__all__ = ["RECORD_TYPE", "grade", "summary"]
