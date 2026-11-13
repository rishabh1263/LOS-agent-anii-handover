"""EVERY PARTY ON THE CASE, EVALUATED SEPARATELY (2026-10-22): the primary applicant and each co-applicant, each with
its OWN identifiers on record, before the application-level aggregation (dedupe_status.py).

Identifiers come from the same sources the LOS dedupe profiles use (profiles.py): the applicant's declared mobile
(`applicants.mobile`), a co-applicant's declared PAN (`co_applicants.pan`), and the PAN read from each party's own
documents (current EXTRACTION findings). The raw values live only IN MEMORY for the MIFIN lookup and are never
returned, logged or stored; the answer carries which identifier was used (PAN preferred: strong) and masked references.

A party with no PAN or mobile on record is CHECK_INCOMPLETE for that party -- never "no match". A co-applicant's
match does not make the applicant a duplicate: each party carries its own outcome; the application status aggregates
them by precedence (dedupe_status.aggregate). Bounded by dedupe_policy.yaml party_evaluation.max_parties.
"""
from __future__ import annotations

from typing import Any

from app.agents.dedupe import identity as I
from app.agents.dedupe import mifin as M
from app.agents.dedupe.profiles import _PAN_KEYS, CO, PRIMARY

_UNAVAILABLE = (M.UNAVAILABLE, M.SOURCE_ERROR, M.MALFORMED)


def identifiers(repository, case_id: str, applicant_id: str) -> list[dict[str, Any]]:
    """[{party_id, role, pan, mobile, sources}] -- raw values, IN MEMORY ONLY (never put in an answer)."""
    parties: dict[str, dict[str, Any]] = {}

    def party(party_id: str, role: str) -> dict[str, Any]:
        return parties.setdefault(party_id, {"party_id": party_id, "role": role, "pan": None, "mobile": None,
                                             "sources": {}})

    primary = party(applicant_id, PRIMARY)
    row = repository.get_applicant(applicant_id) if hasattr(repository, "get_applicant") else None
    if row is not None and I.normalise("MOBILE", getattr(row, "mobile", None)):
        primary["mobile"], primary["sources"]["MOBILE"] = row.mobile, "DECLARED"
    try:
        has_coapp = repository.table_exists("co_applicants")
    except Exception:  # noqa: BLE001 - an unreadable catalogue: no declared co-applicants, the rest still reads
        has_coapp = False
    if has_coapp:
        from app.store.crypto import open_value

        # in the order they were declared (created_at carries microseconds), so "co-applicant 1" means the first one
        for r in repository._all("SELECT co_applicant_id, pan FROM co_applicants WHERE case_id = ? "
                                 "ORDER BY created_at, co_applicant_id", (case_id,)):
            p = party(r["co_applicant_id"], CO)
            pan = open_value(r["pan"]) if r["pan"] else None
            if I.normalise("PAN", pan):
                p["pan"], p["sources"]["PAN"] = pan, "DECLARED"
    findings = repository.get_current_findings_for_cases([case_id], kind="EXTRACTION").get(case_id, [])
    for f in findings:
        payload = f.payload or {}
        pid = f.party_id or applicant_id
        p = party(pid, PRIMARY if pid == applicant_id else CO)
        for k in _PAN_KEYS:
            if I.normalise("PAN", payload.get(k)):
                p["pan"], p["sources"]["PAN"] = str(payload[k]), "DOCUMENT_EXTRACTION"   # a document outranks a declared
                break
    ordered = [primary] + [p for pid, p in parties.items() if pid != applicant_id]
    return ordered


