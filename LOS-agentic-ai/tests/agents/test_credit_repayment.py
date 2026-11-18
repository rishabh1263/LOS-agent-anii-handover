"""
Credit underwriting -- the repayment report (app/agents/credit/repayment): the vendor's verified bank analysis read
with the eligibility case data. Synthetic data only (scripts/make_demo_bank_reports.py builds the statements).
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.credit.repayment import (ai_summary, api_response, config, obligations, report, rules, service,
                                         store, vendor)

ROOT = Path(__file__).resolve().parents[2]
CASE = "CASE-E11D00000001"
SCOPE = "los.credit.underwrite"

_spec = importlib.util.spec_from_file_location("demo_bank", ROOT / "scripts" / "make_demo_bank_reports.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


def statement(story=None, income=60000.0, emi=5000.0, case_id=CASE) -> dict:
    """A synthetic, normalized vendor analysis (whole months marked)."""
    return vendor.finish(demo.report(case_id, income, emi, story))


def eligibility(decision="APPROVED", income=60000, emi=5000, proposed=12000, **extra) -> dict:
    return {"decision": decision, "emi": proposed, "eligible_amount": 600000, "requested_amount": 600000,
            "foir": {"value_pct": 28.3}, "reasons": extra.pop("reasons", []), "missing_fields": [],
            "inputs": {"monthly_income": income, "monthly_obligations": emi, "loan_amount": 600000,
                       "tenure_months": 60, "interest_rate_pct": 12.0, **extra}}


def build(story=None, **kw) -> dict:
    elig = eligibility(**kw)
    return report.build(CASE, elig, statement(story, float(elig["inputs"]["monthly_income"] or 60000),
                                              float(elig["inputs"]["monthly_obligations"] or 0)))


def results(out) -> dict:
    return {o["rule"]: o["result"] for o in out["rules"]}


@pytest.fixture
def files_store(tmp_path, monkeypatch):
    """The files backend in a temp folder, no demo fallback, no case-store co-applicants, nothing clubbed."""
    monkeypatch.setattr(store, "_spec", lambda: {"backend": "files", "files_dir": str(tmp_path / "up"),
                                                 "demo_dir": str(tmp_path / "demo")})
    monkeypatch.setattr(service, "_case_store_co_applicants", lambda case_id: [])
    monkeypatch.setattr(service, "_clubbed", lambda case_id: {})
    return tmp_path


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """The local model is not called in these tests: the AI summary falls back to the facts (its own tests below
    stand a model in)."""
    def down(*a, **k):
        raise ConnectionError("no model in tests")

    monkeypatch.setattr(ai_summary, "_ask", down)
    ai_summary._CACHE.clear()
    ai_summary._WRITING.clear()


# ==========================================================================
# config
# ==========================================================================

def test_config_has_the_five_rules_and_loads_clean():
    config.reload()
    config.check()
    assert list(config.policy()["rules"]) == ["DATA_COMPLETE", "REPAYMENT_TRACK", "TRUE_FOIR", "BALANCE_COVER",
                                               "INCOME_SUPPORT"]
    assert set(config.policy()["eligibility_gate"]) >= {"APPROVED", "CONDITIONAL", "REFER", "DATA_MISSING",
                                                        "REJECTED"}


# ==========================================================================
# vendor input
# ==========================================================================

def _workbook() -> bytes:
    """A tiny synthetic workbook in the vendor's layout."""
    import openpyxl

    wb = openpyxl.Workbook()
    acc = wb.active
    acc.title = "Customer & Account Information"
    for row in (("sName", "TEST PERSON"), ("sBank", "DEMO BANK"), ("sAccountNo", "123456789012"),
                ("sAnalysisStartDate", "2026-07-01"), ("sAnalysisEndDate", "2026-09-15"),
                ("sPan", "ABCDE1234F"), ("sEmail", "x@example.com")):
        acc.append(row)
    mon = wb.create_sheet("Monthly Details")
    mon.append(("Metric", "July-2026", "August-2026", "September-2026", "Total"))
    mon.append(("sTotalCredit", 50000, 52000, 20000, 122000))
    mon.append(("sTotalDebit", 45000, 47000, 10000, 102000))
    mon.append(("sBal5", 10000, 11000, 12000, None))
    xns = wb.create_sheet("AllAccountXns")
    xns.append(("sDate", "sAmount", "sCreditOrDebit", "sMode", "sNarration", "sCategory"))
    xns.append(("2026-07-01", 50000, "CREDIT", "NEFT", "SALARY", "Salary"))
    xns.append(("2026-07-05", 5000, "DEBIT", "NACH", "LOAN EMI", "EMI and Loans"))
    xns.append(("2026-07-09", "", "DEBIT", "UPI", "NO AMOUNT", ""))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_workbook_maps_to_four_blocks_and_drops_identifiers():
    data = vendor.read(_workbook(), "report.xlsx")
    assert set(data) >= {"account", "monthly", "transactions", "vendor_summary"}
    assert data["account"]["account_number_masked"] == "XXXX9012"
    text = json.dumps(data)
    assert "123456789012" not in text and "ABCDE1234F" not in text and "example.com" not in text
    assert [m["month"] for m in data["monthly"]] == ["2026-07", "2026-08", "2026-09"]     # "Total" is not a month
    assert [m["full"] for m in data["monthly"]] == [True, True, False]                   # part September
    assert data["monthly"][0]["eod"] == {"5": 10000.0}
    assert len(data["transactions"]) == 2                                                # the row with no amount
    view = data["vendor_view"]                                                           # the vendor's own names
    assert view["Monthly Details"]["2026-07"]["sTotalCredit"] == 50000
    assert view["Customer & Account Information"]["sName"] == "TEST PERSON"
    assert "sPan" not in view["Customer & Account Information"] and "sEmail" not in view["Customer & Account Information"]


