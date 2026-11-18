"""Legal collateral verification: the API, the ten rules on the synthetic scenarios, the workflow, permissions, the
summary, the PDF and the MCP read tool. Each module runs on its own throwaway PostgreSQL database."""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents.credit.legal_verification import db as legal_db
from app.agents.credit.legal_verification.demo_data import seed

BASE = "/api/v1/credit/legal-verification"
USERS = {
    "cpa.mumbai": ("los.credit.underwrite", ["MUM"]),
    "cpa.pune": ("los.credit.underwrite", ["PUN"]),
    "agency.lex": ("los.legal.agency", ["MUM"]),
    "viewer.mumbai": ("los.read", ["MUM"]),
}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    import os

    from app.store.testing import _admin_dsn, _with_db

    admin = _admin_dsn()
    name = f"legal_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    os.environ["LEGAL_VERIFICATION_DOCUMENTS_DIR"] = str(tmp_path_factory.mktemp("legal_docs"))
    os.environ["LEGAL_VERIFICATION_AI_SUMMARY"] = "off"
    # the module's tests read the demo cases at their staged stages; the ready-to-initiate demo flow has its own test
    os.environ["LEGAL_VERIFICATION_DEMO_START"] = "STAGED"
    os.environ["LEGAL_VERIFICATION_DEMO_AGENCY_REPLY"] = "off"
    db = legal_db.Database(_with_db(admin, name), min_size=0, max_size=4)
    assert db.migrate() == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    assert db.migrate() == []
    legal_db.use(db)
    yield db
    legal_db.use(None)
    db.close()
    os.environ.pop("LEGAL_VERIFICATION_DOCUMENTS_DIR", None)
    os.environ.pop("LEGAL_VERIFICATION_AI_SUMMARY", None)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def client(database, monkeypatch, make_token):
    from fastapi import Depends, FastAPI

    from app.agents.credit.legal_verification.api import router
    from app.agents.credit.legal_verification.config import section
    from app.api import errors
    from app.security.auth import require_jwt

    for user, (_, branches) in USERS.items():
        monkeypatch.setitem(section("access")["branches"], user, branches)
    seed.reset(database)
    app = FastAPI()
    errors.install(app)
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])
    with TestClient(app) as c:
        c.make_token = make_token
        yield c


def login(client, user: str = "cpa.mumbai") -> dict[str, str]:
    scopes, _ = USERS[user]
    return {"Authorization": f"Bearer {client.make_token(subject=user, scopes=scopes)}"}


def ws(client, app_id: str, user: str = "cpa.mumbai") -> dict:
    res = client.get(f"{BASE}/applications/{app_id}", headers=login(client, user))
    assert res.status_code == 200, res.text
    return res.json()


def by_id(workspace: dict) -> dict[str, dict]:
    return {r["id"]: r for r in workspace["run"]["rules"]}


def _pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "SALE DEED (synthetic test)")
    return doc.tobytes()


# ---------------------------------------------------------------------------------------------- the scenarios
def test_the_clean_title_is_clear_for_review_never_declared_clear(client):
    w = ws(client, "LGV-2026-001")
    rules = by_id(w)
    assert len(rules) == 10 and all(r["status"] == "PASS" for r in rules.values()), \
        {k: (r["status"], r["reason"]) for k, r in rules.items() if r["status"] != "PASS"}
    ov = w["run"]["overview"]
    assert ov["recommendation"] == "CLEAR_FOR_REVIEW" and ov["counts"]["passed"] == 9
    assert "does not declare the title clear" in ov["recommendation_note"]
    assert rules["R4"]["comparisons"][-1]["values"]["AGENCY"].endswith("Aditya Kulkarni and Neha Kulkarni")


def test_a_different_owner_and_a_clear_opinion_escalate(client):
    w = ws(client, "LGV-2026-002")
    rules = by_id(w)
    assert rules["R1"]["status"] == "FAIL" and rules["R1"]["severity"] == "CRITICAL"
    row = rules["R1"]["comparisons"][0]
    # the owner the agency read off the Sale Deed is not the applicant
    assert row["values"] == {"APPLICATION": "Vikas Shetty", "AGENCY": "Prakash Shetty"} and row["match"] is False
    assert rules["R9"]["status"] == "FAIL"                       # yet the agency calls the title clear
    assert w["run"]["overview"]["recommendation"] == "ESCALATE"
    order = [r["status"] for r in w["run"]["rules"]]
    assert order == sorted(order, key=lambda s: {"FAIL": 0, "REVIEW": 1, "PASS": 2}[s])    # red, amber, green


def test_missing_search_evidence_is_never_read_as_no_charge(client):
    rules = by_id(ws(client, "LGV-2026-003"))
    assert rules["R6"]["status"] == "REVIEW"
    assert "not proof that there is no charge" in rules["R6"]["reason"] and "CERSAI" in rules["R6"]["reason"]
    assert rules["R7"]["status"] == "REVIEW" and "Encumbrance Certificate" in rules["R7"]["reason"]
    assert "not checked for the last 13 years" in rules["R7"]["reason"]          # the Leviosa form's question
    assert "12 year(s)" in rules["R7"]["reason"]                   # property documents for 12 of the 13 years
    assert ws(client, "LGV-2026-003")["run"]["overview"]["recommendation"] == "REVIEW_REQUIRED"


def test_an_active_cersai_charge_fails_rule_6_and_escalates(client):
    w = ws(client, "LGV-2026-004")
    r6 = by_id(w)["R6"]
    assert r6["status"] == "FAIL" and r6["severity"] == "CRITICAL" and "CERSAI" in r6["reason"]
    assert any(c["values"].get("AGENCY") == "1 search(es) performed" for c in r6["comparisons"])
    assert w["run"]["overview"]["recommendation"] == "ESCALATE"


