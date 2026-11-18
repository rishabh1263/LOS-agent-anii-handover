"""
BANK VERIFICATION AGENT -- the HTTP API (app/agents/credit/bank_verification/api.py) through the real FastAPI app:
the two business APIs, uploads (PDF / CSV / scan / damaged / wrong type / too large), roles, state transitions,
idempotency, the report, the easy-words summary and the read-only endpoints. Synthetic data; the AI summary is off.
"""

from __future__ import annotations

import json

import pytest
from bank_verification_fixtures import AGENCY, CPA, bv_client, bv_home, bv_repo, bv_template  # noqa: F401 - fixtures

from app.agents.credit.bank_verification.demo_data import scenarios, statements

API = "/api/v1/bank-verification"


def _spec(code: str) -> dict:
    return next(s for s in scenarios.SCENARIOS if s["code"] == code)


def _form(code: str) -> dict:
    from app.agents.credit.bank_verification.demo_data.seed import form_for
    from app.agents.credit.bank_verification.schemas import Provenance, StatementData
    from app.agents.credit.bank_verification.services import transaction_service

    s = _spec(code)
    found = form_for({**s, "form": {**s["form"], "manual": True}})
    if found is None:
        src = s["statement"]["spec"]
        data = StatementData.model_validate({**src, "transactions": [{**t, "seq": i} for i, t in
                                                                     enumerate(src["transactions"])]})
        found = transaction_service.prefill(data, Provenance.MANUAL).model_dump(mode="json")
    return found


def _upload(client, case_id: str, content: bytes, name: str, *, mode="AA_FAILURE", entered=None, headers=CPA,
            key: str | None = None):
    body = {"case_id": case_id, "mode": mode, "statement_source": "UPLOAD"}
    if entered is not None:
        body["entered"] = entered
    return client.post(f"{API}/process", data={"request": json.dumps(body)}, files={"statement": (name, content)},
                       headers={**headers, **({"Idempotency-Key": key} if key else {})})


def _agency_input(code: str) -> dict:
    a = dict(_spec(code)["agency"])
    a.pop("with_report", None)
    return a


def _finalize(client, case_id: str, payload: dict, report: bytes | None | str = "auto", headers=AGENCY):
    if report == "auto":                                 # the agency's report for this case, saying what it selected
        from app.agents.credit.bank_verification.demo_data import reports

        report = reports.for_case(case_id, ownership=payload.get("ownership", "CONFIRMED"),
                                  agency_id=payload.get("agency_id"))
    if report is None:                                   # no file: a plain JSON body
        return client.post(f"{API}/{case_id}/finalize", json=payload, headers=headers)
    return client.post(f"{API}/{case_id}/finalize", data={"request": json.dumps(payload)},
                       files={"report": ("agency_report.pdf", report)}, headers=headers)


def _rules(body: dict) -> dict:
    return {r["rule_id"]: r["status"] for r in body["rules"]}


# ---- API 1: process -------------------------------------------------------------------------------------------------
def test_aa_success_with_the_seeded_statement_returns_all_eight_rules(bv_client):
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS",
                                               "statement_source": "SEEDED"}, headers=CPA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processing_status"] == "COMPLETED" and body["source"] == "AA" and body["final_status"] is None
    assert [x["rule_id"] for x in body["rules"]] == [f"R{i}" for i in range(1, 9)]
    assert body["recommended_action"] == "AWAIT_OWNERSHIP_CONFIRMATION"
    assert "99901020304051" not in r.text                         # the account number never leaves in full


