"""
FOS PLAN 6.4 / 6.5 / 11d -- the long tail of case questions: answered by the model ONLY from the case's masked fact
sheet, fact-checked against it; anything not on the sheet is discarded ("not recorded") -- wrong data is never
published. A fake model stands in for Qwen.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.applicant.copilot.capabilities import snapshot_qa
from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


def fake_model(reply: dict):
    async def generate(prefix, content, timeout):
        generate.seen = content
        return {"message": {"content": json.dumps(reply)}}
    return generate


@pytest.fixture
def case(client, demo, _store, monkeypatch):
    monkeypatch.setenv("COPILOT_SNAPSHOT_QA", "true")
    a, c = make_case(client, "Rahul Sharma")
    doc_id = f"{c}:{a}:slip"
    _store.save_document(Document(document_id=doc_id, case_id=c, applicant_id=a, party_id=a,
                                  document_type="SALARY_SLIP", status=DocumentStatus.VERIFIED,
                                  verification_status="PASS"))
    _store.save_finding(CaseFinding(finding_id="x-slip", case_id=c, party_id=a, document_id=doc_id,
                                    source_id="slip.pdf", finding_kind=FindingKind.EXTRACTION, status="PASS",
                                    payload={"fields": {"employer_name": "ACME SOFTWARE PVT LTD", "net_pay": 54000,
                                                        "pay_period": "2026-08"}},
                                    content_hash="x-slip"))
    return a, c


def test_the_fact_sheet_holds_the_case_and_masks_identifiers(case):
    facts = snapshot_qa.fact_sheet(case[1])
    assert facts["extracted.salary_slip.employer_name"] == "ACME SOFTWARE PVT LTD"
    assert facts["document.applicant.salary_slip.status"] == "VERIFIED"
    assert facts["application.product"] and facts["documents.count.total"] == "1"


def test_a_fact_checked_answer_is_published(case):
    model = fake_model({"answer": "The salary slip is from ACME SOFTWARE PVT LTD, net pay 54000.",
                        "keys": ["extracted.salary_slip.employer_name", "extracted.salary_slip.net_pay"]})
    out = asyncio.run(snapshot_qa.answer(case[1], "salary slip kis company ki hai", generator=model))
    assert out["status"] == "ANSWERED" and "ACME" in out["answer"]
    assert "employer_name: ACME SOFTWARE PVT LTD" in model.seen          # the model saw only the sheet


@pytest.mark.parametrize("reply", [
    {"answer": "Net pay is 61000.", "keys": ["extracted.salary_slip.net_pay"]},          # a number not recorded
    {"answer": "The PAN is verified.", "keys": ["document.applicant.pan.status"]},        # a key not on the sheet
    {"answer": "Net pay is 54000.", "keys": []},                                          # no citation
])
def test_anything_not_on_the_sheet_is_discarded(case, reply):
    out = asyncio.run(snapshot_qa.answer(case[1], "net pay kitna hai", generator=fake_model(reply)))
    assert out["status"] == "NOT_RECORDED" and out["answer"].startswith("This is not recorded on the case.")
    assert "61000" not in out["answer"]


def test_not_captured_at_fos_says_where_it_comes_from(case):
    out = asyncio.run(snapshot_qa.answer(case[1], "EMI kitni hogi?", generator=fake_model({"answer": "",
                                                                                         "keys": [],
                                                                                         "missing": "EMI"})))
    assert out["status"] == "NOT_RECORDED" and "Credit stage" in out["answer"]


def test_through_the_api_with_the_model_down_nothing_is_invented(client, case, monkeypatch):
    from app.llm import availability

    monkeypatch.setattr(availability, "provider_reachable", lambda: False)
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": case[1]})
    reply = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY",
                                                      "message": "tenure kitna rakha hai?"}).json()
    assert "54000" not in reply["answer"]
    assert reply["intent"] in ("CASE_SNAPSHOT", "APPLICANT_PROFILE", "UNKNOWN"), reply["intent"]


def test_another_customers_case_is_refused_before_any_read(client, demo, make_token, monkeypatch):
    from tests.integration.test_fos_stage_boundary import FOS_SCOPES

    monkeypatch.setenv("COPILOT_SNAPSHOT_QA", "true")
    own = dict(client.headers)
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    a, theirs = make_case(client, "Not Mine")
    client.headers.clear()
    client.headers.update(own)
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "salary slip kis company ki",
                                                  "case_id": theirs, "applicant_id": a})
    assert r.status_code == 403
