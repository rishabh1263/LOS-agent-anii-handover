"""
THE CUSTOMER-HISTORY ASSESSMENT of one case (FOS stage; API under the credit underwriting agent):

    validated input -> MIFIN match (mifin.py) -> loan history (loan_history.py, + LMS) -> policy (history_policy.py)
    -> deterministic outcome -> explanation (history_summary.py; the model last, never deciding) -> audit + timeline

Serialised per case (two assessments of the same case never interleave). Every failure of a source is a status in the
result -- never an exception and never a clean history. Nothing personal is returned or logged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agents.dedupe import history_policy, history_summary, identity
from app.agents.dedupe import loan_history as H
from app.agents.dedupe import service as dedupe_service

logger = logging.getLogger(__name__)
_AUDIT_LOCK = threading.Lock()
API_VERSION = "1.0"


class CaseNotFound(LookupError):
    """No application record for the case."""


def _validated(parameter: str, value: str) -> str:
    """The parameter (PAN | MOBILE | AP_CODE | PR_CODE) for this form label and a well-formed value, or
    dedupe_service.InvalidParameter (the value is never echoed)."""
    from app.agents.dedupe import mifin

    kind = dedupe_service.resolve(parameter)
    norm = identity.normalise(kind, value)
    if not norm or (kind in ("AP_CODE", "PR_CODE") and mifin.fits(kind, norm) is False):
        raise dedupe_service.InvalidParameter(f"value is not a valid {kind}")
    return kind


def _los_dedupe(case_id: str, kind: str, value: str, repository, can_view_case,
                customer_type: str | None = None, checked: dict[str, Any] | None = None) -> tuple[dict[str, Any], list]:
    """The LOS's own dedupe in the same answer: the supplied value searched across LOS cases, and this case's
    assessment (role swaps, file reuse, simultaneous applications). Returns (section, [(outcome, reason, action)]).
    A failure is reported in the section and adds nothing -- MIFIN and the policy still answer.

    checked: {"party": APPLICANT | CO_APPLICANT, "party_id": ...}. A PAN checked FOR THE APPLICANT is that party's
    PAN in this case's assessment (source DECLARED), so a role change or one-PAN-two-names against it is seen even
    before a PAN document is on the case. A co-applicant check names no co-applicant, so nothing is attached."""
    contributions_cfg = history_policy.policy().get("los_dedupe_contributions") or {}
    section: dict[str, Any] = {}
    try:
        section["search"] = dedupe_service.search(kind, value, customer_type=customer_type, repository=repository,
                                                  can_view_case=can_view_case, exclude_case_id=case_id)
    except Exception as exc:  # noqa: BLE001
        section["search"] = {"status": "FAILED", "error": type(exc).__name__}
    supplied = None
    if kind == "PAN" and checked and checked.get("party") == "APPLICANT" and checked.get("party_id"):
        supplied = {checked["party_id"]: {"pan": value, "source": "DECLARED"}}
    try:
        case = dedupe_service.assess(case_id, repository=repository, can_view_case=can_view_case,
                                     current_identifiers=supplied)
        section["case"] = {k: case[k] for k in ("status", "recommendation", "recommendations", "parties",
                                                  "applications", "documents", "shared_signals", "scenarios",
                                                  "rule_counts", "completeness", "coverage", "policy")}
    except Exception as exc:  # noqa: BLE001
        section["case"] = {"status": "FAILED", "error": type(exc).__name__}
        return section, []
    contributions = [("REVIEW_REQUIRED", spec["reason_code"], spec["next_action"])
                     for rec, spec in contributions_cfg.items() if rec in case.get("recommendations", [])]
    return section, contributions


def _rank(status: str) -> int:
    from app.agents.dedupe import match as M

    return M.rank(status)


def _party_role(label: str | None) -> str:
    """dedupe_policy.yaml party_roles: Applicant / Co-Applicant (labels or codes); unset = the applicant."""
    from app.agents.dedupe import config as dedupe_config

    roles = dedupe_config.get_policy().get("party_roles") or {"Applicant": "APPLICANT"}
    if label is None or not str(label).strip():
        return "APPLICANT"
    wanted = dedupe_config.label_key(label)
    for name, code in roles.items():
        if wanted in (dedupe_config.label_key(name), dedupe_config.label_key(code)):
            return str(code)
    raise dedupe_service.InvalidParameter("party role must be one of: " + ", ".join(roles))


def assess(case_id: str, parameter: str, value: str, *, customer_type: str | None = None,
           requested_product: str | None = None, requested_amount: Any = None, request_id: str | None = None,
           repository=None, generator=None, mifin_source=None, lms_source=None, record: bool = True,
           can_view_case=None, party_role: str | None = None, confirm_match: str | None = None,
           candidate: str | None = None, decided_by: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    kind = _validated(parameter, value)
    if repository is None:
        from app.store import get_repository

        repository = get_repository()
    application = repository.get_application(case_id)
    if application is None:
        raise CaseNotFound(case_id)
    # the amount and product asked for ON THIS CASE when the request does not name them (a check from "Check an
    # existing case" sends none): exposure + requested amount (H08) is then still evaluated, never left unknown
    if requested_amount is None and getattr(application, "loan_amount", None) not in (None, ""):
        requested_amount = application.loan_amount
    if requested_product is None and getattr(application, "product", None):
        requested_product = application.product
    request_id = request_id or f"chs_{uuid.uuid4().hex}"
    declared = None
    if customer_type is not None:
        from app.agents.dedupe import config as dedupe_config

        declared = dedupe_config.get_policy()["customer_types"].get(dedupe_config.label_key(customer_type))
        if declared is None:
            raise dedupe_service.InvalidParameter("customer type must be New or Existing")
    party = _party_role(party_role)
    if party != "APPLICANT":
        declared = None                     # the form's Customer Type is the applicant's: not checked for a co-applicant

    with dedupe_service.serialised(repository, [f"history:{case_id}"]) as locked:
        from app.agents.dedupe import match_decision as MD

        if confirm_match is not None and (confirm_match not in MD.DECISIONS or not candidate):
            raise dedupe_service.InvalidParameter("confirm_match must be SAME_CUSTOMER or NOT_SAME_CUSTOMER, "
                                                  "with the candidate from the previous answer")
        decision_for, decided = MD.resolver(repository, case_id, confirm_match, candidate)
        # the applicant's own name / DOB, for the identity help on a MIFIN match (compared in memory, never returned);
        # a co-applicant check compares nothing (which co-applicant is not part of the request)
        person = None
        if party == "APPLICANT" and hasattr(repository, "get_applicant"):
            row = repository.get_applicant(application.applicant_id)
            person = {"name": getattr(row, "full_name", None), "dob": getattr(row, "date_of_birth", None)} if row else None
            try:                                 # the applicant's PAN on record (declared / read from documents)
                from app.agents.dedupe import party_eval as PE

                pan = PE.identifiers(repository, case_id, application.applicant_id)[0]["pan"]
            except Exception:  # noqa: BLE001 - an unreadable record: PAN not compared, never a match
                pan = None
            if pan:
                person = {**(person or {}), "pan": pan}
        from app.agents.dedupe import config as _dc

        auto = bool((_dc.get_policy().get("officer_confirmation") or {}).get("auto_confirm", False))
        history = H.history(kind, value, mifin_source=mifin_source, lms_source=lms_source, decision_for=decision_for,
                            applicant=person, auto_confirm=auto)
        if confirm_match is not None:
            if "to_record" not in decided:
                raise MD.NothingToConfirm("there is no possible match to decide on for this identifier now")
            MD.record(repository, case_id, decided["to_record"], confirm_match, decided_by)
        evaluation = history_policy.evaluate(history, requested_amount)
    los_section, los_contributions = _los_dedupe(case_id, kind, value, repository, can_view_case, customer_type,
                                                 checked={"party": party, "party_id": application.applicant_id})
    match = history["customer"]["match"]
    found = {"CONFIRMED_MATCH": "MATCH", "POTENTIAL_MATCH": "POSSIBLE_MATCH"}.get(match, "NO_MATCH")
    unavailable = match in ("UNAVAILABLE", "SOURCE_ERROR", "MALFORMED")
    # THE LOOKUP IS MIFIN + THE LOS (2026-11-03): the LOS search finding the identifier on another case is part of
    # who the customer is -- the top-level status and the Customer Type check said NEW / CONSISTENT beside an LOS
    # PAN match. The stronger of the two answers stands; MIFIN being unavailable no longer hides an LOS match.
    los_status = (los_section.get("search") or {}).get("status")
    los_found = los_status if los_status in ("MATCH", "POSSIBLE_MATCH") else None
    combined = max([found] + ([los_found] if los_found else []), key=_rank)
    type_status = combined if (los_found or not unavailable) else "INSUFFICIENT_DATA"
    type_check = dedupe_service.customer_type_check(declared, type_status) if declared else None
    type_spec = (history_policy.policy().get("customer_type_contributions") or {}).get(
        (type_check or {}).get("result"))
    if type_spec:
        los_contributions = [*los_contributions, ("REVIEW_REQUIRED", type_spec["reason_code"],
                                                  type_spec["next_action"])]
    # ONE OUTCOME: the history policy's and the LOS dedupe's, by the same precedence
    outcomes = [evaluation["outcome"]] + [c[0] for c in los_contributions]
    outcome = next(o for o in history_policy.PRECEDENCE if o in outcomes)
    reason_codes = list(dict.fromkeys(evaluation["reason_codes"] + [c[1] for c in los_contributions]))
    next_actions = list(dict.fromkeys([c[2] for c in los_contributions] + evaluation["next_actions"]))
    if outcome != "READY_FOR_DECISION":
        next_actions = [a for a in next_actions if not a.startswith("Proceed to the credit decision")]
    stage_row = repository.get_case_stage(case_id) if hasattr(repository, "get_case_stage") else None
    status = ("UNKNOWN" if unavailable else "AMBIGUOUS" if match == "AMBIGUOUS"
              else "EXISTING" if match == "CONFIRMED_MATCH"
              else "POSSIBLE_EXISTING" if match == "POTENTIAL_MATCH" else "NEW")
    if los_found == "MATCH" and status in ("NEW", "UNKNOWN", "POSSIBLE_EXISTING"):
        status = "EXISTING"                   # the LOS holds this PAN on another case: not a new customer
    elif los_found == "POSSIBLE_MATCH" and status == "NEW":
        status = "POSSIBLE_EXISTING"          # only the mobile agrees: possible, for the officer to decide
    result: dict[str, Any] = {
        "api_version": API_VERSION,
        "request_id": request_id,
        "case_id": case_id,
        "stage": getattr(stage_row, "stage", None),
        "searched_parameter": kind,
        "checked_party": party,
        "customer_status": status,
        # which lookups found the customer (MIFIN = the customer master, LOS = another LOS case)
        "customer_found_in": [s for s, hit in (("MIFIN", found != "NO_MATCH"), ("LOS", bool(los_found))) if hit],
        "customer_type_check": type_check,
        "requested": {"product": requested_product, "amount_rupees": str(requested_amount)
                      if requested_amount is not None else None},
        "los_dedupe": los_section,
        "history": history,
        "evaluation": evaluation,
        "outcome": outcome,
        "reason_codes": reason_codes,
        "next_actions": next_actions,
        "completeness": {"complete": evaluation["complete"] and history["history_status"] in (
            "COMPLETE", "NO_PREVIOUS_LOANS", "NOT_APPLICABLE"), "history_status": history["history_status"],
            "sources": history["sources"]},
        "policy": evaluation["policy"],
        "is_demo": history["is_demo"],
        "serialised": locked,
    }
    from app.agents.dedupe import config as _dedupe_cfg
    from app.agents.dedupe import dedupe_status

    policy_cfg = _dedupe_cfg.get_policy()
    parties = None
    # THE TRIGGER: the customer lookup decides whether the dedupe agent runs (a confirmed NEW customer: it does not)
    trig = dedupe_status.trigger(result, policy_cfg)
    pe = policy_cfg.get("party_evaluation") or {}
    # a NEW applicant with co-applicants: each co-applicant is still looked up; one that is not provably new
    # invokes the agent (trigger.include_co_applicants) -- a co-applicant's check is never skipped silently
    check_coapps = (not trig["dedupe_invoked"] and pe.get("enabled", True)
                    and bool((policy_cfg.get("trigger") or {}).get("include_co_applicants", True)))
    if (trig["dedupe_invoked"] or check_coapps) and pe.get("enabled", True):
        from app.agents.dedupe import party_eval

        try:
            parties = party_eval.evaluate(repository, case_id, application.applicant_id, los_section.get("case"),
                                          mifin_source=mifin_source, max_parties=int(pe.get("max_parties") or 4),
                                          checked={"role": party, "kind": kind, "value": value})
        except Exception as exc:  # noqa: BLE001 - a party evaluation that fails is an incomplete check, never a negative
            logger.warning("customer history: party evaluation failed: %r", type(exc).__name__)
            parties = [{"party_id": None, "role": "ALL_PARTIES", "identifiers_on_record": [], "identifier_used": None,
                        "identifier_source": None, "mifin": None, "los": {}, "status": "CHECK_INCOMPLETE",
                        "match_reasons": ["PARTY_EVALUATION_FAILED"], "existing_customer": False}]
    if check_coapps and parties and any(p["role"] != "PRIMARY_APPLICANT" and (p.get("existing_customer")
                                                                           or p["status"] != "NO_DUPLICATE_FOUND")
                                        for p in parties):
        trig = {**trig, "dedupe_invoked": True, "customer_lookup_parties": "CO_APPLICANT_NOT_NEW",
                "reason": ((policy_cfg.get("trigger") or {}).get("reasons") or {}).get("CO_APPLICANT_NOT_NEW")}
    if trig["dedupe_invoked"]:
        result["dedupe"] = dedupe_status.with_rule_findings(
            {"invoked": True, "trigger": trig, **dedupe_status.derive(result, policy_cfg, parties)}, result, policy_cfg)
    else:
        result["dedupe"] = dedupe_status.not_invoked(result, policy_cfg, trig)
    result["explanation"] = history_summary.explain(result, generator=generator)
    result["timings_ms"] = {"total_ms": round((time.perf_counter() - started) * 1000, 1)}
    result["audit"] = {"recorded": _record(repository, result, actor=decided_by) if record else False}
    return result


def _record(repository, result: dict[str, Any], actor: str | None = None) -> bool:
    """A masked JSONL audit line + a case-timeline event + the check in the dedupe schema's columns (dedupe_checks,
    dedupe_scenario_results, dedupe_matches, audit_events; queued, never waited for). Never raises; says whether the
    line and the event were written."""
    import os
    from pathlib import Path

    ok = True
    line = {"at": datetime.now(timezone.utc).isoformat(), "request_id": result["request_id"],
            "case_id": result["case_id"], "parameter": result["searched_parameter"],
            "party_role": result.get("checked_party"),
            "match": result["history"]["customer"]["match"], "history_status": result["history"]["history_status"],
            "outcome": result["outcome"], "reason_codes": result["reason_codes"],
            "policy_version": result["policy"]["policy_version"], "is_demo": result["is_demo"],
            "explanation_source": result["explanation"]["source"]}
    try:
        from app.observability.audit_log import append_line

        path = Path(os.getenv("CUSTOMER_HISTORY_AUDIT_PATH") or "runtime/audit/customer_history.jsonl")
        with _AUDIT_LOCK:
            append_line(path, json.dumps(line, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.warning("customer history: audit line not written: %r", type(exc).__name__)
    try:
        from app.store.models import CaseEvent

        repository.record_event(CaseEvent(
            event_id=uuid.uuid4().hex, case_id=result["case_id"], event_type="CUSTOMER_HISTORY_ASSESSED",
            summary=(f"{result['customer_status']} customer, history {result['history']['history_status']} -> "
                     f"{result['outcome']} (policy {result['policy']['policy_version']})"),
            ref_id=result["request_id"]))
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.warning("customer history: case event not written: %r", type(exc).__name__)
    try:
        from app.agent_db import dedupe_store

        dedupe_store.record_check(result, actor=actor)
    except Exception as exc:  # noqa: BLE001 - the answer is never failed by its own record
        logger.warning("customer history: dedupe check rows not queued: %r", type(exc).__name__)
    return ok


__all__ = ["API_VERSION", "CaseNotFound", "assess"]
