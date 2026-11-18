"""ONE DEDUPE STATUS for the answer of POST /api/v1/credit/dedupe (2026-10-22): an ADDITIVE block, derived only from
what the assessment already established -- no new search, no new source, nothing a caller can set.

  DUPLICATE_FOUND      approved rules establish a duplicate application. Needs a SIGNED-OFF dedupe policy: while the
                       policy is unsigned, the same evidence is POSSIBLE_DUPLICATE (an unapproved rule never decides).
  POSSIBLE_DUPLICATE   evidence needs review: another same-product application of this customer, a possible / ambiguous
                       MIFIN match not yet decided, an identifier conflict, document reuse, a role swap, other LOS cases
                       holding the identifier.
  NO_DUPLICATE_FOUND   every required search ran and none of the above. An EXISTING CUSTOMER WITH LOANS IS NOT A
                       DUPLICATE: customer_match / existing_loan_match say so and the action is the configured one.
  CHECK_INCOMPLETE     a required search did not run (MIFIN unavailable / errored / malformed, or the LOS search or
                       case assessment failed). Never NO_DUPLICATE_FOUND on a missing source.

The recommended action per status comes from dedupe_policy.yaml decision.actions; the LOS workflow stays the authority.
"""
from __future__ import annotations

from typing import Any

STATUSES = ("DUPLICATE_FOUND", "POSSIBLE_DUPLICATE", "NO_DUPLICATE_FOUND", "CHECK_INCOMPLETE")
_UNAVAILABLE = ("UNAVAILABLE", "SOURCE_ERROR", "MALFORMED")
# LOS dedupe scenarios that are review evidence (service._scenarios)
_REVIEW_SCENARIOS = {"S09_ROLE_CHANGE": "ROLE_CHANGE_ON_ANOTHER_CASE", "S11_IDENTITY_CONFLICT": "PAN_ON_ANOTHER_CUSTOMER_RECORD",
                     "S12_DOCUMENT_REUSE": "DOCUMENT_REUSED_ACROSS_CUSTOMERS"}


def _actions(policy: dict[str, Any]) -> dict[str, str]:
    default = {"DUPLICATE_FOUND": "MANUAL_REVIEW", "POSSIBLE_DUPLICATE": "MANUAL_REVIEW",
               "NO_DUPLICATE_FOUND": "CONTINUE", "CHECK_INCOMPLETE": "MANUAL_REVIEW"}
    return {**default, **((policy.get("decision") or {}).get("actions") or {})}


CUSTOMER_LOOKUP = ("NEW", "EXISTING", "POSSIBLE_EXISTING", "CONFLICTING", "LOOKUP_FAILED")


