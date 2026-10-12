"""
THE KYC PART OF THE FOS -> CPA GATE (Phase 3 step 2, 2026-10-06).

New rule, behind LOS_FOS_CPA_KYC_RULE (default off): a case leaves FOS for CPA
only when, for EVERY party on the case, every KYC check marked `cpa_gate: true`
in app/config/kyc_policies.yaml (name, DOB, address, PAN, father's name; not
income) is recorded as PASSED.

READ, NEVER DECIDED. Every value comes from the KYC finding the KYC Agent
recorded (case_findings, kind KYC): `passed_checks`, `failed_checks`,
`missing_information`, and the per-field values behind a failure. Nothing is
re-compared here, and nothing missing is assumed to have passed:

    check in passed_checks                       PASS
    check failed, field FAIL                     BLOCKED   (with both values)
    check failed, field REVIEW / PARTIAL         REVIEW
    check could not run (missing_information)    NOT_READY
    no KYC result for the party, or a result
    recorded before checks were kept             NOT_READY  (fail closed)

The gate's outcome is the worst over every party and check. Who the parties
are comes from the case record: the primary applicant, the co-applicant the
application names, and every party with an active document.
"""

from __future__ import annotations

import os
from typing import Any

FLAG = "LOS_FOS_CPA_KYC_RULE"

PASS, REVIEW, BLOCKED, NOT_READY = "PASS", "REVIEW", "BLOCKED", "NOT_READY"
_RANK = {PASS: 0, NOT_READY: 1, REVIEW: 2, BLOCKED: 3}

#: KycCheck -> the field name the recorded result publishes (KycField)
_FIELD = {"NAME": "NAME", "DOB": "DATE_OF_BIRTH", "ADDRESS": "ADDRESS", "PAN": "PAN_NUMBER",
          "FATHER_NAME": "FATHER_NAME", "INCOME": "INCOME"}
_LABEL = {"NAME": "name", "DOB": "date of birth", "ADDRESS": "address", "PAN": "PAN number",
          "FATHER_NAME": "father's name", "INCOME": "income", "APPLICATION_NAME": "application name vs PAN",
          "PROPERTY_OWNERSHIP": "property owner vs applicant", "EMPLOYER_CONSISTENCY": "employer across documents"}

APPLICATION_NAME = "APPLICATION_NAME"
PROPERTY_OWNERSHIP = "PROPERTY_OWNERSHIP"
EMPLOYER_CONSISTENCY = "EMPLOYER_CONSISTENCY"
#: the name-match outcome -> the gate status (kyc_policies.yaml application_name)
_NAME_OUTCOME = {"MATCH": PASS, "PARTIAL": REVIEW, "MISMATCH": BLOCKED, "PENDING": NOT_READY, "UNVERIFIABLE": REVIEW}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def required_checks() -> list[str]:
    from app.agents.kyc.agent import cpa_gate_checks

    checks = list(cpa_gate_checks())
    if _name_check_gated():
        checks.append(APPLICATION_NAME)
    return checks


def _name_spec() -> dict[str, Any]:
    from app.agents.kyc import config as kyc_config

    return kyc_config.section("application_name")


def _name_check_enabled() -> bool:
    return bool(_name_spec().get("enabled", False))


def _name_check_gated() -> bool:
    return _name_check_enabled() and bool(_name_spec().get("cpa_gate", False))


