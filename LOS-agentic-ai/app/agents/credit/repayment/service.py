"""
The two steps of the one API: receive a person's vendor analysis (the applicant's, or a co-applicant's), and build the
case's repayment report from every statement on file and the case's eligibility data.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.repayment import config, report, store, vendor


class RepaymentError(RuntimeError):
    """A report cannot be built; `code` says why (the API maps it to a status)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def receive(case_id: str, content: bytes, filename: str, party: str | None = None) -> dict[str, Any]:
    """Validate, normalize and keep one person's vendor analysis (`party` None: the applicant's)."""
    up = config.settings()["upload"]
    if not store.valid_case_id(case_id):
        raise RepaymentError("INVALID_CASE_ID", "case_id must look like CASE-<12 hex>")
    if not store.valid_party(party):
        raise RepaymentError("INVALID_PARTY", "co_applicant_id may hold letters, digits, - and _ only")
    if not content:
        raise RepaymentError("EMPTY_FILE", "the upload is empty")
    if len(content) > int(up["max_bytes"]):
        raise RepaymentError("FILE_TOO_LARGE", f"the upload is larger than {int(up['max_bytes']) // 2**20} MB")
    if not any((filename or "").lower().endswith(ext) for ext in up["accept"]):
        raise RepaymentError("UNSUPPORTED_FILE_TYPE", "send " + " or ".join(up["accept"]))
    try:
        data = vendor.read(content, filename)
    except vendor.VendorReportError as exc:
        raise RepaymentError("INVALID_BANK_ANALYSIS", str(exc)) from None
    store.save(case_id, data, party)
    return {"case_id": case_id, "party": party, "vendor": data.get("vendor"), "period": [data["account"].get("period_start"),
                                                                         data["account"].get("period_end")],
            "months": len(data["monthly"]), "full_months": sum(1 for m in data["monthly"] if m.get("full")),
            "transactions": len(data["transactions"])}


def _eligibility(case_id: str) -> dict[str, Any]:
    """The case's eligibility result, with the officer's saved figures (as the eligibility screen shows it)."""
    from app.agents.eligibility.agent import assess_payload
    from app.agents.eligibility.data import inputs_store

    try:
        overrides = inputs_store.overrides_for(case_id)
    except Exception:  # noqa: BLE001 - no saved figures: the case's own data
        overrides = {}
    out = assess_payload({"case_id": case_id, "overrides": overrides})
    if out.get("error") == "CASE_NOT_FOUND":
        recorded = _from_case_store(case_id)    # a LOS case: its application + the eligibility verdict it recorded
        if recorded is not None:
            return recorded
    if out.get("error"):
        raise RepaymentError(out["error"], "the case's eligibility data could not be read")
    return out["eligibility"]


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip()) if value not in (None, "") else None
    except ValueError:
        return None


def _from_case_store(case_id: str) -> dict[str, Any] | None:
    """A case the eligibility table does not hold (a LOS case): the application's DECLARED figures and the eligibility
    verdict recorded on the case, mapped by policy.yaml recorded_eligibility. No verdict recorded -> DATA_MISSING,
    which never lets the bank report recommend. None when the case store has no such application either."""
    from app.store import get_repository

    try:
        repo = get_repository()
        application = repo.get_application(case_id)
        if application is None:
            return None
        recorded = [f for f in repo.get_current_findings(case_id, kind="FINANCIAL") or []
                    if getattr(f, "source_type", None) == "ELIGIBILITY"]
    except Exception as exc:  # noqa: BLE001 - the case store unreadable is a source failure, not a "no"
        raise RepaymentError("DATA_SOURCE_UNAVAILABLE", f"the case store could not be read ({type(exc).__name__})") \
            from None
    status = str(getattr(recorded[-1].status, "value", recorded[-1].status)).upper() if recorded else "NONE"
    decision = (config.policy().get("recorded_eligibility") or {}).get(status, "DATA_MISSING")
    return {
        "decision": decision, "emi": None, "eligible_amount": None,
        "requested_amount": _number(application.loan_amount), "foir": None, "missing_fields": [],
        "reasons": [f"recorded eligibility {status}" if recorded else "no eligibility verdict recorded on the case"],
        "inputs": {"monthly_income": _number(application.declared_monthly_income),
                   "monthly_obligations": _number(application.declared_monthly_obligations),
                   "loan_amount": _number(application.loan_amount),
                   "tenure_months": int(_number(application.tenure_months) or 0) or None,
                   "interest_rate_pct": _number(application.interest_rate_pct)},
        "source": "case_store",
    }


def _finished(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return vendor.finish(data)          # whole months marked whatever the source (an upload, a demo file, the DB)
    except vendor.VendorReportError as exc:
        raise RepaymentError("INVALID_BANK_ANALYSIS", str(exc)) from None


def _clubbed(case_id: str) -> dict[str, Any]:
    """The co-applicant income the officer clubbed on the eligibility screen (income_details), if any."""
    from app.agents.eligibility.data import inputs_store

    try:
        details = (inputs_store.overrides_for(case_id) or {}).get("income_details") or {}
    except Exception:  # noqa: BLE001 - no saved figures: nothing clubbed
        return {}
    return {"income": _number(details.get("clubbed_income")), "relationship": details.get("clubbed_relationship")}


def _case_store_co_applicants(case_id: str) -> list[dict[str, Any]]:
    """The co-applicants recorded on a LOS case: id, name, relationship (nothing else is read)."""
    try:
        from app.agents.los import co_applicants

        rows = co_applicants.list_for_case(case_id)
    except Exception:  # noqa: BLE001 - not a LOS case / store unreadable: no recorded co-applicants
        return []
    return [{"party": r.get("co_applicant_id"), "name": r.get("name"), "relationship": r.get("relationship")}
            for r in rows if r.get("co_applicant_id")]


def co_applicants_for(case_id: str) -> list[dict[str, Any]]:
    """Every co-applicant the report should speak of: the case's recorded ones, anyone whose statement arrived, and
    the clubbed income's owner -- each with their statement (or None). The clubbed income is the first one's."""
    known = {c["party"]: c for c in _case_store_co_applicants(case_id)}
    for party in store.parties(case_id):
        known.setdefault(party, {"party": party, "name": None, "relationship": None})
    clubbed = _clubbed(case_id)
    people = list(known.values())
    if clubbed.get("income") and not people:
        people = [{"party": None, "name": None, "relationship": clubbed.get("relationship")}]
    for i, co in enumerate(people):
        data = store.load(case_id, co["party"]) if co.get("party") else None
        co["data"] = _finished(data) if data else None
        co["declared_income"] = clubbed.get("income") if i == 0 else None
        co["relationship"] = co.get("relationship") or (clubbed.get("relationship") if i == 0 else None)
    return people


def report_for(case_id: str, applicant_name: str | None = None) -> dict[str, Any]:
    """The repayment report of one case: its eligibility data + every bank statement on file."""
    if not store.valid_case_id(case_id):
        raise RepaymentError("INVALID_CASE_ID", "case_id must look like CASE-<12 hex>")
    bank = store.load(case_id)
    if bank is None:
        raise RepaymentError("NO_BANK_ANALYSIS", "no bank analysis received for this case yet")
    bank = _finished(bank)
    out = report.build(case_id, _eligibility(case_id), bank, applicant_name=applicant_name,
                       co_applicants=co_applicants_for(case_id))
    out["bank_data"] = {"demo": bool(bank.get("demo")), "received_at": bank.get("received_at")}
    return out


__all__ = ["RepaymentError", "co_applicants_for", "receive", "report_for"]