# ---------------------------------------------------------------------------------------------- the workflow
def test_upload_send_agency_report_validate_decide(client):
    h, agency = login(client), login(client, "agency.lex")
    up = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=h, data={"kind": "SALE_DEED"},
                     files={"file": ("deed.pdf", _pdf(), "application/pdf")})
    assert up.status_code == 200, up.text
    w = up.json()
    assert w["case"]["status"] == "DOCUMENTS_UPLOADED" and w["can"]["send"]
    assert w["documents"][0]["read_status"] == "STORED"           # no OCR: the file is stored, never read
    assert all(f["value"] is None for f in w["deed"])
    # the CPA enters the Sale Deed's details from the document
    entered = client.post(f"{BASE}/applications/LGV-2026-005/sale-deed", headers=h, json={"values": {
        "owner_names": "Sneha Iyer", "seller_names": "Joseph Dsouza", "document_type": "SALE_DEED",
        "sale_deed_number": "KRL-1/5512/2018", "sale_deed_date": "19-03-2018", "sub_registrar_office": "Sub-Registrar Kurla 1",
        "property_address": "Flat B-1104, Lakeview Towers, Powai, Mumbai", "pin_code": "400076"}})
    assert entered.status_code == 200, entered.text
    deed = {f["field"]: f for f in entered.json()["deed"]}
    assert deed["owner_names"]["value"] == "Sneha Iyer" and deed["owner_names"]["source"] == "SALE_DEED"
    assert deed["sale_deed_date"]["value"] == "2018-03-19"
    changed = client.post(f"{BASE}/applications/LGV-2026-005/sale-deed", headers=h,
                          json={"values": {"sale_deed_number": "KRL-1/5513/2018"}})
    assert changed.json()["error"]["code"] == "REASON_REQUIRED"      # a change to an entered value needs a reason
    w = entered.json()
    preview = client.get(f"{BASE}/documents/{w['deed_document_id']}/file", headers=h)
    assert preview.status_code == 200 and preview.headers["content-type"] == "application/pdf"

    assert client.post(f"{BASE}/applications/LGV-2026-005/send", headers=h,
                       json={"agency": "LEX_ASSOCIATES"}).json()["error"]["code"] == "NOT_CONFIRMED"
    sent = client.post(f"{BASE}/applications/LGV-2026-005/send", headers={**h, "Idempotency-Key": "k-1"},
                       json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True})
    assert sent.json()["case"]["status"] == "WITH_AGENCY"
    again = client.post(f"{BASE}/applications/LGV-2026-005/send", headers={**h, "Idempotency-Key": "k-1"},
                        json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True})
    assert again.status_code == 200                               # the same click twice: one sending

    bad = client.post(f"{BASE}/applications/LGV-2026-005/agency-report", headers=agency,
                      json={"answers": {"sale_deed_date": "not a date"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "REPORT_INVALID"
    fields = {d["field"] for d in bad.json()["detail"]["details"]}
    assert {"owners", "sale_deed_date", "title_docs_checked", "registered_mortgage", "state", "district",
            "chain_maintained", "title_clear_marketable", "flat_title_certified", "report_date", "report_reference",
            "advocate_name", "documents_examined", "opinion_text"} <= fields

    answers = {"owners": [{"first": "Sneha", "middle": "", "last": "Iyer"}], "property_belongs_to": "APPLICANT",
               "documents_provided": "Sale Deed, EC", "property_history": "Bought from Joseph Dsouza in 2018.",
               "title_docs_checked": "yes", "registered_mortgage": "no",
               "property_address": "Flat B-1104, Lakeview Towers, Powai, Mumbai 400076",
               "property_type": "RESIDENTIAL_FLAT", "document_type": "SALE_DEED", "sale_deed_number": "KRL1/5512/2018",
               "sale_deed_date": "19-03-2018", "sub_registrar_office": "SRO Kurla 1", "ec_obtained": "yes",
               "ec_from": "2005-01-01", "ec_to": "2026-10-01", "encumbrance_found": "no", "cersai_records_found": "no",
               "cersai_searches": [{"search_type": "DEBTOR_BASED", "result": "No relevant results"}],
               "property_doc_years": 20, "property_type": "RESIDENTIAL_FLAT", "state": "Maharashtra",
               "district": "Mumbai Suburban", "title_traced_adequately": "yes", "chain_maintained": "yes",
               "ec_verified": "yes", "flat_title_certified": "yes", "flat_undivided_share": "na",
               "equitable_mortgage_possible": "yes", "title_clear_marketable": "yes", "roc_search": "na",
               "report_date": "2026-10-01", "report_reference": "LV/2026/0005", "advocate_name": "Adv. Test Signer",
               "documents_examined": [{"instrument": "SALE_DEED", "reg_no": "KRL1/5512/2018", "date": "19-03-2018",
                                       "executed_by": "Joseph Dsouza", "in_favour_of": "Sneha Iyer", "copy": "ORIGINAL"}],
               "opinion_text": "Title clear and marketable, subject to the original deed being deposited.",
               "opinion_conditions": [{"condition": "Original Sale Deed deposited before disbursal"}]}
    early = client.post(f"{BASE}/applications/LGV-2026-005/agency-report/generate", headers=agency)
    assert early.status_code == 409 and early.json()["error"]["code"] == "NO_AGENCY_REPORT"   # nothing submitted yet
    before = len(ws(client, "LGV-2026-005")["documents"])
    done = client.post(f"{BASE}/applications/LGV-2026-005/agency-report", headers=agency, json={"answers": answers})
    assert done.status_code == 200, done.text
    submitted = done.json()
    # Submit saves the answers only: no report generated yet, no rule run -- the option to generate it is offered
    assert submitted["agency_report"]["document_id"] is None and submitted["can"]["generate_report"] is True
    assert submitted["run"] is None and submitted["case"]["status"] == "REPORT_RECEIVED"
    assert len(submitted["documents"]) == before
    w = client.post(f"{BASE}/applications/LGV-2026-005/agency-report/generate", headers=agency).json()
    rep = w["agency_report"]
    assert w["can"]["generate_report"] is False
    again = client.post(f"{BASE}/applications/LGV-2026-005/agency-report/generate", headers=agency).json()
    assert again["agency_report"]["document_id"] == rep["document_id"]                  # once per submitted report
    assert len(again["documents"]) == before + 1
    attached = next(d for d in w["documents"] if d["document_id"] == rep["document_id"])
    assert attached["kind"] == "AGENCY_REPORT" and attached["file_name"] == "title-verification-summary-LGV-2026-005.pdf"
    tones = {x["text"]: x["tone"] for x in rep["analysis"]}
    assert tones["The owners on the property papers (Sneha Iyer) are the owners named in the application."] == "ok"
    assert tones["Registered as document no. KRL1/5512/2018 on 19 Mar 2018 at SRO Kurla 1."] == "info"
    assert tones["1 CERSAI search(es), no charge found."] == "ok"
    assert tones["The property type is Residential flat, as in the application."] == "ok"
    assert tones["The title documents for the last 13 years were checked."] == "ok"
    assert tones["The certificate is conditional: 1 condition(s) must be met before it holds."] == "warn"
    assert tones["The agency examined 1 document(s), 1 of them original."] == "info"
    pdf = client.get(f"{BASE}/documents/{rep['document_id']}/file", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    import unicodedata

    import pymupdf
    with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:          # NFKC: the PDF's "ﬁ" ligatures as "fi"
        text = unicodedata.normalize("NFKC", " ".join(p.get_text() for p in doc)).replace("\n", " ")
    assert "Title Verification Summary" in text and "Analysis of the findings" in text and "KRL1/5512/2018" in text
    assert "not the advocate's report or opinion" in text            # the system's summary never poses as the opinion
    assert "AGENCY_REPORT_GENERATED" in [e["event"] for e in w["events"]]
    rules = by_id(w)
    assert w["case"]["status"] == "VALIDATED" and rules["R5"]["status"] == "PASS" and rules["R1"]["status"] == "PASS"
    assert rules["R4"]["status"] == "PASS"                       # the agency confirms the chain and gives the history
    assert w["run"]["overview"]["recommendation"] == "REVIEW_REQUIRED"

    assert client.post(f"{BASE}/applications/LGV-2026-005/decision", headers=h,
                       json={"decision": "ESCALATE_TO_LEGAL"}).json()["error"]["code"] == "REMARKS_REQUIRED"
    final = client.post(f"{BASE}/applications/LGV-2026-005/decision", headers=h,
                        json={"decision": "PROCEED", "remarks": ""})
    assert final.status_code == 200 and final.json()["case"]["status"] == "DECIDED"
    assert client.post(f"{BASE}/applications/LGV-2026-005/validate", headers=h).json()["error"]["code"] == "ALREADY_DECIDED"
    events = [e["event"] for e in final.json()["events"]]
    for e in ("DOCUMENT_UPLOADED", "SENT_TO_AGENCY", "AGENCY_REPORT_SUBMITTED", "VALIDATION_RUN", "DECISION_RECORDED"):
        assert e in events


def test_the_cpa_edits_the_application_details(client):
    h = login(client)
    res = client.put(f"{BASE}/applications/LGV-2026-005/details", headers=h, json={"values": {
        "owner_middle_name": "Ravi", "address_line2": "undefined", "landmark": "", "owner_pan": "ABCPI9876K"}})
    assert res.status_code == 200, res.text
    w = res.json()
    fields = {f["id"]: f["value"] for g in w["application"]["view"] for f in g["fields"]}
    assert fields["owner_middle_name"] == "Ravi" and fields["owner_pan"] == "XXXXXX876K"
    assert "undefined" not in (fields["complete_address"] or "")      # never the literal word in the address
    assert w["application"]["owner_names"] == ["Sneha Ravi Iyer"]
    assert "APPLICATION_EDITED" in [e["event"] for e in w["events"]]
    assert client.put(f"{BASE}/applications/LGV-2026-005/details", headers=h,
                      json={"values": {"applicant_name": "x"}}).status_code == 422          # not an editable field
    assert client.put(f"{BASE}/applications/LGV-2026-005/details", headers=login(client, "viewer.mumbai"),
                      json={"values": {"landmark": "x"}}).status_code == 403
    sample = client.get(f"{BASE}/applications/LGV-2026-005/sample?part=application", headers=h).json()["values"]
    assert sample["pin_code"].isdigit() and sample["owner_first_name"] == "Sneha"
    geo = {f["id"]: f["value"] for f in ws(client, "LGV-2026-001")["application"]["geo"]}
    assert geo["latitude"] and geo["captured_at"]


def test_the_rules_run_only_once_the_agency_report_is_in_and_the_agency_attaches_its_file(client):
    h, agency = login(client), login(client, "agency.lex")
    assert ws(client, "LGV-2026-006")["can"]["validate"] is False                 # with the agency, no report yet
    res = client.post(f"{BASE}/applications/LGV-2026-006/validate", headers=h)
    assert res.status_code == 409 and res.json()["error"]["code"] == "NO_AGENCY_REPORT"
    up = client.post(f"{BASE}/applications/LGV-2026-006/documents", headers=agency, data={"kind": "AGENCY_REPORT"},
                     files={"file": ("legal-report.pdf", _pdf(), "application/pdf")})
    assert up.status_code == 200, up.text
    assert any(d["kind"] == "AGENCY_REPORT" for d in up.json()["documents"])
    res = client.post(f"{BASE}/applications/LGV-2026-006/documents", headers=agency, data={"kind": "SALE_DEED"},
                      files={"file": ("deed.pdf", _pdf(), "application/pdf")})
    assert res.status_code == 403                                                  # the agency only attaches its report


def test_save_the_vendor_then_waive(client):
    h = login(client)
    saved = client.post(f"{BASE}/applications/LGV-2026-005/vendor", headers=h, json={"agency": "TITLEWISE"})
    assert saved.status_code == 200 and saved.json()["case"]["selected_agency"] == "TITLEWISE"
    assert saved.json()["case"]["status"] == "NEW"                  # saving the vendor does not send the case
    assert client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=h,
                       json={"reason": " "}).json()["error"]["code"] == "WAIVE_REASON_REQUIRED"
    w = client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=h, json={"reason": "Property already under our charge"}).json()
    assert w["case"]["status"] == "WAIVED" and w["case"]["waive_reason"] == "Property already under our charge"
    assert not any(w["can"][k] for k in ("upload", "send", "save", "waive", "decide"))
    assert client.post(f"{BASE}/applications/LGV-2026-005/send", headers=h, json={"agency": "TITLEWISE",
                       "confirmation_accepted": True}).json()["error"]["code"] == "ALREADY_WAIVED"
    assert "CASE_WAIVED" in [e["event"] for e in w["events"]]
    assert client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=login(client, "viewer.mumbai"),
                       json={"reason": "x"}).status_code == 403


def test_application_details_show_masked_pans_only(client):
    w = ws(client, "LGV-2026-001")
    fields = {f["id"]: f["value"] for g in w["application"]["view"] for f in g["fields"]}
    assert fields["owner_pan"].startswith("XXXXXX") and fields["owner_pan"].endswith("821A")
    assert fields["owner_first_name"] == "Aditya" and fields["city"] == "Navi Mumbai" and fields["collateral_no"] == "Collateral 1"
    res = client.put(f"{BASE}/applications/LGV-T-9", headers={"Authorization": f"Bearer {client.make_token(subject='los', scopes='los.write')}"},
                     json={"branch": "MUM", "applicant_name": "Test Person", "property_type": "RESIDENTIAL_FLAT",
                           "property_address": "Flat 1, Test Street, Mumbai 400001",
                           "details": {"owner_first_name": "Test", "owner_last_name": "Person", "owner_pan": "ABCPT1234Z"}})
    assert res.status_code == 200, res.text
    stored = ws(client, "LGV-T-9")
    pan = next(f["value"] for g in stored["application"]["view"] for f in g["fields"] if f["id"] == "owner_pan")
    assert pan == "XXXXXX234Z" and "ABCPT" not in str(stored)       # the full PAN is never stored
    assert stored["application"]["owner_names"] == ["Test Person"]


def test_old_demo_rows_get_their_details_without_a_reset(client, database):
    """8010's demo rows were seeded before migration 0003: their details are filled, nothing else is touched."""
    with database.tx() as conn:
        conn.execute("UPDATE legal_verification_applications SET details = '{}'::jsonb WHERE application_id = 'LGV-2026-006'")
        before = conn.execute("SELECT count(*) AS n FROM legal_verification_audit WHERE application_id = 'LGV-2026-006'").fetchone()["n"]
    assert seed.fill_missing_details(database) == 1
    assert seed.fill_missing_details(database) == 0                    # once
    fields = {f["id"]: f["value"] for g in ws(client, "LGV-2026-006")["application"]["view"] for f in g["fields"]}
    assert fields["owner_first_name"] == "Karan" and fields["city"] == "Vashi"
    with database.tx() as conn:
        after = conn.execute("SELECT count(*) AS n FROM legal_verification_audit WHERE application_id = 'LGV-2026-006'").fetchone()["n"]
    assert after == before                                             # the case's history is untouched


def test_proceed_is_blocked_while_the_recommendation_is_escalate(client):
    res = client.post(f"{BASE}/applications/LGV-2026-002/decision", headers=login(client), json={"decision": "PROCEED"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "PROCEED_BLOCKED"


def test_a_deed_correction_is_audited_but_the_rules_read_the_agency(client):
    h = login(client)
    w = ws(client, "LGV-2026-002")
    res = client.post(f"{BASE}/documents/{w['deed_document_id']}/corrections", headers=h,
                      json={"field": "owner_names", "value": "Vikas Shetty", "reason": ""})
    assert res.json()["error"]["code"] == "REASON_REQUIRED"
    res = client.post(f"{BASE}/documents/{w['deed_document_id']}/corrections", headers=h,
                      json={"field": "owner_names", "value": "Vikas Shetty", "reason": "Misread from the scan"})
    deed = {f["field"]: f for f in res.json()["deed"]}
    assert deed["owner_names"]["value"] == "Vikas Shetty" and deed["owner_names"]["source"] == "CORRECTED"
    rules = by_id(client.post(f"{BASE}/applications/LGV-2026-002/validate", headers=h).json())
    # the Sale Deed's details come from the agency's report: a CPA's re-keying does not clear a mismatch
    assert rules["R1"]["status"] == "FAIL" and rules["R1"]["comparisons"][0]["values"] == {
        "APPLICATION": "Vikas Shetty", "AGENCY": "Prakash Shetty"}
    assert "FIELD_CORRECTED" in [e["event"] for e in ws(client, "LGV-2026-002")["events"]]


# ---------------------------------------------------------------------------------------------- who may do what
def test_branches_and_permissions(client):
    assert client.get(f"{BASE}/applications/LGV-2026-001", headers=login(client, "cpa.pune")).status_code == 404
    assert client.get(f"{BASE}/applications", headers=login(client, "cpa.pune")).json()["applications"] == []
    viewer = login(client, "viewer.mumbai")
    assert ws(client, "LGV-2026-001", "viewer.mumbai")["can"]["upload"] is False
    res = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=viewer, data={"kind": "SALE_DEED"},
                      files={"file": ("deed.pdf", _pdf(), "application/pdf")})
    assert res.status_code == 403
    res = client.post(f"{BASE}/applications/LGV-2026-006/agency-report", headers=login(client), json={"answers": {}})
    assert res.status_code == 403                              # the CPA does not fill the agency's report


def test_upload_refuses_a_wrong_file(client):
    h = login(client)
    res = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=h, data={"kind": "SALE_DEED"},
                      files={"file": ("x.exe", b"MZ...", "application/octet-stream")})
    assert res.status_code == 415
    res = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=h, data={"kind": "NOPE"},
                      files={"file": ("deed.pdf", _pdf(), "application/pdf")})
    assert res.status_code == 422