def test_aa_failure_uploaded_pdf_is_read_and_verified(bv_client):
    s = _spec("CLEAN_AA_FAILURE")
    r = _upload(bv_client, "BV-2026-002", statements.build_pdf(s["statement"]["spec"]), "my_statement.pdf",
                entered=_form("CLEAN_AA_FAILURE"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert _rules(body)["R1"] == "PASS" and _rules(body)["R2"] == "PASS" and _rules(body)["R6"] == "NOT_APPLICABLE"
    assert body["sources"]["statement_file"]["extraction_status"] == "COMPLETE"


def test_csv_statement_is_supported(bv_client):
    s = _spec("CLEAN_AA_FAILURE")
    r = _upload(bv_client, "BV-2026-002", statements.build_csv(s["statement"]["spec"]), "statement.csv",
                entered=_form("CLEAN_AA_FAILURE"))
    assert r.status_code == 200 and _rules(r.json())["R5"] == "PASS"


def test_a_scan_is_reported_unreadable_and_never_filled_with_demo_data(bv_client):
    s = _spec("CLEAN_AA_FAILURE")
    r = _upload(bv_client, "BV-2026-002", statements.build_scanned_pdf(s["statement"]["spec"]), "scan.pdf",
                entered={"bank_name": "Harbourline Bank"})
    body = r.json()
    assert r.status_code == 200 and body["sources"]["statement"] is None
    assert body["sources"]["statement_file"]["extraction_status"] == "UNREADABLE"
    assert _rules(body)["R3"] == "REVIEW_REQUIRED" and body["recommended_action"] == "REQUEST_READABLE_STATEMENT"


def test_a_damaged_pdf_is_reported_not_crashed(bv_client):
    r = _upload(bv_client, "BV-2026-002", b"%PDF-1.4\n garbage that is not a pdf body", "broken.pdf")
    assert r.status_code == 200 and r.json()["sources"]["statement_file"]["extraction_status"] == "UNREADABLE"


@pytest.mark.parametrize("content, name, status, code", [
    (b"MZ\x90\x00 an executable", "statement.pdf", 415, "UNSUPPORTED_FILE_TYPE"),
    (b"\x89PNG\r\n\x1a\n image", "statement.png", 415, "UNSUPPORTED_FILE_TYPE"),
    (b"date,debit,credit\n", "statement.pdf", 415, "FILE_TYPE_MISMATCH"),
    (b"", "statement.pdf", 422, "EMPTY_FILE"),
])
def test_invalid_uploads_are_refused_with_a_clear_code(bv_client, content, name, status, code):
    r = _upload(bv_client, "BV-2026-002", content, name)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_an_oversized_upload_is_refused(bv_client, monkeypatch):
    from app.agents.credit.bank_verification import config

    monkeypatch.setitem(config.cfg()["uploads"], "max_bytes", 1000)
    r = _upload(bv_client, "BV-2026-002", b"%PDF-1.4\n" + b"0" * 2000, "big.pdf")
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_validation_errors_share_one_shape(bv_client):
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "MAYBE"}, headers=CPA)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR" and r.json()["error"]["details"]
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS", "x": 1}, headers=CPA)
    assert r.status_code == 422
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-NOPE-1", "mode": "AA_SUCCESS"}, headers=CPA)
    assert r.status_code == 404 and r.json()["error"]["code"] == "CASE_NOT_FOUND"


def test_aa_success_without_aa_data_is_refused(bv_client):
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-002", "mode": "AA_SUCCESS"}, headers=CPA)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_AA_DATA"


def test_edit_details_and_rerun(bv_client):
    """The CPA corrects the typo that failed R1 (BV-2026-003) and re-runs: R1 passes."""
    before = bv_client.get(f"{API}/cases/BV-2026-003", headers=CPA).json()["latest_run"]
    assert _rules(before)["R1"] == "FAIL"
    fixed = _form("BANK_ACCOUNT_MISMATCH")
    fixed["account_number"] = _spec("BANK_ACCOUNT_MISMATCH")["statement"]["spec"]["details"]["account_number"]
    fixed["account_type"] = "Savings"
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-003", "mode": "AA_FAILURE", "entered": fixed},
                       headers=CPA)
    assert r.status_code == 200 and _rules(r.json())["R1"] == "PASS" and r.json()["run_id"] != before["run_id"]


