"""
BANK VERIFICATION AGENT -- end-to-end QA of the AA failure / AA success flows through the real HTTP API (2026-10-26
validation pass): every failure scenario the owner listed, with synthetic statements built on the fly, the eight-rule
response contract, masking, and the finalize prerequisites. Each test states the business expectation it proves.
"""

from __future__ import annotations

import copy
import json

import pytest
from bank_verification_fixtures import AGENCY, CPA, bv_client, bv_home, bv_repo, bv_template  # noqa: F401 - fixtures

from app.agents.credit.bank_verification.demo_data import scenarios, statements

API = "/api/v1/bank-verification"
STATUSES = {"PASS", "FAIL", "REVIEW_REQUIRED", "NOT_APPLICABLE"}


def _spec(code: str) -> dict:
    return copy.deepcopy(next(s for s in scenarios.SCENARIOS if s["code"] == code))


def _truth(spec: dict) -> dict:
    """The form exactly as the statement prints it (what a careful CPA types): bank, number, type, latest three."""
    from app.agents.credit.bank_verification.schemas import Provenance, StatementData
    from app.agents.credit.bank_verification.services import transaction_service

    data = StatementData.model_validate({**spec, "transactions": [{**t, "seq": i} for i, t in
                                                                  enumerate(spec["transactions"])]})
    return transaction_service.prefill(data, Provenance.MANUAL).model_dump(mode="json")


def _upload(client, case_id: str, content: bytes, entered: dict, *, name="statement.pdf", mode="AA_FAILURE"):
    body = {"case_id": case_id, "mode": mode, "statement_source": "UPLOAD", "entered": entered}
    return client.post(f"{API}/process", data={"request": json.dumps(body)}, files={"statement": (name, content)},
                       headers=CPA)


def _rules(body: dict) -> dict:
    return {r["rule_id"]: r for r in body["rules"]}


def _row(rule: dict, field: str) -> dict:
    return next(c for c in rule["comparisons"] if c["field"] == field)


def _agency(code: str, **over) -> dict:
    a = dict(_spec(code)["agency"])
    a.pop("with_report", None)
    return {**a, **over}


def _finalize(client, case_id: str, payload: dict, report: bytes | None | str = "auto"):
    if report == "auto":
        from app.agents.credit.bank_verification.demo_data import reports

        report = reports.for_case(case_id, ownership=payload.get("ownership", "CONFIRMED"),
                                  agency_id=payload.get("agency_id"))
    if report is None:
        return client.post(f"{API}/{case_id}/finalize", json=payload, headers=AGENCY)
    return client.post(f"{API}/{case_id}/finalize", data={"request": json.dumps(payload)},
                       files={"report": ("agency_report.pdf", report)}, headers=AGENCY)


# ---- the AA failure flow, end to end -----------------------------------------------------------------------------------
def test_aa_failure_end_to_end_application_prefill_upload_compare_and_positive(bv_client):
    """Open -> bank + account from the application -> CPA types type + 3 rows -> upload -> run: every value is compared
    with what the agent READ from the uploaded file; then the agency's evidence + report make it POSITIVE."""
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    opened = bv_client.get(f"{API}/cases/BV-2026-002").json()
    assert opened["latest_run"] is None and set(opened["prefill"]["provenance"].values()) == {"APPLICATION"}
    spec = _spec("CLEAN_AA_FAILURE")["statement"]["spec"]
    truth = _truth(spec)
    entered = {**opened["prefill"], "account_type": truth["account_type"], "transactions": truth["transactions"]}
    r = _upload(bv_client, "BV-2026-002", statements.build_pdf(spec), entered)
    assert r.status_code == 200, r.text
    body = r.json()
    rules = _rules(body)
    # the statement was READ (not just stored): its own values are on the right of every comparison
    assert body["sources"]["statement_file"]["extraction_status"] == "COMPLETE"
    assert body["sources"]["statement"]["transactions"], "the uploaded file's transactions were extracted"
    assert rules["R1"]["status"] == "PASS" and rules["R2"]["status"] == "PASS"
    assert all(c["right"] for c in rules["R2"]["comparisons"])
    assert rules["R6"]["status"] == "NOT_APPLICABLE"                  # no AA data in AA failure
    assert rules["R4"]["status"] == "REVIEW_REQUIRED" and body["overall_status"] == "REVIEW_REQUIRED"
    assert body["form"]["provenance"]["account_number"] == "APPLICATION"
    out = _finalize(bv_client, "BV-2026-002", _agency("CLEAN_AA_FAILURE"))
    assert out.status_code == 200, out.text
    assert out.json()["final_status"] == "POSITIVE" and out.json()["report_document_id"]