# ---------------------------------------------------------------------------------------------- outputs
def test_fill_sample_is_realistic_and_agrees_with_the_application(client):
    h = login(client)
    d = client.get(f"{BASE}/applications/LGV-2026-006/sample?part=deed", headers=h).json()["values"]
    assert d["owner_names"] == ["Karan Mehta"] and d["pin_code"] == "400703" and d["document_type"] == "SALE_DEED"
    r = client.get(f"{BASE}/applications/LGV-2026-006/sample?part=report", headers=h).json()["values"]
    assert r["sale_deed_number"] == "VSH-2/3315/2020"                # the report describes the deed on file
    assert r["title_history"][-1]["to_owner"] == "Karan Mehta" and len(r["cersai_searches"]) == 2
    done = client.post(f"{BASE}/applications/LGV-2026-006/agency-report", headers=login(client, "agency.lex"),
                       json={"answers": r})
    assert done.status_code == 200, done.text
    assert done.json()["run"] is None                              # submitting runs no rule: generating the report does
    rules = by_id(client.post(f"{BASE}/applications/LGV-2026-006/agency-report/generate", headers=h).json())
    for rid in ("R1", "R3", "R4", "R5", "R6", "R8", "R9"):
        assert rules[rid]["status"] == "PASS", (rid, rules[rid]["reason"])
    assert client.get(f"{BASE}/applications/LGV-2026-006/sample?part=nope", headers=h).status_code == 422


