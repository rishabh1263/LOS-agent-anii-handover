"""
BANK VERIFICATION AGENT -- the agency's report read and checked against the case on screen
(services/report_check.py, config agency_report). Positive and negative cases with synthetic reports
(demo_data/reports.py); the finalize gate; the check-only endpoint. The OCR-read photo is marked `ocr`.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from bank_verification_fixtures import AGENCY, CPA, bv_client, bv_home, bv_repo, bv_template  # noqa: F401 - fixtures

from app.agents.credit.bank_verification.demo_data import reports, scenarios

API = "/api/v1/bank-verification"
CASE = "BV-2026-001"                                   # clean AA success: every check but ownership passes


def _agency(**over) -> dict:
    a = dict(next(s for s in scenarios.SCENARIOS if s["code"] == "CLEAN_AA_SUCCESS")["agency"])
    a.pop("with_report", None)
    return {**a, **over}


def _check(client, report: bytes, *, ownership="CONFIRMED", name="report.pdf", headers=AGENCY, case=CASE):
    body = {"ownership": ownership, "agency_id": _agency()["agency_id"]}
    return client.post(f"{API}/cases/{case}/report/check", data={"request": json.dumps(body)},
                       files={"report": (name, report)}, headers=headers)


def _row(out: dict, field: str) -> dict:
    return next(c for c in out["checks"] + out["comparisons"] if c["field"] == field)


def _finalize(client, report: bytes, **over):
    return client.post(f"{API}/{CASE}/finalize", data={"request": json.dumps(_agency(**over))},
                       files={"report": ("agency_report.pdf", report)}, headers=AGENCY)


# ---- positive --------------------------------------------------------------------------------------------------------
def test_a_correct_report_passes_and_the_case_becomes_positive(bv_client):
    out = _check(bv_client, reports.for_case(CASE)).json()
    assert out["status"] == "PASS", out
    assert all(c["match"] is True for c in out["checks"] + out["comparisons"])
    done = _finalize(bv_client, reports.for_case(CASE)).json()
    assert done["final_status"] == "POSITIVE" and done["report_check"]["status"] == "PASS"


@pytest.mark.parametrize("override", [
    {"account_number": "XXXXXXXXXX4051"},              # masked on the report: its last four digits compared
    {"applicant_name": "Ishaan K Verma"},             # a middle initial
    {"applicant_name": "VERMA ISHAAN"},               # another order, capitals
    {"bank_name": "SBI"},                             # a short name of the same bank
    {"bank_name": None},                              # optional: not printed is fine
])
def test_the_same_facts_written_another_way_still_pass(bv_client, override):
    out = _check(bv_client, reports.for_case(CASE, **override)).json()
    assert out["status"] == "PASS", out["reason"]


def test_a_negative_report_agrees_with_a_negative_decision(bv_client):
    report = reports.for_case("BV-2026-010", ownership="NOT_CONFIRMED")
    body = dict(next(s for s in scenarios.SCENARIOS if s["code"] == "OWNERSHIP_NEGATIVE")["agency"])
    body.pop("with_report", None)
    done = bv_client.post(f"{API}/BV-2026-010/finalize", data={"request": json.dumps(body)},
                          files={"report": ("r.pdf", report)}, headers=AGENCY).json()
    assert done["final_status"] == "NEGATIVE" and done["report_check"]["status"] == "PASS"


@pytest.mark.ocr
def test_a_photographed_report_is_read_by_ocr(bv_client):
    out = _check(bv_client, reports.build_image(reports.fields_for(CASE)), name="report.png").json()
    assert _row(out, "readable")["match"] is True and _row(out, "case_ref")["match"] is True
    assert out["status"] in ("PASS", "REVIEW_REQUIRED")               # OCR may miss a field: never a false FAIL
    assert _row(out, "applicant_name")["match"] is not False


# ---- negative --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("override, field", [
    ({"applicant_name": "Rohan Mehta"}, "applicant_name"),
    ({"account_number": "99901020309999"}, "account_number"),
    ({"bank_name": "HDFC Bank"}, "bank_name"),
    ({"report_date": (date.today() + timedelta(days=5)).strftime("%d-%m-%Y")}, "report_date"),
    ({"report_date": (date.today() - timedelta(days=45)).strftime("%d-%m-%Y")}, "report_date"),
    ({"agency": "Some Other Agency"}, "agency"),
])
def test_a_report_that_disagrees_with_the_case_fails_and_blocks_positive(bv_client, override, field):
    report = reports.for_case(CASE, **override)
    out = _check(bv_client, report).json()
    assert out["status"] == "FAIL" and _row(out, field)["match"] is False, out
    done = _finalize(bv_client, report).json()
    assert done["final_status"] == "REVIEW_REQUIRED"
    assert any("report check" in b.lower() for b in done["blocked_reasons"])


def test_the_report_of_another_case_is_caught(bv_client):
    other = reports.for_case("BV-2026-002")
    out = _check(bv_client, other).json()
    assert out["status"] == "FAIL" and _row(out, "case_ref")["match"] is False
    assert "another case" in _row(out, "case_ref")["note"]


def test_a_report_saying_not_confirmed_against_a_confirmed_selection_fails(bv_client):
    out = _check(bv_client, reports.for_case(CASE, ownership="NOT_CONFIRMED"), ownership="CONFIRMED").json()
    assert out["status"] == "FAIL" and _row(out, "ownership")["match"] is False


@pytest.mark.parametrize("override, field", [
    ({"case_ref": None, "application_ref": None}, "case_ref"),
    ({"account_number": None}, "account_number"),
    ({"report_date": None}, "report_date"),
    ({"account_number": "XXXXXXXXXXXX51"}, "account_number"),       # masked with too few digits to compare
])
def test_a_report_missing_a_required_fact_needs_review(bv_client, override, field):
    out = _check(bv_client, reports.for_case(CASE, **override)).json()
    assert out["status"] == "REVIEW_REQUIRED" and _row(out, field)["match"] is None, out


@pytest.mark.parametrize("content", [b"%PDF-1.4\n not a real pdf", reports.build_pdf({})])
def test_an_unreadable_report_needs_review_and_never_makes_positive(bv_client, content):
    out = _check(bv_client, content).json()
    assert out["status"] == "REVIEW_REQUIRED" and _row(out, "readable")["match"] is None
    assert _finalize(bv_client, content).json()["final_status"] == "REVIEW_REQUIRED"


# ---- the endpoint and the switch -----------------------------------------------------------------------------------------
def test_the_check_stores_nothing_and_is_for_the_agency_only(bv_client, bv_repo):
    before = len(bv_repo.documents(CASE))
    assert _check(bv_client, reports.for_case(CASE)).status_code == 200
    assert len(bv_repo.documents(CASE)) == before
    assert _check(bv_client, reports.for_case(CASE), headers=CPA).status_code == 403
    assert _check(bv_client, b"MZ\x90\x00 exe", name="report.pdf").status_code == 415


def test_the_gate_follows_config(bv_client, monkeypatch):
    from app.agents.credit.bank_verification import config

    monkeypatch.setitem(config.cfg()["agency_report"], "required_for_positive", False)
    done = _finalize(bv_client, reports.for_case(CASE, applicant_name="Rohan Mehta")).json()
    assert done["final_status"] == "POSITIVE" and done["report_check"]["status"] == "FAIL"   # shown, not blocking


# ---- account type and the latest transactions (owner 2026-10-27) ------------------------------------------------------
def _latest_on_screen(client) -> list[str]:
    return reports.fields_for(CASE)["transactions"]


def test_the_reports_account_type_and_transactions_are_compared_with_the_screen(bv_client):
    out = _check(bv_client, reports.for_case(CASE)).json()
    assert _row(out, "account_type")["match"] is True
    rows = [c for c in out["comparisons"] if c["field"].startswith("transaction_")]
    assert len(rows) == 3 and all(c["match"] is True for c in rows) and all(c["right"] for c in rows)


def test_a_report_type_written_another_way_still_matches(bv_client):
    assert _row(_check(bv_client, reports.for_case(CASE, account_type="SB Account")).json(), "account_type")["match"] is True


@pytest.mark.parametrize("change, part", [
    (lambda d, a, s: f"{d} / {float(a.replace(',', '')) + 500:,.2f} / {s}", "amount"),
    (lambda d, a, s: f"{d} / {a} / {'Credit' if s == 'Debit' else 'Debit'}", "debit / credit"),
])
def test_a_report_transaction_that_differs_is_a_mismatch_saying_what(bv_client, change, part):
    rows = _latest_on_screen(bv_client)
    d, a, s = rows[0].split(" / ")
    out = _check(bv_client, reports.for_case(CASE, transactions=[change(d, a, s)] + rows[1:])).json()
    first = _row(out, "transaction_1")
    assert out["status"] == "FAIL" and first["match"] is False and part in first["note"]


def test_a_report_transaction_not_on_screen_and_a_wrong_type_fail(bv_client):
    out = _check(bv_client, reports.for_case(CASE, transactions=["01-01-2020 / 99.00 / Debit"])).json()
    assert _row(out, "transaction_1")["match"] is False and "not among" in _row(out, "transaction_1")["note"]
    out = _check(bv_client, reports.for_case(CASE, account_type="Current")).json()
    assert out["status"] == "FAIL" and _row(out, "account_type")["match"] is False


def test_a_report_without_type_or_transactions_is_still_fine(bv_client):
    out = _check(bv_client, reports.for_case(CASE, account_type=None, transactions=None)).json()
    assert out["status"] == "PASS"
    assert _row(out, "account_type")["match"] is None and _row(out, "transactions")["match"] is None


def test_the_systems_own_report_uploaded_back_is_not_an_agency_report(bv_client):
    """Owner 2026-10-27 uploaded the downloaded verification report as the agency's: caught as the wrong document,
    with no field read out of its sentences."""
    own = bv_client.get(f"{API}/cases/{CASE}/report?format=pdf").content
    out = _check(bv_client, own).json()
    assert out["status"] == "FAIL" and _row(out, "report_kind")["match"] is False
    assert "own verification report" in _row(out, "report_kind")["note"] and out["comparisons"] == []


def test_a_label_inside_a_sentence_is_not_a_value(bv_client):
    fields = reports.fields_for(CASE, applicant_name=None)
    fields["remarks"] = "Customer name on the statement differs from the applicant's name."
    out = _check(bv_client, reports.build_pdf(fields)).json()
    assert _row(out, "applicant_name")["match"] is None and _row(out, "applicant_name")["left"] is None


def test_with_no_transactions_on_the_form_the_report_is_compared_with_the_statements_latest(bv_client):
    """Owner's case 03: the CPA left the rows blank, so the report's rows were all "Mismatch" against nothing."""
    body = {"case_id": "BV-2026-002", "mode": "AA_FAILURE", "statement_source": "ON_FILE",
            "entered": {"bank_name": None, "account_number": None, "account_type": None, "transactions": [],
                        "provenance": {}}}
    assert bv_client.post(f"{API}/process", json=body, headers=CPA).status_code == 200
    out = _check(bv_client, reports.for_case("BV-2026-002"), case="BV-2026-002").json()
    rows = [c for c in out["comparisons"] if c["field"].startswith("transaction_")]
    assert len(rows) == 3 and all(c["match"] is True and c["right"] for c in rows)
