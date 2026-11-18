"""
DEDUPE ON ALL PARAMETERS AT ONCE (sir's review, 2026-10-24).

A check on one identifier can look clean while another tells a different story: the PAN finds nobody, but the
mobile belongs to an existing customer, or the loan account entered is someone else's. This runs the ordinary
dedupe check (customer_history.assess) for EVERY parameter that has a value -- PAN, mobile, customer ID (AP code),
loan account (PR code) -- and puts the answers side by side:

  rows         one per parameter: where its value came from (ENTERED / CASE_RECORD), MIFIN's match, the customer
               it names, the dedupe status, the rule findings, the recommendation -- plus that parameter's full
               answer, so the page can open the same detailed view as a single check
  missing      the parameters that had no value (none entered, none on the case record): NOT checked, said so
  consistency  do the parameters that found a MIFIN customer all name the SAME customer? Two different customers
               behind one application is a finding of its own (dedupe_policy.yaml decision.all_parameters)
  combined     ONE recommendation: a conflict holds the case; otherwise the most serious of the rows
               (decision.all_parameters.precedence). Never an approval.

Values are never echoed: PAN / mobile masked (mifin.yaml privacy.value_mask), AP / PR codes as the single check
shows them (mifin.yaml privacy.show_full_codes). Each per-parameter check is audited as usual.
"""
from __future__ import annotations

from typing import Any

#: the request field -> the label customer_history.assess takes (dedupe_policy.yaml parameters.*.labels)
LABELS = {"PAN": "PAN", "MOBILE": "Mobile No", "AP_CODE": "AP Code", "PR_CODE": "PR Code"}
NAMED = ("CONFIRMED_MATCH", "POTENTIAL_MATCH")


class NothingToCheck(ValueError):
    """No parameter had a value: nothing entered and nothing on the case record."""


def _spec() -> dict[str, Any]:
    from app.agents.dedupe import config

    return ((config.get_policy().get("decision") or {}).get("all_parameters") or {})


def _shown(parameter: str, value: str) -> str:
    from app.agents.dedupe import mifin

    if parameter in ("PAN", "MOBILE"):
        return mifin._masked(parameter, value) or ""
    return value if mifin.full_codes() else (mifin.mask_ref(value) or "")


def _case_values(repository, case_id: str) -> dict[str, str | None]:
    """The applicant's own PAN / mobile on the case record (declared or read from documents) -- in memory only."""
    from app.agents.dedupe import party_eval

    application = repository.get_application(case_id)
    if application is None:
        return {}
    try:
        primary = party_eval.identifiers(repository, case_id, application.applicant_id)[0]
    except Exception:  # noqa: BLE001 - an unreadable record: those parameters are simply missing
        return {}
    return {"PAN": primary.get("pan"), "MOBILE": primary.get("mobile")}


def assess_all(case_id: str, values: dict[str, str | None], *, party_role: str | None = None,
               request_id: str, repository=None, **assess_kwargs: Any) -> dict[str, Any]:
    from app.agents.dedupe import customer_history

    if repository is None:
        from app.store import get_repository

        repository = get_repository()
    spec = _spec()
    order = [str(p).upper() for p in spec.get("order") or LABELS]
    entered = {p: (str(values.get(p) or "").strip() or None) for p in LABELS}
    on_case = _case_values(repository, case_id) if str(party_role or "APPLICANT").upper().startswith("APPLICANT") else {}
    if repository.get_application(case_id) is None:
        raise customer_history.CaseNotFound(case_id)

    rows, missing = [], []
    for p in order:
        value, source = entered.get(p), "ENTERED"
        if not value and on_case.get(p):
            value, source = on_case[p], "CASE_RECORD"
        if not value:
            missing.append({"parameter": p, "reason": "NO_VALUE"})
            continue
        result = customer_history.assess(case_id, LABELS[p], value, party_role=party_role,
                                         request_id=f"{request_id}_{p.lower()}", repository=repository,
                                         **assess_kwargs)
        customer = (result.get("history") or {}).get("customer") or {}
        dedupe = result.get("dedupe") or {}
        rows.append({
            "parameter": p, "value_shown": _shown(p, value), "source": source,
            "match": customer.get("match"), "customer_code": customer.get("customer_code"),
            "customer_ref": customer.get("customer_ref"),
            "dedupe_status": dedupe.get("status"),
            "recommendation": (dedupe.get("workflow_recommendation") or {}).get("recommendation"),
            "rule_findings": dedupe.get("rule_findings") or [],
            "outcome": result.get("outcome"), "request_id": result.get("request_id"),
            "result": result,
        })
    if not rows:
        raise NothingToCheck("no PAN, mobile, customer ID or loan account to check: enter at least one")

    # THE CROSS-CHECK: every parameter that found a MIFIN customer must find the same one
    named = [(r["parameter"], r["customer_code"] or r["customer_ref"]) for r in rows
             if r["match"] in NAMED and (r["customer_code"] or r["customer_ref"])]
    customers = sorted({c for _, c in named})
    conflict = len(customers) > 1
    consistency = {"status": "CONFLICT" if conflict else "SAME_CUSTOMER" if customers else "NO_CUSTOMER_FOUND",
                   "customers": customers, "by_parameter": [{"parameter": p, "customer": c} for p, c in named]}

    precedence = [str(x) for x in spec.get("precedence") or []]
    rank = {r: i for i, r in enumerate(precedence)}
    worst = min((r["recommendation"] for r in rows if r["recommendation"]),
                key=lambda x: rank.get(x, len(rank)), default=None)
    reasons = sorted({f for r in rows for f in r["rule_findings"]})
    if conflict:
        reasons = [str(spec.get("conflict_reason_code") or "PARAMETERS_POINT_TO_DIFFERENT_CUSTOMERS"), *reasons]
        recommendation = str(spec.get("conflict_recommendation") or "HOLD_FOR_REVIEW")
        if worst and rank.get(worst, len(rank)) < rank.get(recommendation, len(rank)):
            recommendation = worst                      # a more serious row still wins
    else:
        recommendation = worst
    return {
        "request_id": request_id, "case_id": case_id, "mode": "ALL_PARAMETERS",
        "checked": [r["parameter"] for r in rows], "missing": missing, "rows": rows,
        "consistency": consistency,
        "combined": {"recommendation": recommendation, "reason_codes": reasons,
                     "reason": str(spec.get("conflict_reason") or "") if conflict else None},
    }


__all__ = ["LABELS", "NothingToCheck", "assess_all"]