def test_summary_points_follow_the_order_and_the_pdf_has_everything(client):
    h = login(client)
    s = client.get(f"{BASE}/applications/LGV-2026-002/summary", headers=h).json()
    assert s["available"] and s["source"] == "rules"
    assert s["points"][0].startswith("Critical: Owner Details Match")
    assert s["points"][-1].startswith("Next action:")
    assert any(p.startswith("Agency vs documents:") for p in s["points"])
    res = client.get(f"{BASE}/applications/LGV-2026-002/report", headers=h)
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    import pymupdf

    import unicodedata

    text = unicodedata.normalize("NFKC", " ".join(p.get_text() for p in pymupdf.open(stream=res.content, filetype="pdf")))
    text = " ".join(text.split())
    assert "Escalate" in text and "Owner Details Match" in text and "CERSAI" in text
    # the agency's report in its current fields: every section, the deed's details, and the analysis
    for heading in ("Property owner and document details", "Mortgage and disbursal requirements", "Property location",
                    "Legal opinion and title checks", "Search reports and title chain"):
        assert heading in text, heading
    assert "HVL-2/1107/2015" in text and "Haveli 2" in text and "Prakash Shetty" in text
    assert "Drawn only from the answers" in text and "ADVERSE" in text
    assert "Not answered Not answered Not answered" not in text      # no block of old field names left empty


def test_the_ai_points_are_checked_and_names_go_back_in_order():
    from app.agents.credit.legal_verification.services import summary as s

    source = ["Mismatch: Owner -- [name] differs from [name].", "Passed: Address", "Next action: Verify."]
    assert s.accepted(source, ["The owner [name] differs from [name].", "Passed: address.", "Next: verify."])
    assert not s.accepted(source, ["Owner differs.", "Passed: address."])                      # a point dropped
    assert not s.accepted(source, ["The owner check passed.", "Passed: address.", "Next: verify."])  # not a pass
    assert not s.accepted(source, ["Owner differs in 3 places.", "Passed.", "Next."])               # a new number
    assert not s.accepted(source, ["Title is clear.", "Passed.", "Next."])                           # banned
    assert s._unmasked("[name] vs [name]", ["A B", "C D"]) == "A B vs C D"
    assert s._unmasked("[name] only", ["A B", "C D"]) is None


def test_the_mcp_tool_reads_the_case_for_the_callers_branches(client):
    from app.mcp import credit

    async def call(scopes: str, user: str):
        credit.CALLER_CLAIMS.set({"sub": user, "scope": scopes})
        return await credit.READ_TOOLS["legal_verification.get"]("LGV-2026-004")

    env = asyncio.run(call("los.credit.underwrite", "cpa.mumbai"))
    assert env.ok and env.result["recommendation"] == "ESCALATE" and len(env.result["rules"]) == 10
    env = asyncio.run(call("los.credit.underwrite", "cpa.pune"))
    assert env.ok is False and env.error.code == "NOT_FOUND"


def test_every_text_key_the_screen_uses_is_in_the_config():
    """The screen has no words of its own: each t("...") key is in legal_verification_wording.yaml `screen`; the keys
    built from a list (nav, steps, loader, groups, report rows) are checked for every value of that list."""
    import re
    from pathlib import Path

    from app.agents.credit.legal_verification.config import words

    screen = words("screen")
    src = Path("app/agents/credit/legal_verification/frontend/src")
    keys = set()
    for f in [*src.rglob("*.tsx"), *src.rglob("*.ts")]:
        if "test" not in f.parts:
            keys |= set(re.findall(r'\bt\("([a-z_.]+)"', f.read_text(encoding="utf-8")))
    keys |= {f"report.{g}.{c}" for g, cols in (("people", ("first", "middle", "last", "add", "remove")),) for c in cols}
    keys |= {f"loader.{s}" for s in ("start", "open", "upload", "deed", "validate", "send", "report", "decide", "save",
                                       "waive")}
    steps = ("application", "report", "rules", "final")
    keys |= {f"loader.switch_{s}" for s in steps} | {f"sections.{s}" for s in steps}
    keys |= {f"steps_hint.{s}_{state}" for s in steps for state in ("open", "done", "waiting")}
    keys |= {f"rules.group.{s}" for s in ("FAIL", "REVIEW", "PASS")}
    keys |= {f"worklist.tiles.{s}" for s in ("total", "new", "agency", "escalate", "review", "clear")}
    keys |= {f"report.chain.{s}" for s in ("date", "from_owner", "to_owner", "document", "add", "remove")}
    keys |= {f"report.searches.{s}" for s in ("search_type", "searched_at", "reference", "parameters", "result", "add", "remove")}
    assert len(keys) > 120

    def has(path: str) -> bool:
        node = screen
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return isinstance(node, str)

    assert sorted(k for k in keys if not has(k)) == []