def _status(match: str | None, los_status: str | None, used: str | None) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if used is None:
        return "CHECK_INCOMPLETE", ["NO_PAN_OR_MOBILE_ON_RECORD"]
    if match in _UNAVAILABLE:
        return "CHECK_INCOMPLETE", [f"MIFIN_{match}"]
    if match == M.POTENTIAL:
        reasons.append("POSSIBLE_MATCH_NOT_CONFIRMED")
    if match == M.AMBIGUOUS:
        reasons.append("MORE_THAN_ONE_CUSTOMER_MATCHED")
    if los_status in ("MATCH", "POSSIBLE_MATCH"):
        reasons.append("IDENTIFIER_ON_OTHER_CUSTOMER_RECORD_IN_LOS")
    if match == M.CONFIRMED:
        reasons.insert(0, f"{used}_MATCHES_EXISTING_CUSTOMER")
    review = [r for r in reasons if not r.endswith("_MATCHES_EXISTING_CUSTOMER")]
    return ("POSSIBLE_DUPLICATE" if review else "NO_DUPLICATE_FOUND"), reasons


def evaluate(repository, case_id: str, applicant_id: str, los_case: dict[str, Any] | None, *, mifin_source=None,
             max_parties: int = 4, lookup_cache: dict | None = None,
             checked: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One evaluation per party (bounded). lookup_cache: (kind, normalised value) -> (MifinResult, raw), so a party
    whose identifier was the one searched is not looked up twice."""
    cache = lookup_cache if lookup_cache is not None else {}
    los_parties = {p.get("party_id"): p for p in (los_case or {}).get("parties") or []}
    out: list[dict[str, Any]] = []
    for p in identifiers(repository, case_id, applicant_id)[:max_parties]:
        # the APPLICANT being checked with no identifier on record: the identifier the officer entered for this check
        # is theirs (checked = {"role", "kind", "value"}); a co-applicant check names no single co-applicant
        # A PAN entered for the applicant counts whenever none is on record: PAN is the strong identifier, so the
        # applicant's own line never contradicts the searched PAN's match (owner 2026-10-23); a mobile only when the
        # applicant has no identifier at all
        if (checked and checked.get("role") == "APPLICANT" and p["role"] == PRIMARY and not p["pan"]
                and (checked.get("kind") == "PAN" or (checked.get("kind") == "MOBILE" and not p["mobile"]))):
            p[checked["kind"].lower()] = checked["value"]
            p["sources"][checked["kind"]] = "ENTERED_FOR_CHECK"
        used = "PAN" if p["pan"] else ("MOBILE" if p["mobile"] else None)
        found = None
        if used:
            value = p["pan"] if used == "PAN" else p["mobile"]
            key = (used, I.normalise(used, value))
            if key not in cache:
                cache[key] = M.lookup(used, value, source=mifin_source)
            found, _raw = cache[key]
        pub = found.public() if found is not None else None
        match = found.match if found is not None else None
        decided = None
        if match == M.POTENTIAL and found.customer_id:      # an officer's decision on this candidate, on this case
            from app.agents.dedupe import match_decision as MD

            decided = MD.latest(repository, case_id, MD.candidate(found.customer_id))
            match = {"SAME_CUSTOMER": M.CONFIRMED, "NOT_SAME_CUSTOMER": M.NO_MATCH}.get(decided, match)
        los = los_parties.get(p["party_id"]) or {}
        status, reasons = _status(match, los.get("status"), used)
        out.append({
            "party_id": p["party_id"], "role": p["role"],
            "identifiers_on_record": [k for k in ("PAN", "MOBILE") if p[k.lower()]],
            "identifier_used": used, "identifier_source": p["sources"].get(used) if used else None,
            "mifin": ({"match": match, "officer_decision": decided, "customer_ref": pub["customer_ref"],
                       "customer_code": pub.get("customer_code"),          # in full (owner 2026-10-23)
                       "role_in_source": pub.get("customer_type"),
                       "loan_count": pub["loan_count"], "loans_by_status_class": pub["loans_by_status_class"]}
                      if found is not None else None),
            "los": {"status": los.get("status"), "matches": len(los.get("matches") or []),
                    "reason_codes": los.get("reason_codes") or []},
            "status": status, "match_reasons": reasons,
            "existing_customer": match == M.CONFIRMED,
        })
    return out


__all__ = ["evaluate", "identifiers"]