# ---- the failure scenarios ---------------------------------------------------------------------------------------------
def _case04():
    spec = _spec("LATEST_TXN_MISMATCH")["statement"]["spec"]
    return spec, _truth(spec)


def _index_of(rows: list[dict], amount: str) -> int:
    return next(i for i, t in enumerate(rows) if float(t["amount"]) == float(amount))


def test_amount_5000_on_the_statement_typed_as_5500_is_an_amount_mismatch(bv_client):
    spec, entered = _case04()
    i = _index_of(entered["transactions"], "5000")
    entered["transactions"][i]["amount"] = "5500.00"
    rules = _rules(_upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json())
    row = _row(rules["R2"], f"transaction_{i + 1}")
    assert rules["R2"]["status"] == "FAIL" and row["match"] is False
    assert "₹5,500.00" in row["left"] and "₹5,000.00" in row["right"] and "amount" in row["note"]


def test_a_debit_typed_as_credit_is_a_debit_credit_mismatch(bv_client):
    spec, entered = _case04()
    i = _index_of(entered["transactions"], "5000")
    entered["transactions"][i]["direction"] = "CREDIT"
    rules = _rules(_upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json())
    row = _row(rules["R2"], f"transaction_{i + 1}")
    assert rules["R2"]["status"] == "FAIL" and row["match"] is False and "debit / credit" in row["note"]
    assert "Credit" in row["left"] and "Debit" in row["right"]


def test_sbi_typed_for_a_state_bank_of_india_statement_matches(bv_client):
    spec = _spec("CLEAN_AA_SUCCESS")["statement"]["spec"]
    assert spec["details"]["bank_name"] == "State Bank of India"
    entered = {**_truth(spec), "bank_name": "SBI"}
    rules = _rules(_upload(bv_client, "BV-2026-002", statements.build_pdf(spec), entered).json())
    assert _row(rules["R1"], "bank_name")["match"] is True


def test_a_different_account_number_fails_and_a_masked_one_is_never_a_full_match(bv_client):
    spec, entered = _case04()
    entered["account_number"] = entered["account_number"][:-1] + ("0" if entered["account_number"][-1] != "0" else "1")
    rules = _rules(_upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json())
    assert rules["R1"]["status"] == "FAIL" and _row(rules["R1"], "account_number")["match"] is False
    # the statement prints only the last four digits: the right number can only be partly confirmed -> REVIEW
    spec, entered = _case04()
    full = spec["details"]["account_number"]
    spec["details"]["account_number"] = "X" * (len(full) - 4) + full[-4:]
    rules = _rules(_upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json())
    number = _row(rules["R1"], "account_number")
    assert number["match"] is None and rules["R1"]["status"] == "REVIEW_REQUIRED"


def _encrypted(content: bytes) -> bytes:
    import pymupdf

    doc = pymupdf.open(stream=content, filetype="pdf")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u-secret", owner_pw="o-secret")


@pytest.mark.parametrize("kind", ["scan_without_ocr", "corrupted", "password"])
def test_an_unreadable_statement_is_honest_and_never_passes(bv_client, kind):
    spec, entered = _case04()
    content = {"scan_without_ocr": lambda: statements.build_scanned_pdf(spec),
               "corrupted": lambda: b"%PDF-1.4\n not a real pdf body",
               "password": lambda: _encrypted(statements.build_pdf(spec))}[kind]()
    body = _upload(bv_client, "BV-2026-004", content, entered).json()
    rules = _rules(body)
    assert body["sources"]["statement_file"]["extraction_status"] == "UNREADABLE"
    assert rules["R1"]["status"] == rules["R2"]["status"] == rules["R3"]["status"] == "REVIEW_REQUIRED"
    assert body["overall_status"] != "PASS" and body["recommended_action"] == "REQUEST_READABLE_STATEMENT"


