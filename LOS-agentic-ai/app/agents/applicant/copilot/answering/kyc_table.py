"""
THE KYC TABLE (FOS plan section 4; flag COPILOT_KYC_TABLE, config chatbot.kyc_table.enabled -- ON in dev).

Per party, one row per (check, document):

    check | document | value on document | value on application | result | reason

FROM RECORDED FINDINGS ONLY. The KYC finding names, per check, the documents compared and -- for a failed check --
what each said; the document's own value otherwise comes from its EXTRACTION finding (what the verification gate
released). The application's value is the applicant record (the co-applicant's declared profile for them). The
result of a row is the EXISTING profile-match comparator for that check (app/agents/los/profile_match.py, which
delegates to name_match / the date and PAN normalisers / the address comparator) -- no new matcher. Every value is
masked per the disclosure policy (app/security/sensitivity.py) before it is shown.

THE ODD ONE OUT. When, for one check, every document but one agrees and the one differs, that document is named
as the likely odd one out with the fix (upload a correct one) -- said as "likely", never as a verdict.
"""

from __future__ import annotations

import os
from typing import Any

FLAG = "COPILOT_KYC_TABLE"
_ON = {"1", "true", "yes", "on"}

#: KYC check -> (applicant-record attribute, co-applicant profile key, extraction keys, sensitivity field name)
_CHECKS: dict[str, tuple[str | None, str | None, tuple[str, ...], str]] = {
    "NAME": ("full_name", "name", ("name", "employee_name", "account_holder", "holder_name"), "full_name"),
    "DATE_OF_BIRTH": ("date_of_birth", "dob", ("date_of_birth",), "date_of_birth"),
    "PAN_NUMBER": (None, "pan", ("pan_number", "pan"), "pan_number"),
    "FATHER_NAME": (None, "father_name", ("father_name", "guardian_name", "relation_name"), "father_name"),
    "ADDRESS": ("address", "address", ("address",), "address"),
}
_ALIAS = {"DOB": "DATE_OF_BIRTH", "PAN": "PAN_NUMBER"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("kyc_table")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    # every wording is config (applicant_agent.yaml chatbot.kyc_table.labels, per language) -- MASTER SPEC 14
    value = (_cfg().get("labels") or {}).get(key, key)
    return language_lock.pick(value).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _shown(sensitivity_field: str, value: Any) -> str | None:
    """The value as it may be shown (masked per policy), or None when withheld / absent."""
    from app.security import sensitivity

    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("full") or value.get("raw") or ", ".join(str(v) for v in value.values() if v)
    disclosure = sensitivity.disclosure(sensitivity_field)
    if disclosure == "withhold":
        return None
    text = sensitivity.mask(str(value)) if disclosure == "masked" else str(value)
    return sensitivity.mask_identifiers(text) if sensitivity_field in ("address", "full_name") else text


def _compare(check: str, declared: Any, document_value: Any, document_type: str = "") -> str | None:
    """PASS / PARTIAL / FAIL by the existing profile-match comparator for this check, or None."""
    if declared in (None, "") or document_value in (None, ""):
        return None
    from app.agents.kyc.schemas import KycField
    from app.agents.los import profile_match

    try:
        field = KycField(check)
        compare = profile_match._COMPARATORS.get(field)
        if compare is None:
            return None
        evidence = profile_match.Evidence(value=document_value, source_id="kyc-table",
                                          document_type=document_type or "UNKNOWN",
                                          fields={"address": document_value} if check == "ADDRESS" else {})
        status, _score, _method = compare(str(declared), evidence)
        return str(getattr(status, "value", status)).upper()
    except Exception:  # noqa: BLE001 - a value the comparator cannot read is reported as not compared
        return None


def _extracted(repository: Any, case_id: str) -> dict[tuple[str | None, str], dict[str, Any]]:
    """(party_id, DOCUMENT_TYPE) -> the fields the verification gate released for that document."""
    types = {d.document_id: str(d.document_type).upper() for d in repository.list_documents(case_id) or []}
    out: dict[tuple[str | None, str], dict[str, Any]] = {}
    try:
        findings = repository.get_current_findings(case_id, kind="EXTRACTION") or []
    except Exception:  # noqa: BLE001
        findings = []
    for f in findings:
        payload = getattr(f, "payload", None) or {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        kind = types.get(getattr(f, "document_id", None))
        if kind and isinstance(fields, dict):
            out[(getattr(f, "party_id", None), kind)] = fields
    return out


def _declared(repository: Any, case_id: str, party_id: str | None, role: str) -> dict[str, Any]:
    """The application's values for this party: the applicant record, or the co-applicant's declared profile."""
    application = repository.get_application(case_id)
    if role == "CO_APPLICANT":
        try:
            from app.agents.los import co_applicants

            for row in co_applicants.list_for_case(case_id, repository):
                if party_id in (row.get("co_applicant_id"), row.get("applicant_id"), None):
                    return {spec[1]: row.get(spec[1]) for spec in _CHECKS.values() if spec[1]}
        except Exception:  # noqa: BLE001 - no co-applicant table: nothing declared to compare
            return {}
        return {}
    applicant = repository.get_applicant(getattr(application, "applicant_id", None)) if application else None
    return {spec[0]: getattr(applicant, spec[0], None) for spec in _CHECKS.values() if spec[0] and applicant}


def build(case_id: str, repository: Any = None, *, party: str | None = None) -> dict[str, Any]:
    """{"parties": [{party_role, rows, odd_one_out, status}]} for one case, from its recorded findings."""
    from app.store import get_repository

    repository = repository or get_repository()
    documents = repository.list_documents(case_id) or []
    role_of = {getattr(d, "party_id", None): str(getattr(getattr(d, "party_role", None), "value",
                                                         getattr(d, "party_role", None)) or "PRIMARY_APPLICANT")
               for d in documents}
    extracted = _extracted(repository, case_id)
    try:
        findings = repository.get_current_findings(case_id, kind="KYC") or []
    except Exception:  # noqa: BLE001
        findings = []
    parties = []
    for finding in findings:
        party_id = getattr(finding, "party_id", None)
        role = role_of.get(party_id) or "PRIMARY_APPLICANT"
        if role not in ("PRIMARY_APPLICANT", "CO_APPLICANT"):
            role = "PRIMARY_APPLICANT"
        if party and role != party:
            continue
        declared = _declared(repository, case_id, party_id, role)
        rows: list[dict[str, Any]] = []
        for field in (getattr(finding, "payload", None) or {}).get("fields") or []:
            check = _ALIAS.get(str(field.get("field") or "").upper(), str(field.get("field") or "").upper())
            if check not in _CHECKS:
                continue
            app_key, co_key, keys, sens = _CHECKS[check]
            if check == "NAME":
                from app.agents.kyc import config as _kyc_config

                keys = _kyc_config.source_keys("name_fields", keys)      # the keys KYC itself reads
            declared_value = declared.get(co_key if role == "CO_APPLICANT" else app_key) if (app_key or co_key) else None
            for source in field.get("sources") or []:
                kind = str(source.get("document_type") or "").upper()
                value = source.get("value")
                if value in (None, ""):
                    found = extracted.get((party_id, kind)) or extracted.get((None, kind)) or {}
                    value = next((found.get(k) for k in keys if found.get(k) not in (None, "")), None)
                result = _compare(check, declared_value, value, kind)
                reason = (_label("not_read") if value in (None, "")
                          else _label("not_on_application") if declared_value in (None, "")
                          else {"PASS": _label("matches"), "PARTIAL": _label("partial")}.get(result or "",
                                                                                           _label("differs")))
                rows.append({"check": check, "check_label": _readable(check), "document_type": kind,
                             "document_label": _readable(kind),
                             "value_on_document": _shown(sens, value), "value_on_application": _shown(sens, declared_value),
                             "result": result or str(field.get("status") or "NOT_COMPARED").upper(),
                             "reason": reason, "_raw": value})
        parties.append({"party_role": role, "party_label": "Co-applicant" if role == "CO_APPLICANT" else "Applicant",
                        "status": str(getattr(finding, "status", "") or "").upper(),
                        "rows": rows, "cross": _cross(rows), "odd_one_out": _odd_one_out(rows)})
    for p in parties:
        for r in p["rows"]:
            r.pop("_raw", None)
    return {"parties": parties}


def _same(check: str, a: Any, b: Any) -> bool:
    if check in ("NAME", "FATHER_NAME"):
        return _compare("NAME", a, b) == "PASS"
    norm = lambda v: " ".join(str(v).upper().split())  # noqa: E731
    return norm(a) == norm(b)


def _cross(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    MASTER SPEC section 6 B -- documents vs EACH OTHER: per check, every pair of documents that both carry a value,
    compared by the same rule as the odd one out (names by the name matcher, other fields normalised).
    """
    out = []
    for check in dict.fromkeys(r["check"] for r in rows):
        values = [r for r in rows if r["check"] == check and r.get("_raw") not in (None, "")]
        for i, first in enumerate(values):
            for second in values[i + 1:]:
                if first["document_type"] == second["document_type"]:
                    continue
                out.append({"check": check, "check_label": first["check_label"],
                            "document_1": first["document_label"], "value_1": first["value_on_document"],
                            "document_2": second["document_label"], "value_2": second["value_on_document"],
                            "result": "MATCH" if _same(check, first["_raw"], second["_raw"]) else "DIFFERS"})
    return out


def _odd_one_out(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per check: the one document that differs while all the others (at least two) agree."""
    out = []
    for check in {r["check"] for r in rows}:
        values = [r for r in rows if r["check"] == check and r.get("_raw") not in (None, "")]
        if len(values) < 3:
            continue
        for candidate in values:
            others = [r for r in values if r is not candidate]
            if all(_same(check, others[0]["_raw"], o["_raw"]) for o in others[1:]) \
                    and not _same(check, candidate["_raw"], others[0]["_raw"]):
                out.append({"check": check, "check_label": candidate["check_label"],
                            "document_type": candidate["document_type"], "document_label": candidate["document_label"],
                            "fix": {"action": "UPLOAD_DOCUMENT", "document_type": candidate["document_type"]}})
                break
    return out


def _passed(status: str) -> bool:
    return str(status or "").upper() in {str(s).upper() for s in _cfg().get("passed_statuses") or ["PASS", "PASSED"]}


def render(table: dict[str, Any]) -> str:
    """
    Per party (MASTER SPEC section 6): A -- the application form vs each document; B -- the documents vs each other,
    as two separate markdown tables; then the likely odd one out and what to upload.
    """
    lines: list[str] = []
    for party in table.get("parties") or []:
        if not party["rows"]:
            continue
        lines += ["", f"**{_label('heading', party=party['party_label'])}**", "",
                  f"*{_label('a_title')}*", "",
                  f"| {_label('field')} | {_label('form_value')} | {_label('document')} | "
                  f"{_label('document_value')} | {_label('result')} |", "|---|---|---|---|---|"]
        for r in party["rows"]:
            lines.append(f"| {r['check_label']} | {r['value_on_application'] or '--'} | {r['document_label']} | "
                         f"{r['value_on_document'] or '--'} | {_result(r['result'], r['reason']) if r['value_on_application'] and r['value_on_document'] else r['reason']} |")
        cross = party.get("cross") or []
        if cross:
            lines += ["", f"*{_label('b_title')}*", "",
                      f"| {_label('field')} | {_label('document_1')} | {_label('value')} | {_label('document_2')} | "
                      f"{_label('value')} | {_label('result')} |", "|---|---|---|---|---|---|"]
            for c in cross:
                lines.append(f"| {c['check_label']} | {c['document_1']} | {c['value_1'] or '--'} | {c['document_2']} | "
                             f"{c['value_2'] or '--'} | {_label('match') if c['result'] == 'MATCH' else _label('differs_b')} |")
        for odd in party.get("odd_one_out") or []:
            lines += ["", _label("odd_one_out", document=odd["document_label"], check=odd["check_label"].lower())]
    return "\n".join(lines).strip() or _label("none")


def _result(result: str, reason: str) -> str:
    return {"PASS": _label("match"), "PARTIAL": _label("partial_short")}.get(str(result).upper(), reason)


def to_upload(table: dict[str, Any]) -> dict[str, list[str]]:
    """Per party label: the documents to upload again -- a failed A row's document, and the odd one out."""
    out: dict[str, list[str]] = {}
    for party in table.get("parties") or []:
        docs = [r["document_label"] for r in party["rows"] if str(r["result"]).upper() in ("FAIL", "FAILED", "MISMATCH")]
        docs += [o["document_label"] for o in party.get("odd_one_out") or []]
        if docs:
            out[party["party_label"]] = list(dict.fromkeys(docs))
    return out


def verdict(table: dict[str, Any]) -> str | None:
    """
    KYC failed or in review for any party: "Not ready for CPA. KYC is not complete." then the reasons (A, then B),
    the next step naming the exact documents per party, and the offer of a customer message. None when complete.
    """
    open_parties = [p for p in table.get("parties") or [] if p["rows"] and not _passed(p["status"])]
    if not open_parties:
        return None
    lines = [_label("not_ready")]
    for p in open_parties:
        for r in p["rows"]:
            if str(r["result"]).upper() not in ("PASS",):
                lines.append(_label("reason_a", party=p["party_label"], check=r["check_label"].lower(),
                                    document=r["document_label"], reason=r["reason"].lower()))
        for c in p.get("cross") or []:
            if c["result"] != "MATCH":
                lines.append(_label("reason_b", party=p["party_label"], check=c["check_label"].lower(),
                                    first=c["document_1"], second=c["document_2"]))
    uploads = to_upload({"parties": open_parties})
    named = "; ".join(f"{party}: {', '.join(docs)}" for party, docs in uploads.items())
    lines += ["", _label("next_step", documents=named) if named else _label("next_step_generic")]
    from app.agents.applicant.copilot.answering import contract

    lines.append(contract.ask(_label("draft_message")))
    return "\n".join(dict.fromkeys(lines)).strip()


def attach(published: dict[str, Any]) -> dict[str, Any]:
    """A KYC answer gets the A / B tables (and, when KYC is not complete, the plain verdict first). Both routes."""
    if not isinstance(published, dict) or not enabled() or str(published.get("intent") or "") != "KYC_RESULT":
        return published
    case_id = published.get("case_id")
    if not case_id:
        return published
    party = str(published.get("subject_party") or "").upper() or None
    table = build(case_id, party=party if party in ("PRIMARY_APPLICANT", "CO_APPLICANT") else None)
    if not any(p["rows"] for p in table["parties"]):
        return published
    published["kyc_table"] = table
    if isinstance(published.get("presentation"), dict):
        published["presentation"]["kyc_table"] = table
    text = render(table)
    said = verdict(table)
    from app.agents.applicant.copilot.answering.counts import keep_case_header

    for key in ("answer", "answer_markdown"):
        if not isinstance(published.get(key), str):
            continue
        if said:
            # the verdict IS the answer: it replaces the prose (which said the same reasons less plainly)
            published[key] = keep_case_header(published[key], said + "\n\n" + text, case_id)
        elif text not in published[key]:
            published[key] = published[key].rstrip() + "\n\n" + text
    return published

__all__ = ["FLAG", "attach", "build", "enabled", "render", "to_upload", "verdict"]
