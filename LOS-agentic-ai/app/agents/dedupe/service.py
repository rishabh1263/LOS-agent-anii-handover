"""
THE DEDUPE AGENT -- Phase A: only what the LOS's own data can answer.

Parameters (owner, 2026-10-22): PAN, MOBILE, AP_CODE (= customer id), PR_CODE (= loan account number). PAN and mobile
are stored by the LOS and matched as keyed HMACs; AP code and PR code are NOT stored by this LOS and are reported
INSUFFICIENT_DATA -- never "no match".

    assess(case_id):  profiles (this case's parties, the other cases' parties) -> comparisons (MATCH / POSSIBLE_MATCH /
                      NO_MATCH) -> signals (closed set) -> policy rules (TRIGGERED / NOT_TRIGGERED / NOT_EVALUATED /
                      EVALUATION_FAILED) -> scenarios + recommendation (configured precedence) -> the fraud agent's
                      DedupeResult
    search(parameter, value): the dropdown lookup -- which cases hold this PAN / mobile.

NEVER CLAIMED: loan-book history, repayment / default behaviour, top-up eligibility, closure or RCU outcomes. Those
sources do not exist in this LOS; they are reported DATA_UNAVAILABLE and their rules NOT_EVALUATED in every response.
NEVER RETURNED: a PAN, a mobile or an HMAC token. A matched case the caller may not open is REDACTED.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.credit.policy import NotComparable, matches
from app.agents.dedupe import config as C
from app.agents.dedupe import identity as I
from app.agents.dedupe import match as M
from app.agents.dedupe import profiles as P

logger = logging.getLogger(__name__)
_AUDIT_LOCK = threading.Lock()
REDACTED = "REDACTED"

TRIGGERED, NOT_TRIGGERED, NOT_EVALUATED, FAILED = "TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUATED", "EVALUATION_FAILED"


class CaseNotFound(LookupError):
    """No such case in the store."""


class InvalidParameter(ValueError):
    """A search value that is not a well-formed instance of its parameter."""


# ---- concurrency ---------------------------------------------------------------------------------------------------
@contextmanager
def serialised(repository, keys: list[str]):
    """Two assessments of the same person / customer record run one after the other: a session-level Postgres
    advisory lock per key, taken in sorted order (two callers never deadlock). Yields whether a lock was taken."""
    pool_get = getattr(repository, "_pool_get", None)
    if pool_get is None or not keys:
        yield False
        return
    ordered = sorted(set(keys))
    with pool_get().connection() as conn:
        taken: list[str] = []
        try:
            for key in ordered:
                conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
                taken.append(key)
            yield True
        finally:
            for key in reversed(taken):
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _repo(repository):
    if repository is not None:
        return repository
    from app.store import get_repository

    return get_repository()


def parameter_coverage(policy: dict) -> list[dict[str, Any]]:
    """Each dedupe parameter: whether this LOS stores it, and so whether it was compared."""
    out = []
    for name in C.PARAMETERS:
        spec = policy["parameters"][name]
        stored = spec["field"] is not None
        out.append({"parameter": name, "strength": spec["strength"], "stored_by_los": stored,
                    "evaluated": stored, **({} if stored else {"status": M.INSUFFICIENT,
                                                               "reason": "PARAMETER_NOT_STORED_BY_LOS"})})
    return out


# ---- policy --------------------------------------------------------------------------------------------------------
def evaluate(policy: dict, signals: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rule in policy["rules"]:
        value = signals.get(rule["inputs"][0])
        row = {"rule_id": rule["rule_id"], "policy_version": rule["policy_version"], "severity": rule["severity"],
               "recommendation": rule["recommendation"], "signal": rule["inputs"][0], "value": value,
               "confirmation_status": rule["confirmation_status"]}
        if not rule["enabled"]:
            row.update(outcome=NOT_EVALUATED, reason="DISABLED")
        elif rule["threshold"] is None:
            row.update(outcome=NOT_EVALUATED, reason="THRESHOLD_UNCONFIGURED")
        elif value is None:
            row.update(outcome=NOT_EVALUATED, reason="DATA_UNAVAILABLE")
        else:
            try:
                row.update(outcome=TRIGGERED if matches(rule, value) else NOT_TRIGGERED, reason=None)
            except NotComparable:
                row.update(outcome=FAILED, reason="VALUE_NOT_COMPARABLE")
        out.append(row)
    for entry in policy["not_implemented"]:
        out.append({"rule_id": entry["rule_id"], "policy_version": policy["policy_version"], "severity": None,
                    "recommendation": None, "signal": None, "value": None, "confirmation_status": None,
                    "outcome": NOT_EVALUATED, "reason": "DATA_UNAVAILABLE", "needs_source": entry["needs"]})
    return out


# ---- the current case's in-memory values (the LOS flow, before ingest has stored them) -----------------------------
def _overlay(current: list[P.Profile], case_row: dict, supplied: dict[str, dict] | None) -> list[P.Profile]:
    by_party = {p.party_id: p for p in current}
    for party_id, values in (supplied or {}).items():
        p = by_party.get(party_id)
        if p is None:
            role = P.PRIMARY if party_id == case_row["applicant_id"] else P.CO
            p = P.Profile(case_row["case_id"], case_row["applicant_id"], party_id, role, case_row.get("product"),
                          str(case_row.get("created_at") or "") or None)
            by_party[party_id] = p
        # the LOS flow hands released extraction values; the dedupe API hands the value the officer is checking FOR
        # this party (source DECLARED) -- what the officer typed is what is compared, labelled as typed
        source = str(values.get("source") or "DOCUMENT_EXTRACTION")
        for k in ("pan_number", "pan"):
            if values.get(k):
                p.add("PAN", str(values[k]), source)
    return list(by_party.values())


# ---- the assessment ------------------------------------------------------------------------------------------------
def assess(case_id: str, *, repository=None, current_identifiers: dict[str, dict] | None = None,
           can_view_case: Callable[[str], bool] | None = None, request_id: str | None = None,
           record: bool = True) -> dict[str, Any]:
    """The dedupe assessment of `case_id`.

    current_identifiers: party_id -> released extraction values of THIS run (the LOS flow runs before ingest stores
    them); merged with what the store already holds.
    can_view_case: whether the caller may open a matched case (None = internal caller: every case visible).
    """
    started = time.perf_counter()
    repository = _repo(repository)
    policy = C.get_policy()
    limits = policy["limits"]
    request_id = request_id or f"ddp_{uuid.uuid4().hex}"

    current_rows, _ = P.case_rows(repository, limit=1, case_ids=[case_id])
    if not current_rows:
        raise CaseNotFound(case_id)
    case_row = current_rows[0]

    failed: list[dict[str, str]] = []
    scanned, truncated = P.case_rows(repository, limit=int(limits["max_scan_cases"]))
    others = [r for r in scanned if r["case_id"] != case_id]
    try:
        profiles = P.build(repository, [case_row] + others)
    except Exception as exc:  # noqa: BLE001 - a failed source, never a clean history
        logger.warning("dedupe: profile read failed for %s: %r", case_id, exc)
        failed.append({"source": "LOS_CASE_STORE", "error": type(exc).__name__})
        profiles = []
    current = _overlay([p for p in profiles if p.case_id == case_id], case_row, current_identifiers)
    candidates = [p for p in profiles if p.case_id != case_id]

    keys = [f"dedupe:app:{case_row['applicant_id']}"] + [f"dedupe:pan:{min(p.pan)}" for p in current if p.pan]
    with serialised(repository, keys) as locked:
        result = _assess(case_row, current, candidates, others, policy, truncated, failed, repository,
                         can_view_case or (lambda _cid: True), request_id)
    result["coverage"]["serialised"] = locked
    result["timings_ms"] = {"total_ms": round((time.perf_counter() - started) * 1000, 1)}
    if record:
        _record(repository, result)
    return result


def _assess(case_row, current, candidates, others, policy, truncated, failed, repository, visible,
            request_id) -> dict[str, Any]:
    case_id, applicant_id = case_row["case_id"], case_row["applicant_id"]
    by_case = {r["case_id"]: r for r in others}
    matching, limits = policy["matching"], policy["limits"]

    def ref(cid: str) -> dict[str, Any]:
        if visible(cid):
            return {"case_id": cid, "customer_record": by_case.get(cid, {}).get("applicant_id"), "visible": True}
        return {"case_id": REDACTED, "customer_record": REDACTED, "visible": False}

    parties: list[dict[str, Any]] = []
    same_person_cases: set[str] = set()       # cases where THIS case's primary person is primary again (PAN)
    pan_other_records: set[str] = set()       # other customer records holding the primary's PAN
    possible_records: set[str] = set()        # other customer records sharing only the mobile
    shared_mobile_records: set[str] = set()   # other records sharing the mobile with a DIFFERENT PAN (a family)
    name_conflicts: set[tuple[str, str]] = set()   # (case, party) elsewhere: this party's PAN under a different name
    role_changes = 0
    for cur in sorted(current, key=lambda p: (p.role != P.PRIMARY, p.party_id)):
        available = (["PAN"] if cur.pan else []) + (["MOBILE"] if cur.mobile else [])
        if not cur.has_identity:
            parties.append({"party_id": cur.party_id, "role": cur.role, "status": M.INSUFFICIENT,
                            "parameters_available": [], "reason_codes": ["NO_PAN_AND_NO_MOBILE"], "matches": []})
            continue
        found, best = [], M.NO_MATCH
        for other in candidates:
            cmp = M.compare(cur, other, matching)
            other_record = other.applicant_id != applicant_id
            if cmp.status == M.NO_MATCH:
                if "MOBILE" in cmp.shared_signals and other_record:
                    shared_mobile_records.add(other.applicant_id)
                continue
            best = max(best, cmp.status, key=M.rank)
            role_change = cmp.status == M.MATCH and other.role != cur.role
            found.append({**ref(other.case_id),
                          "relation": "OTHER_CUSTOMER_RECORD" if other_record else "SAME_CUSTOMER_RECORD",
                          "matched_role": other.role, "role_change": role_change, "status": cmp.status,
                          "matched_fields": cmp.matched_fields, "reason_codes": cmp.reason_codes,
                          "name_differs": cmp.name_conflict,
                          "confidence": cmp.confidence, "product": other.product})
            if cmp.status == M.MATCH:
                role_changes += role_change
                if cmp.name_conflict:
                    name_conflicts.add((other.case_id, other.party_id))
                if cur.role == P.PRIMARY and other.role == P.PRIMARY:
                    same_person_cases.add(other.case_id)
                    if other_record:
                        pan_other_records.add(other.applicant_id)
            elif other_record and cur.role == other.role == P.PRIMARY:
                possible_records.add(other.applicant_id)
        parties.append({"party_id": cur.party_id, "role": cur.role, "status": best,
                        "parameters_available": available, "parameter_sources": dict(sorted(cur.sources.items())),
                        "matches": found})

    # the same customer's other applications: this record's other cases + the same primary PAN's cases elsewhere
    same_customer = {r["case_id"] for r in others if r["applicant_id"] == applicant_id} | same_person_cases
    product = case_row.get("product")
    same_product = [by_case[c] for c in same_customer if product and by_case.get(c, {}).get("product") == product]
    created = _when(case_row.get("created_at"))
    gaps = [abs((created - _when(r["created_at"])).total_seconds()) for r in same_product
            if created and _when(r.get("created_at"))]

    # documents: a byte-identical file on another customer's case
    doc_hashes, doc_missing, doc_truncated = {}, 0, False
    try:
        own, own_missing, _ = P.file_hashes(repository, [case_id])
        theirs, their_missing, doc_truncated = P.file_hashes(repository, [r["case_id"] for r in others],
                                                             limit=int(limits["max_scan_documents"]))
        doc_hashes, doc_missing = {**theirs, **own}, own_missing + their_missing
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedupe: document store read failed for %s: %r", case_id, exc)
        failed.append({"source": "LOS_DOCUMENT_STORE", "error": type(exc).__name__})
    mine = set(doc_hashes.get(case_id, []))
    reused_other = reused_same = 0
    reuse_refs: list[dict[str, Any]] = []
    for cid, hashes in doc_hashes.items():
        common = mine & set(hashes) if cid != case_id else set()
        if not common:
            continue
        if cid in same_customer:
            reused_same += len(common)
        else:
            reused_other += len(common)
            reuse_refs.append({**ref(cid), "files_in_common": len(common)})

    failed_names = {f["source"] for f in failed}
    partial = bool(truncated or doc_truncated or failed)
    signals = {
        "identity.other_customer_records": len(pan_other_records),
        "identity.possible_matches": len(possible_records),
        "identity.pan_name_conflicts": len(name_conflicts),
        "identity.role_changes": role_changes,
        "applications.other_cases_same_product": len(same_product) if product else None,
        "applications.hours_since_previous_same_product": round(min(gaps) / 3600, 2) if gaps else None,
        "applications.seconds_between_same_product_creates": round(min(gaps), 1) if gaps else None,
        "documents.reused_across_customers": None if "LOS_DOCUMENT_STORE" in failed_names else reused_other,
        "coverage.partial": partial,
    }
    rules = evaluate(policy, signals)
    has_identity = any(p["status"] != M.INSUFFICIENT for p in parties)
    recs = {r["recommendation"] for r in rules if r["outcome"] == TRIGGERED}
    if not has_identity:
        recs.add("DATA_UNAVAILABLE")
    if partial:
        recs.add("PARTIAL_ANALYSIS")
    recs.add("LINK_EXISTING_CUSTOMER" if same_customer else "CONTINUE_STANDARD_ONBOARDING")
    ordered = [r for r in policy["recommendation_precedence"] if r in recs]
    status = (M.INSUFFICIENT if not has_identity else
              max((p["status"] for p in parties if p["status"] != M.INSUFFICIENT), key=M.rank))
    return {
        "request_id": request_id,
        "case_id": case_id,
        "policy": {"policy_id": policy["policy_id"], "policy_version": policy["policy_version"],
                   "signed_off": bool(policy.get("signed_off"))},
        "status": status,
        "recommendation": ordered[0],
        "recommendations": ordered,
        "parameters": parameter_coverage(policy),
        "parties": parties,
        "applications": {"same_customer_other_cases": len(same_customer),
                         "same_product_other_cases": signals["applications.other_cases_same_product"],
                         "hours_since_previous_same_product": signals["applications.hours_since_previous_same_product"]},
        "documents": {"files_on_case": len(mine), "reused_on_other_customers_cases": reused_other,
                      "reused_on_same_customer_cases": reused_same, "reuse": reuse_refs,
                      "files_missing_from_store": doc_missing},
        "shared_signals": {"mobile_shared_with_other_customer_records": len(shared_mobile_records)},
        "scenarios": _scenarios(same_customer, same_product, signals, has_identity, parties),
        "rules": rules,
        "rule_counts": {o: sum(r["outcome"] == o for r in rules) for o in (TRIGGERED, NOT_TRIGGERED, NOT_EVALUATED,
                                                                          FAILED)},
        # NEVER A CLEAN RESULT BY OMISSION: any rule not evaluated (an unconfigured business window, a source the LOS
        # lacks, a failed read) makes the assessment incomplete, and says which and why
        "completeness": {
            "complete": not any(r["outcome"] in (NOT_EVALUATED, FAILED) for r in rules) and not partial,
            "not_evaluated": [{"rule_id": r["rule_id"], "reason": r["reason"]} for r in rules
                              if r["outcome"] in (NOT_EVALUATED, FAILED)],
            "note": "NO_MATCH covers only the rules evaluated and the LOS case store searched; it is not a clean "
                    "dedupe result while any rule is not evaluated.",
        },
        "dedupe_result": _visible_ids(
            _fraud_contract(policy, pan_other_records, shared_mobile_records, status, rules), others, visible),
        "coverage": _coverage(policy, others, truncated, doc_truncated, partial, failed),
    }


def _coverage(policy, others, truncated, doc_truncated, partial, failed) -> dict[str, Any]:
    return {"mode": "SCAN", "cases_scanned": len(others), "scan_limit": int(policy["limits"]["max_scan_cases"]),
            "truncated": bool(truncated), "documents_truncated": bool(doc_truncated), "partial": partial,
            "failed_sources": failed,
            "sources": [{"source": s["source"], "available": bool(s.get("available", True)),
                         **({"reason": s["reason"]} if s.get("reason") else {})} for s in policy["sources"]]}


def _scenarios(same_customer, same_product, signals, has_identity, parties) -> list[dict[str, Any]]:
    """Phase A scenarios DETECTED / NOT_DETECTED with their evidence; the rest NOT_EVALUATED with the source needed."""
    out = []

    def add(sid: str, detected: bool, evidence: dict):
        out.append({"scenario": sid, "status": "DETECTED" if detected else "NOT_DETECTED", "evidence": evidence})

    any_match = any(m["status"] in (M.MATCH, M.POSSIBLE) for p in parties for m in p["matches"])
    add("S01_NEW_CUSTOMER_CANDIDATE", has_identity and not same_customer and not any_match,
        {"searched": "LOS case store only"})
    add("S02_EXISTING_CUSTOMER", bool(same_customer), {"other_cases": len(same_customer)})
    add("S09_ROLE_CHANGE", bool(signals["identity.role_changes"]), {"matches": signals["identity.role_changes"]})
    add("S10_SIMULTANEOUS_APPLICATION", bool(same_product), {"same_product_cases": len(same_product)})
    # ONE PAN, TWO IDENTITIES: the primary's PAN on another customer record, or any party's PAN held elsewhere under a
    # different name (any role -- a co-applicant's PAN on another case is the same person only if the name agrees)
    add("S11_IDENTITY_CONFLICT", bool(signals["identity.other_customer_records"]
                                      or signals.get("identity.pan_name_conflicts")),
        {"other_customer_records": signals["identity.other_customer_records"],
         "same_pan_different_name": signals.get("identity.pan_name_conflicts") or 0})
    add("S11_POSSIBLE_MATCH", bool(signals["identity.possible_matches"]),
        {"records_sharing_only_mobile": signals["identity.possible_matches"]})
    add("S12_DOCUMENT_REUSE", bool(signals["documents.reused_across_customers"]),
        {"files": signals["documents.reused_across_customers"]})
    add("S15_PARTIAL_HISTORY", bool(signals["coverage.partial"]) or not has_identity,
        {"partial": signals["coverage.partial"], "identity_available": has_identity})
    for sid, source in (("S03_REPEAT_LOAN", "LOAN_MANAGEMENT_SYSTEM"), ("S04_TOP_UP", "LOAN_MANAGEMENT_SYSTEM"),
                        ("S05_BALANCE_TRANSFER", "LOAN_MANAGEMENT_SYSTEM"),
                        ("S06_MULTIPLE_ACTIVE_LOANS", "LOAN_MANAGEMENT_SYSTEM"),
                        ("S07_HISTORICAL_DEFAULT", "LOAN_MANAGEMENT_SYSTEM"),
                        ("S08_CLOSURE_CONFLICT", "LOAN_MANAGEMENT_SYSTEM"), ("S13_BORROWING_PATTERN", "CREDIT_BUREAU"),
                        ("S14_COLLATERAL_REUSE", "COLLATERAL_REGISTRY"),
                        ("S17_TOP_UP_AFTER_RESTRUCTURING", "LOAN_MANAGEMENT_SYSTEM"),
                        ("S18_FRAUD_CASE_CLEARED", "RCU_CASE_SYSTEM")):
        out.append({"scenario": sid, "status": NOT_EVALUATED,
                    "evidence": {"reason": "DATA_UNAVAILABLE", "needs_source": source}})
    return out


def _fraud_contract(policy, pan_other_records, shared_mobile_records, status, rules) -> dict[str, Any]:
    """The fraud agent's DedupeResult, unchanged in shape (matched_parameters, matched_customer_ids) plus extra keys the
    model allows. Parameter names come from dedupe_policy.yaml parameters.*.fraud_parameter. Only a PAN on ANOTHER
    customer record is a match; a mobile only when fraud_dedupe.report_shared_mobile. AP / PR codes are not stored by
    this LOS, so they are never reported as matched."""
    params, ids = [], set()
    names = {k: v["fraud_parameter"] for k, v in policy["parameters"].items()}
    if pan_other_records:
        params.append(names["PAN"])
        ids |= pan_other_records
    if policy["fraud_dedupe"].get("report_shared_mobile") and shared_mobile_records:
        params.append(names["MOBILE"])
        ids |= shared_mobile_records
    return {"matched_parameters": params, "matched_customer_ids": sorted(ids)[:100], "status": status,
            "policy_version": policy["policy_version"], "source": "LOS_DEDUPE_AGENT",
            "parameters_not_stored": [names[p] for p in C.PARAMETERS if policy["parameters"][p]["field"] is None],
            "rules_triggered": [r["rule_id"] for r in rules if r["outcome"] == TRIGGERED]}


def _visible_ids(contract: dict[str, Any], others: list[dict], visible: Callable[[str], bool]) -> dict[str, Any]:
    """Another customer's record id is shown only to a caller who may open one of that customer's cases: the API's
    officer sees REDACTED, the internal fraud path (every case visible) the real ids."""
    cases_of: dict[str, list[str]] = {}
    for r in others:
        cases_of.setdefault(r["applicant_id"], []).append(r["case_id"])
    contract["matched_customer_ids"] = [
        cid if any(visible(c) for c in cases_of.get(cid, [])) else REDACTED for cid in contract["matched_customer_ids"]]
    return contract


def fraud_dedupe(case_id: str, *, current_identifiers: dict[str, dict] | None = None, repository=None):
    """The DedupeResult for the fraud agent, or None when the assessment could not run or had nothing to compare --
    the fraud agent then reports its usual data gap, exactly the behaviour before this agent existed."""
    from app.agents.fraud_risk.schemas import DedupeResult

    try:
        result = assess(case_id, repository=repository, current_identifiers=current_identifiers)
    except Exception as exc:  # noqa: BLE001 - never takes the risk path down
        logger.warning("dedupe: unavailable for %s: %r", case_id, exc)
        return None
    if result["status"] == M.INSUFFICIENT:
        return None
    return DedupeResult(**result["dedupe_result"])


# ---- the dropdown lookup -------------------------------------------------------------------------------------------
def customer_type_check(declared: str | None, status: str) -> dict[str, Any] | None:
    """The form's Customer Type against what the lookup found. Only the LOS case store was searched, so an EXISTING
    customer the LOS has never seen is a review, not a contradiction: their loans live in the LMS."""
    if declared is None:
        return None
    if status == M.INSUFFICIENT:
        result = "NOT_VERIFIABLE"
    elif declared == "NEW":
        result = {M.MATCH: "DECLARED_NEW_BUT_FOUND", M.POSSIBLE: "DECLARED_NEW_POSSIBLE_MATCH"}.get(status, "CONSISTENT")
    else:
        result = {M.MATCH: "CONSISTENT", M.POSSIBLE: "POSSIBLE_MATCH_ONLY"}.get(status, "EXISTING_NOT_FOUND_IN_LOS")
    return {"declared": declared, "result": result, "searched": "LOS case store only"}


def resolve(parameter: str, policy: dict | None = None) -> str:
    """A dropdown label ("Mobile No", "PR Code", "Customer ID" ...) -> PAN | MOBILE | AP_CODE | PR_CODE."""
    found = (policy or C.get_policy())["labels"].get(C.label_key(parameter))
    if not found:
        raise InvalidParameter("dedupe parameter must be one of PAN, Mobile No, AP Code, PR Code")
    return found


def search(parameter: str, value: str, *, customer_type: str | None = None, repository=None,
           can_view_case: Callable[[str], bool] | None = None, request_id: str | None = None,
           exclude_case_id: str | None = None) -> dict[str, Any]:
    """Which cases hold this PAN / mobile -- the form's Dedupe Parameter + Dedupe Parameter Value, checked against its
    Customer Type. AP_CODE / PR_CODE are not stored by this LOS: INSUFFICIENT_DATA, naming the source they need.
    exclude_case_id: the case the check is run FOR -- it holds the value itself and is never "another case"."""
    started = time.perf_counter()
    policy = C.get_policy()
    parameter = resolve(parameter, policy)
    declared = None
    if customer_type is not None:
        declared = policy["customer_types"].get(C.label_key(customer_type))
        if declared is None:
            raise InvalidParameter("customer type must be New or Existing")
    spec = policy["parameters"][parameter]
    request_id = request_id or f"dds_{uuid.uuid4().hex}"
    base = {"request_id": request_id, "parameter": parameter, "strength": spec["strength"],
            "policy": {"policy_id": policy["policy_id"], "policy_version": policy["policy_version"],
                       "signed_off": bool(policy.get("signed_off"))}}
    if parameter in ("AP_CODE", "PR_CODE"):
        from app.agents.dedupe import mifin

        if mifin.fits(parameter, I.normalise(parameter, value)) is False:
            raise InvalidParameter(f"value is not a valid {parameter} (expected the MIFIN format, e.g. "
                                   f"{'AP + 8 digits' if parameter == 'AP_CODE' else 'PR + 8 digits'})")
    if spec["field"] is None:
        return {**base, "status": M.INSUFFICIENT, "reason_codes": ["PARAMETER_NOT_STORED_BY_LOS"],
                "needs_source": spec.get("needs_source"), "matches": [],
                "customer_type_check": customer_type_check(declared, M.INSUFFICIENT),
                "coverage": {"mode": "NONE", "cases_scanned": 0, "partial": True}}
    norm = I.normalise(parameter, value)
    if not norm:
        raise InvalidParameter(f"value is not a valid {parameter}")
    repository = _repo(repository)
    visible = can_view_case or (lambda _cid: True)
    rows, truncated = P.case_rows(repository, limit=int(policy["limits"]["max_scan_cases"]))
    probe = P.Profile("", "", "", P.PRIMARY)
    probe.add(parameter, value, "SEARCH")
    by_case = {r["case_id"]: r for r in rows}
    found: list[dict[str, Any]] = []
    for other in P.build(repository, rows):
        hit = (probe.pan & other.pan) if parameter == "PAN" else (probe.mobile & other.mobile)
        if not hit or (exclude_case_id and other.case_id == exclude_case_id):
            continue
        status = M.MATCH if spec["strength"] == "STRONG" else M.POSSIBLE
        conf = policy["matching"]["confidence"]["strong_parameter" if status == M.MATCH else "supporting_only"]
        shown = visible(other.case_id)
        found.append({"case_id": other.case_id if shown else REDACTED,
                      "customer_record": by_case[other.case_id]["applicant_id"] if shown else REDACTED,
                      "visible": shown, "role": other.role, "product": other.product if shown else None,
                      "status": status, "matched_fields": [parameter], "confidence": conf,
                      "reason_codes": [f"{parameter}_EXACT"]})
    best = max((m["status"] for m in found), key=M.rank, default=M.NO_MATCH)
    return {**base, "status": best, "reason_codes": [] if found else [f"NO_{parameter}_MATCH_IN_LOS"],
            "matches": found, "customer_records": len({m["customer_record"] for m in found if m["visible"]}),
            "customer_type_check": customer_type_check(declared, best),
            "coverage": {"mode": "SCAN", "cases_scanned": len(rows), "truncated": bool(truncated),
                         "partial": bool(truncated)},
            "timings_ms": {"total_ms": round((time.perf_counter() - started) * 1000, 1)}}


# ---- audit ---------------------------------------------------------------------------------------------------------
def _rule_set_hash() -> str:
    """The dedupe policy in force as written (not the normalised form: the same rules -> the same hash in every
    process), registered for the audit database's rule_sets; its hash."""
    from app.agent_db import audit_store, rule_store

    digest, content = rule_store.load_with_hash(C.RULE_SET)
    info = rule_store.describe(C.RULE_SET, content)
    registered = audit_store.register_rule_set(C.RULE_SET, content, version_label=info["version_label"],
                                               status=info["status"], source_file=C.policy_path(),
                                               is_demo=info["is_demo"])
    return registered if registered == digest else digest