def test_the_agency_report_analysis_reads_only_the_answers():
    from datetime import date
    from app.agents.credit.legal_verification.services import agency_report

    app = {"owner_names": ["Vikas Shetty"], "details": {"district": "Pune", "state": "Maharashtra"}}
    answers = {"owner_names": ["Prakash Shetty"], "document_type": "SALE_DEED", "sale_deed_number": "HVL-2/1107/2015",
               "sale_deed_date": "2015-02-11", "sub_registrar_office": "Haveli 2", "district": "Pune",
               "state": "Maharashtra", "encumbrance_found": "no", "ec_from": "2008-01-01", "ec_to": "2026-09-25",
               "cersai_records_found": "no", "cersai_searches": [{"search_type": "DEBTOR_BASED"}],
               "title_history": [{"date": "1988-05-09", "from_owner": "Govind Joshi", "to_owner": "Anil Deshpande"},
                                 {"date": "2015-02-11", "from_owner": "Anil Deshpande", "to_owner": "Prakash Shetty"}],
               "title_clear_marketable": "yes"}
    lines = agency_report.analysis(app, answers, date(2026, 10, 29))
    assert lines == agency_report.analysis(app, answers, date(2026, 10, 29))      # the same answers, the same report
    by = {x["text"]: x["tone"] for x in lines}
    assert by["The owners on the property papers (Prakash Shetty) differ from the application (Vikas Shetty)."] == "bad"
    assert by["The property lies in Pune, Maharashtra, as in the application."] == "ok"
    assert by["The title runs without a break through 2 transfer(s) from Govind Joshi, over 38 year(s)."] == "ok"
    assert by["No encumbrance in the Encumbrance Certificate over 18 year(s)."] == "ok"
    # the agency calls the title clear although its own findings are adverse: the report says so
    assert lines[-1] == {"tone": "bad", "text": "The title is declared clear and marketable, yet 1 finding(s) above are adverse."}
    broken = agency_report.analysis(app, {**answers, "title_history": [
        {"date": "1988-05-09", "from_owner": "Govind Joshi", "to_owner": "Anil Deshpande"},
        {"date": "2015-02-11", "from_owner": "Ramesh Kale", "to_owner": "Prakash Shetty"}]}, date(2026, 10, 29))
    assert {"tone": "bad", "text": "The title chain breaks: Anil Deshpande → Ramesh Kale."} in broken


def test_seeded_reports_are_generated_and_the_option_returns_after_a_new_submission(client, database):
    w = ws(client, "LGV-2026-001")
    assert w["agency_report"]["document_id"] and w["agency_report"]["analysis"] and not w["can"]["generate_report"]
    with database.tx() as conn:                                     # a submitted report not yet generated
        conn.execute("UPDATE legal_verification_agency_reports SET document_id = NULL, analysis = '[]' "
                     "WHERE report_id = %s", (w["agency_report"]["report_id"],))
    assert ws(client, "LGV-2026-001")["can"]["generate_report"] is True
    again = client.post(f"{BASE}/applications/LGV-2026-001/agency-report/generate", headers=login(client)).json()
    assert again["agency_report"]["document_id"] and again["agency_report"]["analysis"] == w["agency_report"]["analysis"]
    assert again["agency_report"]["answers"] == w["agency_report"]["answers"]      # nothing else touched


def test_purge_demo_removes_only_demo_rows_and_their_files(client, database):
    from app.agents.credit.legal_verification.config import documents_dir

    with database.tx() as conn:                                     # one REAL application next to the demo ones
        conn.execute("INSERT INTO legal_verification_applications (application_id, branch, applicant_name, property_type, "
                     "property_address, owner_names, is_demo) VALUES ('LGV-REAL-1', 'MUM', 'Test Owner', "
                     "'RESIDENTIAL_FLAT', 'Unit 1, Test Lane', '[\"Test Owner\"]', false)")
        demo_files = [r["file_ref"] for r in conn.execute("SELECT file_ref FROM legal_verification_documents")]
    other = documents_dir() / "not-ours.pdf"                        # a file no demo row references
    other.write_bytes(b"%PDF-1.4")
    done = seed.purge_demo(database)
    from app.agents.credit.legal_verification.config import demo
    assert done["applications"] == len(demo()["applications"]) and done["files"] == len(demo_files)
    assert other.exists() and not any((documents_dir() / f).exists() for f in demo_files)
    with database.tx() as conn:
        left = {r["application_id"] for r in conn.execute("SELECT application_id FROM legal_verification_applications")}
        demo_ids = {str(a["application_id"]) for a in demo()["applications"]}
        # every demo application gone; every real one kept (an earlier test's real intake too)
        assert "LGV-REAL-1" in left and not left & demo_ids
        assert conn.execute("SELECT count(*) AS n FROM legal_verification_documents").fetchone()["n"] == 0
    other.unlink()
    seed.reset(database)                                            # the module's later tests start from the demo again


