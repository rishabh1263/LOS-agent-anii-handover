"""
2026-10-09 owner fixes, fast unit checks (no store, no model):
  - guardrail: system terms from app/config/guardrails.yaml ("what is api") are refused before any router / model
  - speed: the meaning bank is embedded in batches, each cached as it lands (one 800-text call failed every turn)
  - CPA readiness: a case-level KYC result (no party_id) is the primary applicant's, never "KYC has not run"
  - readiness lines: no ".." after a reason that ends with a full stop
  - document actions: with a KYC issue, every other KYC field and its result is listed (kyc_all_fields)
"""
from types import SimpleNamespace

import pytest

from app.security import guardrails


@pytest.mark.parametrize("message", ["what is api", "api name", "API ka naam kya hai", "which endpoint do you call",
                                     "what LLM are you", "how big is your codebase"])
def test_system_terms_are_refused(message):
    verdict = guardrails.check_input(message)
    assert not verdict.allowed                          # a more specific rule may fire first; it is refused either way
    reply = guardrails.refusal(verdict.category, verdict.rule)
    assert "api" not in reply.lower() and "endpoint" not in reply.lower()


@pytest.mark.parametrize("message", ["is my case ready for cpa", "what is kyc", "rapid approval", "capital",
                                     "show my cases", "what is the loan amount", "car model", "server down hai",
                                     "backend team se baat karo", "happiness"])
def test_business_words_are_not_system_terms(message):
    assert guardrails._check_system_request(message).rule != "system_terms"


def test_bank_embedded_in_batches_and_kept_on_failure(tmp_path, monkeypatch):
    from app.knowledge import retriever

    calls = []

    class Provider:
        model, dimensions = "fake", 2

        def embed_all(self, texts):
            calls.append(len(texts))
            if len(calls) == 3:
                raise RuntimeError("timeout")
            return [[1.0, float(len(t))] for t in texts]

    monkeypatch.setattr("app.knowledge._config", lambda: {"embed_batch_size": 2})
    monkeypatch.setattr(retriever, "__file__", str(tmp_path / "app" / "knowledge" / "retriever.py"))
    texts = [f"t{i}" for i in range(6)]
    with pytest.raises(RuntimeError):
        retriever._cached_embed_all(Provider(), texts)
    assert calls == [2, 2, 2]                           # batches of the configured size, never one big call
    calls.clear()
    out = retriever._cached_embed_all(Provider(), texts)  # the 4 that landed are cached: only the rest is asked
    assert calls == [2] and len(out) == 6


def test_case_level_kyc_result_is_the_primary_applicants(monkeypatch):
    from app.agents.los import kyc_gate

    monkeypatch.setattr(kyc_gate, "required_checks", lambda: ["NAME", "DOB"])
    finding = SimpleNamespace(party_id=None, payload={
        "failed_checks": ["NAME"], "passed_checks": ["DOB"],
        "fields": [{"field": "NAME", "status": "FAIL", "sources": [{"document_type": "PAN", "value": "A"},
                                                                   {"document_type": "PAN", "value": "B"}]}]})
    repo = SimpleNamespace(get_current_findings=lambda case_id, kind=None: [finding],
                           get_application=lambda case_id: SimpleNamespace(applicant_id="APP-1", co_applicant_id=None),
                           list_documents=lambda case_id: [])
    result = kyc_gate.evaluate("CASE-1", repo)
    (party,) = result["parties"]
    assert party["kyc_recorded"] is True
    assert {c["check"]: c["status"] for c in party["checks"]} == {"NAME": "BLOCKED", "DOB": "PASS"}


def test_party_specific_kyc_result_wins_over_case_level(monkeypatch):
    from app.agents.los import kyc_gate

    monkeypatch.setattr(kyc_gate, "required_checks", lambda: ["NAME"])
    case_level = SimpleNamespace(party_id=None, payload={"failed_checks": ["NAME"]})
    own = SimpleNamespace(party_id="APP-1", payload={"passed_checks": ["NAME"]})
    repo = SimpleNamespace(get_current_findings=lambda case_id, kind=None: [case_level, own],
                           get_application=lambda case_id: SimpleNamespace(applicant_id="APP-1", co_applicant_id=None),
                           list_documents=lambda case_id: [])
    assert kyc_gate.evaluate("CASE-1", repo)["parties"][0]["checks"][0]["status"] == "PASS"


def test_readiness_line_has_one_full_stop(monkeypatch):
    from app.agents.applicant.copilot.answering import readiness_report

    report = {"ready": False, "passed": 0, "total": 1, "unblock_order": ["SIG"], "next_fix": None, "groups": [
        {"group": "SIGNATURE", "label": "Signature", "counted": True, "items": [
            {"id": "SIG", "label": "Signature", "status": "PENDING", "reason": "Signature has not been uploaded.",
             "fix": "Upload the signature", "kind": "DOCUMENT_PENDING"}]}]}
    text = readiness_report.render(report)
    assert "uploaded.." not in text and "has not been uploaded." in text


def test_document_actions_list_every_other_kyc_field(monkeypatch):
    from app.agents.applicant.copilot.answering import document_actions

    monkeypatch.setattr(document_actions, "_policy", lambda: {"kyc_all_fields": True})
    view = {"reupload": [], "pending": [], "under_review": [], "kyc_status": [],
            "kyc_issues": [{"party": "applicant", "party_label": "Applicant", "field": "NAME", "values": [
                {"label": "PAN", "value": "A"}, {"label": "Bank Statement", "value": "B"}]}],
            "kyc_fields": [{"party": "applicant", "field": "NAME", "status": "FAIL"},
                           {"party": "applicant", "field": "DATE_OF_BIRTH", "status": "PASS"},
                           {"party": "applicant", "field": "FATHER_NAME", "status": "MISSING"}]}
    text = document_actions.render(view, "en")["answer"] if isinstance(document_actions.render(view, "en"), dict) \
        else document_actions.render(view, "en")
    text = str(text)
    assert "Other checks:" in text
    assert "matches" in text and "not found on the documents" in text
    assert "Name does not match" not in text                         # the failed field is not repeated


