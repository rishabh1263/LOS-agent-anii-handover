"""
LOAN HISTORY -- MIFIN (who the customer is, which prospects / loans) + the LMS (each loan's state) -> one verified,
source-backed history (Phase B, 2026-10-22).

NOTHING IS INFERRED. A loan's state is the LMS's when the LMS has the loan, else MIFIN's (mapped only where mifin.yaml
maps it, else UNKNOWN). Totals are computed only from LMS figures on LMS-ACTIVE loans; any gap makes the total PARTIAL
with the known part and the loans that are missing -- never a silent sum, never "0" for "unknown".

history_status:
  UNAVAILABLE        MIFIN could not answer (not configured, error, malformed) -- history UNKNOWN, not empty
  NOT_APPLICABLE     MIFIN found no customer (a new-customer candidate)
  NOT_ATTRIBUTED     potential / ambiguous match: loans are not linked to this applicant (counted only)
  UNKNOWN            confirmed customer, but MIFIN did not send its loan section
  NO_PREVIOUS_LOANS  confirmed customer, MIFIN's loan list is present and empty
  COMPLETE           every loan resolved by the LMS
  PARTIAL            some loans resolved by the LMS
  MIFIN_ONLY         the LMS could not answer: MIFIN's own facts only
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from app.agents.dedupe import identity as I
from app.agents.dedupe import lms as L
from app.agents.dedupe import mifin as M


def _num(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _total(loans: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """Sum of an LMS field over LMS-ACTIVE loans: COMPLETE, PARTIAL (known part + missing loans) or NOT_APPLICABLE."""
    active = [x for x in loans if x["effective_status"] == "ACTIVE" and not x["conflicting_duplicate"]]
    conflicting = [x["prospect_ref"] for x in loans if x["conflicting_duplicate"]]
    # a loan whose state nobody confirmed MAY be live: the total cannot be complete while one exists
    unknown = [x["prospect_ref"] for x in loans if x["effective_status"] == "UNKNOWN" and not x["conflicting_duplicate"]]
    # DISBURSED per MIFIN, not confirmed by the LMS (the LMS would have said ACTIVE / CLOSED): may still be live
    unconfirmed = [x["prospect_ref"] for x in loans if x["effective_status"] == "DISBURSED" and not x["conflicting_duplicate"]]
    if not active:
        if conflicting:
            return {"status": "NOT_EVALUATED", "value": None, "reason": "CONFLICTING_DUPLICATE_RECORDS",
                    "loans_conflicting": conflicting, "loans_status_unknown": unknown, "loans_not_confirmed": unconfirmed}
        if unknown:
            return {"status": "NOT_EVALUATED", "value": None, "reason": "LOAN_STATUS_UNKNOWN",
                    "loans_status_unknown": unknown, "loans_not_confirmed": unconfirmed}
        if unconfirmed:
            return {"status": "NOT_EVALUATED", "value": None, "reason": "LIVE_STATE_NOT_CONFIRMED_BY_LMS",
                    "loans_not_confirmed": unconfirmed}
        return {"status": "NOT_APPLICABLE", "value": None, "reason": "NO_ACTIVE_LOAN_CONFIRMED_BY_LMS"}
    known = [x for x in active if x["_lms"].get(field) is not None]
    missing = [x["prospect_ref"] for x in active if x["_lms"].get(field) is None]
    value = sum((x["_lms"][field] for x in known), Decimal(0))
    return {"status": "PARTIAL" if missing or unknown or conflicting or unconfirmed else "COMPLETE",
            "value": _num(value) if known else None, "loans_counted": len(known), "loans_missing": missing,
            "loans_status_unknown": unknown, "loans_conflicting": conflicting, "loans_not_confirmed": unconfirmed,
            "currency": "INR"}


def _topup(loan: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether a RUNNING loan meets the top-up conditions (customer_history_policy.yaml topup). Every value must be
    known: a missing one is NOT_EVALUATED, never eligible. A loan that is not running is NOT_APPLICABLE."""
    from datetime import date

    if loan["effective_status"] not in ("ACTIVE", "DISBURSED"):
        return {"status": "NOT_APPLICABLE", "reasons": []}
    if loan.get("conflicting_duplicate"):
        return {"status": "NOT_EVALUATED", "reasons": ["CONFLICTING_RECORDS"]}
    lms = loan.get("_lms") or {}
    sanctioned = (loan.get("amounts") or {}).get("sanctioned_amount")
    need = {"disbursed_on": lms.get("disbursed_on"), "dpd_max_12m": lms.get("dpd_max_12m"),
            "principal_outstanding": lms.get("principal_outstanding"), "sanctioned_amount": sanctioned}
    missing = [k for k, v in need.items() if v is None]
    if missing or not cfg:
        return {"status": "NOT_EVALUATED", "reasons": [f"{k.upper()}_MISSING" for k in missing] or ["NOT_CONFIGURED"]}
    try:
        start = date.fromisoformat(str(need["disbursed_on"])[:10])
        months = (date.today().year - start.year) * 12 + date.today().month - start.month - (date.today().day < start.day)
        pct = float(need["principal_outstanding"]) * 100 / float(sanctioned) if float(sanctioned) > 0 else None
    except (ValueError, TypeError):
        return {"status": "NOT_EVALUATED", "reasons": ["VALUE_NOT_READABLE"]}
    reasons = []
    if months < int(cfg.get("min_months_since_disbursal", 0)):
        reasons.append("TOO_SOON_AFTER_DISBURSAL")
    if int(need["dpd_max_12m"]) > int(cfg.get("max_dpd_12m", 10 ** 6)):
        reasons.append("DPD_ABOVE_LIMIT")
    if pct is None or pct > float(cfg.get("max_outstanding_pct", 100)):
        reasons.append("OUTSTANDING_ABOVE_LIMIT")
    return {"status": "NOT_ELIGIBLE" if reasons else "ELIGIBLE", "reasons": reasons, "months_since_disbursal": months,
            "outstanding_pct": round(pct, 1) if pct is not None else None}


