"""
ELIGIBILITY RE-ASSESSED WHEN THE CASE'S TERMS CHANGE (2026-10-13).

The decision is made from the case's STRUCTURED applicant data (the configured DataProvider, by case id) -- never from
documents. When an officer edits the loan terms (form PUT / MCP application.update), the recorded decision may
describe a loan that no longer exists, so it is re-assessed here:

  * the SAME registered agent through the SAME orchestrator call the pipeline makes (`flow._eligibility_for`), so a
    re-assessed decision is exactly what the pipeline would publish for the case now;
  * a case never assessed is NOT assessed here (nothing recorded, nothing to re-assess);
  * recorded like the pipeline records it (a FINANCIAL finding, source ELIGIBILITY -- the latest write is the current
    one) plus a timeline event, and audited. A failure never undoes the edit and is SAID (NOT_REASSESSED).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: The application / applicant fields a decision depends on (once the LOS provider reads them from the case).
TERMS = frozenset({"loan_amount", "tenure_months", "interest_rate_pct", "product", "declared_monthly_obligations",
                   "property_value", "date_of_birth"})


def affects(changed: Any) -> bool:
    """Whether any of the changed field names is one the decision is computed from."""
    return bool(set(str(c) for c in (changed or [])) & TERMS)


def _source(finding: Any) -> str:
    return str(getattr(finding, "source_type", "") or "").upper()


def _value(enum_or_text: Any) -> str:
    return str(getattr(enum_or_text, "value", enum_or_text) or "")


def _recorded(case_id: str, repository: Any) -> Any:
    """The current ELIGIBILITY finding of the case, or None."""
    from app.store.models import FindingKind

    financial = repository.get_current_findings(case_id, kind=FindingKind.FINANCIAL) or []
    return next((f for f in reversed(financial) if _source(f) == "ELIGIBILITY"), None)


def record(case_id: str, verdict: dict[str, Any]) -> None:
    """The decision recorded on the case the way the pipeline records it (FINANCIAL finding, source ELIGIBILITY)."""
    from app.store import get_repository
    from app.store.models import CaseFinding, FindingKind

    digest = hashlib.sha256(json.dumps(["ELIGIBILITY", verdict], sort_keys=True, default=str)
                            .encode("utf-8")).hexdigest()
    get_repository().save_finding(CaseFinding(
        finding_id=uuid.uuid4().hex, case_id=case_id, party_id=None, finding_kind=FindingKind.FINANCIAL,
        status=verdict.get("status"), reason_codes=list(verdict.get("reason_codes") or []), payload=verdict,
        source_type="ELIGIBILITY", content_hash=digest))


async def refresh(case_id: str, *, actor: str, request_id: str, reason: str) -> dict[str, Any] | None:
    """
    Re-assess the recorded decision. None when the case was never assessed, else
    {"status": PASS / REVIEW / FAIL / SKIPPED | NOT_REASSESSED, "decision": ..., "previous": ..., "foir_pct": ...}.
    """
    from app.agents.applicant import audit
    from app.agents.los import flow
    from app.store import get_repository, request_cache
    from app.store.models import CaseEvent

    repository = get_repository()
    try:
        old = _recorded(case_id, repository)
    except Exception:  # noqa: BLE001 - unreadable record: said, never guessed
        logger.warning("eligibility refresh: the recorded decision of %s could not be read", case_id, exc_info=True)
        return {"status": "NOT_REASSESSED", "reason": "RECORD_UNREADABLE"}
    if old is None:
        return None
    previous = str((getattr(old, "payload", None) or {}).get("decision") or _value(getattr(old, "status", None))) or None
    primary = getattr(repository.get_application(case_id), "applicant_id", None)
    try:
        request_cache.invalidate()                    # the terms just written, never this request's memo
        verdict = await flow._eligibility_for(case_id)
    except Exception as exc:  # noqa: BLE001 - the edit stands; the decision is said to be out of date
        logger.warning("eligibility refresh of %s failed: %r", case_id, exc)
        verdict = None
    if not verdict:
        audit.record(request_id=request_id, subject=actor, applicant_id=primary, case_id=case_id,
                     intent="ELIGIBILITY_REASSESS", tools=["eligibility_agent"], write=True, status="FAILED",
                     detail=f"reason={reason}")
        return {"status": "NOT_REASSESSED", "previous": previous}
    record(case_id, verdict)
    decision = str(verdict.get("decision") or "")
    foir = (verdict.get("foir") or {}).get("value_pct")
    try:
        repository.record_event(CaseEvent(
            event_id=f"evt_{uuid.uuid4().hex}", case_id=case_id, event_type="ELIGIBILITY_REASSESSED",
            summary=f"Eligibility re-assessed after {reason}: {previous or '-'} -> {decision}"
                    + (f" (FOIR {foir}%)" if foir is not None else "")))
    except Exception:  # noqa: BLE001 - the timeline describes the write; it never undoes it
        logger.warning("eligibility refresh: timeline event not written for %s", case_id)
    audit.record(request_id=request_id, subject=actor, applicant_id=primary, case_id=case_id,
                 intent="ELIGIBILITY_REASSESS", tools=["eligibility_agent"], write=True, status="OK",
                 detail=f"reason={reason} {previous}->{decision}")
    request_cache.invalidate()
    return {"status": verdict.get("status"), "decision": decision, "previous": previous, "foir_pct": foir}


#: the recorded inputs compared with the provider's current data
_COMPARED = ("product", "age", "net_monthly_income", "existing_emi", "bureau_score", "property_value",
             "requested_amount", "tenure_months", "rate_percent")


def stale_fields(recorded: dict[str, Any] | None, case_id: str) -> list[str]:
    """
    The inputs the RECORDED decision was computed from that the data source no longer holds -- read-only, nothing is
    rewritten. [] when nothing was recorded, or the source cannot be read now (not knowing is not "stale").
    """
    from app.agents.eligibility.data.providers import get_provider

    if not (recorded or {}).get("decision"):
        return []
    try:
        current = vars(get_provider().get(case_id))
    except Exception:  # noqa: BLE001 - CaseNotFound / ProviderUnavailable / not connected: unknown, not stale
        return []
    # the recorded input values are in the result's `rules` / `inputs`; the full input is not kept, so compare the
    # ones the result carries
    kept = {"requested_amount": recorded.get("requested_amount"), "product": recorded.get("product"),
            "tenure_months": (recorded.get("inputs") or {}).get("tenure_months"),
            "net_monthly_income": (recorded.get("inputs") or {}).get("monthly_income"),
            "existing_emi": (recorded.get("inputs") or {}).get("monthly_obligations")}

    def same(a: Any, b: Any) -> bool:
        if a in (None, "") and b in (None, ""):
            return True
        try:
            return abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            return str(a or "").strip().upper() == str(b or "").strip().upper()

    return [f for f in _COMPARED if f in kept and not same(kept[f], current.get(f))]


__all__ = ["TERMS", "affects", "record", "refresh", "stale_fields"]