def test_rows_lost_at_the_end_of_a_statement_are_insufficient_evidence_not_a_pass(bv_client, monkeypatch):
    """The reader drops the newest rows (a last page lost): the older rows must not be taken as "the latest three"."""
    from app.agents.credit.bank_verification.services import statement_service

    real = statement_service._extract

    def last_page_lost(content):
        result = real(content)
        newest = max(t.date for t in result.transactions)
        result.transactions = [t for t in result.transactions if t.date != newest]
        return result

    monkeypatch.setattr(statement_service, "_extract", last_page_lost)
    spec, _ = _case04()
    from app.agents.credit.bank_verification.schemas import StatementData
    from app.agents.credit.bank_verification.services import transaction_service

    newest = max(t["date"] for t in spec["transactions"])
    shorter = {**spec, "transactions": [t for t in spec["transactions"] if t["date"] != newest]}
    typed_from_what_was_read = _truth(shorter)
    body = _upload(bv_client, "BV-2026-004", statements.build_pdf(spec), typed_from_what_was_read).json()
    r2 = _rules(body)["R2"]
    assert r2["status"] == "REVIEW_REQUIRED" and all(c["match"] is None for c in r2["comparisons"])
    assert "end of the statement" in r2["reason"]
    assert StatementData and transaction_service                      # imported for the reader's types


def test_a_long_statement_takes_the_latest_three_by_date(bv_client):
    """Many rows, printed newest-first: the latest three are chosen by date, not by print position."""
    from datetime import date
    from decimal import Decimal

    spec, entered = _case04()
    days = [date(2026, month, day) for month in range(1, 7) for day in (3, 11, 19, 27)]
    step = Decimal("100.00")
    balance = spec["opening_balance"] + step * len(days)          # the older rows end at the old opening balance
    opening, extra = balance, []
    for i, day in enumerate(days):
        balance -= step
        extra.append({"date": day, "narration": "UPI SMALL", "amount": step, "direction": "DEBIT",
                      "reference": f"TXNQA{i:04d}", "balance": balance})
    spec["transactions"] = extra + spec["transactions"]
    spec["opening_balance"] = opening
    body = _upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json()
    assert len(body["sources"]["statement"]["transactions"]) >= len(extra) + 3
    assert _rules(body)["R2"]["status"] == "PASS"


@pytest.mark.parametrize("case_id, rule, expected", [
    ("BV-2026-006", "R5", "FAIL"),                 # BALANCE_INCONSISTENCY: the running balance breaks
    ("BV-2026-008", "R7", "REVIEW_REQUIRED"),      # DUPLICATE_STATEMENT: the same file in another case
    ("BV-2026-012", "R6", "FAIL"),                 # AA_STATEMENT_MISMATCH: AA success, AA vs statement differ
    ("BV-2026-001", "R6", "PASS"),                 # CLEAN_AA_SUCCESS: AA and statement agree
])
def test_seeded_scenarios_raise_the_right_rule(bv_client, case_id, rule, expected):
    run = bv_client.get(f"{API}/cases/{case_id}").json()["latest_run"]
    assert _rules(run)[rule]["status"] == expected


def test_aa_success_compares_aa_with_the_statement_field_by_field(bv_client):
    run = bv_client.get(f"{API}/cases/BV-2026-012").json()["latest_run"]
    r6 = _rules(run)["R6"]
    differs = [c for c in r6["comparisons"] if c["match"] is False]
    assert differs and all(c["left"] and c["right"] for c in differs)
    assert any(c["field"] == "account_holder" for c in differs)


# ---- finalize prerequisites ---------------------------------------------------------------------------------------------
def test_ownership_pending_is_never_positive(bv_client):
    out = _finalize(bv_client, "BV-2026-009", _agency("OWNERSHIP_PENDING"))
    assert out.status_code == 200 and out.json()["final_status"] == "REVIEW_REQUIRED"


def test_everything_confirmed_but_no_report_is_rejected(bv_client):
    payload = _agency("CLEAN_AA_SUCCESS")
    assert payload["ownership"] == "CONFIRMED"
    out = _finalize(bv_client, "BV-2026-001", payload, report=None)
    assert out.status_code == 422 and out.json()["error"]["code"] == "REPORT_REQUIRED"
    assert bv_client.get(f"{API}/cases/BV-2026-001").json()["case"]["status"] not in ("POSITIVE", "NEGATIVE")


def test_everything_present_follows_the_business_rules(bv_client):
    out = _finalize(bv_client, "BV-2026-001", _agency("CLEAN_AA_SUCCESS"))
    assert out.status_code == 200 and out.json()["final_status"] == "POSITIVE" and out.json()["blocked_reasons"] == []