@pytest.mark.parametrize("content, name, msg", [
    (b"not a workbook", "r.xlsx", "readable"),
    (b"{oops", "r.json", "JSON"),
    (b'{"account": {}}', "r.json", "account, monthly"),
    (b'{"account": {}, "monthly": [], "transactions": []}', "r.json", "no monthly"),
    (b"a,b", "r.csv", ".xlsx"),
])
def test_bad_input_is_a_clear_error(content, name, msg):
    with pytest.raises(vendor.VendorReportError, match=msg):
        vendor.read(content, name)


def test_workbook_without_the_monthly_sheet_is_refused():
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Customer & Account Information"
    wb.create_sheet("AllAccountXns")
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(vendor.VendorReportError, match="Monthly Details"):
        vendor.read(buf.getvalue(), "r.xlsx")


# ==========================================================================
# the obligation map (karz ka naksha)
# ==========================================================================

def test_declared_emi_is_found_and_nothing_hidden():
    ob = obligations.build(statement(), 5000)
    assert ob["monthly_emi_in_bank"] == pytest.approx(5000) and ob["undeclared_emi"] == 0
    assert ob["missed_emis"] == 0 and ob["new_loans"] == 0


def test_hidden_emi_is_found():
    ob = obligations.build(statement("hidden_emi"), 5000)
    assert ob["undeclared_emi"] == pytest.approx(round(60000 * 0.22, -2))
    assert any("QUICKCASH" in (loan["lender"] or "").upper() for loan in ob["loans"])


def test_the_map_says_who_found_each_loan():
    ob = obligations.build(statement("hidden_emi"), 5000)
    found = {l["lender"].upper(): l["found_by"] for l in ob["loans"]}
    assert found["HOME FIRST FINANCE"].startswith("vendor") and found["QUICKCASH FINSERV"].startswith("ours")
    assert ob["found_only_by_us"] == 1


def test_monthly_bank_charges_are_not_an_emi():
    ob = obligations.build(statement("bounces"), 5000)
    assert [l["lender"].upper() for l in ob["loans"]] == ["HOME FIRST FINANCE"]


def test_new_loan_and_missed_emi():
    assert obligations.build(statement("new_loan"), 5000)["new_loans"] == 1
    assert obligations.build(statement("missed_emi"), 5000)["missed_emis"] == 1