def test_the_analysis_weighs_the_ec_the_chain_and_a_conditional_opinion():
    from datetime import date
    from app.agents.credit.legal_verification.services import agency_report

    today = date(2026, 10, 30)
    app = {"owner_names": ["Asha Rao"], "details": {"district": "Pune", "state": "Maharashtra"}}
    base = {"owner_names": ["Asha Rao"], "report_date": "2026-10-20", "report_reference": "LV/1", "advocate_name": "Adv. X",
            "sale_deed_number": "S-2", "sale_deed_date": "2020-12-14", "sub_registrar_office": "SRO 1",
            "district": "Pune", "state": "Maharashtra", "title_clear_marketable": "yes",
            "ec_from": "2012-01-01", "ec_to": "2026-10-01", "encumbrance_found": "no",
            "title_history": [
                {"date": "2016-03-03", "instrument": "SALE_DEED", "reg_no": "S-1", "from_owner": "City Board", "to_owner": "Kiran Das"},
                {"date": "2020-12-14", "instrument": "SALE_DEED", "reg_no": "S-2", "from_owner": "Kiran Das", "to_owner": "Asha Rao"},
                {"date": "2020-12-22", "instrument": "RECTIFICATION_DEED", "reg_no": "R-9", "from_owner": "Kiran Das",
                 "to_owner": "Asha Rao"}],
            "ec_entries": [
                {"date": "2016-03-03", "nature": "TRANSFER", "doc_no": "S-1"},
                {"date": "2020-12-14", "nature": "TRANSFER", "doc_no": "S-2"},
                {"date": "2020-12-22", "nature": "MORTGAGE", "doc_no": "M-1", "executant": "Asha Rao",
                 "claimant": "Sahyadri Home Finance Ltd"},
                {"date": "2023-03-13", "nature": "FURTHER_CHARGE", "doc_no": "M-2", "executant": "Asha Rao",
                 "claimant": "Sahyadri Home Finance"},
                {"date": "2025-01-07", "nature": "RELEASE", "doc_no": "R-1", "executant": "Sahyadri Home Finance Ltd",
                 "claimant": "Asha Rao"},
                {"date": "2025-01-07", "nature": "MORTGAGE", "doc_no": "M-3", "executant": "Asha Rao",
                 "claimant": "Western Small Finance Bank Ltd"}]}

    def tones(answers):
        return {x["text"]: x["tone"] for x in agency_report.analysis(app, answers, today)}

    # the open charge with no condition on the opinion: an adverse finding -- never read as active, never as discharged
    plain = tones(base)
    assert plain["The EC shows a charge with no release after it: Western Small Finance Bank Ltd (2025-01-07, doc. M-3)."] == "warn"
    assert plain["The EC shows a charge released later: Sahyadri Home Finance Ltd (released 2025-01-07). The lender's No Due is still needed to confirm it."] == "info"
    assert plain["The certificate states no condition, yet a charge in the EC is still open."] == "bad"
    # a rectification moves no title: the chain is unbroken, and both transfers are in the EC under their numbers
    assert plain["Corrective deed(s) on the title, moving no ownership: Rectification Deed R-9 (2020-12-22)."] == "info"
    assert not any(t.startswith("The title chain breaks") for t in plain)
    assert plain["2 of 2 registered transfer(s) also appear in the Encumbrance Certificate."] == "ok"
    # the same report, conditional on that lender's release and No Due: the condition is shown, not a pass
    conditional = tones(base | {
        "opinion_conditions": [{"condition": "Release deed and No Due of Western Small Finance Bank produced"}],
        "documents_required": [{"document": "No Due certificate from Western Small Finance Bank", "stage": "PRIOR_TO_MORTGAGE"}]})
    assert conditional["The certificate is conditional: 1 condition(s) must be met before it holds."] == "warn"
    assert conditional["The release and No Due for Western Small Finance Bank Ltd are among the documents the bank must take: the opinion depends on them."] == "warn"
    assert "The certificate states no condition, yet a charge in the EC is still open." not in conditional
    # a transfer missing from the EC, a period that disagrees, copies only, a boundary left out
    odd = tones(base | {"ec_entries": base["ec_entries"][1:], "search_from_year": 2012, "search_to_year": 2026,
                        "ec_from": "2013-01-01",
                        "documents_examined": [{"instrument": "RECTIFICATION_DEED", "copy": "PHOTOCOPY"}],
                        "boundary_north": "Unit 56", "boundary_south": "Unit 58", "boundary_east": "Road"})
    assert odd["Not found in the Encumbrance Certificate: S-1 (2016-03-03)."] == "warn"
    assert odd["The search period reported (2012–2026) differs from the Encumbrance Certificate's (2013–2026)."] == "warn"
    assert odd["The agency examined 1 document(s), none of them an original."] == "warn"
    assert odd["Boundaries not given: West."] == "warn"
    # the rules agree: a rectification is not a break in the chain
    from app.agents.credit.legal_verification.rules import engine
    ev = engine.Evidence(application={"owner_names": ["Asha Rao"]}, agency=base, today=today)
    assert engine.history(ev)["status"] != "FAIL"


def test_the_demo_starts_ready_to_initiate_and_the_report_arrives_after_initiate(client, database, monkeypatch):
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "READY_TO_INITIATE")
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "on")
    try:
        seed.reset(database)
        h = login(client)
        for app_id in ("LGV-2026-001", "LGV-2026-002", "LGV-2026-005", "LGV-2026-006"):
            w = ws(client, app_id)
            # documents already uploaded, the legal vendor already chosen, nothing sent: Initiate is open
            assert w["case"]["status"] == "DOCUMENTS_UPLOADED" and w["deed_document_id"], app_id
            assert w["case"]["selected_agency"] and w["case"]["agency"] is None and w["can"]["send"] is True, app_id
            assert w["agency_report"] is None and w["run"] is None
        # Initiate: the agency's report arrives in the Verification Report; the rules wait for the report to be generated
        w = client.post(f"{BASE}/applications/LGV-2026-002/send", headers={**h, "Idempotency-Key": "demo-1"},
                        json={"agency": "TITLEWISE", "confirmation_accepted": True}).json()
        assert w["agency_report"] and w["case"]["status"] == "REPORT_RECEIVED" and w["run"] is None
        assert w["can"]["generate_report"] is True                  # the pop-up offers it
        w = client.post(f"{BASE}/applications/LGV-2026-002/agency-report/generate", headers=h).json()
        assert w["run"] and w["case"]["status"] == "VALIDATED"
        assert by_id(w)["R1"]["status"] == "FAIL"                  # the demo agency reports the deed's real owner
        # a case with no report in the demo file gets a realistic one, consistent with the case
        w = client.post(f"{BASE}/applications/LGV-2026-005/send", headers={**h, "Idempotency-Key": "demo-2"},
                        json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True}).json()
        assert w["agency_report"]["answers"]["owner_names"] == w["application"]["owner_names"]
        w = client.post(f"{BASE}/applications/LGV-2026-005/agency-report/generate", headers=h).json()
        assert w["run"] and by_id(w)["R1"]["status"] == "PASS"
        assert [e["event"] for e in w["events"]].count("AGENCY_REPORT_SUBMITTED") == 1
    finally:
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "STAGED")
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "off")
        seed.reset(database)                                        # the module's later tests start from the staged demo


def test_the_demo_starts_with_nothing_uploaded_initiate_opens_after_the_sale_deed(client, database, monkeypatch):
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "READY_TO_UPLOAD")
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "on")
    try:
        seed.reset(database)
        h = login(client)
        w = ws(client, "LGV-2026-002")
        # nothing uploaded, nothing sent: the legal vendor is chosen, Initiate waits for the Sale Deed
        assert w["documents"] == [] and w["case"]["status"] == "NEW" and w["case"]["selected_agency"]
        assert w["can"]["send"] is False and w["agency_report"] is None and w["run"] is None
        refused = client.post(f"{BASE}/applications/LGV-2026-002/send", headers={**h, "Idempotency-Key": "u-0"},
                              json={"agency": "TITLEWISE", "confirmation_accepted": True})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "NO_SALE_DEED"
        up = client.post(f"{BASE}/applications/LGV-2026-002/documents", headers=h, data={"kind": "SALE_DEED"},
                         files={"file": ("deed.pdf", _pdf(), "application/pdf")})
        assert up.status_code == 200 and up.json()["can"]["send"] is True                 # now Initiate opens
        w = client.post(f"{BASE}/applications/LGV-2026-002/send", headers={**h, "Idempotency-Key": "u-1"},
                        json={"agency": "TITLEWISE", "confirmation_accepted": True}).json()
        assert w["agency_report"] and w["run"] is None and w["case"]["status"] == "REPORT_RECEIVED"
        w = client.post(f"{BASE}/applications/LGV-2026-002/agency-report/generate", headers=h).json()
        assert w["run"] and w["case"]["status"] == "VALIDATED"
    finally:
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "STAGED")
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "off")
        seed.reset(database)


