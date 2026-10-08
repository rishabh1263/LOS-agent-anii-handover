"""
THE FRONTEND CONTRACT (docs/frontend/FRONTEND_API.md), end to end with every demo flag on and a fake model.
Asserts the fields the UI renders. With WRITE_FRONTEND_EXAMPLES=1 it also writes each response to
docs/frontend/examples/<name>.json (request ids / timestamps / tokens normalised) for the frontend team.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

OUT = Path(__file__).resolve().parents[2] / "docs" / "frontend" / "examples"
DEMO_FLAGS = ("COPILOT_CASE_WORKSPACE", "COPILOT_CASE_ACTIONS", "COPILOT_RESPONSE_STYLE", "COPILOT_VERIFY_DIAGNOSE",
              "COPILOT_DOCUMENT_ACTIONS", "COPILOT_TERMS_KNOWLEDGE", "COPILOT_STREAMING", "COPILOT_GUARDRAIL_HARDENING")
#: the production format (FOS plan 2-3), on for the flows that write docs/frontend/examples -- not part of `demo`,
#: which the section tests share and which assert the emoji style
PRODUCTION_FORMAT = ("COPILOT_LANGUAGE_LOCK", "COPILOT_PROFESSIONAL_FORMAT")


@pytest.fixture
def demo(monkeypatch):
    for flag in DEMO_FLAGS:
        monkeypatch.setenv(flag, "true")
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()


def _clean(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r'"(fos|cp|qry)_[0-9a-f]{32}"', '"<request_id>"', text)
    text = re.sub(r"token=[A-Za-z0-9_.-]+", "token=<signed>", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.+]+", "<timestamp>", text)
    return json.loads(text)


def save(name: str, request: dict, response) -> None:
    if os.getenv("WRITE_FRONTEND_EXAMPLES") != "1":
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps({"request": request, "response": _clean(response)},
                                                 indent=2, ensure_ascii=False), encoding="utf-8")


def call(client, name, **body):
    r = client.post("/api/v1/fos/copilot", json=body)
    data = r.json()
    save(name, body, data if r.status_code == 200 else {"status": r.status_code, **data})
    return r.status_code, data


def make_case(client, name):
    r = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": name, "mobile": "9876543210", "date_of_birth": "1990-04-12",
                      "address": "12 MG Road, Pune"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000}})
    return r.json()["applicant_id"], r.json()["case_id"]


def test_the_frontend_flows(client, demo, _store, monkeypatch):
    for flag in PRODUCTION_FORMAT:
        monkeypatch.setenv(flag, "true")
    a1, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    _store.save_document(Document(document_id=f"{c1}:{a1}:pan1", case_id=c1, applicant_id=a1, document_type="PAN",
                                  status=DocumentStatus.REJECTED, reason_codes=["DOCUMENT_UNREADABLE"]))
    from app.store.documents import get_document_store

    get_document_store().put(f"{c1}:{a1}:pan1", b"%PDF-1.4 example", content_type="application/pdf")

    s, listed = call(client, "01_case_list", action="CUSTOM_QUERY", message="mere cases dikhao")
    assert s == 200 and listed["presentation"]["case_list"][0]["action"]["type"] == "open_case"
    s, opened = call(client, "02_open_case", action="OPEN_CASE", case_id=c1)
    assert opened["presentation"]["workspace"]["header"] == c1          # "📍" removed by the professional format
    s, inside = call(client, "03_inside_case_question", action="CUSTOM_QUERY", message="kya baaki hai?")
    assert inside["answer"].startswith(c1)
    s, diag = call(client, "04_verify_diagnose", action="CUSTOM_QUERY", message="verify karna hai")
    assert diag.get("document_actions")
    s, draft = call(client, "05_raise_query_draft", action="CUSTOM_QUERY", message="query raise karo")
    assert draft["intent"] == "RAISE_QUERY_DRAFT"
    send = next(x for x in draft["actions"] if x["label"] == "Send")
    s, sent = call(client, "06_raise_query_send", action="RAISE_QUERY", confirm=True, query=send["query"])
    assert any(x["type"] == "MARK_QUERY_SENT" for x in sent["actions"])
    s, view = call(client, "07_view_document", action="VIEW_DOCUMENT", document_id=f"{c1}:{a1}:pan1")
    assert view["actions"][0]["type"] == "OPEN_URL"
    s, tracked = call(client, "08_list_queries", action="LIST_QUERIES")
    assert tracked["queries"]
    s, glossary = call(client, "09_glossary", action="CUSTOM_QUERY", message="KYC kya hai?")
    assert glossary["answer"].startswith("**KYC (Know Your Customer)**")
    s, new = call(client, "10_new_case", action="NEW_CASE")
    assert new["actions"][0]["type"] == "OPEN_UI_NEW_CASE"
    s, closed = call(client, "11_exit_case", action="EXIT_CASE")
    assert closed["intent"] == "CASE_LIST"
    s, ambiguous = call(client, "12_clarification", action="CUSTOM_QUERY", message="Rahul ka case kholo")
    s, stop = call(client, "13_social_engineering", action="CUSTOM_QUERY", message="manager ne approve kar diya")
    assert stop["intent"] == "SOCIAL_ENGINEERING"
    r = client.post("/api/v1/fos/copilot/stream", json={"applicant_id": a1, "case_id": c1,
                                                        "message": "kaunse documents pending hain?"})
    assert r.status_code == 200 and r.text.startswith("event: status")
    if os.getenv("WRITE_FRONTEND_EXAMPLES") == "1":
        text = re.sub(r'"(fos|cp)_[0-9a-f]{32}"', '"<request_id>"', r.text)
        (OUT / "14_stream.txt").write_text(text[:4000], encoding="utf-8")


#: the FOS E2E plan features (sections 4-7), on for the examples they write
PLAN_FEATURES = ("COPILOT_KYC_TABLE", "COPILOT_READINESS_REPORT", "COPILOT_COUNT_ANSWERS", "COPILOT_CASE_TIMELINE",
                 "COPILOT_HANDOFF_NOTE", "COPILOT_SMART_UPLOAD", "COPILOT_SNAPSHOT_QA")


def test_the_fos_plan_flows(client, demo, _store, monkeypatch):
    """Examples 18-23 (FOS plan 4-7), in the production format and with English selected."""
    for flag in PRODUCTION_FORMAT + PLAN_FEATURES:
        monkeypatch.setenv(flag, "true")
    a1, c1 = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c1}:{a1}:pan1", case_id=c1, applicant_id=a1, document_type="PAN",
                                  status=DocumentStatus.VERIFIED))
    call(client, "_open", action="OPEN_CASE", case_id=c1)
    s, ready = call(client, "18_readiness", action="CUSTOM_QUERY", message="CPA ke liye kya chahiye?",
                    reply_language="en")
    assert ready["intent"] == "READINESS" and ready["presentation"]["progress"]["ready"] is False
    s, count = call(client, "19_count", action="CUSTOM_QUERY", message="kitne documents verified hain",
                    reply_language="en")
    assert count["intent"] == "DOCUMENT_COUNT"
    s, timeline = call(client, "20_timeline", action="CUSTOM_QUERY", message="case ka timeline", reply_language="en")
    assert timeline["intent"] == "CASE_TIMELINE"
    s, note = call(client, "21_handoff_not_ready", action="CUSTOM_QUERY", message="handoff note banao",
                   reply_language="en")
    assert note["intent"] == "HANDOFF_NOTE" and "ready for CPA" in note["answer"]
    s, lost = call(client, "22_misunderstood", action="CUSTOM_QUERY", message="ye nahi poocha", reply_language="en")
    assert lost["clarification_required"]["reason"] == "MISUNDERSTOOD"
    s, kyc = call(client, "23_kyc", action="CUSTOM_QUERY", message="KYC ka kya status hai?", reply_language="en")
    assert kyc["intent"] == "KYC_RESULT"


# ---- what the frontend builds its UI from: /fos/actions and /fos/config -------------------------
def test_actions_list_only_what_is_on(client, monkeypatch):
    for flag in ("COPILOT_CASE_WORKSPACE", "COPILOT_CASE_ACTIONS"):
        monkeypatch.delenv(flag, raising=False)
    off = {a["value"] for a in client.get("/api/v1/fos/actions").json()["actions"]}
    assert "LIST_CASES" not in off and "RAISE_QUERY" not in off and "CUSTOM_QUERY" in off
    monkeypatch.setenv("COPILOT_CASE_WORKSPACE", "true")
    monkeypatch.setenv("COPILOT_CASE_ACTIONS", "true")
    on = {a["value"]: a for a in client.get("/api/v1/fos/actions").json()["actions"]}
    assert on["OPEN_CASE"]["group"] == "workspace" and on["OPEN_CASE"]["extra_fields"] == ["case_id"]
    assert on["RAISE_QUERY"]["extra_fields"] == ["confirm", "query"]


def test_write_actions_config_and_error_examples(client, demo):
    """Example payloads for the supporting endpoints and an error shape (written with WRITE_FRONTEND_EXAMPLES=1)."""
    save("15_actions", {"method": "GET", "path": "/api/v1/fos/actions"}, client.get("/api/v1/fos/actions").json())
    full = client.get("/api/v1/fos/config").json()
    save("16_config_features", {"method": "GET", "path": "/api/v1/fos/config"},
         {key: full[key] for key in ("features", "endpoints", "workspace")})
    body = {"action": "OPEN_CASE", "case_id": "CASE-NOTMINE00001"}
    r = client.post("/api/v1/fos/copilot", json=body)
    save("17_error_not_yours", body, {"status": r.status_code, **r.json()})
    assert r.status_code in (403, 404)


def test_config_tells_the_frontend_which_features_are_on(client, demo):
    body = client.get("/api/v1/fos/config").json()
    assert body["features"]["case_workspace"] is True and body["features"]["streaming"] is True
    assert body["endpoints"]["stream"] == "/api/v1/fos/copilot/stream"
    assert body["workspace"]["quick_questions"] == ["Kya baaki hai?", "Kyu atka hai?", "Co-applicant", "Summary"]