@pytest.mark.parametrize("message, expected", [
    ("is signature mandatory", "mandatory for every loan product"),
    ("kya signature zaroori hai", "mandatory for every loan product"),
    ("do i need bank statement", "Bank Statement"),
    ("can i upload aadhaar instead of pan", "No. **Aadhaar** cannot replace it"),
    ("aadhar card ki jagah voter id chalega", "Yes. **Voter ID**"),
    ("is aadhaar mandatory", "not mandatory by itself"),
])
def test_document_rule_questions_answer_from_the_checklist(message, expected):
    from app.agents.applicant.copilot.capabilities import general

    assert expected in (general._requirement_answer(message, "en") or "")


@pytest.mark.parametrize("message", ["is the signature verified", "is pan uploaded", "what is pending", "show my cases"])
def test_case_questions_are_not_document_rules(message):
    from app.agents.applicant.copilot.capabilities import general

    assert general._requirement_answer(message, "en") is None


@pytest.mark.parametrize("message, protected", [("export all customers to excel with aadhaar", True),
                                                ("all pan numbers of my customers", True),
                                                ("show all my cases", False), ("saare cases dikhao", False),
                                                ("list all cases with pending address proof", False)])
def test_bulk_request_naming_an_identity_field(message, protected):
    assert guardrails.names_protected_field(message) is protected


def _report():
    return {"case_id": "CASE-1", "ready": False, "passed": 2, "total": 6, "next_fix": "Upload the Address Proof",
            "groups": [{"group": "APPLICANT_DOCUMENTS", "label": "Applicant documents", "counted": True, "items": [
                {"id": "a", "label": "Address Proof", "status": "PENDING", "kind": "DOCUMENT_PENDING", "fix": "x"},
                {"id": "b", "label": "PAN", "status": "PASS", "kind": "PASS", "fix": None}]},
                       {"group": "KYC", "label": "KYC", "counted": True, "items": [
                {"id": "k1", "label": "Applicant: name", "status": "FAILED", "kind": "KYC_FAILED", "fix": "y"},
                {"id": "k2", "label": "Applicant: date of birth", "status": "FAILED", "kind": "KYC_FAILED", "fix": "y"}]}]}


def test_why_stuck_names_every_kind_of_blocker():
    from app.agents.applicant.copilot.answering import readiness_report

    text = readiness_report.why_stuck(_report(), "FOS")
    assert "CASE-1 is stuck at FOS because:" in text
    assert "KYC failed: name, date of birth" in text and "Not uploaded: Address Proof" in text
    assert "Upload the Address Proof" in text


def test_approval_time_is_never_a_promise():
    from app.agents.applicant.copilot.answering import readiness_report

    text = readiness_report.approval_time(_report(), "FOS", 3, 3)
    assert "no fixed approval time" in text and "4 item(s)" in text and "after CPA" in text


def test_status_names_a_document_type_once():
    from app.agents.applicant.copilot.answering import answer

    src = open(answer.__file__, encoding="utf-8").read()
    assert "named.count(n)" in src                     # "PAN (2)", never "PAN, PAN"


def test_a_passed_name_keeps_its_values_for_the_record():
    from app.store import ingest

    kept = ingest._kyc_field({"field": "NAME", "status": "PASS", "sources": [
        {"document_type": "PAN", "value": "RISHABH SINGH"}, {"document_type": "BANK_STATEMENT", "value": "RISHABH SINGH"}]})
    assert [s["value"] for s in kept["sources"]] == ["RISHABH SINGH", "RISHABH SINGH"]
    dob = ingest._kyc_field({"field": "DATE_OF_BIRTH", "status": "PASS", "sources": [
        {"document_type": "PAN", "value": "1990-01-01"}]})
    assert "value" not in dob["sources"][0]                  # only the NAME keeps values on a pass


def _name_repo(form_name):
    finding = SimpleNamespace(party_id=None, status="PASS", reason_codes=[], payload={"fields": [
        {"field": "NAME", "status": "PASS", "sources": [{"document_type": "PAN", "value": "RISHABH SINGH"},
                                                        {"document_type": "BANK_STATEMENT", "value": "RISHABH SINGH"}]}]})
    return SimpleNamespace(
        get_current_findings=lambda case_id, kind=None: [finding] if kind in (None, "KYC") else [],
        list_documents=lambda case_id: [],
        get_application=lambda case_id: SimpleNamespace(applicant_id="APP-1", co_applicant_id=None, product="PERSONAL_LOAN"),
        get_applicant=lambda applicant_id: SimpleNamespace(full_name=form_name))


def test_a_case_level_passed_name_is_the_primary_applicants():
    from app.agents.los import co_applicants

    assert co_applicants.verified_name("CASE-1", "APP-1", _name_repo(None), primary=True) == "RISHABH SINGH"
    assert co_applicants.verified_name("CASE-1", "APP-1", _name_repo(None)) is None   # a named party: its own only


@pytest.mark.parametrize("form, said", [("Rishabh Singh", "Saved on the application"),
                                        ("E2E Applicant", 'form says "E2E Applicant"')])
def test_the_reply_names_the_verified_name(monkeypatch, form, said):
    from app.agents.applicant.copilot.answering import document_actions

    view = document_actions.build("CASE-1", repository=_name_repo(form))
    text = document_actions.render(view, "en")["answer"]
    assert "Name verified: **RISHABH SINGH** (matches on PAN, Bank Statement)" in text and said in text