# ---- idempotency -----------------------------------------------------------------------------------------------------
def test_the_same_idempotency_key_replays_and_a_different_request_conflicts(bv_client):
    pdf = statements.build_pdf(_spec("CLEAN_AA_FAILURE")["statement"]["spec"])
    first = _upload(bv_client, "BV-2026-002", pdf, "s.pdf", entered=_form("CLEAN_AA_FAILURE"), key="k-1")
    again = _upload(bv_client, "BV-2026-002", pdf, "s.pdf", entered=_form("CLEAN_AA_FAILURE"), key="k-1")
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] is True and again.json()["run_id"] == first.json()["run_id"]
    other = _upload(bv_client, "BV-2026-002", pdf, "s.pdf", entered={"bank_name": "Other"}, key="k-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_the_same_inputs_without_a_key_do_not_make_a_second_run(bv_client):
    body = {"case_id": "BV-2026-001", "mode": "AA_SUCCESS"}
    a = bv_client.post(f"{API}/process", json=body, headers=CPA).json()
    b = bv_client.post(f"{API}/process", json=body, headers=CPA).json()
    assert b["replayed"] is True and a["run_id"] == b["run_id"]


# ---- roles -----------------------------------------------------------------------------------------------------------
def test_a_cpa_cannot_finalize_and_an_agency_cannot_process(bv_client):
    r = _finalize(bv_client, "BV-2026-001", _agency_input("CLEAN_AA_SUCCESS"), headers=CPA)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_FOR_ROLE"
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS"}, headers=AGENCY)
    assert r.status_code == 403
    r = bv_client.get(f"{API}/cases", headers={"X-Demo-Role": "ROOT"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "UNKNOWN_ROLE"


# ---- API 2: finalize -------------------------------------------------------------------------------------------------
def test_finalize_positive_records_the_decision_and_the_report(bv_client):
    r = _finalize(bv_client, "BV-2026-001", _agency_input("CLEAN_AA_SUCCESS"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final_status"] == "POSITIVE" and body["is_final"] and body["report_document_id"]
    assert {x["rule_id"]: x["status"] for x in body["rules"]}["R4"] == "PASS"     # re-run with the branch outcome
    case = bv_client.get(f"{API}/cases/BV-2026-001", headers=CPA).json()
    assert case["case"]["status"] == "POSITIVE" and any(d["role"] == "REPORT" for d in case["documents"])


def test_finalize_without_the_report_is_refused(bv_client):
    r = _finalize(bv_client, "BV-2026-011", _agency_input("MISSING_REPORT"), report=None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "REPORT_REQUIRED"


def test_finalize_with_missing_checklist_or_short_rationale_is_refused(bv_client):
    payload = {**_agency_input("CLEAN_AA_SUCCESS"), "rationale": "ok", "checklist": {"bank_name": "VERIFIED"}}
    r = _finalize(bv_client, "BV-2026-001", payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "MANDATORY_EVIDENCE_MISSING"
    assert len(r.json()["error"]["details"]["missing"]) == 2


def test_negative_ownership_finalizes_negative_and_then_the_case_is_closed(bv_client):
    r = _finalize(bv_client, "BV-2026-010", _agency_input("OWNERSHIP_NEGATIVE"))
    assert r.status_code == 200 and r.json()["final_status"] == "NEGATIVE"
    again = _finalize(bv_client, "BV-2026-010", {**_agency_input("OWNERSHIP_NEGATIVE"), "rationale": "changed " * 5})
    assert again.status_code == 409 and again.json()["error"]["code"] == "CASE_FINALIZED"
    reprocess = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-010", "mode": "AA_SUCCESS"}, headers=CPA)
    assert reprocess.status_code == 409


def test_finalize_before_any_run_is_refused(bv_client, bv_home):
    from app.agents.credit.bank_verification.repository import Repository

    Repository(bv_home).put_case({"case_id": "BV-NEW-1", "application_ref": "LEV-X", "applicant_name": "Test",
                                  "loan_amount": "1", "product": "PERSONAL_LOAN", "stage": "CPA",
                                  "mode": "AA_FAILURE", "status": "PENDING_VERIFICATION"})
    r = _finalize(bv_client, "BV-NEW-1", _agency_input("CLEAN_AA_SUCCESS"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_PROCESSED"


def test_a_wrong_agency_is_refused(bv_client):
    bv_client.post(f"{API}/process", json={"case_id": "BV-2026-002", "mode": "AA_FAILURE", "statement_source": "SEEDED",
                                           "entered": _form("CLEAN_AA_FAILURE"), "agency_id": "AGY-TRUEVISIT"},
                   headers=CPA)
    r = _finalize(bv_client, "BV-2026-002", _agency_input("CLEAN_AA_FAILURE"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "AGENCY_NOT_ASSIGNED"
    assert "TrueVisit Agency" in r.json()["error"]["message"] and "AGY-" not in r.json()["error"]["message"]


# ---- report, summary, read-only ----------------------------------------------------------------------------------------
def test_report_downloads_as_pdf_and_html(bv_client):
    pdf = bv_client.get(f"{API}/cases/BV-2026-012/report", headers=CPA)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and "attachment" in pdf.headers["content-disposition"]
    page = bv_client.get(f"{API}/cases/BV-2026-012/report?format=html", headers=CPA)
    assert page.status_code == 200 and "Automated Risk Assessment" in page.text
    for hidden in ("DEMO", "demo", "ynthetic", "imulated", "Leviosa", "RUN-", ">R1<"):
        assert hidden not in page.text, hidden                    # nothing technical / demo-ish in the report
    assert "99901223445566" not in page.text


def test_summary_covers_all_eight_checks_in_easy_words_without_the_model(bv_client):
    r = bv_client.get(f"{API}/cases/BV-2026-012/summary", headers=CPA)
    body = r.json()
    assert r.status_code == 200 and body["source"] == "rules" and body["pending"] is False
    assert [c["rule_id"] for c in body["checklist"]] == [f"R{i}" for i in range(1, 9)]
    assert all(c["text"] and c["status_label"] for c in body["checklist"])
    assert "Pooja Deshmukh" in body["text"]


def test_dashboard_summary_and_filters(bv_client):
    summary = bv_client.get(f"{API}/summary", headers=CPA).json()
    assert summary["total"] == 12 and summary["by_source"] == {"AA_SUCCESS": 6, "AA_FAILURE": 6}
    failures = bv_client.get(f"{API}/cases?source=AA_FAILURE", headers=CPA).json()["cases"]
    assert len(failures) == 6 and all(c["mode"] == "AA_FAILURE" for c in failures)
    assert bv_client.get(f"{API}/cases?q=pooja", headers=CPA).json()["cases"][0]["case_id"] == "BV-2026-012"
    assert all(c["risk_level"] == "HIGH" for c in bv_client.get(f"{API}/cases?risk=HIGH", headers=CPA).json()["cases"])


def test_documents_history_and_demo_reset(bv_client):
    case = bv_client.get(f"{API}/cases/BV-2026-001", headers=CPA).json()
    doc = next(d for d in case["documents"] if d["role"] == "STATEMENT")
    f = bv_client.get(f"{API}/cases/BV-2026-001/documents/{doc['document_id']}", headers=CPA)
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    assert bv_client.get(f"{API}/cases/BV-2026-002/documents/{doc['document_id']}", headers=CPA).status_code == 404
    assert bv_client.get(f"{API}/cases/BV-2026-001/history", headers=CPA).json()["events"]
    _finalize(bv_client, "BV-2026-001", _agency_input("CLEAN_AA_SUCCESS"))
    reset = bv_client.post(f"{API}/demo/reset", headers=CPA)
    assert reset.status_code == 200 and reset.json()["cases"] == 12
    assert bv_client.get(f"{API}/cases/BV-2026-001", headers=CPA).json()["case"]["status"] != "POSITIVE"


def test_search_finds_one_case_by_its_exact_number_never_a_partial(bv_client):
    assert bv_client.get(f"{API}/lookup?ref=APL-2026-10401").json() == {"case_id": "BV-2026-001"}
    assert bv_client.get(f"{API}/lookup?ref= bv-2026-012 ").json() == {"case_id": "BV-2026-012"}
    assert bv_client.get(f"{API}/lookup?ref=APL-2026").status_code == 404          # no partial match, no list
    assert bv_client.get(f"{API}/lookup?ref=BV-2025-101").status_code == 404       # closed earlier cases stay out


def test_without_a_role_header_one_user_can_run_and_decide(bv_client):
    """The screen sends no role: the configured single user runs the check and records the agency decision."""
    assert bv_client.post(f"{API}/process", json={"case_id": "BV-2026-009", "mode": "AA_SUCCESS"}).status_code == 200
    r = _finalize(bv_client, "BV-2026-009", _agency_input("OWNERSHIP_PENDING"), headers={})
    assert r.status_code == 200 and r.json()["final_status"] == "REVIEW_REQUIRED"


def test_a_los_case_opens_by_case_or_applicant_id_with_its_bank_statement(bv_client, monkeypatch):
    """The search opens a case of the LOS (fake LOS store here): its header, and its bank statement as on file."""
    from types import SimpleNamespace as NS

    from app.agents.credit.bank_verification.services import los_link

    pdf = statements.build_pdf(_spec("CLEAN_AA_FAILURE")["statement"]["spec"])
    app = NS(case_id="CASE-ABC123DEF456", applicant_id="APP-0011223344AA", loan_amount="275000",
             product="PERSONAL_LOAN", created_at="2026-10-26")
    doc = NS(document_id="DOC-LOS-1", document_type="BANK_STATEMENT", status=NS(value="VERIFIED"), uploaded_at="2026-10-26")

    class FakeLOS:
        def get_application(self, case_id):
            return app if case_id == app.case_id else None

        def list_applications(self, applicant_id):
            return [app] if applicant_id == app.applicant_id else []

        def get_applicant(self, applicant_id):
            return NS(full_name="Test Applicant")

        def get_case_stage(self, case_id):
            return NS(stage="CPA")

        def list_documents(self, case_id):
            return [doc]

    class FakeStore:
        def get(self, key):
            return pdf

    monkeypatch.setenv("BANK_VERIFICATION_LOS", "on")
    monkeypatch.setattr(los_link, "_repo", lambda: FakeLOS())
    import app.store.documents as store_module
    monkeypatch.setattr(store_module, "get_document_store", lambda: FakeStore())

    assert bv_client.get(f"{API}/lookup?ref=app-0011223344aa").json() == {"case_id": "CASE-ABC123DEF456"}
    assert bv_client.get(f"{API}/lookup?ref=CASE-ABC123DEF456").json() == {"case_id": "CASE-ABC123DEF456"}
    detail = bv_client.get(f"{API}/cases/CASE-ABC123DEF456").json()
    assert detail["case"]["applicant_name"] == "Test Applicant" and detail["case"]["stage"] == "CPA"
    assert [d["role"] for d in detail["documents"]] == ["STATEMENT"]               # copied once, not per search
    r = bv_client.post(f"{API}/process", json={"case_id": "CASE-ABC123DEF456", "mode": "AA_FAILURE",
                                               "entered": _form("CLEAN_AA_FAILURE")})
    assert r.status_code == 200 and _rules(r.json())["R1"] == "PASS"
    assert bv_client.get(f"{API}/lookup?ref=CASE-NOPE").status_code == 404


def test_a_long_statement_is_read_in_the_background_and_the_result_arrives(bv_client, monkeypatch):
    from app.agents.credit.bank_verification import config
    from app.agents.credit.bank_verification.services import verification_service

    monkeypatch.setitem(config.cfg()["background"], "over_pages", 0)          # every statement counts as long here
    pdf = statements.build_pdf(_spec("CLEAN_AA_FAILURE")["statement"]["spec"])
    r = _upload(bv_client, "BV-2026-002", pdf, "long.pdf", entered=_form("CLEAN_AA_FAILURE"))
    assert r.status_code == 202 and r.json()["processing_status"] == "READING" and r.json()["pages"] == 1
    again = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-002", "mode": "AA_FAILURE"})
    assert again.status_code in (200, 409)                                     # 409 while it is still being read
    if again.status_code == 409:
        assert again.json()["error"]["code"] == "STATEMENT_BEING_READ"
    verification_service.wait_background()
    detail = bv_client.get(f"{API}/cases/BV-2026-002").json()
    assert detail["case"]["status"] != "READING" and _rules(detail["latest_run"])["R1"] == "PASS"
    events = [e["event"] for e in bv_client.get(f"{API}/cases/BV-2026-002/history").json()["events"]]
    assert "STATEMENT_QUEUED" in events and events[-1] in ("VERIFICATION_RUN", "VERIFICATION_REPLAYED")


def test_a_case_left_reading_by_a_restart_is_never_stuck(bv_client, bv_repo):
    bv_repo.update_case("BV-2026-003", status="READING")                       # as if the server stopped mid-read
    detail = bv_client.get(f"{API}/cases/BV-2026-003").json()
    assert detail["case"]["status"] == "REVIEW_REQUIRED"
    assert bv_client.get(f"{API}/cases/BV-2026-003/history").json()["events"][-1]["event"] == "STATEMENT_READ_FAILED"


def test_the_start_state_verifies_nothing_and_aa_failure_waits_for_an_upload(bv_client, bv_home):
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    failure = bv_client.get(f"{API}/cases/BV-2026-004").json()
    assert failure["latest_run"] is None and failure["documents"] == []
    # AA failure opens with the bank account the customer gave on the application (masked), nothing else
    assert failure["prefill"]["bank_name"] == "Axis Bank" and failure["prefill"]["account_number"].endswith("7881")
    assert "X" in failure["prefill"]["account_number"] and failure["prefill"]["transactions"] == []
    assert set(failure["prefill"]["provenance"].values()) == {"APPLICATION"}
    success = bv_client.get(f"{API}/cases/BV-2026-001").json()
    assert success["latest_run"] is None and success["prefill"]["bank_name"] == "State Bank of India"
    assert set(success["prefill"]["provenance"].values()) == {"AA"}
    sample = next((bv_home / "sample_statements").glob("BV-2026-004_*"))
    blank = {"bank_name": None, "account_number": None, "account_type": None, "transactions": [], "provenance": {}}
    r = _upload(bv_client, "BV-2026-004", sample.read_bytes(), "statement.pdf", entered=blank)
    body = r.json()
    # AA failure: the CPA types the details -- nothing is filled from the statement, a blank is "not entered"
    assert r.status_code == 200 and _rules(body)["R1"] == "REVIEW_REQUIRED" and _rules(body)["R2"] == "REVIEW_REQUIRED"
    assert body["form"]["bank_name"] is None and "not entered on the form" in str(body["rules"][0]["comparisons"])
    typed = _form("LATEST_TXN_MISMATCH")                                     # what the CPA typed (2 rows typed wrong)
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-004", "mode": "AA_FAILURE", "entered": typed})
    assert _rules(r.json())["R1"] == "PASS" and _rules(r.json())["R2"] == "FAIL"   # the typing mistake is caught


def test_the_application_account_is_checked_against_the_statement(bv_client, bv_home):
    """Owner 2026-10-26 "account number application se bharo": the application's account (sent back masked, as the
    screen shows it) is kept in full and compared; a statement of ANOTHER account is caught by the bank details check."""
    assert bv_client.post(f"{API}/demo/reset").status_code == 200
    shown = bv_client.get(f"{API}/cases/BV-2026-004").json()["prefill"]
    own = next((bv_home / "sample_statements").glob("BV-2026-004_*")).read_bytes()
    other = next((bv_home / "sample_statements").glob("BV-2026-002_*")).read_bytes()      # another customer's account
    body = _upload(bv_client, "BV-2026-004", own, "statement.pdf", entered=shown).json()
    assert body["form"]["provenance"]["account_number"] == "APPLICATION"
    assert _rules(body)["R1"] == "REVIEW_REQUIRED"           # number and bank match; the type was not entered
    numbers = [c for c in body["rules"][0]["comparisons"] if c["field"] == "account_number"]
    assert numbers[0]["match"] is True
    body = _upload(bv_client, "BV-2026-004", other, "statement.pdf", entered=shown).json()
    assert _rules(body)["R1"] == "FAIL"


def test_the_report_prints_no_internal_code(bv_client):
    html = bv_client.get(f"{API}/cases/BV-2026-002/report?format=html").text
    assert "AA_FAILURE" not in html and "AA_SUCCESS" not in html and "REVIEW_REQUIRED" not in html
    assert "AA not received" in html


# ---- AA received AND a statement uploaded: every source compared, each check one job (Sahil Sir, 2026-10-28) ----------
def _by_rule(body: dict) -> dict:
    return {r["rule_id"]: r for r in body["rules"]}


def test_aa_received_without_a_statement_is_checked_against_the_aa_data(bv_client):
    r = bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS",
                                               "statement_source": "NONE"}, headers=CPA)
    assert r.status_code == 200, r.text
    rules = _by_rule(r.json())
    assert rules["R1"]["status"] == "PASS" and {c["right_label"] for c in rules["R1"]["comparisons"]} == {"AA data"}
    assert rules["R2"]["status"] == "PASS" and {c["right_label"] for c in rules["R2"]["comparisons"]} == {"AA data"}
    assert rules["R3"]["status"] == "NOT_APPLICABLE" and "Account Aggregator" in rules["R3"]["reason"]
    assert rules["R6"]["status"] == "NOT_APPLICABLE" and rules["R7"]["status"] == "NOT_APPLICABLE"


def test_aa_received_and_a_different_statement_uploaded_is_caught_by_the_cross_check(bv_client):
    import copy
    from decimal import Decimal

    spec = copy.deepcopy(_spec("CLEAN_AA_SUCCESS")["statement"]["spec"])
    spec["details"]["account_number"] = "99901020309999"                 # the statement is of another account
    last = spec["transactions"][-1]
    last["amount"] = last["amount"] + Decimal("1000.00")                 # and one row says a different amount
    r = _upload(bv_client, "BV-2026-001", statements.build_pdf(spec), "other.pdf", mode="AA_SUCCESS")
    assert r.status_code == 200, r.text
    rules = _by_rule(r.json())
    # the form (from the AA data) is checked against the AA data: it agrees
    assert rules["R1"]["status"] == "PASS" and {c["right_label"] for c in rules["R1"]["comparisons"]} == {"AA data"}
    # the AA data against the statement: the account and the changed row are named
    r6 = rules["R6"]
    assert r6["status"] == "FAIL"
    differs = {c["field"] for c in r6["comparisons"] if c["match"] is False}
    assert "account_number" in differs
    assert any("only" in (c.get("note") or "").lower() or c["field"].startswith("transaction") for c in r6["comparisons"]
               if c["match"] is False and c["field"] != "account_number")
    assert rules["R3"]["status"] != "NOT_APPLICABLE"                        # the uploaded file itself is checked


def test_aa_not_received_still_checks_the_form_against_the_statement(bv_client):
    s = _spec("CLEAN_AA_SUCCESS")["statement"]["spec"]
    r = _upload(bv_client, "BV-2026-001", statements.build_pdf(s), "st.pdf", mode="AA_FAILURE", entered=_form("CLEAN_AA_SUCCESS"))
    assert r.status_code == 200, r.text
    rules = _by_rule(r.json())
    assert {c["right_label"] for c in rules["R1"]["comparisons"]} == {"Statement"}
    assert rules["R6"]["status"] == "NOT_APPLICABLE"


# ---- AA received: Waive (a reason) and Retrigger; AA not received: Run verification (owner 2026-10-30) -------------------
def test_aa_received_retrigger_runs_the_eight_checks_again_and_is_audited(bv_client):
    first = bv_client.post(f"{API}/cases/BV-2026-001/retrigger", json={}, headers=CPA)
    assert first.status_code == 200, first.text
    assert len(first.json()["rules"]) == 8
    again = bv_client.post(f"{API}/cases/BV-2026-001/retrigger", json={}, headers=CPA)
    assert again.status_code == 200
    events = [e["event"] for e in bv_client.get(f"{API}/cases/BV-2026-001/history", headers=CPA).json()["events"]]
    assert events.count("AA_RETRIGGERED") == 2


def test_aa_received_waive_needs_a_reason_and_closes_the_case(bv_client):
    blank = bv_client.post(f"{API}/cases/BV-2026-001/waive", json={"reason": "   "}, headers=CPA)
    assert blank.status_code == 422 and blank.json()["error"]["code"] == "WAIVE_REASON_REQUIRED"
    done = bv_client.post(f"{API}/cases/BV-2026-001/waive", json={"reason": "AA data received from the bank"}, headers=CPA)
    assert done.status_code == 200 and done.json()["status"] == "WAIVED"
    assert bv_client.get(f"{API}/cases/BV-2026-001", headers=CPA).json()["case"]["status"] == "WAIVED"
    history = bv_client.get(f"{API}/cases/BV-2026-001/history", headers=CPA).json()["events"]
    waived = next(e for e in history if e["event"] == "BANK_VERIFICATION_WAIVED")
    assert "AA data received from the bank" in json.dumps(waived["detail"])
    # a waived case takes no new run, no second waiver, no agency decision ...
    for r in (bv_client.post(f"{API}/process", json={"case_id": "BV-2026-001", "mode": "AA_SUCCESS"}, headers=CPA),
              bv_client.post(f"{API}/cases/BV-2026-001/waive", json={"reason": "again"}, headers=CPA),
              _finalize(bv_client, "BV-2026-001", _agency_input("CLEAN_AA_SUCCESS"))):
        assert r.status_code == 409, r.text


def test_retrigger_after_waive_withdraws_the_waiver_and_runs_the_checks_again(bv_client):
    # ... but it can be RETRIGGERED (owner 2026-11-12): the waiver is withdrawn, audited, and the checks run again
    bv_client.post(f"{API}/cases/BV-2026-001/waive", json={"reason": "AA data received from the bank"}, headers=CPA)
    again = bv_client.post(f"{API}/cases/BV-2026-001/retrigger", json={}, headers=CPA)
    assert again.status_code == 200, again.text
    case = bv_client.get(f"{API}/cases/BV-2026-001", headers=CPA).json()["case"]
    assert case["status"] != "WAIVED"
    history = bv_client.get(f"{API}/cases/BV-2026-001/history", headers=CPA).json()["events"]
    assert any(e["event"] == "BANK_VERIFICATION_WAIVER_WITHDRAWN" for e in history)


def test_waive_and_retrigger_are_for_aa_data_and_the_cpa_only(bv_client):
    for path in ("waive", "retrigger"):
        body = {"reason": "x"} if path == "waive" else {}
        no_aa = bv_client.post(f"{API}/cases/BV-2026-002/{path}", json=body, headers=CPA)    # AA not received
        assert no_aa.status_code == 409 and no_aa.json()["error"]["code"] == "AA_ONLY", path
        agency = bv_client.post(f"{API}/cases/BV-2026-001/{path}", json=body, headers=AGENCY)
        assert agency.status_code == 403 and agency.json()["error"]["code"] == "FORBIDDEN_FOR_ROLE", path
        assert bv_client.post(f"{API}/cases/BV-NOPE-1/{path}", json=body, headers=CPA).status_code == 404
    # an unknown field is refused, like every request of this API
    extra = bv_client.post(f"{API}/cases/BV-2026-001/waive", json={"reason": "x", "x": 1}, headers=CPA)
    assert extra.status_code == 422