def trigger(result: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """THE TRIGGER (business requirement, 2026-10-22): the customer lookup for the checked person decides whether the
    dedupe agent runs. Lookup = MIFIN (the customer master) + the LOS's own records for the searched identifier.

      NEW               MIFIN answered "no record" AND the LOS search ran without a match (or the identifier is one
                        the LOS does not store) -> the agent is NOT invoked (dedupe_policy.yaml trigger.invoke_for_new)
      EXISTING          MIFIN confirmed the customer, or the LOS holds the identifier on another case -> invoked
      POSSIBLE_EXISTING MIFIN's possible match (not yet decided by an officer) -> invoked
      CONFLICTING       several MIFIN customers, or MIFIN's record holds a different identifier -> invoked
      LOOKUP_FAILED     MIFIN unavailable / errored / malformed, or the LOS search failed -> invoked; NEVER "NEW"
    """
    customer = result["history"]["customer"]
    search = (result.get("los_dedupe") or {}).get("search") or {}
    match = customer.get("match")
    los_hit = search.get("status") in ("MATCH", "POSSIBLE_MATCH") or bool(search.get("matches"))
    if match in _UNAVAILABLE or search.get("status") == "FAILED" or not search:
        lookup = "LOOKUP_FAILED"
    elif match == "AMBIGUOUS" or customer.get("searched_identifier_matches") is False:
        lookup = "CONFLICTING"
    elif match == "POTENTIAL_MATCH":
        lookup = "POSSIBLE_EXISTING"
    elif match == "CONFIRMED_MATCH" or los_hit:
        lookup = "EXISTING"
    elif match == "NO_MATCH" and search.get("status") in ("NO_MATCH", "INSUFFICIENT_DATA"):
        lookup = "NEW"
    else:                                     # anything not provably new is never treated as new
        lookup = "LOOKUP_FAILED"
    cfg = (policy or {}).get("trigger") or {}
    invoked = lookup != "NEW" or bool(cfg.get("invoke_for_new", False))
    return {"customer_lookup": lookup, "dedupe_invoked": invoked,
            "reason": ((cfg.get("reasons") or {}).get(lookup))}


def not_invoked(result: dict[str, Any], policy: dict[str, Any], trig: dict[str, Any]) -> dict[str, Any]:
    """The answer for a confirmed NEW customer: the agent did not run; the application continues through the normal
    LOS checks. No dedupe status is claimed (there was no dedupe), the lookup that proved NEW is shown."""
    cfg = (policy or {}).get("trigger") or {}
    rec = cfg.get("new_customer_recommendation")
    value = rec if rec in RECOMMENDATIONS else "UNABLE_TO_DECIDE"
    texts = ((policy or {}).get("decision") or {}).get("texts") or {}
    return {"invoked": False, "trigger": trig, "status": None, "search_complete": True,
            "customer_match": False, "existing_loan_match": False, "application_match": False,
            "match_reasons": ["NEW_CUSTOMER_NO_RECORD"], "matched_records": [], "searches_missing": [],
            "parties": [], "parties_incomplete": [],
            "workflow_recommendation": {"recommendation": value, "reason": trig.get("reason"),
                                        "next_action": (texts.get("next_actions") or {}).get(value),
                                        "policy_question": None if rec in RECOMMENDATIONS else
                                        "What should happen to a new customer? (dedupe_policy.yaml trigger)"},
            "assessment": {"existing_customer_or_loan": "NOT_FOUND", "duplicate_application": "NOT_EVALUATED",
                           "conflicts": [], "search_complete": True, "credit_decision": "NOT_DECIDED_BY_DEDUPE"},
            "policy": {"policy_id": policy.get("policy_id"), "policy_version": policy.get("policy_version"),
                       "signed_off": bool(policy.get("signed_off"))}}


def derive(result: dict[str, Any], policy: dict[str, Any],
           parties: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    customer = result["history"]["customer"]
    los = result.get("los_dedupe") or {}
    search, case = los.get("search") or {}, los.get("case") or {}
    scenarios = {s.get("scenario"): s.get("status") for s in case.get("scenarios") or []}
    match = customer.get("match")
    reasons: list[str] = []
    records: list[dict[str, Any]] = []
    missing: list[str] = []

    # ---- which required searches ran
    if match in _UNAVAILABLE:
        missing.append(f"MIFIN_{match}")
    if search.get("status") == "FAILED":
        missing.append("LOS_SEARCH_FAILED")
    if case.get("status") == "FAILED":
        missing.append("LOS_CASE_ASSESSMENT_FAILED")

    # ---- what matched (safe references only: masked MIFIN refs, LOS case ids the caller may open, else REDACTED)
    customer_match = match == "CONFIRMED_MATCH"
    if customer_match:
        reasons.append(f"{result['searched_parameter']}_MATCHES_EXISTING_CUSTOMER")
        records.append({"record_ref": customer.get("customer_code") or customer.get("customer_ref"),
                        "record_type": "EXISTING_CUSTOMER",
                        "source": "MIFIN", "matched_fields": [result["searched_parameter"]],
                        "role": customer.get("role_in_source")})
    loans = result["history"].get("loans") or []
    existing_loan_match = customer_match and bool(loans)
    if existing_loan_match:
        reasons.append("EXISTING_LOANS_ON_RECORD")
        for loan in loans:
            records.append({"record_ref": loan.get("prospect_code") or loan.get("prospect_ref"),
                            "record_type": "EXISTING_LOAN", "source": "MIFIN",
                            "status": loan.get("effective_status")})
    review: list[str] = []
    if match == "POTENTIAL_MATCH":
        review.append("POSSIBLE_MATCH_NOT_CONFIRMED")
    if match == "AMBIGUOUS":
        review.append("MORE_THAN_ONE_CUSTOMER_MATCHED")
    if customer.get("searched_identifier_matches") is False:
        review.append("IDENTIFIER_DIFFERS_ON_MIFIN_RECORD")
    matches = search.get("matches") or []
    for m in matches:
        records.append({"record_ref": m.get("case_id"), "record_type": "LOS_APPLICATION", "source": "LOS",
                        "matched_fields": m.get("matched_fields") or [], "role": m.get("role")})
    if matches:
        review.append("IDENTIFIER_ON_OTHER_LOS_CASES")
    for scenario, code in _REVIEW_SCENARIOS.items():
        if scenarios.get(scenario) == "DETECTED":
            review.append(code)
    application_match = scenarios.get("S10_SIMULTANEOUS_APPLICATION") == "DETECTED"
    if application_match:
        review.append("SAME_PRODUCT_APPLICATION_EXISTS")
    reasons += review

    signed = bool((policy or {}).get("signed_off"))
    if missing:
        checked_status = "CHECK_INCOMPLETE"
    elif application_match and signed:
        checked_status = "DUPLICATE_FOUND"
    elif review:
        checked_status = "POSSIBLE_DUPLICATE"
    else:
        checked_status = "NO_DUPLICATE_FOUND"

    # ---- every party on the case (party_eval.py), then ONE application-level status. An incomplete check anywhere
    # is never a negative; a co-applicant's match stays that party's -- the application only aggregates.
    parties = parties or []
    party_gaps = [f"{p['role']}:{r}" for p in parties if p["status"] == "CHECK_INCOMPLETE" for r in p["match_reasons"]]
    statuses = [checked_status] + [p["status"] for p in parties]
    if "CHECK_INCOMPLETE" in statuses:
        status = "CHECK_INCOMPLETE"
    else:
        status = next(s for s in ("DUPLICATE_FOUND", "POSSIBLE_DUPLICATE", "NO_DUPLICATE_FOUND") if s in statuses)
    complete = not missing and not party_gaps
    workflow = recommend(status, policy)
    conflicts = [r for r in reasons if r in _CONFLICTS] + [
        f"{p['role']}:{r}" for p in parties for r in p["match_reasons"] if r in _CONFLICTS]
    existing = customer_match or any(p.get("existing_customer") for p in parties)
    return {"status": status, "checked_identifier_status": checked_status,
            "customer_match": customer_match, "existing_loan_match": existing_loan_match,
            "application_match": application_match, "match_reasons": list(dict.fromkeys(reasons)),
            "matched_records": records, "search_complete": complete, "searches_missing": missing,
            "parties": parties, "parties_incomplete": party_gaps,
            "recommended_action": _actions(policy)[status],
            "workflow_recommendation": workflow,
            "assessment": {
                "existing_customer_or_loan": "FOUND" if (existing or existing_loan_match) else "NOT_FOUND",
                "duplicate_application": ("CONFIRMED" if status == "DUPLICATE_FOUND" else
                                          "POSSIBLE" if application_match or status == "POSSIBLE_DUPLICATE"
                                          else "UNKNOWN" if status == "CHECK_INCOMPLETE" else "NOT_FOUND"),
                "conflicts": list(dict.fromkeys(conflicts)),
                "search_complete": complete,
                # the dedupe agent never decides credit: underwriting and the mandatory LOS checks still decide
                "credit_decision": "NOT_DECIDED_BY_DEDUPE",
            },
            "policy": {"policy_id": policy.get("policy_id"), "policy_version": policy.get("policy_version"),
                       "signed_off": signed}}


# reasons that are CONFLICTS (identifiers pointing at different records), as opposed to plain evidence
_CONFLICTS = {"IDENTIFIER_DIFFERS_ON_MIFIN_RECORD", "PAN_ON_ANOTHER_CUSTOMER_RECORD", "MORE_THAN_ONE_CUSTOMER_MATCHED",
              "IDENTIFIER_ON_OTHER_CUSTOMER_RECORD_IN_LOS"}
RECOMMENDATIONS = ("PROCEED_TO_NEXT_STAGE", "HOLD_FOR_REVIEW", "BLOCK_APPLICATION", "UNABLE_TO_DECIDE")


_IDENTITY = {"pan": "PAN_DIFFERS_FROM_MIFIN_RECORD", "name": "NAME_DIFFERS_FROM_MIFIN_RECORD",
             "dob": "DOB_DIFFERS_FROM_MIFIN_RECORD"}


def with_rule_findings(block: dict[str, Any], result: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """A PROCEED never stands beside a finding: triggered customer-history rules and the applicant's identity differing
    from the matched MIFIN record become HOLD (dedupe_policy.yaml decision.rule_findings), named in the reasons."""
    spec = ((policy or {}).get("decision") or {}).get("rule_findings") or {}
    customer = (result.get("history") or {}).get("customer") or {}
    found = [r["reason_code"] for r in (result.get("evaluation") or {}).get("rules") or []
             if r.get("outcome") == "TRIGGERED" and r.get("reason_code")]
    ident = customer.get("identity_check") or {}
    if customer.get("match") in ("CONFIRMED_MATCH", "POTENTIAL_MATCH"):
        found += [code for field, code in _IDENTITY.items() if ident.get(field) == "MISMATCH"]
    found = list(dict.fromkeys(found))
    block = {**block, "rule_findings": found}
    workflow = block.get("workflow_recommendation") or {}
    target = spec.get("recommendation")
    if found and workflow.get("recommendation") == "PROCEED_TO_NEXT_STAGE" and target in RECOMMENDATIONS:
        texts = ((policy or {}).get("decision") or {}).get("texts") or {}
        block["workflow_recommendation"] = {**workflow, "recommendation": target, "reason": spec.get("reason"),
                                            "next_action": (texts.get("next_actions") or {}).get(target)}
        block["match_reasons"] = list(dict.fromkeys([*(block.get("match_reasons") or []), *found]))
    return block


def recommend(status: str, policy: dict[str, Any]) -> dict[str, Any]:
    """The workflow recommendation for a status, from dedupe_policy.yaml decision.workflow. A status the policy does
    not map (null) is UNABLE_TO_DECIDE with the open policy question -- never an invented block / proceed."""
    decision = (policy or {}).get("decision") or {}
    mapped = (decision.get("workflow") or {}).get(status)
    value = mapped if mapped in RECOMMENDATIONS else "UNABLE_TO_DECIDE"
    texts = decision.get("texts") or {}
    question = (decision.get("questions") or {}).get(status) if value == "UNABLE_TO_DECIDE" else None
    return {"recommendation": value, "reason": (texts.get("reasons") or {}).get(status),
            "next_action": (texts.get("next_actions") or {}).get(value),
            "policy_question": question}


__all__ = ["CUSTOMER_LOOKUP", "RECOMMENDATIONS", "STATUSES", "derive", "not_invoked", "recommend", "trigger",
           "with_rule_findings"]