def test_the_rail_shows_a_configured_name_for_a_user_id(client, monkeypatch):
    from app.agents.credit.legal_verification import config as c

    monkeypatch.setitem(c.cfg(), "user_names", {"cpa.mumbai": "A Configured Name"})
    assert client.get(f"{BASE}/config", headers=login(client)).json()["user"]["name"] == "A Configured Name"
    assert client.get(f"{BASE}/config", headers=login(client, "viewer.mumbai")).json()["user"]["name"] == "viewer.mumbai"


def test_end_to_end_upload_initiate_report_rules_summary_decision_pdf(client, database, monkeypatch):
    """The whole journey on one case, as the CPA meets it in the demo."""
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "READY_TO_UPLOAD")
    monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "on")
    try:
        seed.reset(database)
        h, app_id = login(client), "LGV-2026-002"
        w = ws(client, app_id)
        assert w["case"]["status"] == "NEW" and not w["documents"] and not w["can"]["send"]     # 1. nothing yet
        w = client.post(f"{BASE}/applications/{app_id}/documents", headers=h, data={"kind": "SALE_DEED"},
                        files={"file": ("deed.pdf", _pdf(), "application/pdf")}).json()          # 2. the Sale Deed
        assert w["can"]["send"] and w["case"]["status"] == "DOCUMENTS_UPLOADED"
        w = client.post(f"{BASE}/applications/{app_id}/send", headers={**h, "Idempotency-Key": "e2e"},
                        json={"agency": w["case"]["selected_agency"], "confirmation_accepted": True}).json()   # 3. Initiate
        assert w["agency_report"] and w["run"] is None                                            # 4. the agency's answers
        assert w["can"]["generate_report"]                                                         #    the pop-up offers it
        w = client.post(f"{BASE}/applications/{app_id}/agency-report/generate", headers=h).json()  # 5. the report, attached
        assert w["agency_report"]["document_id"] and w["agency_report"]["analysis"]
        assert w["run"] and w["case"]["status"] == "VALIDATED"                                     # 6. THEN the rules on it
        rules = by_id(w)
        assert len(rules) == 10 and rules["R1"]["status"] == "FAIL"                               #    the owner differs
        assert w["run"]["overview"]["recommendation"] == "ESCALATE"
        s = client.get(f"{BASE}/applications/{app_id}/summary", headers=h).json()               # 6. the summary in points
        assert s["points"] and s["points"][-1].startswith("Next action:")
        blocked = client.post(f"{BASE}/applications/{app_id}/decision", headers=h, json={"decision": "PROCEED"})
        assert blocked.status_code == 409                                                         # 7. no Proceed on Escalate
        w = client.post(f"{BASE}/applications/{app_id}/decision", headers=h,
                        json={"decision": "ESCALATE_TO_LEGAL", "remarks": "Owner on the deed differs from the application."}).json()
        assert w["case"]["status"] == "DECIDED" and w["case"]["decision"] == "ESCALATE_TO_LEGAL"
        pdf = client.get(f"{BASE}/applications/{app_id}/report", headers=h)                      # 8. the CPA's PDF
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        events = [e["event"] for e in ws(client, app_id)["events"]]                                # 9. the audit trail
        for e in ("CASE_OPENED", "DOCUMENT_UPLOADED", "SENT_TO_AGENCY", "AGENCY_REPORT_SUBMITTED", "VALIDATION_RUN",
                  "AGENCY_REPORT_GENERATED", "DECISION_RECORDED", "REPORT_DOWNLOADED"):
            assert e in events, e
    finally:
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_START", "STAGED")
        monkeypatch.setenv("LEGAL_VERIFICATION_DEMO_AGENCY_REPLY", "off")
        seed.reset(database)


# ---------------------------------------------------------------------------------------------- production: negatives
def _code(res):
    body = res.json()
    return res.status_code, (body.get("error") or {}).get("code") or (body.get("detail") or {}).get("code")