# ==========================================================================
# the 5 rules and the verdict
# ==========================================================================

def test_clean_case_is_recommended_with_reasons():
    out = build()
    assert out["verdict"] == "RECOMMENDED" and out["can_repay"] == "YES"
    assert set(results(out).values()) == {"PASS"}
    assert out["reasons"] and out["suggestion"]["fits"] is True
    assert len(out["sections"]) == 7 and out["policy"]["signed_off"] is bool(config.policy()["signed_off"])
    assert all(o["vendor_fields"] for o in out["rules"])            # every rule names the vendor fields it read


@pytest.mark.parametrize("story, rule, result, verdict", [
    ("short_data", "DATA_COMPLETE", "REFER", "REFER"),
    ("one_bounce", "REPAYMENT_TRACK", "REFER", "REFER"),
    ("bounces", "REPAYMENT_TRACK", "DECLINE", "NOT_RECOMMENDED"),
    ("missed_emi", "REPAYMENT_TRACK", "REFER", "REFER"),
    ("hidden_emi", "TRUE_FOIR", "DECLINE", "NOT_RECOMMENDED"),
    ("weak_inflow", "INCOME_SUPPORT", "REFER", "REFER"),
])
def test_each_story_trips_its_rule(story, rule, result, verdict):
    kw = {"proposed": 21000} if story == "hidden_emi" else {}
    out = build(story, **kw)
    assert results(out)[rule] == result, out["rules"]
    assert out["verdict"] == verdict


def test_a_thin_balance_refers():
    out = build("thin_balance", proposed=25000)
    assert results(out)["BALANCE_COVER"] == "REFER" and out["verdict"] == "REFER"


@pytest.mark.parametrize("times, result", [(2.0, "PASS"), (1.0, "REFER"), (0.4, "DECLINE"), (None, "REFER")])
def test_balance_cover_bands(times, result):
    m = {"emi_cover_times": times, "abb": 10000, "abb_trend_pct": None}
    assert rules.balance_cover(m, {"proposed_emi": 5000}).result == result


def test_missing_figures_refer_never_pass():
    out = build(income=None, proposed=None, loan_amount=None)
    r = results(out)
    assert r["TRUE_FOIR"] == r["INCOME_SUPPORT"] == r["BALANCE_COVER"] == "REFER"
    assert out["verdict"] == "REFER" and out["suggestion"] is None


def test_holder_name_mismatch_refers():
    elig = eligibility()
    bank = statement()
    bank["account"]["holder_name"] = "SOMEONE ELSE"
    out = report.build(CASE, elig, bank, applicant_name="Test Person")
    holder = config.wording()["rules"]["DATA_COMPLETE"]["holder"]
    assert results(out)["DATA_COMPLETE"] == "REFER" and holder in out["reasons"][0]


def test_vendor_vs_ours_flags_the_emi_the_vendor_missed():
    out = build("hidden_emi", proposed=9000)
    checks = {c["key"]: c for c in out["sections"]["vendor_vs_ours"]}
    assert checks["foir_pct"]["agree"] is False
    assert checks["foir_pct"]["vendor"]["sheet"] == "Overall Analysis" and checks["foir_pct"]["vendor"]["field"] == "FOIR"
    assert checks["cheque_bounces_6m"]["agree"] is True
    item = config.wording()["vendor_items"]["emi_payments"]["label"]
    assert any(f"The vendor's {item} is" in r for r in out["reason_groups"]["watch"])


def test_vendor_vs_ours_agrees_on_a_clean_case():
    out = build()
    assert all(c["agree"] in (True, None) for c in out["sections"]["vendor_vs_ours"])
    assert set(out["sections"]["vendor_report"]) >= {"Overall Analysis", "Monthly Details"}


def test_verdict_mapping():
    o = lambda *rs: [{"result": r} for r in rs]  # noqa: E731
    assert rules.verdict(o("PASS", "PASS")) == "RECOMMENDED"
    assert rules.verdict(o("PASS", "REFER")) == "REFER"
    assert rules.verdict(o("REFER", "DECLINE")) == "NOT_RECOMMENDED"


