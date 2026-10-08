"""
MASTER SPEC section 6 -- KYC as two separate checks per party (A: application form vs documents, B: documents vs
each other), the plain verdict when KYC is not complete, the next step naming the exact documents, the customer
message offer; CPA readiness "X of Y checks passed" equal to the gate; a ready case says a person confirms.
Every dev flag on, the markdown + tts reply.
"""

from __future__ import annotations

import pytest

from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def kyc_failed(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    for kind, name in (("PAN", "RAHUL SHARMA"), ("DRIVING_LICENCE", "RAHUL SHARMA"), ("BANK_STATEMENT", "ROHIT VERMA")):
        doc_id = f"{c}:{a}:{kind.lower()}"
        _store.save_document(Document(document_id=doc_id, case_id=c, applicant_id=a, party_id=a, document_type=kind,
                                      status=DocumentStatus.VERIFIED, verification_status="PASS"))
        _store.save_finding(CaseFinding(finding_id=f"x-{kind}", case_id=c, party_id=a, document_id=doc_id,
                                        source_id=f"{kind.lower()}.jpg", finding_kind=FindingKind.EXTRACTION,
                                        status="PASS", payload={"fields": {"name": name}}, content_hash=f"x-{kind}"))
    _store.save_finding(CaseFinding(
        finding_id="k", case_id=c, party_id=a, finding_kind=FindingKind.KYC, status="FAIL",
        reason_codes=["NAME_MISMATCH"], content_hash="k",
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "RAHUL SHARMA"}, {"document_type": "DRIVING_LICENCE", "value": "RAHUL SHARMA"},
            {"document_type": "BANK_STATEMENT", "value": "ROHIT VERMA"}]}]}))
    say(client, f"{c} kholo")
    return a, c


def test_kyc_failed_says_it_plainly_with_a_then_b_and_the_documents(client, kyc_failed):
    md = say(client, "KYC ka status kya hai?")
    lines = md.split("\n")
    assert lines[1] == "Not ready for CPA. KYC is not complete.", md
    a_title, b_title = md.index("A. Application form vs documents"), md.index("B. Documents vs each other")
    assert a_title < b_title
    assert "| Field | Form value | Document | Document value | Result |" in md
    assert "| Field | Document 1 | Value | Document 2 | Value | Result |" in md
    reasons = md[:a_title]
    assert reasons.index("on the Bank Statement") < reasons.index("differs between")       # A reasons, then B
    assert "**Next step:** Ask the customer to upload correct documents that match the application form " \
           "(Applicant: Bank Statement)." in md
    assert "Likely odd one out: the Bank Statement" in md
    assert "(ask:Draft%20a%20message%20to%20the%20customer)" in md


def test_the_draft_link_makes_the_customer_message(client, kyc_failed):
    say(client, "KYC ka status kya hai?")
    md = say(client, "Draft a message to the customer")
    assert "\n> " in md and "(action:copy?ref=draft-1)" in md, md


def test_the_voice_says_the_verdict_and_the_next_step(client, kyc_failed):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "KYC ka status kya hai?",
                                                 "reply_language": "en"}).json()
    assert r["tts"].startswith("Not ready for CPA. KYC is not complete.") and "Next step" in r["tts"], r["tts"]


def test_kyc_in_hindi_keeps_the_lock(client, kyc_failed):
    md = say(client, "KYC ka status kya hai?", lang="hi")
    assert "CPA के लिए तैयार नहीं। KYC पूरा नहीं हुआ।" in md


def test_readiness_counts_equal_the_gate(client, prod, _store):
    from app.agents.applicant.copilot.answering import readiness_report

    _, c = make_case(client, "Rahul Sharma")
    say(client, f"{c} kholo")
    md = say(client, "CPA ke liye kya chahiye?")
    report = readiness_report.build(c)
    assert f"CPA readiness: {report['passed']} of {report['total']} checks passed." in md
    assert report["ready"] is False