# ---- the eight-rule contract, masking, errors ----------------------------------------------------------------------------
@pytest.mark.parametrize("case_id", ["BV-2026-001", "BV-2026-002", "BV-2026-005"])
def test_every_run_carries_all_eight_rules_with_the_full_contract(bv_client, case_id):
    run = bv_client.get(f"{API}/cases/{case_id}").json()["latest_run"]
    assert [r["rule_id"] for r in run["rules"]] == [f"R{i}" for i in range(1, 9)]
    for r in run["rules"]:
        assert r["rule_name"] and r["status"] in STATUSES and r["reason"]
        assert "recommended_action" in r and "evidence" in r and "comparisons" in r
        if r["status"] in ("FAIL", "REVIEW_REQUIRED") and r["rule_id"] != "R8":
            assert r["recommended_action"], r["rule_id"]
    if run["mode"] == "AA_FAILURE":
        assert _rules(run)["R6"]["status"] == "NOT_APPLICABLE" and _rules(run)["R6"]["reason"]


def test_full_account_numbers_never_leave_the_api(bv_client):
    spec, entered = _case04()
    full = spec["details"]["account_number"]
    r = _upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered)
    assert r.status_code == 200 and full not in r.text
    for path in ("/cases/BV-2026-004", "/cases/BV-2026-004/history", "/cases/BV-2026-004/summary",
                 "/cases/BV-2026-004/report?format=html"):
        assert full not in bv_client.get(f"{API}{path}").text, path


def test_an_internal_error_is_a_clean_message_without_a_stack_trace(bv_client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.agents.credit.bank_verification.main import create_app
    from app.agents.credit.bank_verification.services import statement_service

    def boom(*a, **k):
        raise RuntimeError("secret path C:/internal/db 99904456677881")

    monkeypatch.setattr(statement_service, "read", boom)
    spec, entered = _case04()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        r = _upload(client, "BV-2026-004", statements.build_pdf(spec), entered)
    assert r.status_code == 500
    assert "Traceback" not in r.text and "secret" not in r.text and "99904456677881" not in r.text


def test_every_rule_result_is_persisted_with_its_run(bv_client, bv_repo):
    spec, entered = _case04()
    run_id = _upload(bv_client, "BV-2026-004", statements.build_pdf(spec), entered).json()["run_id"]
    with bv_repo._db() as db:                                        # the store as it is: SQLite or PostgreSQL
        stored = [(r["rule_id"], r["status"]) for r in
                  db.execute("SELECT rule_id, status FROM rule_results WHERE run_id = ? ORDER BY rule_id", (run_id,))]
    assert [r[0] for r in stored] == [f"R{i}" for i in range(1, 9)] and all(s in STATUSES for _, s in stored)


# ---- option B: the statement fills the form, the CPA checks each value --------------------------------------------------
def _read(client, case_id: str, content: bytes, name="statement.pdf"):
    return client.post(f"{API}/cases/{case_id}/statement", data={"request": "{}"}, files={"statement": (name, content)},
                       headers=CPA)


def test_on_upload_the_statement_fills_type_and_the_latest_three_tagged_from_statement(bv_client):
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, truth = _case04()
    r = _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "READ" and out["extraction_status"] == "COMPLETE" and out["transactions_read"] >= 3
    filled = out["filled"]
    assert filled["account_type"] == truth["account_type"]
    assert [t["amount"] for t in filled["transactions"]] == [t["amount"] for t in truth["transactions"]]
    assert {filled["provenance"][k] for k in ("account_type", "transaction_1", "transaction_2", "transaction_3")} \
        == {"EXTRACTED"}
    # the case screen opens with the application's account + what the statement filled
    prefill = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    assert prefill["provenance"]["account_number"] == "APPLICATION"
    assert prefill["provenance"]["transaction_1"] == "EXTRACTED" and len(prefill["transactions"]) == 3


def test_values_the_cpa_did_not_check_are_never_a_match(bv_client):
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, _ = _case04()
    _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    form = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    body = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-004", "mode": "AA_FAILURE",
                                                  "statement_source": "ON_FILE", "entered": form}, headers=CPA).json()
    rules = _rules(body)
    assert rules["R2"]["status"] == "REVIEW_REQUIRED"
    assert all(c["match"] is None and "not yet checked" in c["note"] for c in rules["R2"]["comparisons"])
    assert _row(rules["R1"], "account_type")["match"] is None
    assert _row(rules["R1"], "account_number")["match"] is True        # the application's number is still compared