@pytest.mark.parametrize("decision, verdict", [
    ("APPROVED", "RECOMMENDED"), ("CONDITIONAL", "RECOMMENDED"), ("REFER", "REFER"),
    ("DATA_MISSING", "REFER"), ("REJECTED", "NOT_RECOMMENDED"),
])
def test_eligibility_decision_caps_a_clean_bank(decision, verdict):
    out = build(decision=decision, reasons=["below the product minimum"])
    assert out["verdict"] == verdict
    if verdict != "RECOMMENDED":
        said = config.wording()["eligibility"]["decisions"][decision]
        assert out["reason_groups"]["problems"][0].startswith(f"Eligibility check: {said}")


def test_a_bad_bank_is_never_lifted_by_eligibility():
    assert build("bounces", decision="APPROVED")["verdict"] == "NOT_RECOMMENDED"


def test_report_shows_only_the_masked_account():
    bank = statement()
    bank["account"]["account_number_masked"] = "XXXX1234"
    sections = json.dumps(report.build(CASE, eligibility(), bank)["sections"])
    assert "XXXX1234" in sections and "ifsc" not in sections.lower()


# ==========================================================================
# service + store
# ==========================================================================

def test_receive_then_report(files_store, monkeypatch):
    monkeypatch.setattr(service, "_eligibility", lambda case_id: eligibility())
    kept = service.receive(CASE, json.dumps(demo.report(CASE, 60000.0, 5000.0, None)).encode(), "a.json")
    assert kept["months"] == 12 and kept["full_months"] == 12 and kept["transactions"] > 0
    out = service.report_for(CASE)
    assert out["verdict"] == "RECOMMENDED" and out["bank_data"]["demo"] is False and out["bank_data"]["received_at"]


def test_demo_file_is_used_and_marked(files_store, monkeypatch):
    monkeypatch.setattr(service, "_eligibility", lambda case_id: eligibility())
    (files_store / "demo").mkdir()
    (files_store / "demo" / f"{CASE}.json").write_text(json.dumps(demo.report(CASE, 60000.0, 5000.0, None)))
    assert service.report_for(CASE)["bank_data"]["demo"] is True


@pytest.mark.parametrize("case_id, content, name, code", [
    ("case-1", b"{}", "a.json", "INVALID_CASE_ID"),
    (CASE, b"", "a.json", "EMPTY_FILE"),
    (CASE, b"x", "a.pdf", "UNSUPPORTED_FILE_TYPE"),
    (CASE, b"{oops", "a.json", "INVALID_BANK_ANALYSIS"),
])
def test_receive_refuses(files_store, case_id, content, name, code):
    with pytest.raises(service.RepaymentError) as exc:
        service.receive(case_id, content, name)
    assert exc.value.code == code


def test_receive_refuses_a_too_large_file(files_store, monkeypatch):
    monkeypatch.setitem(config.settings()["upload"], "max_bytes", 10)
    with pytest.raises(service.RepaymentError) as exc:
        service.receive(CASE, b"x" * 11, "a.json")
    assert exc.value.code == "FILE_TOO_LARGE"


def test_no_analysis_is_not_found(files_store):
    with pytest.raises(service.RepaymentError) as exc:
        service.report_for(CASE)
    assert exc.value.code == "NO_BANK_ANALYSIS"


# ==========================================================================
# HTTP API -- the one call: POST /api/v1/credit/assess/{case_id}
# ==========================================================================

URL = f"/api/v1/credit/assess/{CASE}"


@pytest.fixture
def client():
    import main

    return TestClient(main.app)


def _auth(make_token, scopes=(SCOPE,)):
    return {"Authorization": f"Bearer {make_token(subject='credit-officer', scopes=list(scopes))}"}


def _json(story=None):
    return ("a.json", json.dumps(demo.report(CASE, 60000.0, 5000.0, story)).encode(), "application/json")


@pytest.fixture
def elig(monkeypatch):
    monkeypatch.setattr(service, "_eligibility", lambda case_id: eligibility())