def test_every_action_refuses_what_it_must(client, database, monkeypatch):
    """Wrong role, wrong state, wrong input, wrong file: each refused with its own code; nothing half-written."""
    from app.agents.credit.legal_verification import config as c

    h, viewer, agency = login(client), login(client, "viewer.mumbai"), login(client, "agency.lex")
    pune = login(client, "cpa.pune")
    try:
        # unknown application / another branch's case: not found, never shown
        assert client.get(f"{BASE}/applications/LGV-NOPE", headers=h).status_code == 404
        assert client.get(f"{BASE}/applications/LGV-2026-002", headers=pune).status_code == 404
        doc = ws(client, "LGV-2026-002")["documents"][0]["document_id"]
        assert client.get(f"{BASE}/documents/{doc}/file", headers=pune).status_code == 404
        assert client.get(f"{BASE}/documents/DOC-NOPE/file", headers=h).status_code == 404
        # Initiate: a panel agency, a Sale Deed, the right role, once
        url = f"{BASE}/applications/LGV-2026-005/send"
        assert _code(client.post(url, headers=h, json={"agency": "NOPE", "confirmation_accepted": True})) == (422, "UNKNOWN_AGENCY")
        assert _code(client.post(url, headers=h, json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True})) == (409, "NO_SALE_DEED")
        assert client.post(url, headers=viewer, json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True}).status_code == 403
        assert _code(client.post(f"{BASE}/applications/LGV-2026-006/send", headers={**h, "Idempotency-Key": "n-1"},
                                 json={"agency": "LEX_ASSOCIATES", "confirmation_accepted": True})) == (409, "ALREADY_WITH_AGENCY")
        # the legal vendor is fixed once the case is sent
        assert _code(client.post(f"{BASE}/applications/LGV-2026-006/vendor", headers=h,
                                 json={"agency": "TITLEWISE"})) == (409, "ALREADY_WITH_AGENCY")
        assert _code(client.post(f"{BASE}/applications/LGV-2026-005/vendor", headers=h, json={"agency": "NOPE"})) == (422, "UNKNOWN_AGENCY")
        # uploads: a file, a size; the agency may attach only its own report
        up = f"{BASE}/applications/LGV-2026-005/documents"
        assert _code(client.post(up, headers=h, data={"kind": "SALE_DEED"},
                                 files={"file": ("e.pdf", b"", "application/pdf")})) == (422, "FILE_EMPTY")
        limit = c.cfg()["documents"]["max_mb"]
        c.cfg()["documents"]["max_mb"] = 0                         # (restored at once: the fixtures keep their patches)
        try:
            assert _code(client.post(up, headers=h, data={"kind": "SALE_DEED"},
                                     files={"file": ("d.pdf", _pdf(), "application/pdf")})) == (413, "FILE_TOO_LARGE")
        finally:
            c.cfg()["documents"]["max_mb"] = limit
        assert client.post(up, headers=agency, data={"kind": "SALE_DEED"},
                           files={"file": ("d.pdf", _pdf(), "application/pdf")}).status_code == 403
        # the agency's report: only on a case sent to it, every answer valid, nothing half-written
        rep = f"{BASE}/applications/LGV-2026-005/agency-report"
        assert _code(client.post(rep, headers=agency, json={"answers": {}})) == (409, "NOT_WITH_AGENCY")
        rep6 = f"{BASE}/applications/LGV-2026-006/agency-report"
        good = client.get(f"{BASE}/applications/LGV-2026-006/sample?part=report", headers=h).json()["values"]
        bad = client.post(rep6, headers=agency, json={"answers": {**good, "documents_examined": [{"instrument": "NOT_ONE"}]}})
        assert _code(bad) == (422, "REPORT_INVALID")
        assert "documents_examined" in {d["field"] for d in bad.json()["detail"]["details"]}
        bad_date = {**good, "ec_entries": [{"date": "not a date", "nature": "TRANSFER"}]}
        assert _code(client.post(rep6, headers=agency, json={"answers": bad_date})) == (422, "REPORT_INVALID")
        assert ws(client, "LGV-2026-006")["agency_report"] is None
        # generate / decide / the PDF before their turn
        assert _code(client.post(f"{BASE}/applications/LGV-2026-006/agency-report/generate", headers=h)) == (409, "NO_AGENCY_REPORT")
        assert client.post(f"{BASE}/applications/LGV-2026-002/agency-report/generate", headers=viewer).status_code == 403
        assert _code(client.post(f"{BASE}/applications/LGV-2026-006/decision", headers=h,
                                 json={"decision": "ESCALATE_TO_LEGAL", "remarks": "x"})) == (409, "NOT_VALIDATED")
        assert _code(client.post(f"{BASE}/applications/LGV-2026-002/decision", headers=h,
                                 json={"decision": "APPROVE_EVERYTHING"})) == (422, "UNKNOWN_DECISION")
        assert client.post(f"{BASE}/applications/LGV-2026-002/decision", headers=viewer,
                           json={"decision": "REJECT_COLLATERAL", "remarks": "x"}).status_code == 403
        assert _code(client.get(f"{BASE}/applications/LGV-2026-006/report", headers=h)) == (409, "NOT_VALIDATED")
        # a newer report waits for its own checks: no decision on the old run
        newer = client.get(f"{BASE}/applications/LGV-2026-001/sample?part=report", headers=h).json()["values"]
        submitted = client.post(f"{BASE}/applications/LGV-2026-001/agency-report", headers=agency, json={"answers": newer})
        assert submitted.status_code == 200 and submitted.json()["can"]["decide"] is False
        assert _code(client.post(f"{BASE}/applications/LGV-2026-001/decision", headers=h,
                                 json={"decision": "PROCEED"})) == (409, "RULES_PENDING")
        # waive: a reason, the role, never once the agency's report is in
        assert _code(client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=h, json={"reason": "  "})) == (422, "WAIVE_REASON_REQUIRED")
        assert client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=viewer, json={"reason": "x"}).status_code == 403
        assert _code(client.post(f"{BASE}/applications/LGV-2026-002/waive", headers=h, json={"reason": "x"})) == (409, "REPORT_ALREADY_IN")
        # a waived case takes nothing more
        assert client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=h, json={"reason": "Pre-approved title"}).status_code == 200
        for res in (client.post(up, headers=h, data={"kind": "SALE_DEED"}, files={"file": ("d.pdf", _pdf(), "application/pdf")}),
                    client.post(f"{BASE}/applications/LGV-2026-005/vendor", headers=h, json={"agency": "TITLEWISE"}),
                    client.post(f"{BASE}/applications/LGV-2026-005/waive", headers=h, json={"reason": "again"}),
                    client.put(f"{BASE}/applications/LGV-2026-005/details", headers=h, json={"values": {"landmark": "x"}})):
            assert res.status_code == 409, res.text
        # a decided case takes nothing more
        assert client.post(f"{BASE}/applications/LGV-2026-002/decision", headers=h,
                           json={"decision": "ESCALATE_TO_LEGAL", "remarks": "Owner differs."}).status_code == 200
        for res in (client.post(f"{BASE}/applications/LGV-2026-002/decision", headers=h,
                                json={"decision": "REJECT_COLLATERAL", "remarks": "x"}),
                    client.post(f"{BASE}/applications/LGV-2026-002/validate", headers=h),
                    client.post(f"{BASE}/applications/LGV-2026-002/documents", headers=h, data={"kind": "SALE_DEED"},
                                files={"file": ("d.pdf", _pdf(), "application/pdf")})):
            assert _code(res) == (409, "ALREADY_DECIDED"), res.text
        # demo-only helpers are refused outside demo mode
        c.cfg()["demo_mode"] = False
        try:
            assert _code(client.get(f"{BASE}/applications/LGV-2026-006/sample?part=report", headers=h)) == (403, "DEMO_ONLY")
        finally:
            c.cfg()["demo_mode"] = True
    finally:
        seed.reset(database)


def test_a_value_not_found_stays_in_the_row_as_not_found():
    from app.agents.credit.legal_verification.rules import engine

    ev = engine.Evidence(application={"owner_names": ["A B"], "details": {"district": "Pune", "state": "Maharashtra"}},
                         agency={"owner_names": ["A B"], "state": "Maharashtra"})
    rows = {r["label"]: r for r in engine.address(ev)["comparisons"]}
    district = next(r for name, r in rows.items() if name.lower().startswith("district"))
    assert district["values"] == {"APPLICATION": "Pune", "AGENCY": None} and district["match"] is None


def test_a_file_is_what_its_bytes_say_and_a_table_is_bounded(client, database):
    h, agency = login(client), login(client, "agency.lex")
    try:
        fake = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=h, data={"kind": "SALE_DEED"},
                           files={"file": ("deed.pdf", b"MZ\x90\x00 not a pdf at all", "application/pdf")})
        assert fake.status_code == 415 and fake.json()["error"]["code"] == "FILE_TYPE"
        png = client.post(f"{BASE}/applications/LGV-2026-005/documents", headers=h, data={"kind": "SALE_DEED"},
                          files={"file": ("deed.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")})
        assert png.status_code == 200                                  # a real PNG header: accepted
        good = client.get(f"{BASE}/applications/LGV-2026-006/sample?part=report", headers=h).json()["values"]
        many = {**good, "ec_entries": [{"date": "2020-01-01", "nature": "TRANSFER", "doc_no": str(i)} for i in range(61)]}
        res = client.post(f"{BASE}/applications/LGV-2026-006/agency-report", headers=agency, json={"answers": many})
        assert res.status_code == 422
        assert any(d["field"] == "ec_entries" and "more rows" in d["message"] for d in res.json()["detail"]["details"])
    finally:
        seed.reset(database)


def test_two_clicks_at_once_generate_one_report(client, database):
    """Generate pressed twice at the same moment: the case row is locked -- one report, one run."""
    from concurrent.futures import ThreadPoolExecutor

    h, agency = login(client), login(client, "agency.lex")
    try:
        answers = client.get(f"{BASE}/applications/LGV-2026-006/sample?part=report", headers=h).json()["values"]
        assert client.post(f"{BASE}/applications/LGV-2026-006/agency-report", headers=agency,
                           json={"answers": answers}).status_code == 200
        url = f"{BASE}/applications/LGV-2026-006/agency-report/generate"
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: client.post(url, headers=h).status_code, range(2)))
        assert codes == [200, 200]
        w = ws(client, "LGV-2026-006")
        generated = [d for d in w["documents"] if d["document_id"] == w["agency_report"]["document_id"]]
        assert len(generated) == 1
        assert sum(1 for d in w["documents"] if d["kind"] == "AGENCY_REPORT") == 1          # not two
        assert [e["event"] for e in w["events"]].count("VALIDATION_RUN") == 1               # one run of the rules
    finally:
        seed.reset(database)