def application_name_match(case_id: str, party_id: str, role: str, repository: Any) -> dict[str, Any]:
    """
    THE APPLICATION'S NAME AGAINST THE NAME ON THE PARTY'S VERIFIED PAN (kyc_policies.yaml application_name).

    {"outcome": MATCH | PARTIAL | MISMATCH | PENDING | UNVERIFIABLE, "reason", "declared", "pan_name",
     "pan_document_id", "pan_verification", "score", "method"} -- computed from the case's records on every call.
    Only a PAN whose verification PASSED counts; the application's name is never changed here.
    """
    out: dict[str, Any] = {"outcome": "PENDING", "reason": "no verified PAN for this party yet", "declared": None,
                           "pan_name": None, "pan_document_id": None, "pan_verification": None, "score": None,
                           "method": None}
    declared = _declared_name(case_id, party_id, role, repository)
    out["declared"] = declared
    pans = [d for d in repository.list_documents(case_id) or []
            if _value(getattr(d, "document_type", "")).upper() == "PAN"
            and _value(getattr(d, "status", "")).upper() != "SUPERSEDED"
            and getattr(d, "party_id", None) in (party_id, None)]
    verified = [d for d in pans if _value(getattr(d, "status", "")).upper() == "VERIFIED"]
    if not verified:
        if pans:
            out["pan_verification"] = _value(getattr(pans[-1], "status", "")).upper() or None
            out["reason"] = "the PAN has not passed verification yet"
        return out
    names = _pan_names(case_id, verified, repository)
    if not names:
        out.update(outcome="UNVERIFIABLE", reason="no holder name was read from the verified PAN",
                   pan_document_id=getattr(verified[-1], "document_id", None), pan_verification="VERIFIED")
        return out
    if not (declared or "").strip():
        out.update(outcome="UNVERIFIABLE", reason="the application carries no name to compare",
                   pan_document_id=names[-1][0], pan_name=names[-1][1], pan_verification="VERIFIED")
        return out
    from app.agents.kyc.schemas import KycField
    from app.agents.los import profile_match

    compare = profile_match._COMPARATORS[KycField.NAME]
    worst = None
    for document_id, pan_name in names:                     # every verified PAN of the party: the worst result counts
        status, score, method = compare(declared, profile_match.Evidence(value=pan_name, source_id=document_id,
                                                                         document_type="PAN"))
        status = str(getattr(status, "value", status)).upper()
        allowed = {str(m).upper() for m in _name_spec().get("match_methods") or ["EXACT", "TOKEN_SET"]}
        if status == "PASS" and str(getattr(method, "value", method)).upper() not in allowed:
            status = "PARTIAL"            # a lenient match (fuzzy / initials) is for a person to confirm
        rank = {"PASS": 0, "PARTIAL": 1, "FAIL": 2}.get(status, 1)
        if worst is None or rank > worst[0]:
            worst = (rank, status, score, method, document_id, pan_name)
    _, status, score, method, document_id, pan_name = worst
    outcome = {"PASS": "MATCH", "PARTIAL": "PARTIAL", "FAIL": "MISMATCH"}.get(status, "UNVERIFIABLE")
    reason = {"MATCH": "the application name matches the name on the verified PAN",
              "PARTIAL": "the application name only partly matches the verified PAN (a person should check)",
              "MISMATCH": "the application name differs from the name on the verified PAN",
              "UNVERIFIABLE": "the names could not be compared"}[outcome]
    out.update(outcome=outcome, reason=reason, pan_name=pan_name, pan_document_id=document_id,
               pan_verification="VERIFIED", score=score, method=str(getattr(method, "value", method)))
    return out


def _declared_name(case_id: str, party_id: str, role: str, repository: Any) -> str | None:
    if role == "CO_APPLICANT":
        try:
            from app.agents.los import co_applicants

            for row in co_applicants.list_for_case(case_id, repository):
                if party_id in (row.get("co_applicant_id"), row.get("applicant_id")):
                    return row.get("name") or row.get("full_name")
        except Exception:  # noqa: BLE001 - no co-applicant record: nothing declared
            return None
        return None
    person = repository.get_applicant(party_id) if party_id else None
    return getattr(person, "full_name", None) if person is not None else None