def test_api_one_call_sends_the_file_and_answers(files_store, client, make_token, elig):
    r = client.post(URL, files={"file": _json("one_bounce")}, data={"run_underwriting": "false"},
                    headers=_auth(make_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert list(out)[:5] == ["request_id", "case_id", "decision", "can_repay", "summary"]
    assert list(out)[-1] == "ai_summary"                                    # the AI summary comes last
    assert out["decision"] == "REFER" and out["can_repay"] == "NEEDS_REVIEW"
    assert [c["result"] for c in out["checks"]].count("review") == 1
    assert {c["result"] for c in out["checks"]} <= {"pass", "review", "fail"}
    assert all(c["label"] and c["limit"] and c["value"] for c in out["checks"])
    assert "eligibility" not in out                                          # used in the backend, not shown
    assert "bounced" in out["problems"][0]
    assert any("bounced" in step for step in out["next_steps"])
    assert out["working"][0].startswith("Money in each month")
    assert "people" not in out                                               # alone: the account section says it
    assert len(json.dumps(out, ensure_ascii=False)) < 7000                    # mid-short
    assert "underwriting" not in out                                         # not in the answer
    assert out["ai_summary"]["source"] == "rules" and out["ai_summary"]["text"].startswith(out["summary"])
    assert "detail" not in out                                              # only on request; empty fields left out


def test_api_reasons_are_plain_words_no_codes(files_store, client, make_token, elig):
    out = client.post(URL, files={"file": _json("hidden_emi")}, data={"run_underwriting": "false"},
                      headers=_auth(make_token)).json()
    texts = (out.get("problems") or []) + (out.get("watch") or []) + out["next_steps"] + out["working"] + \
        [s["meaning"] for s in out["report"].values()] +         list(out["insights"].values()) + [out["summary"]]
    assert all((t[0].isupper() or t[0].isdigit() or t[0] == "₹") and t.endswith((".", "?")) for t in texts)
    for code in ("TRUE_FOIR", "REFER", "DECLINE", "None", "Rs ", "--", "_"):
        assert not any(code in t for t in texts), code
    assert any("₹" in t for t in texts)
    assert any(l["found_by"] == "our check" for l in out["loans"])


def test_api_second_call_uses_the_statement_on_file(files_store, client, make_token, elig):
    client.post(URL, files={"file": _json()}, data={"run_underwriting": "false"}, headers=_auth(make_token))
    r = client.post(URL, data={"run_underwriting": "false", "detail": "true", "explain": "false"},
                    headers=_auth(make_token))
    out = r.json()
    assert r.status_code == 200 and out["detail"]["statement"]["status"] == "ON_FILE"
    assert set(out["detail"]["sections"]) >= {"obligations", "vendor_report"} and "ai_summary" not in out
    assert out["detail"]["report"]["monthly"]["months"] and out["detail"]["checks"][0]["finding"]


def test_api_xlsx_file(files_store, client, make_token, elig):
    r = client.post(URL, files={"file": ("r.xlsx", _workbook(), "application/octet-stream")},
                    data={"run_underwriting": "false"}, headers=_auth(make_token))
    assert r.status_code == 200, r.text
    assert r.json()["report"]["account"]["key"]["full_months"] == 2


def test_api_co_applicant_statement_joins_the_household(files_store, client, make_token, monkeypatch):
    monkeypatch.setattr(service, "_eligibility", lambda case_id: eligibility(income=100000, proposed=9000))
    monkeypatch.setattr(service, "_clubbed", lambda case_id: {"income": 40000.0, "relationship": "Spouse"})
    h = _auth(make_token)
    first = client.post(URL, files={"file": _json()}, data={"run_underwriting": "false"}, headers=h).json()
    assert first["checks"][4]["result"] == "review"                        # ₹60k in vs ₹1,00,000 declared
    assert any("(Spouse)" in w and "has not been received" in w for w in first["watch"])
    assert "Get the co-applicant's bank statement." in first["next_steps"]
    co = ("c.json", json.dumps(demo.report(CASE, 40000.0, 0.0, None)).encode(), "application/json")
    r = client.post(URL, files={"file": co}, data={"party": "co_applicant", "run_underwriting": "false"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [p["role"] for p in out["people"]] == ["Applicant", "Co-applicant"]
    assert out["people"][1]["relationship"] == "Spouse" and out["people"][1]["statement"] == "received"
    assert out["checks"][4]["result"] == "pass"                            # ₹60k + ₹40k = the declared ₹1,00,000
    assert "Applicant ₹" in out["working"][0] and "Co-applicant ₹" in out["working"][0]


def test_api_underwriting_not_at_credit_stage_is_said_not_failed(files_store, client, make_token, elig):
    r = client.post(URL, files={"file": _json()}, data={"run_underwriting": "true", "detail": "true"},
                    headers=_auth(make_token))
    assert r.status_code == 200, r.text
    assert "underwriting" not in r.json()
    uw = r.json()["detail"]["underwriting"]
    assert uw["ran"] is False and uw["message"] and "status" not in uw


@pytest.mark.parametrize("case_id, name, body, extra, status, code", [
    ("CASE-NOTVALID", "a.json", b"{}", {}, 422, "INVALID_CASE_ID"),
    (CASE, "a.pdf", b"x", {}, 415, "UNSUPPORTED_FILE_TYPE"),
    (CASE, "a.json", b"", {}, 400, "EMPTY_FILE"),
    (CASE, "a.json", b"{oops", {}, 422, "INVALID_BANK_ANALYSIS"),
    (CASE, "a.json", b"{}", {"party": "co_applicant", "co_applicant_id": "../x"}, 422, "INVALID_PARTY"),
])
def test_api_refusals(files_store, client, make_token, case_id, name, body, extra, status, code):
    r = client.post(f"/api/v1/credit/assess/{case_id}", files={"file": (name, body, "application/json")},
                    data=extra, headers=_auth(make_token))
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert detail["code"] == code and detail["request_id"].startswith("cra_") and detail["message"]


def test_api_without_a_statement_is_404_in_plain_words(files_store, client, make_token):
    r = client.post(URL, headers=_auth(make_token))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NO_BANK_ANALYSIS"
    assert "Send the vendor's file" in r.json()["detail"]["message"]


def test_api_needs_a_token_and_the_scope(files_store, client, make_token):
    assert client.post(URL).status_code == 401
    assert client.post(URL, headers=_auth(make_token, scopes=("unrelated.scope",))).status_code == 403


def test_api_read_all_may_read_but_not_send_a_file(files_store, client, make_token, elig):
    client.post(URL, files={"file": _json()}, data={"run_underwriting": "false"}, headers=_auth(make_token))
    reader = _auth(make_token, scopes=("los.read",))
    assert client.post(URL, data={"run_underwriting": "false"}, headers=reader).status_code == 200
    r = client.post(URL, files={"file": _json()}, headers=reader)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FILE_NOT_ALLOWED"


def test_api_contract_is_published(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/v1/credit/assess/{case_id}"]["post"]
    assert "AssessResponse" in json.dumps(op["responses"]["200"])
    assert "/api/v1/credit/bank-analysis/{case_id}" not in spec["paths"]
    assert "/api/v1/credit/repayment/{case_id}" not in spec["paths"]


# ==========================================================================
# the AI summary
# ==========================================================================

def _answer(story=None, **kw):
    return api_response.build(build(story, **kw), request_id="x", statement_status="ON_FILE",
                              underwriting=api_response.underwriting_block(None, requested=False),
                              explain=False).model_dump()


def _model_says(monkeypatch, text):
    monkeypatch.setattr(ai_summary, "_ask", lambda messages, spec: text)
    monkeypatch.setitem(config.settings()["ai_summary"], "timeout_seconds", 2)   # wait for the stand-in model
    ai_summary._CACHE.clear()
    ai_summary._WRITING.clear()


def test_ai_summary_from_the_model_when_it_passes_the_check(monkeypatch):
    _model_says(monkeypatch, "The customer is unlikely to pay the new EMI. 3 EMI or cheque payment(s) bounced in "
                             "the last 6 months.")
    out = ai_summary.summarise(_answer("bounces"))
    assert out["source"] == "ai" and out["text"].startswith("The customer is unlikely")


@pytest.mark.parametrize("text", [
    "The customer is unlikely to pay. 7 EMIs bounced.",                        # a figure the facts do not hold
    "The customer is unlikely to pay. Nine EMIs bounced.",                     # a wrong count in words (9)
    "The customer can pay the new EMI easily.",                                # against the answer
    "The can_repay value is NO.",                                              # a field name
])
def test_ai_summary_refused_falls_back_to_the_facts(monkeypatch, text):
    _model_says(monkeypatch, text)
    answer = _answer("bounces")
    out = ai_summary.summarise(answer)
    assert out["source"] == "rules" and out["text"].startswith(answer["summary"])


def test_ai_summary_off_or_slow_never_blocks(monkeypatch):
    import time as _time

    monkeypatch.setitem(config.settings()["ai_summary"], "timeout_seconds", 0.2)
    monkeypatch.setattr(ai_summary, "_ask", lambda messages, spec: _time.sleep(2) or "late")
    ai_summary._CACHE.clear()
    started = _time.perf_counter()
    assert ai_summary.summarise(_answer())["source"] == "rules"
    assert _time.perf_counter() - started < 1.5


# ==========================================================================
# the vendor report section by section, and our two insights
# ==========================================================================

def test_the_answer_reads_the_vendor_report_in_its_own_order(files_store, client, make_token, elig):
    out = client.post(URL, files={"file": _json()}, data={"run_underwriting": "false", "explain": "false"},
                      headers=_auth(make_token)).json()
    assert list(out["report"]) == ["account", "monthly", "balances", "income", "spending", "loans", "bounces", "vendor"]
    for name, section in out["report"].items():
        assert section["meaning"].endswith(".") and section["key"], name
    assert out["report"]["income"]["key"]["salary_months"] == 6
    assert out["report"]["income"]["key"]["top_payer"] == "Acme Infotech Pvt Ltd (100%)"
    assert list(out).index("report") < list(out).index("checks")          # the report first, the checks after


def test_emi_dry_run_counts_the_months_the_new_emi_would_bounce():
    from app.agents.credit.repayment import insights

    ok = build()["insights"]["emi_dry_run"]
    assert ok["cleared"] == ok["of"] == 6 and not ok["concern"]
    high = insights.emi_dry_run(statement("thin_balance"), [f"2026-0{m}" for m in range(4, 10)],
                                {"loans": []}, 70000.0)
    assert high["cleared"] < high["of"] and high["concern"] and "would have bounced" in high["line"]


def test_monthly_surplus_shows_a_customer_who_spends_it_all():
    thin = build("thin_balance", proposed=25000)
    surplus = thin["insights"]["monthly_surplus"]
    assert surplus["left_after_new_emi"] < 0 and surplus["concern"]
    assert config.wording()["insights"]["surplus"]["watch"] in thin["reason_groups"]["watch"]
    clean = build(proposed=9000)["insights"]["monthly_surplus"]
    assert not clean["concern"]                                          # a small gap: spending adjusts


def test_section_flags_say_what_needs_a_look():
    sec = build("thin_balance", proposed=25000)["vendor_sections"]
    assert sec["balances"]["flags"] and "below the new EMI" in sec["balances"]["flags"][0]
    bounced = build("bounces")["vendor_sections"]["bounces"]
    assert bounced["figures"]["emi_or_cheque_bounces"] == 3 and "bounced" in bounced["meaning"]


def test_ai_summary_count_words_become_digits_and_field_names_words():
    assert ai_summary._clean("3 bounced. Next_step is to ask why. Three EMIs bounced.") ==         "3 bounced. Next step is to ask why. 3 EMIs bounced."
    assert "1" not in ai_summary._clean("One thing in their favour is the income.")


def test_ai_summary_slower_than_the_wait_is_ready_next_time(monkeypatch):
    import time as _time

    text = "The customer is unlikely to pay the new EMI. 3 EMI or cheque payment(s) bounced in the last 6 months."
    monkeypatch.setitem(config.settings()["ai_summary"], "timeout_seconds", 0.2)
    monkeypatch.setattr(ai_summary, "_ask", lambda messages, spec: _time.sleep(0.5) or text)
    ai_summary._CACHE.clear()
    answer = _answer("bounces")
    first = ai_summary.summarise(answer)
    assert first["source"] == "rules" and first["pending"] is True        # not ready in time: the facts' note
    assert ai_summary.summarise(answer)["pending"] is True                # asked again meanwhile: no second call
    _time.sleep(0.8)
    assert ai_summary.summarise(answer) == {"text": text, "source": "ai"}  # finished meanwhile: ready now


def test_loan_rows_say_what_is_special_about_a_loan():
    hidden = {l["lender"]: l for l in _answer("hidden_emi", proposed=9000)["loans"]}
    assert hidden["Home First Finance"]["note"] is None                     # declared, the vendor's: nothing to say
    note = hidden["Quickcash Finserv"]["note"]
    assert "our check found" in note and "Not declared" in note
    missed = _answer("missed_emi")["loans"][0]
    assert missed["missed_months"] == ["Jul 2026"] and "Jul 2026" in missed["note"]


def test_an_eligibility_only_case_says_it_is_not_in_the_los(files_store, client, make_token, elig, monkeypatch):
    import app.api.routes.credit_api as credit_api

    monkeypatch.setattr(credit_api, "_in_los", lambda case_id: False)
    out = client.post(URL, files={"file": _json()},
                      data={"explain": "false", "run_underwriting": "true", "detail": "true"},
                      headers=_auth(make_token)).json()
    assert out["detail"]["underwriting"]["message"] == config.wording()["underwriting"]["refused"]["NOT_IN_LOS"]


def test_the_story_tells_the_case_in_five_plain_lines():
    story = _answer("hidden_emi", proposed=9974, income=50000, emi=15000)["story"]
    assert list(story) == ["customer", "found", "because", "fine", "in_one_line"]
    assert "Salary of" in story["customer"] and "the new EMI is ₹9,974" in story["customer"]
    assert "₹11,000" in story["found"] and "Quickcash Finserv" in story["found"] and "not declared" in story["found"]
    assert "undeclared EMI" in story["because"] and "71.9%" in story["because"]
    assert story["in_one_line"].startswith("The income and the balance are fine, but")
    assert story["in_one_line"].endswith("so the new EMI does not fit.")
    clean = _answer(proposed=9000)["story"]
    assert clean["because"] == "All 5 checks pass." and clean["in_one_line"].endswith("the new EMI fits.")
    bounced = _answer("bounces")["story"]["because"]
    assert bounced == "3 EMI or cheque payment(s) bounced." and "0" not in bounced


def test_monthly_surplus_says_how_long_the_savings_cover_the_gap():
    hidden = build("hidden_emi", proposed=9974, income=50000, emi=15000)["insights"]["monthly_surplus"]
    assert hidden["months_covered"] == 10 and "for about 10 months. After that, EMIs may bounce." in hidden["line"]
    thin = build("thin_balance", proposed=25000)["insights"]["monthly_surplus"]
    assert thin["months_covered"] == 0 and "cannot cover this even for 1 month" in thin["line"]
    clean = build(proposed=9000)["insights"]["monthly_surplus"]                # tenure 60 months in these tests
    assert "for the whole loan (60 months)" in clean["line"]


def test_insights_are_one_plain_line_each():
    found = _answer("hidden_emi", proposed=9974, income=50000, emi=15000)["insights"]
    dry, left = found["emi_dry_run"], found["monthly_surplus"]
    assert dry.startswith("If this new EMI of ₹9,974 had started 6 months ago, it would have been paid every month.")
    assert dry.endswith("see the next point.")                               # the money does not last the month
    assert "saves only about ₹3,045 a month" in left and "₹6,929 short every month" in left
    assert "for about 10 months. After that, EMIs may bounce." in left
    assert "whole loan (60 months)" in _answer(proposed=9000)["insights"]["monthly_surplus"]
