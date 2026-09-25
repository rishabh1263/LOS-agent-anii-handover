"""
The Universal Copilot answers from the CURRENT authoritative case state.

THE LIVE DEFECT. CASE-E2CC6749D0C5 was processed twice. The first run read
the PAN name as "INHU AS" (an extraction bug, since fixed); the second, as
"TINKU DAS". Both runs' findings were kept -- correctly: that is the case's
history. But case memory handed EVERY run's findings to the answer
builders, oldest first, and the mismatch explanation took the first
comparison it met. So the Copilot kept telling the officer the PAN said
"INHU AS" long after the pipeline had corrected it.

THE RULE THESE TESTS HOLD:

    a case answer is built from the CURRENT finding of each kind --
    the latest-written one -- scoped to the case and the party asked
    about, and quoted, not rephrased.

Everything here runs through the real HTTP route, the real agent and a
real SQLite store, fed by the real ingest path the LOS pipeline uses.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
FOS = "/api/v1/fos/copilot"

FOS_SCOPES = ["read_applicant", "read_application", "read_documents",
              "read_verification", "read_pending_items", "read_next_action"]


# ==========================================================================
# FIXTURES
# ==========================================================================


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "truth.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def processed(case_id="CASE-A", applicant_id="APP-A", *,
              pan_name="RISHABH AJIT SINGH", slip_name="VENKATESH GOUD MARAGOUNI",
              bank_name=None, pan_status="PASS", documents=("PAN", "SALARY_SLIP"),
              dl_name="RISHABH SINGH", eligibility=None, income=None) -> dict:
    """
    One LOS result, shaped as the pipeline returns it, and persisted
    through the same ingest the /los/process route calls.
    """
    from app.store.ingest import persist_los_result

    docs, compared = [], []
    if "PAN" in documents:
        pan = {"source_id": "pan.jpg", "type": "PAN", "party_id": applicant_id,
               "verification": pan_status,
               "reason_codes": [] if pan_status == "PASS" else ["PAN_STRUCTURE_INVALID"]}
        if pan_status == "PASS":
            pan["extraction"] = {"name": pan_name, "father_name": "AJIT SINGH",
                                 "date_of_birth": "2002-06-12",
                                 "pan_number": "NUHPS4875K"}
            compared.append({"source_id": "pan.jpg", "document_type": "PAN",
                             "value": pan_name})
        docs.append(pan)
    if "SALARY_SLIP" in documents:
        docs.append({"source_id": "slip.pdf", "type": "SALARY_SLIP",
                     "party_id": applicant_id, "verification": "PASS",
                     "reason_codes": [],
                     "extraction": {"name": slip_name, "employer_name": "SHRIRAM"}})
        compared.append({"source_id": "slip.pdf", "document_type": "SALARY_SLIP",
                         "value": slip_name})
    if "BANK_STATEMENT" in documents:
        docs.append({"source_id": "bank.pdf", "type": "BANK_STATEMENT",
                     "party_id": applicant_id, "verification": "PASS",
                     "reason_codes": [],
                     # A statement's released extraction carries no holder
                     # name; KYC records the one it compared.
                     "extraction": {"account_number_masked": "XXXXXX1015"}})
        compared.append({"source_id": "bank.pdf", "document_type": "BANK_STATEMENT",
                         "value": bank_name})
    if "DRIVING_LICENCE" in documents:
        docs.append({"source_id": "dl.jpg", "type": "DRIVING_LICENCE",
                     "party_id": applicant_id, "verification": "PASS",
                     "reason_codes": [], "extraction": {"name": dl_name}})

    names = {s["value"] for s in compared if s["value"]}
    mismatch = len(names) > 1
    result = {
        "request_id": f"r-{case_id}", "applicant_id": applicant_id,
        "case_id": case_id, "status": "PARTIAL" if mismatch else "SUCCESS",
        "decision": "REVIEW" if mismatch else "PASS",
        "next_action": "MANUAL_REVIEW" if mismatch else "PROCEED",
        "documents": docs,
    }
    if len(compared) >= 2:
        result["kyc"] = {
            "status": "REVIEW" if mismatch else "PASS",
            "overall_score": 10 if mismatch else 95, "overall_confidence": 90,
            "reason_codes": ["NAME_MISMATCH"] if mismatch else [],
            "fields": [{"field": "NAME", "status": "FAIL" if mismatch else "PASS",
                        "match_score": 10 if mismatch else 100, "confidence": 90,
                        "reason_code": "NAME_MISMATCH" if mismatch else None,
                        "sources": [s for s in compared if s["value"]]}],
        }
    if eligibility:
        result["eligibility"] = eligibility
    if income:
        result["income_consistency"] = income
    persist_los_result(result)
    return result


def ask(client, message, case_id="CASE-A", applicant_id="APP-A", **extra):
    return client.post(COPILOT, json={"applicant_id": applicant_id,
                                      "case_id": case_id, "message": message,
                                      **extra})


def answered(client, message, **kw) -> dict:
    response = ask(client, message, **kw)
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# POSITIVE
# ==========================================================================


def test_p1_the_pan_name_is_the_recorded_extraction(client, repo):
    processed()

    body = answered(client, "What is my PAN name?")

    assert body["intent"] == "DOCUMENT_DETAILS"
    assert "RISHABH AJIT SINGH" in body["answer"]
    assert body["grounded"] is True
    cited = {(s.get("finding_kind"), s.get("document_type")) for s in body["sources"]}
    assert ("EXTRACTION", "PAN") in cited


@pytest.mark.parametrize("question", [
    "Which name is on my PAN?",
    "What name was extracted from my PAN?",
    "What is the name on my PAN card?",
])
def test_p1_every_phrasing_reads_the_same_record(client, repo, question):
    processed()
    assert "RISHABH AJIT SINGH" in answered(client, question)["answer"]


def test_p2_the_salary_slip_name(client, repo):
    processed()

    body = answered(client, "What is the name on my salary slip?")

    assert "VENKATESH GOUD MARAGOUNI" in body["answer"]
    assert "RISHABH" not in body["answer"]


def test_p3_the_bank_account_holder_as_kyc_recorded_it(client, repo):
    processed(documents=("PAN", "BANK_STATEMENT"), bank_name="GUDDI DEVI")

    body = answered(client, "What is the account holder name on my bank statement?")

    assert "GUDDI DEVI" in body["answer"]
    assert body["grounded"] is True
    assert any(s.get("finding_kind") == "KYC" for s in body["sources"])


def test_p4_pan_versus_salary_slip_names_both_recorded_values(client, repo):
    processed()

    body = answered(client, "Does my PAN match my salary slip?")

    assert body["intent"] == "CASE_HISTORY"
    assert "RISHABH AJIT SINGH" in body["answer"]
    assert "VENKATESH GOUD MARAGOUNI" in body["answer"]
    assert body["grounded"] is True


def test_p5_application_status(client, repo):
    processed()

    body = answered(client, "What is my application status?")

    assert body["intent"] == "APPLICATION_STATUS"
    assert body["grounded"] is True
    # The tools that ran: the application read, and the checklist the
    # planner always adds for a case question.
    assert body["tool_invoked"] == ["application.get", "documents.checklist"]


def test_p6_pending_documents(client, repo):
    processed(documents=("PAN",))

    body = answered(client, "What documents are pending?")

    assert body["intent"] == "DOCUMENTS_PENDING"
    assert body["grounded"] is True
    assert "documents.get" in body["tool_invoked"]


def test_p7_verification_result(client, repo):
    processed()

    body = answered(client, "Is my PAN verified?")

    assert body["intent"] == "DOCUMENT_VERIFICATION"
    assert body["grounded"] is True
    assert body["tool_invoked"][0] == "documents.verification"


def test_p8_the_recorded_kyc_finding_explains_the_review(client, repo):
    processed()

    body = answered(client, "Why is my application under review?")

    assert "RISHABH AJIT SINGH" in body["answer"]
    assert "VENKATESH GOUD MARAGOUNI" in body["answer"]
    assert "under review" in body["answer"]  # the recorded REVIEW, in words
    kinds = {s.get("kind") for s in body["sources"]}
    assert {"case_finding", "case_decision"} <= kinds
    assert body["grounded"] is True


def test_p9_income_consistency(client, repo):
    processed(income={"status": "PASS", "reason_codes": ["INCOME_CONSISTENT"],
                      "salary_slip": {"net_pay": 29866.0},
                      "bank_statement": {"salary_credit_monthly": 29866.0}})

    body = answered(client, "Does my salary slip match my bank statement?")

    assert body["intent"] == "INCOME_EVIDENCE"
    assert body["answer"]


def test_p10_eligibility_is_read_not_computed(client, repo):
    processed(eligibility={
        "status": "PASS", "reason_codes": ["ELIGIBILITY_WITHIN_POLICY"],
        "foir": {"status": "PASS", "value_pct": 23.93, "limit_pct": 50.0},
        "ltv": {"status": "NOT_APPLICABLE"},
        "inputs": {"monthly_income": 50000.0, "income_source": "SALARY_SLIP_NET"},
        "policy": {"id": "PL_DUMMY_V1", "version": "1", "status": "CONFIRMED"}})

    body = answered(client, "What is my eligibility status?")

    assert body["intent"] == "ELIGIBILITY"
    assert "23.93%" in body["answer"]
    assert body["grounded"] is True


def test_p11_risk_stays_downstream_and_discloses_nothing(client, repo):
    """
    THE STAGE BOUNDARY, kept. The FOS Copilot does not own risk: the
    question is routed, never answered, and carries no case data.
    """
    processed()

    body = answered(client, "What is my risk status?")

    assert body["category"] == "DOWNSTREAM"
    assert body["grounded"] is False
    assert "RISHABH" not in body["answer"]


def test_p12_the_recorded_decision(client, repo):
    processed()

    body = answered(client, "What is the current decision?")

    assert body["intent"] == "CASE_HISTORY"
    assert "under review" in body["answer"]  # the recorded REVIEW, in words
    assert body["grounded"] is True


def test_p13_case_360_aggregates_the_stored_records(make_token, repo):
    import main

    processed()
    # The officer who opened the case owns it (app/security/access.py).
    repo.grant_access("test-subject", "APPLICANT", "APP-A")
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})

    response = c.post(FOS, json={"applicant_id": "APP-A", "case_id": "CASE-A",
                                 "action": "GET_CASE_360"})

    assert response.status_code == 200, response.text
    body = response.json()
    stored = {d.document_type for d in repo.list_documents("CASE-A")}
    shown = {d.get("type") or d.get("document_type") for d in body["documents"]}
    assert stored <= shown
    assert body["grounded"] is True


def test_p15_a_generic_question_carries_no_case_data(client, repo):
    processed()

    body = answered(client, "What does PAN name mismatch mean?")

    assert body["category"] == "KNOWLEDGE_ONLY"
    for name in ("RISHABH", "VENKATESH"):
        assert name not in body["answer"]
    assert not [s for s in body["sources"] if s.get("kind") == "case_finding"]
    assert body["tool_invoked"] == []


# ==========================================================================
# NEGATIVE
# ==========================================================================


def test_n1_one_case_never_answers_with_another_cases_name(client, repo):
    processed("CASE-A", "APP-A", pan_name="NAME ALPHA")
    processed("CASE-B", "APP-B", pan_name="NAME BRAVO")

    answer = answered(client, "What is my PAN name?")["answer"]

    assert "NAME ALPHA" in answer
    assert "NAME BRAVO" not in answer


@pytest.mark.parametrize("message", ["What is my PAN name?",
                                     "Why is my application under review?"])
def test_n2_n11_another_applicants_case_is_refused(client, repo, message):
    processed("CASE-A", "APP-A", pan_name="NAME ALPHA")
    processed("CASE-B", "APP-B", pan_name="NAME BRAVO")

    response = ask(client, message, case_id="CASE-B", applicant_id="APP-A")

    assert response.status_code == 403
    assert "NAME BRAVO" not in response.text


def test_n3_a_reprocessed_pan_answers_with_the_new_name(client, repo):
    """THE LIVE DEFECT, on the direct question."""
    processed(pan_name="INHU AS")
    processed(pan_name="TINKU DAS")

    answer = answered(client, "What is my PAN name?")["answer"]

    assert "TINKU DAS" in answer
    assert "INHU AS" not in answer


def test_n3_the_mismatch_explanation_uses_the_new_name(client, repo):
    """THE LIVE DEFECT, exactly as it was seen."""
    processed(pan_name="INHU AS")
    processed(pan_name="TINKU DAS")

    for question in ("What exactly is the mismatch in my documents?",
                     "Why is my application under review?"):
        answer = answered(client, question)["answer"]
        assert "TINKU DAS" in answer, question
        assert "INHU AS" not in answer, question


def test_n3_a_run_that_returns_to_an_earlier_result_is_current(client, repo):
    """
    OLD -> NEW -> OLD. The third run's findings are identical to the
    first's, so the store updates the first run's rows in place and their
    `created_at` does not move. Ordered by creation, NEW would still look
    current; by last write, OLD is -- which is what was processed last.
    """
    processed(pan_name="FIRST NAME")
    processed(pan_name="SECOND NAME")
    processed(pan_name="FIRST NAME")

    answer = answered(client, "What is my PAN name?")["answer"]

    assert "FIRST NAME" in answer
    assert "SECOND NAME" not in answer


def test_n4_history_is_kept_but_never_answers(client, repo):
    processed(pan_name="OLD NAME")
    processed(pan_name="NEW NAME")

    history = [f.payload.get("name") for f in repo.get_case_findings("CASE-A")
               if f.payload.get("pan_number")]
    assert history == ["OLD NAME", "NEW NAME"]      # nothing was deleted

    current = [f.payload.get("name") for f in repo.get_current_findings("CASE-A")
               if f.payload.get("pan_number")]
    assert current == ["NEW NAME"]

    assert "OLD NAME" not in answered(client, "What is my PAN name?")["answer"]


from app.knowledge.grounding import GroundedContext


class _Confident(GroundedContext):
    """Retrieval that is confident and cites nothing."""

    @property
    def grounded(self) -> bool:
        return True


@pytest.mark.parametrize("question,expected", [
    ("What is my PAN name?", "NEW NAME"),
    ("What exactly is the mismatch in my documents?", "NEW NAME"),
])
def test_n5_a_model_cannot_replace_a_recorded_value(client, repo, monkeypatch,
                                                    question, expected):
    """
    Confident retrieval and a model that answers with a different name.
    The recorded answer is quoted; the model is never asked.
    """
    from app.api.routes import copilot_api
    from app.knowledge import grounding

    processed(pan_name="OLD NAME")
    processed(pan_name="NEW NAME")

    calls = []

    async def hallucinate(*args, **kwargs):
        calls.append(1)
        return "The name on your PAN is SOMEBODY ELSE."

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", hallucinate)

    body = answered(client, question)

    assert expected in body["answer"]
    assert "SOMEBODY ELSE" not in body["answer"]
    assert calls == []
    assert body["response_source"] == "STRUCTURED"


def test_n5_a_model_phrased_answer_says_so(client, repo, monkeypatch):
    """Where a model may phrase, the response says a model did."""
    from app.api.routes import copilot_api
    from app.knowledge import grounding

    processed()

    # A COMPLETE phrasing: the answer validator rejects one that drops
    # the recorded hold or the names behind it (see the test below).
    phrased = ("Your application is at Basic Document Verification and is "
               "under review: the PAN says RISHABH AJIT SINGH but the salary "
               "slip says VENKATESH GOUD MARAGOUNI.")

    async def phrase(*args, **kwargs):
        return phrased

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", phrase)

    body = answered(client, "What is my application status?")

    assert body["answer"] == phrased
    assert body["response_source"] == "LLM"


def test_n5b_a_half_answer_is_replaced_by_the_record(client, repo, monkeypatch):
    """
    "Your application is currently being processed" is true and leaves out
    the review and its reason. The validator rejects it, the structured
    answer is published, and the response says STRUCTURED -- not LLM.
    """
    from app.api.routes import copilot_api
    from app.knowledge import grounding

    processed()

    async def phrase(*args, **kwargs):
        return "Your application is currently being processed."

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", phrase)

    body = answered(client, "What is my application status?")

    assert "under review" in body["answer"]
    assert "RISHABH AJIT SINGH" in body["answer"]
    assert body["response_source"] == "STRUCTURED"


def test_n7_a_case_that_does_not_exist_is_not_answered(client, repo):
    processed()

    response = ask(client, "What is my PAN name?", case_id="CASE-NOPE")

    assert response.status_code in (403, 404)
    assert "RISHABH" not in response.text


@pytest.mark.parametrize("documents", [("SALARY_SLIP",), ("DRIVING_LICENCE",),
                                       ("SALARY_SLIP", "DRIVING_LICENCE")])
def test_n8_n10_n16_no_pan_means_no_pan_name(client, repo, documents):
    """Never another document's name in the PAN's place."""
    processed(documents=documents, slip_name="SALARY PERSON", dl_name="LICENCE PERSON")

    body = answered(client, "What is my PAN name?")

    assert "No PAN has been recorded" in body["answer"]
    assert "SALARY PERSON" not in body["answer"]
    assert "LICENCE PERSON" not in body["answer"]
    assert body["grounded"] is False
    assert body["sources"] == []


def test_n9_a_pan_that_failed_is_reported_not_quoted(client, repo):
    processed(pan_status="FAIL")

    body = answered(client, "What is my PAN name?")

    # The recorded FAIL and its reason, in words -- the code itself stays
    # in the structured response, never in the sentence.
    assert "did not pass verification" in body["answer"]
    assert "not structurally valid" in body["answer"]
    assert "PAN_STRUCTURE_INVALID" not in body["answer"]
    assert "RISHABH" not in body["answer"]
    assert "VENKATESH" not in body["answer"]


def test_n12_a_caller_without_the_document_scope_is_refused(make_token, repo):
    import main

    processed()
    c = TestClient(main.app)
    c.headers.update({"Authorization":
                      f"Bearer {make_token(scopes=['read_application'])}"})

    response = c.post(COPILOT, json={"applicant_id": "APP-A", "case_id": "CASE-A",
                                     "message": "What is my PAN name?"})

    assert response.status_code == 403
    assert "RISHABH" not in response.text


async def test_n13_an_unknown_tool_is_refused_and_never_reported_as_run():
    from app.agents.applicant.agent import _call_tools

    results, trace, errors = await _call_tools(
        ("no.such.tool",), applicant_id="APP-A", case_id="CASE-A",
        document_type=None)

    assert results == {}
    assert trace == []
    assert errors[0]["code"] == "UNKNOWN_TOOL"


def test_n13_a_refused_tool_is_not_listed_as_invoked(make_token, repo):
    """A plan step the caller's scope does not cover never ran."""
    import main

    processed()
    c = TestClient(main.app)
    # May ask about pending items, may not read documents: DOCUMENTS_PENDING
    # plans documents.get AND workflow.pending_items.
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['read_documents'])}"})

    body = c.post(COPILOT, json={"applicant_id": "APP-A", "case_id": "CASE-A",
                                 "message": "What documents are pending?"}).json()

    for tool in body.get("tool_invoked") or []:
        assert tool != "workflow.pending_items"


def test_n14_a_failing_model_falls_back_to_the_record(client, repo, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.llm import availability

    processed()
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)

    def broken():
        raise TimeoutError("model timed out")

    monkeypatch.setattr("app.llm.provider.create_ollama_client", broken)

    body = answered(client, "What is my application status?")

    assert body["answer"]
    assert body["response_source"] == "STRUCTURED"
    assert body["grounded"] is True


# ==========================================================================
# THE RESPONSE CONTRACT
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What is my PAN name?", "What is my application status?",
    "Why is my application under review?", "What does KYC mean?",
    "What is my risk status?",
])
def test_every_response_carries_the_whole_contract(client, repo, question):
    processed()

    body = answered(client, question, party_id="APP-A", conversation_id="conv-1")

    for key in ("request_id", "case_id", "applicant_id", "party_id",
                "conversation_id", "stage", "intent", "answer",
                "response_source", "grounded", "sources", "tool_invoked",
                "errors"):
        assert key in body, key
    # NO CONTRADICTION: grounded answers have evidence; ungrounded ones
    # cite no case finding.
    if body["grounded"]:
        assert body["sources"] or body["tool_invoked"]
    else:
        assert not [s for s in body["sources"] if s.get("kind") == "case_finding"]
