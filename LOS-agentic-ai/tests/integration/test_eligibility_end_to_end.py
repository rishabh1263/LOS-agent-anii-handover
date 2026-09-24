"""
Eligibility, end to end, through the routes a caller actually uses.

    POST /api/v1/fos/applicants      the case, with its loan terms
    POST /api/v1/los/process         a salary slip -> income evidence -> eligibility
    GET  /api/v1/eligibility/{case}  the recorded verdict
    POST /api/v1/copilot/query       the Universal Copilot, reading that verdict

NOTHING HERE IS HAND-BUILT. Income arrives on a document that is classified,
verified and released by the extraction gate; the loan terms come from the
application record; the EMI and FOIR are computed by the deterministic
service; and the verdict is read back from case memory. No request carries a
FOIR, an EMI or an income figure.

THE ONE ANSWER RULE. The verdict the LOS response publishes, the verdict the
read endpoint returns, and the figures the Copilot quotes are compared to
each other. Two of the three are reads of what the first recorded, so they
must be identical, and a difference is a defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config

SLIP = Path("samples/documents/demo_salary_slip_rahul_sharma.pdf")

pytestmark = pytest.mark.skipif(
    not SLIP.exists(), reason="run samples/documents/make_demo_salary_slip.py first")

SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
    "documents:read", "documents:write", "kyc:read", "agents:execute",
]

#: The Swagger demonstration case. Income is NOT here: it comes off the slip.
DEMO_APPLICATION = {
    "product": "PERSONAL_LOAN",
    "loan_amount": 500000,
    "employment_type": "SALARIED",
    "tenure_months": 60,
    "declared_monthly_obligations": 10000,
}


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    from app.agents.eligibility import config as policy_file
    from app.agents.los import config as los_config
    from app.store import set_repository
    from app.store.sqlite_repo import SQLiteRepository

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(uploads))
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.delenv("ELIGIBILITY_POLICY_PATH", raising=False)
    monkeypatch.delenv("ELIGIBILITY_POLICY_PROVIDER", raising=False)
    los_config.reload()
    agent_config.reload()
    policy_file.reset_policy_cache()

    repository = SQLiteRepository(tmp_path / "e2e.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    los_config.reload()
    agent_config.reload()
    policy_file.reset_policy_cache()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=SCOPES)}"})
    return c


def open_case(client, **overrides) -> tuple[str, str]:
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210",
                      "date_of_birth": "1990-04-12", "address": "Pune, Maharashtra"},
        "application": {**DEMO_APPLICATION, **overrides},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def process_slip(client, applicant_id: str, case_id: str) -> dict:
    response = client.post(
        "/api/v1/los/process",
        files=[("files", ("salary_slip.pdf", SLIP.read_bytes(), "application/pdf"))],
        data={"operation": "PROCESS", "applicant_id": applicant_id,
              "case_id": case_id, "expected_types": "SALARY_SLIP"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def read(client, applicant_id: str, case_id: str) -> dict:
    response = client.get(f"/api/v1/eligibility/{case_id}",
                          params={"applicant_id": applicant_id})
    assert response.status_code == 200, response.text
    return response.json()


def ask(client, applicant_id: str, case_id: str, message: str) -> dict:
    response = client.post("/api/v1/copilot/query", json={
        "applicant_id": applicant_id, "case_id": case_id, "message": message})
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# 18. THE WHOLE PATH
# ==========================================================================


def test_the_demo_case_is_assessed_from_recorded_evidence(client):
    applicant_id, case_id = open_case(client)

    body = process_slip(client, applicant_id, case_id)
    eligibility = body["eligibility"]
    inputs = eligibility["inputs"]

    assert eligibility["status"] == "PASS", eligibility
    assert eligibility["reason_codes"] == ["ELIGIBILITY_WITHIN_POLICY"]
    assert eligibility["foir"] == {"status": "PASS", "value_pct": 43.27, "limit_pct": 50.0}
    # Unsecured: there is no property to measure against.
    assert eligibility["ltv"] == {"status": "NOT_APPLICABLE"}
    # Off the SLIP, not the request.
    assert inputs["monthly_income"] == 50000.0
    assert inputs["income_source"] == "SALARY_SLIP_NET"
    assert inputs["monthly_obligations"] == 10000.0
    assert inputs["obligations_source"] == "DECLARED"
    # Computed by the deterministic service at the POLICY rate.
    assert inputs["interest_rate_pct"] == 14.0
    assert inputs["interest_rate_source"] == "POLICY"
    assert inputs["proposed_emi"] == 11634.13
    # And the policy says what it is.
    assert eligibility["policy"] == {"id": "PL_DUMMY_V1", "version": "1.0",
                                     "status": "DEMO_NON_PRODUCTION", "source": "config"}


def test_the_read_endpoint_returns_exactly_what_the_pipeline_published(client):
    """THE ONE ANSWER RULE: a read of what was recorded is what was recorded."""
    applicant_id, case_id = open_case(client)
    published = process_slip(client, applicant_id, case_id)["eligibility"]

    recorded = read(client, applicant_id, case_id)

    assert recorded["recorded"] is True
    # The WHOLE object, not a selection of fields: a field-by-field
    # comparison passed while the read quietly dropped `evidence`.
    assert recorded["eligibility"] == published


def test_risk_bands_the_published_ratio(client):
    """Risk reads eligibility's FOIR; it does not compute a second one."""
    applicant_id, case_id = open_case(client)
    body = process_slip(client, applicant_id, case_id)

    assert "risk" in body
    assert body["eligibility"]["foir"]["value_pct"] == 43.27