def test_checked_values_match_and_say_so_and_the_file_is_read_once(bv_client, bv_repo):
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, _ = _case04()
    _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    form = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    form["provenance"] = {k: ("CONFIRMED" if v == "EXTRACTED" else v) for k, v in form["provenance"].items()}
    body = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-004", "mode": "AA_FAILURE",
                                                  "statement_source": "ON_FILE", "entered": form}, headers=CPA).json()
    rules = _rules(body)
    assert rules["R1"]["status"] == "PASS" and rules["R2"]["status"] == "PASS"
    assert all("checked by the CPA" in c["note"] for c in rules["R2"]["comparisons"])
    with bv_repo._db() as db:
        reads = db.execute("SELECT count(*) AS n FROM extractions WHERE case_id = 'BV-2026-004'").fetchone()["n"]
    assert reads == 1                                                  # read at upload, reused by the run


def test_a_value_the_cpa_corrected_is_compared_as_typed(bv_client):
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, _ = _case04()
    _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    form = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    form["provenance"] = {k: ("CONFIRMED" if v == "EXTRACTED" else v) for k, v in form["provenance"].items()}
    form["transactions"][0]["amount"] = "9.99"                          # changed by the CPA
    form["provenance"]["transaction_1"] = "MANUAL"
    body = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-004", "mode": "AA_FAILURE",
                                                  "statement_source": "ON_FILE", "entered": form}, headers=CPA).json()
    assert _rules(body)["R2"]["status"] == "FAIL"


def test_an_unreadable_upload_fills_nothing_and_says_so(bv_client):
    spec, _ = _case04()
    out = _read(bv_client, "BV-2026-004", statements.build_scanned_pdf(spec)).json()   # OCR is off in the tests
    assert out["status"] == "READ" and out["extraction_status"] == "UNREADABLE" and out["filled"] is None
    assert _read(bv_client, "BV-2026-004", b"\x89PNG\r\n\x1a\n").status_code == 415


def test_a_long_upload_is_read_in_the_background_then_fills_the_form(bv_client, monkeypatch):
    from app.agents.credit.bank_verification import config
    from app.agents.credit.bank_verification.services import verification_service

    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    monkeypatch.setitem(config.cfg()["background"], "over_pages", 0)
    spec, _ = _case04()
    r = _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    assert r.status_code == 202 and r.json()["status"] == "READING"
    verification_service.wait_background()
    detail = bv_client.get(f"{API}/cases/BV-2026-004").json()
    assert detail["case"]["status"] != "READING" and len(detail["prefill"]["transactions"]) == 3


def test_the_claim_comes_from_the_application_and_the_statement_only_fills_a_missing_claim(bv_client, monkeypatch):
    """Bank + account number are the customer's claim (application); the statement is the evidence. With an application
    account the statement never replaces it; without one (LOS today) the statement fills it, to be checked."""
    from app.agents.credit.bank_verification import config

    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, _ = _case04()
    _read(bv_client, "BV-2026-004", statements.build_pdf(spec))
    claim = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    assert claim["provenance"]["bank_name"] == claim["provenance"]["account_number"] == "APPLICATION"
    assert claim["provenance"]["account_type"] == "EXTRACTED"
    monkeypatch.setitem(config.cfg()["application_details"], "enabled", False)   # no account on the application
    fallback = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    assert fallback["provenance"]["bank_name"] == fallback["provenance"]["account_number"] == "EXTRACTED"
    assert fallback["bank_name"] == "Axis Bank" and "X" in fallback["account_number"]   # masked on screen
    body = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-004", "mode": "AA_FAILURE",
                                                  "statement_source": "ON_FILE", "entered": fallback}, headers=CPA).json()
    number = _row(_rules(body)["R1"], "account_number")
    assert number["match"] is None and "not yet checked" in number["note"]       # unchecked: never a match


def test_the_upload_warns_when_the_latest_rows_filled_may_not_be_the_latest(bv_client, monkeypatch):
    """The newest rows lost by the read (owner 2026-10-27, a real scan): the read says so, the screen warns the CPA."""
    from app.agents.credit.bank_verification.services import statement_service

    real = statement_service._extract

    def last_rows_lost(content):
        result = real(content)
        newest = max(t.date for t in result.transactions)
        result.transactions = [t for t in result.transactions if t.date != newest]
        return result

    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    spec, _ = _case04()
    assert _read(bv_client, "BV-2026-004", statements.build_pdf(spec)).json()["latest_uncertain"] is False
    monkeypatch.setattr(statement_service, "_extract", last_rows_lost)
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    assert _read(bv_client, "BV-2026-004", statements.build_pdf(spec)).json()["latest_uncertain"] is True