def _pan_names(case_id: str, verified: list[Any], repository: Any) -> list[tuple[str, str]]:
    """(document_id, holder name) read from each verified PAN's released extraction."""
    keys = [str(k) for k in _name_spec().get("pan_name_fields") or ["name"]]
    wanted = {getattr(d, "document_id", None) for d in verified}
    out = []
    try:
        findings = repository.get_current_findings(case_id, kind="EXTRACTION") or []
    except Exception:  # noqa: BLE001 - unreadable extraction: nothing read
        return []
    for f in findings:
        if getattr(f, "document_id", None) not in wanted:
            continue
        payload = getattr(f, "payload", None) or {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        name = next((str(fields[k]).strip() for k in keys if isinstance(fields, dict) and fields.get(k)), None)
        if name:
            out.append((getattr(f, "document_id", None), name))
    return out


def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def _parties(case_id: str, repository: Any) -> dict[str, str]:
    """party_id -> role, from the case record (never from a caller)."""
    parties: dict[str, str] = {}
    application = repository.get_application(case_id)
    if application is not None:
        if getattr(application, "applicant_id", None):
            parties[application.applicant_id] = "PRIMARY_APPLICANT"
        if getattr(application, "co_applicant_id", None):
            parties[application.co_applicant_id] = "CO_APPLICANT"
    for document in repository.list_documents(case_id) or []:
        if _value(getattr(document, "status", "")).upper() == "SUPERSEDED":
            continue
        party = getattr(document, "party_id", None)
        if party and party not in parties:
            parties[party] = _value(getattr(document, "party_role", "")) or "PRIMARY_APPLICANT"
    return parties


def _field_detail(payload: dict[str, Any], check: str) -> tuple[str, list[dict[str, Any]]]:
    """(field status, the values each document carried) for one check's field."""
    wanted = _FIELD.get(check, check)
    for field in payload.get("fields") or []:
        if isinstance(field, dict) and str(field.get("field") or "").upper() == wanted:
            values = [{"document_type": s.get("document_type"), "value": s.get("value")}
                      for s in field.get("sources") or [] if isinstance(s, dict) and s.get("value")]
            return str(field.get("status") or "").upper(), values
    return "", []


def _check_outcome(check: str, payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"check": check, "label": _LABEL.get(check, check.lower()), "status": NOT_READY,
                           "reason": "no KYC result recorded for this check", "values": []}
    passed = [str(c).upper() for c in payload.get("passed_checks") or []]
    failed = [str(c).upper() for c in payload.get("failed_checks") or []]
    missing = [str(c).upper() for c in payload.get("missing_information") or []]
    if check in passed:
        out.update(status=PASS, reason=None)
    elif check in failed:
        field_status, values = _field_detail(payload, check)
        status = BLOCKED if field_status == "FAIL" else REVIEW
        out.update(status=status, values=values,
                   reason="the documents disagree" if status == BLOCKED else "needs a reviewer's check")
    elif check in missing:
        out.update(reason="could not be checked from the uploaded documents")
    else:
        # NO SUMMARY for this check but the KYC record carries the FIELD results: a failed / under-review field is
        # said as such ("KYC failed: the documents disagree"), never "no KYC result recorded". FAIL-CLOSED: a field
        # result can only BLOCK or ask for review here -- a check never passes without the KYC agent's own summary.
        field_status, values = _field_detail(payload, check)
        if field_status == "FAIL":
            out.update(status=BLOCKED, values=values, reason="the documents disagree")
        elif field_status in ("REVIEW", "MISMATCH", "PARTIAL"):
            out.update(status=REVIEW, values=values, reason="needs a reviewer's check")
    return out


# ==========================================================================
# CROSS-DOCUMENT: PROPERTY OWNERSHIP and EMPLOYER CONSISTENCY (industry defaults, 2026-10-11, kyc_policies.yaml)
# ==========================================================================

def _section(name: str) -> dict[str, Any]:
    from app.agents.kyc import config as kyc_config

    return kyc_config.section(name)


def _released(case_id: str, repository: Any, document_type: str) -> list[tuple[str, dict[str, Any]]]:
    """(document_id, released fields) of the case's current documents of one type -- a superseded document never
    counts, and a field exists only where verification PASSED (the extraction is released behind the gate)."""
    current = {getattr(d, "document_id", None): d for d in repository.list_documents(case_id) or []
               if _value(getattr(d, "document_type", "")).upper() == document_type
               and _value(getattr(d, "status", "")).upper() != "SUPERSEDED"}
    out = []
    for f in repository.get_current_findings(case_id, kind="EXTRACTION") or []:
        if getattr(f, "document_id", None) in current:
            fields = (getattr(f, "payload", None) or {}).get("fields") or {}
            if isinstance(fields, dict):
                out.append((f.document_id, fields))
    return out


def _party_names(case_id: str, repository: Any) -> list[tuple[str, str]]:
    """(role, application name) of every party on the case: the applicant, then each co-applicant."""
    names: list[tuple[str, str]] = []
    application = repository.get_application(case_id)
    if application is not None and getattr(application, "applicant_id", None):
        declared = _declared_name(case_id, application.applicant_id, "PRIMARY_APPLICANT", repository)
        if declared:
            names.append(("PRIMARY_APPLICANT", declared))
    try:
        from app.agents.los import co_applicants

        for row in co_applicants.list_for_case(case_id, repository):
            name = row.get("name") or row.get("full_name")
            if name:
                names.append(("CO_APPLICANT", str(name)))
    except Exception:  # noqa: BLE001 - no co-applicant record: the applicant alone
        pass
    return names


def property_ownership_match(case_id: str, repository: Any) -> dict[str, Any]:
    """
    THE PROPERTY'S OWNER (the buyer on its sale deed) AGAINST THE CASE'S PARTIES (kyc_policies.yaml
    property_ownership). {"outcome": MATCH | PARTIAL | MISMATCH | PENDING | UNVERIFIABLE | NOT_APPLICABLE,
    "required", "reason", "buyers", "matched", "parties", "document_id"} -- from the records, on every call.
    A buyer must be the applicant or a co-applicant; nothing is written and no name is changed.
    """
    spec = _section("property_ownership")
    application = repository.get_application(case_id)
    product = str(getattr(application, "product", "") or "").upper()
    required = bool(spec.get("enabled")) and product in {str(x).upper() for x in spec.get("products") or []}
    out: dict[str, Any] = {"outcome": "NOT_APPLICABLE", "required": required, "reason": "", "buyers": [],
                           "matched": None, "parties": [], "document_id": None}
    if not spec.get("enabled"):
        return out
    deeds = _released(case_id, repository, "SALE_DEED")
    if not deeds:
        if required:
            out.update(outcome="PENDING", reason="no verified sale deed on the case yet")
        return out
    keys = [str(k) for k in spec.get("buyer_fields") or ["buyer_names"]]
    document_id, buyers = deeds[-1][0], []
    for doc_id, fields in deeds:
        for key in keys:
            value = fields.get(key)
            found = [str(v) for v in (value if isinstance(value, list) else [value]) if str(v or "").strip()]
            if found:
                document_id, buyers = doc_id, found
    parties = _party_names(case_id, repository)
    out.update(document_id=document_id, buyers=buyers, parties=[n for _, n in parties])
    if not buyers:
        out.update(outcome="UNVERIFIABLE", reason="no buyer name was read from the sale deed")
        return out
    if not parties:
        out.update(outcome="UNVERIFIABLE", reason="the application carries no name to compare")
        return out
    from app.agents.kyc.schemas import KycField
    from app.agents.los import profile_match

    compare = profile_match._COMPARATORS[KycField.NAME]
    allowed = {str(m).upper() for m in spec.get("match_methods") or ["EXACT", "TOKEN_SET"]}
    best = None                                   # the best (party, buyer) pairing: a strict match wins
    for role, declared in parties:
        for buyer in buyers:
            status, score, method = compare(declared, profile_match.Evidence(
                value=buyer, source_id=document_id, document_type="SALE_DEED"))
            status = str(getattr(status, "value", status)).upper()
            if status == "PASS" and str(getattr(method, "value", method)).upper() not in allowed:
                status = "PARTIAL"
            rank = {"PASS": 2, "PARTIAL": 1}.get(status, 0)
            if best is None or rank > best[0]:
                best = (rank, role, declared, buyer)
    rank, role, declared, buyer = best
    outcome = {2: "MATCH", 1: "PARTIAL"}.get(rank, "MISMATCH")
    out.update(outcome=outcome, matched={"role": role, "name": declared, "buyer": buyer} if rank else None,
               reason={"MATCH": "the property's owner on the sale deed is a party to the application",
                       "PARTIAL": "the owner on the sale deed only partly matches a party (a person should check)",
                       "MISMATCH": "the owner on the sale deed is not the applicant or a co-applicant"}[outcome])
    return out


def _organisation(name: str, legal_words: set[str]) -> str:
    import re as _re

    words = _re.findall(r"[a-z0-9]+", str(name or "").lower())
    return " ".join(w for w in words if w not in legal_words)


def employer_consistency(case_id: str, repository: Any) -> dict[str, Any]:
    """
    ONE EMPLOYER ACROSS THE APPLICANT'S DOCUMENTS (kyc_policies.yaml employer_consistency): the employer printed on
    each released salary slip / employment proof, legal-form words removed. {"outcome": MATCH | MISMATCH |
    NOT_APPLICABLE, "employers": [{document_type, value}], "reason"}. Fewer than two documents naming an employer:
    nothing to compare (NOT_APPLICABLE).
    """
    spec = _section("employer_consistency")
    out: dict[str, Any] = {"outcome": "NOT_APPLICABLE", "employers": [], "reason": ""}
    if not spec.get("enabled"):
        return out
    keys = [str(k) for k in spec.get("employer_fields") or ["employer_name"]]
    legal = {str(w).lower() for w in spec.get("legal_form_words") or []}
    seen = []
    for document_type in ("SALARY_SLIP", "EMPLOYMENT_PROOF"):
        for _doc_id, fields in _released(case_id, repository, document_type):
            value = next((fields.get(k) for k in keys if str(fields.get(k) or "").strip()), None)
            if value:
                seen.append({"document_type": document_type, "value": str(value)})
    out["employers"] = seen
    if len(seen) < 2:
        return out
    distinct = {_organisation(e["value"], legal) for e in seen}
    if len(distinct) == 1:
        out.update(outcome="MATCH", reason="every document names the same employer")
    else:
        out.update(outcome="MISMATCH", reason="the documents name different employers (a person should check)")
    return out


def evaluate(case_id: str, repository: Any = None) -> dict[str, Any]:
    """
    The KYC gate for one case: {"status", "required_checks", "parties": [...]}.
    Raises nothing for missing data (NOT_READY); a store error propagates so the
    caller can fail closed.
    """
    from app.store import get_repository

    repository = repository or get_repository()
    required = required_checks()
    latest: dict[str, Any] = {}
    for finding in repository.get_current_findings(case_id, kind="KYC") or []:
        latest[str(getattr(finding, "party_id", "") or "")] = finding

    parties = []
    for party_id, role in _parties(case_id, repository).items():
        finding = latest.get(party_id)
        if finding is None and role == "PRIMARY_APPLICANT":
            # A CASE-LEVEL KYC RESULT (recorded with no party_id, 2026-10-09: "is my case ready" said "KYC has not
            # run" beside a KYC table of mismatches) is the primary applicant's -- its own recorded checks, nothing
            # assumed; a party-specific result always wins
            finding = latest.get("")
        payload = dict(getattr(finding, "payload", None) or {}) if finding is not None else {}
        document_checks = [c for c in required if c != APPLICATION_NAME]
        if finding is None:
            checks = [{"check": c, "label": _LABEL.get(c, c.lower()), "status": NOT_READY,
                       "reason": "KYC has not run for this party", "values": []} for c in document_checks]
        else:
            checks = [_check_outcome(c, payload) for c in document_checks]
        if _name_check_enabled():
            # the application's name against the verified PAN: shown whenever enabled, counted for the gate only
            # when cpa_gate is set (then it is in `required`)
            match = application_name_match(case_id, party_id, role, repository)
            values = [v for v in ({"document_type": "APPLICATION", "value": match["declared"]},
                                  {"document_type": "PAN", "value": match["pan_name"]}) if v["value"]]
            checks.append({"check": APPLICATION_NAME, "label": _LABEL[APPLICATION_NAME],
                           "status": _NAME_OUTCOME.get(match["outcome"], REVIEW), "reason": match["reason"],
                           "values": values, "name_match": match, "counted": APPLICATION_NAME in required})
        if role == "PRIMARY_APPLICANT":
            # CASE-LEVEL CROSS-DOCUMENT CHECKS, shown on the applicant's line (kyc_policies.yaml, 2026-10-11)
            prop = property_ownership_match(case_id, repository)
            if prop["outcome"] != "NOT_APPLICABLE":
                spec = _section("property_ownership")
                status_of = {"MATCH": PASS, "PARTIAL": REVIEW, "PENDING": NOT_READY, "UNVERIFIABLE": REVIEW,
                             "MISMATCH": str(spec.get("mismatch_status") or BLOCKED).upper()}
                counted_here = bool(prop["required"] and spec.get("cpa_gate"))
                if counted_here and PROPERTY_OWNERSHIP not in required:
                    required = [*required, PROPERTY_OWNERSHIP]
                checks.append({"check": PROPERTY_OWNERSHIP, "label": _LABEL[PROPERTY_OWNERSHIP],
                               "status": status_of.get(prop["outcome"], REVIEW), "reason": prop["reason"],
                               "values": [{"document_type": "SALE_DEED", "value": ", ".join(prop["buyers"])}]
                               + [{"document_type": "APPLICATION", "value": n} for n in prop["parties"][:2]]
                               if prop["buyers"] else [],
                               "property_ownership": prop, "counted": counted_here})
            employer = employer_consistency(case_id, repository)
            if employer["outcome"] != "NOT_APPLICABLE":
                checks.append({"check": EMPLOYER_CONSISTENCY, "label": _LABEL[EMPLOYER_CONSISTENCY],
                               "status": PASS if employer["outcome"] == "MATCH" else REVIEW,
                               "reason": employer["reason"], "values": employer["employers"],
                               "employer_consistency": employer,
                               "counted": bool(_section("employer_consistency").get("cpa_gate"))})
        counted = [c for c in checks if c.get("counted", True)]
        status = max((c["status"] for c in counted), key=lambda s: _RANK[s]) if counted else PASS
        parties.append({"party_id": party_id, "party_role": role, "status": status,
                        "kyc_recorded": finding is not None,
                        "affected_documents": list(payload.get("affected_documents") or []),
                        "checks": checks})

    if not parties:
        status = NOT_READY                      # no party on record: nothing has been checked
    else:
        status = max((p["status"] for p in parties), key=lambda s: _RANK[s])
    return {"status": status, "required_checks": required, "parties": parties}


def blocking_summary(result: dict[str, Any]) -> list[str]:
    """One plain sentence per failing check, naming whose and the values -- for people, never codes."""
    lines = []
    for party in result.get("parties") or []:
        whose = "the co-applicant's" if party.get("party_role") == "CO_APPLICANT" else "the applicant's"
        for c in party.get("checks") or []:
            if c["status"] == PASS:
                continue
            values = c.get("values") or []
            if len(values) >= 2:
                shown = ", ".join(f"{str(v.get('document_type') or '').replace('_', ' ').lower()} shows "
                                  f"\"{v.get('value')}\"" for v in values[:3])
                lines.append(f"KYC {c['label']} mismatch ({whose}): {shown}.")
            else:
                lines.append(f"KYC {c['label']} ({whose}): {c.get('reason')}.")
    return lines


__all__ = ["FLAG", "blocking_summary", "employer_consistency", "enabled", "evaluate", "property_ownership_match",
           "required_checks"]