# ==========================================================================
# 17. THE SAME CASE, TWICE
# ==========================================================================


def test_reprocessing_the_case_gives_the_same_verdict(client, _environment):
    applicant_id, case_id = open_case(client)

    first = process_slip(client, applicant_id, case_id)["eligibility"]
    findings_after_first = len(_environment.get_case_findings(case_id))
    second = process_slip(client, applicant_id, case_id)["eligibility"]

    assert first == second
    assert len(_environment.get_case_findings(case_id)) == findings_after_first, \
        "the same verdict was recorded twice"


# ==========================================================================
# THE ABSENCES, THROUGH THE REAL PATH
# ==========================================================================


def test_undeclared_obligations_are_reported_not_assumed(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=None)

    eligibility = process_slip(client, applicant_id, case_id)["eligibility"]

    assert eligibility["status"] == "SKIPPED"
    assert "OBLIGATIONS_NOT_CAPTURED" in eligibility["reason_codes"]
    assert eligibility["foir"] == {"status": "SKIPPED", "limit_pct": 50.0}
    # The instalment is still arithmetic over what WAS captured.
    assert eligibility["inputs"]["proposed_emi"] == 11634.13


def test_high_obligations_breach_the_ratio(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=20000)

    eligibility = process_slip(client, applicant_id, case_id)["eligibility"]

    assert eligibility["status"] == "REVIEW"
    assert "FOIR_ABOVE_THRESHOLD" in eligibility["reason_codes"]
    assert eligibility["foir"] == {"status": "REVIEW", "value_pct": 63.27, "limit_pct": 50.0}


def test_a_case_with_no_income_document_is_not_assessed_on_nothing(client):
    """No slip, no statement: nothing released, nothing invented."""
    applicant_id, case_id = open_case(client)

    recorded = read(client, applicant_id, case_id)

    assert recorded["recorded"] is False
    assert recorded["eligibility"] is None


# ==========================================================================
# 19 / 20. THE COPILOT READS; IT DOES NOT CALCULATE
# ==========================================================================


def test_the_copilot_quotes_the_recorded_verdict(client):
    applicant_id, case_id = open_case(client)
    process_slip(client, applicant_id, case_id)

    reply = ask(client, applicant_id, case_id,
                "Am I eligible based on the current eligibility assessment?")

    assert reply["intent"] == "ELIGIBILITY"
    assert reply["answer"].startswith("Eligibility: PASS")
    assert "FOIR 43.27% (limit 50.0%) -- PASS" in reply["answer"]
    assert "LTV: not applicable" in reply["answer"]
    assert "demonstration policy" in reply["answer"]


def test_the_copilot_explains_a_skip_from_its_reason_codes(client):
    applicant_id, case_id = open_case(client, declared_monthly_obligations=None)
    process_slip(client, applicant_id, case_id)

    reply = ask(client, applicant_id, case_id, "Why is my eligibility under review?")

    assert reply["intent"] == "ELIGIBILITY"
    assert reply["answer"].startswith("Eligibility: SKIPPED")
    assert "already repays each month" in reply["answer"]
    assert "FOIR: not computed" in reply["answer"], \
        "a ratio was quoted for a case that computed none"


def test_the_copilot_says_so_when_nothing_was_assessed(client):
    applicant_id, case_id = open_case(client)

    reply = ask(client, applicant_id, case_id, "What is my FOIR?")

    assert "has not been evaluated" in reply["answer"]
    assert "%" not in reply["answer"]