def _record(repository, result: dict[str, Any]) -> None:
    """A masked audit line (no identifier, no token) and a case-timeline event. Never raises."""
    import os
    from pathlib import Path

    line = {"at": datetime.now(timezone.utc).isoformat(), "request_id": result["request_id"],
            "case_id": result["case_id"], "policy_version": result["policy"]["policy_version"],
            "status": result["status"], "recommendation": result["recommendation"],
            "rules": {r["rule_id"]: r["outcome"] for r in result["rules"]},
            "coverage": {k: result["coverage"].get(k) for k in ("mode", "cases_scanned", "partial", "serialised")}}
    try:
        line["rule_set_hash"] = _rule_set_hash()
    except Exception as exc:  # noqa: BLE001 - the decision is still audited, without its rule set
        logger.warning("dedupe: rule set not registered: %r", exc)
    try:
        from app.observability.audit_log import append_line

        path = Path(os.getenv("DEDUPE_AUDIT_PATH") or "runtime/audit/dedupe_decisions.jsonl")
        with _AUDIT_LOCK:
            append_line(path, json.dumps(line, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedupe: audit line not written: %r", exc)
    try:
        from app.store.models import CaseEvent

        repository.record_event(CaseEvent(
            event_id=uuid.uuid4().hex, case_id=result["case_id"], event_type="DEDUPE_ASSESSED",
            summary=f"Dedupe {result['status']} -> {result['recommendation']} "
                    f"(policy {result['policy']['policy_version']})", ref_id=result["request_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedupe: case event not written: %r", exc)


__all__ = ["CaseNotFound", "InvalidParameter", "REDACTED", "assess", "customer_type_check", "evaluate",
           "fraud_dedupe", "parameter_coverage", "resolve", "search", "serialised"]