def history(parameter: str, value: str, *, mifin_source: M.MifinClient | None = None,
            lms_source: L.LmsClient | None = None, decision_for=None,
            applicant: dict[str, Any] | None = None, auto_confirm: bool = False) -> dict[str, Any]:
    """decision_for(candidate) -> SAME_CUSTOMER | NOT_SAME_CUSTOMER | None: an officer's decision on a POSSIBLE match
    (match_decision.py). SAME -> treated as confirmed; NOT_SAME -> treated as no match; none -> loans withheld."""
    from app.agents.dedupe import match_decision as MD

    started = time.perf_counter()
    found, raw = M.lookup(parameter, value, source=mifin_source)
    cand = MD.candidate(found.customer_id) if found.match == M.POTENTIAL else None
    decision = decision_for(cand) if (cand and decision_for) else None
    # the officer's help on a possible match: name / DOB against MIFIN's record (compared in memory, never returned)
    ident = M.compare_identity(raw, name=(applicant or {}).get("name"), dob=(applicant or {}).get("dob"))         if found.match in (M.POTENTIAL, M.CONFIRMED) else {"name": "NOT_COMPARED", "dob": "NOT_COMPARED"}
    searched_same = (M.same_identifier(raw, parameter.upper(), value)
                     if raw and parameter.upper() in ("PAN", "MOBILE") else None)
    # PAN, for every case (owner 2026-10-23): the searched PAN, else the applicant's PAN on record, against MIFIN's
    # (keyed HMACs in memory -- neither PAN is returned)
    our_pan = value if parameter.upper() == "PAN" else (applicant or {}).get("pan")
    pan_same = (M.same_identifier(raw, "PAN", our_pan)
                if raw and our_pan and found.match in (M.POTENTIAL, M.CONFIRMED) else None)
    ident = {**ident, "pan": "MATCH" if pan_same else "MISMATCH" if pan_same is False else "NOT_COMPARED"}
    auto = False
    if (cand and decision is None and auto_confirm and searched_same is True
            and ident["name"] == "MATCH" and ident["dob"] == "MATCH"):
        decision, auto = "SAME_CUSTOMER", True      # dedupe_policy.yaml officer_confirmation.auto_confirm (OFF)
    if decision == "SAME_CUSTOMER":
        found.match = M.CONFIRMED
    elif decision == "NOT_SAME_CUSTOMER":
        found.match = M.NO_MATCH
    pub = found.public()
    customer = {
        # the AP code in full only for a CONFIRMED customer: a possible match may be someone else
        "match": found.match, "customer_ref": pub["customer_ref"],
        # the AP number in full whenever MIFIN names a customer, a possible match too (owner 2026-10-23); its LOANS
        # stay withheld until the match is confirmed
        "customer_code": pub.get("customer_code"),
        "match_basis": pub["match_basis"],
        "searched_identifier_matches": searched_same,
        "identity_check": {"searched_identifier": ("MATCH" if searched_same else "MISMATCH" if searched_same is False
                                                   else "NOT_COMPARED"), **ident},
        # both sides for the matching table, for a customer MIFIN names (mifin.yaml privacy.show_identity_values)
        "identity_values": (M.identity_values(raw, name=(applicant or {}).get("name"), dob=(applicant or {}).get("dob"),
                                              pan=(applicant or {}).get("pan"), searched=parameter, searched_value=value)
                            if found.match in (M.POTENTIAL, M.CONFIRMED) else None),
        "identity_fields_present": pub["identity_fields_present"], "warnings": found.warnings,
        "role_in_source": pub.get("customer_type"),
        # a possible match needs an officer's decision; once made it is shown with who made it (the case timeline)
        "confirmation": {"required": bool(cand) and decision is None, "candidate": cand, "decision": decision,
                         "decided_by": ("AUTO_RULE" if auto else "OFFICER") if decision else None},
    }
    sources = [{"source": "MIFIN", "status": "AVAILABLE" if found.match not in (M.UNAVAILABLE, M.SOURCE_ERROR,
                                                                                  M.MALFORMED) else found.match,
                "is_demo": found.is_demo, "contract_version": pub["contract_version"]}]
    base = {"customer": customer, "is_demo": found.is_demo, "sources": sources, "loans": [], "loan_count": 0,
            "fields_not_in_mifin": pub["fields_not_in_source"]}

    def done(status: str, **extra) -> dict[str, Any]:
        out = {**base, "history_status": status, **extra}
        out["timings_ms"] = {"total_ms": round((time.perf_counter() - started) * 1000, 1)}
        return out

    if found.match in (M.UNAVAILABLE, M.SOURCE_ERROR, M.MALFORMED):
        return done("UNAVAILABLE", aggregates=None)
    if found.match == M.NO_MATCH:
        return done("NOT_APPLICABLE", aggregates=None)
    if found.match in (M.POTENTIAL, M.AMBIGUOUS):
        return done("NOT_ATTRIBUTED", aggregates=None, loans_withheld=len(found.loans))
    # the confirmed customer's RCU cases and pledged properties (R09 / R10): counts only
    from app.agents.dedupe import extra_sources as X
    from app.agents.dedupe import history_policy as HP

    _hp = HP.policy()
    base["rcu"] = X.rcu(found.customer_id, _hp.get("rcu_adverse_statuses") or [])
    base["collateral"] = X.collateral(found.customer_id)
    for _name, _key in (("RCU", "rcu"), ("COLLATERAL", "collateral")):
        sources.append({"source": _name, "status": base[_key]["status"], "is_demo": base[_key]["status"] == "AVAILABLE"})
    if found.loan_section != "PRESENT":
        return done("UNKNOWN", aggregates=None)
    if not found.loans:
        return done("NO_PREVIOUS_LOANS", aggregates={"active_loans": 0})

    lms_client = lms_source or L.client()
    lms_demo = bool(getattr(lms_client, "is_demo", False))
    lms_state, loans = "AVAILABLE", []
    searched_account = I.normalise("PR_CODE", value) if parameter.upper() == "PR_CODE" else None
    for loan in found.loans:
        row = loan.public(pub["loans"][0]["currency"] if pub["loans"] else "INR")
        # the loan whose account number WAS the searched value (the rest belong to the same customer)
        row["searched_loan_account"] = bool(searched_account
                                            and I.normalise("PR_CODE", loan.prospect_code) == searched_account)
        detail, lms_status = None, "UNAVAILABLE"
        if lms_state != "UNAVAILABLE":
            try:
                answer = lms_client.loan(loan.prospect_code)
            except L.LmsUnavailable:
                lms_state = "UNAVAILABLE"                 # one outage: stop asking, every loan is MIFIN-only
            except Exception:  # noqa: BLE001 - a client bug is an unavailable source
                lms_state = "UNAVAILABLE"
            else:
                if answer is None:
                    lms_status = "NOT_FOUND"
                else:
                    detail = L.parse(loan.prospect_code, answer, is_demo=lms_demo)
                    # a reply with nothing readable (malformed, another loan's) confirms nothing: UNREADABLE
                    lms_status = "FOUND" if detail.values else "UNREADABLE"
        effective = detail.status if detail and detail.status != "UNKNOWN" else loan.status_class
        row.update({
            "lms_status": lms_status,
            "lms": ({k: (_num(v) if isinstance(v, Decimal) else v) for k, v in detail.values.items()}
                    if detail else None),
            "lms_unavailable_fields": detail.unavailable if detail else list(L.FIELDS),
            "lms_problems": detail.problems if detail else [],
            "effective_status": effective,
            "status_source": "LMS" if detail and detail.status != "UNKNOWN" else "MIFIN",
            "_lms": detail.values if detail else {},
            # every previous case, checked: all PRs MIFIN lists sit under ONE customer id (the confirmed customer),
            # so the applicant is common to each; the PAN result is the customer record's, stated on every case
            "applicant_check": {"common_applicant": True, "customer_code": customer["customer_code"],
                                "role_in_source": customer["role_in_source"], "pan": ident["pan"],
                                "name": ident["name"], "dob": ident["dob"]},
        })
        loans.append(row)

    base["common_applicant"] = {
        "cases": len(loans), "same_customer_on_all": True, "basis": "ALL_CASES_UNDER_ONE_MIFIN_CUSTOMER_ID",
        "customer_code": customer["customer_code"], "pan": ident["pan"],
        "cancelled_cases": sum(r["effective_status"] == "CANCELED" for r in loans),
        "cancelled_with_reason": sum(r["effective_status"] == "CANCELED" and bool(r["cancel_reason"]) for r in loans)}
    final = {str(s).upper() for s in M.cfg()["loans"].get("final_without_lms") or []}
    for r in loans:                                   # a cancelled application: final from MIFIN, nothing in the LMS
        r["resolved"] = r["lms_status"] == "FOUND" or (r["status_class"] in final and r["effective_status"] in final)
    resolved = sum(r["resolved"] for r in loans)
    status = ("COMPLETE" if resolved == len(loans)
              else "MIFIN_ONLY" if lms_state == "UNAVAILABLE" or resolved == 0 else "PARTIAL")
    by_status: dict[str, int] = {}
    for r in loans:
        by_status[r["effective_status"]] = by_status.get(r["effective_status"], 0) + 1
    dpd = [r["_lms"]["dpd_max_12m"] for r in loans if r["_lms"].get("dpd_max_12m") is not None]
    aggregates = {
        "by_status": by_status,
        "active_loans": by_status.get("ACTIVE", 0),
        "unknown_status_loans": by_status.get("UNKNOWN", 0),
        "conflicting_duplicates": sum(r["conflicting_duplicate"] for r in loans),
        "active_exposure": _total(loans, "principal_outstanding"),
        "monthly_emi_burden": _total(loans, "emi"),
        "worst_dpd_12m": {"value": max(dpd) if dpd else None, "loans_reporting": len(dpd),
                          "status": "NOT_APPLICABLE" if status == "COMPLETE" and not any(
                              r["lms_status"] == "FOUND" for r in loans)            # nothing was ever disbursed
                          else "COMPLETE" if status == "COMPLETE" and len(dpd) == sum(
                              r["lms_status"] == "FOUND" for r in loans) else ("PARTIAL" if dpd else "UNAVAILABLE")},
        "adverse_events": {"settled": sum(bool(r["_lms"].get("settled")) for r in loans),
                           "written_off": sum(bool(r["_lms"].get("written_off")) for r in loans),
                           "status": "COMPLETE" if status == "COMPLETE" else "PARTIAL"},
    }
    topup_cfg = HP.policy().get("topup") or {}
    for r in loans:
        r["topup"] = _topup(r, topup_cfg)
    for r in loans:
        r.pop("_lms")
    sources.append({"source": "LMS", "status": lms_state if lms_state == "UNAVAILABLE"
                    else ("AVAILABLE" if resolved == len(loans) else "PARTIAL"), "is_demo": lms_demo,
                    "provider": getattr(lms_client, "name", None),         # LEVIOSA / DEMO / None (not configured)
                    "loans_resolved": resolved, "loans_total": len(loans)})
    base["is_demo"] = found.is_demo or lms_demo
    return done(status, loans=loans, loan_count=len(loans), aggregates=aggregates)


__all__ = ["history"]