def test_the_copilot_cannot_override_the_recorded_verdict(client, monkeypatch):
    """
    The model is switched ON and told to say something else. The answer
    comes from the record regardless: the eligibility path never calls it.
    """
    from app.agents.applicant import agent

    applicant_id, case_id = open_case(client)
    process_slip(client, applicant_id, case_id)

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()
    called: list[str] = []

    async def overriding_model(message, *_args, **_kwargs):
        called.append(message)
        return "You are not eligible. Your FOIR is 99%.", "llm", 1.0

    monkeypatch.setattr(agent, "generate_answer", overriding_model)

    reply = ask(client, applicant_id, case_id, "Am I eligible?")

    assert called == [], "the eligibility answer was handed to the model"
    assert "99%" not in reply["answer"]
    assert "43.27%" in reply["answer"]


def test_the_read_endpoint_refuses_another_applicants_case(client):
    applicant_id, case_id = open_case(client)
    other_id, _ = open_case(client)

    response = client.get(f"/api/v1/eligibility/{case_id}",
                          params={"applicant_id": other_id})

    assert response.status_code == 403


def test_the_read_endpoint_needs_the_contract_scope(make_token):
    """The scope comes from the eligibility.get ToolContract."""
    import main

    weak = TestClient(main.app)
    weak.headers.update({"Authorization": f"Bearer {make_token(scopes=['read_documents'])}"})

    response = weak.get("/api/v1/eligibility/CASE-X", params={"applicant_id": "APP-X"})

    assert response.status_code == 403



# ==========================================================================
# LTV, THROUGH THE REAL PATH -- a secured product
# ==========================================================================

HOME_LOAN = {"product": "HOME_LOAN", "loan_amount": 2000000, "tenure_months": 240,
             "declared_monthly_obligations": 5000, "property_value": 3000000}


def test_a_home_loan_is_assessed_on_foir_and_ltv(client):
    applicant_id, case_id = open_case(client, **HOME_LOAN)

    eligibility = process_slip(client, applicant_id, case_id)["eligibility"]

    assert eligibility["status"] == "PASS", eligibility
    assert eligibility["foir"] == {"status": "PASS", "value_pct": 44.71, "limit_pct": 50.0}
    assert eligibility["ltv"] == {"status": "PASS", "value_pct": 66.67, "limit_pct": 80.0}
    assert eligibility["inputs"]["property_value"] == 3000000.0
    assert eligibility["inputs"]["property_value_source"] == "DECLARED"
    assert eligibility["policy"]["id"] == "HL_DUMMY_V1"


def test_a_home_loan_above_the_ltv_limit_reviews(client):
    applicant_id, case_id = open_case(client, **{**HOME_LOAN, "property_value": 2200000})

    eligibility = process_slip(client, applicant_id, case_id)["eligibility"]

    assert eligibility["status"] == "REVIEW"
    assert "LTV_ABOVE_MAXIMUM" in eligibility["reason_codes"]
    assert eligibility["ltv"] == {"status": "REVIEW", "value_pct": 90.91, "limit_pct": 80.0}


def test_a_home_loan_without_a_property_value_is_not_assessed(client):
    applicant_id, case_id = open_case(client, **{**HOME_LOAN, "property_value": None})

    eligibility = process_slip(client, applicant_id, case_id)["eligibility"]

    assert eligibility["status"] == "SKIPPED"
    assert "LTV_NOT_AVAILABLE" in eligibility["reason_codes"]
    assert eligibility["ltv"] == {"status": "SKIPPED", "limit_pct": 80.0}


def test_risk_bands_the_published_ltv(client):
    applicant_id, case_id = open_case(client, **{**HOME_LOAN, "property_value": 2200000})

    body = process_slip(client, applicant_id, case_id)

    assert any(flag.startswith("LTV_BREACH") for flag in body["risk"]["flags"])


def test_the_copilot_answers_ltv_from_the_record(client):
    applicant_id, case_id = open_case(client, **HOME_LOAN)
    process_slip(client, applicant_id, case_id)

    reply = ask(client, applicant_id, case_id, "What is my eligibility status?")

    assert "LTV 66.67% (limit 80.0%) -- PASS" in reply["answer"]
    assert "property ₹3,000,000 (declared)" in reply["answer"]


def test_the_published_result_is_short():
    """Structured and short: the whole verdict stays well under a kilobyte."""
    import json

    from app.agents.eligibility.engine import evaluate
    from app.agents.eligibility.schemas import EligibilityInputs

    public = evaluate(EligibilityInputs(
        monthly_income=50000, income_source="SALARY_SLIP_NET",
        monthly_obligations=5000, obligations_source="DECLARED",
        loan_amount=2000000, tenure_months=240, product="HOME_LOAN",
        employment_type="SALARIED", property_value=3000000,
        property_value_source="DECLARED")).public()

    assert len(json.dumps(public)) < 800
