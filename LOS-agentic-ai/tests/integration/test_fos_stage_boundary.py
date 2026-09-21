"""
What the FOS stage owns, and what it must not reach for.

THE DISTINCTION THIS SUITE DEFENDS:

    DOCUMENT VERIFICATION   is this upload usable as the type it claims?
    KYC                     do these documents describe the same person?

They are different questions asked at different stages, and the FOS copilot
owns only the first. A field officer uploading a PAN and a licence was being
handed a cross-document comparison of name, date of birth and father's name --
a KYC result, produced by an upload endpoint, at a stage with no authority to
act on it. Two documents passing verification does not mean anything about
whether they belong to one person, and FOS is not the desk that decides.

KYC ITSELF IS UNTOUCHED. The agent, its matching, its thresholds and its
tests are all unchanged. What changed is who calls it: the FOS upload no
longer does, and /api/v1/los/process still does.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "real_batch"
PAN = SAMPLES / "pan_bw2.jpg"
LICENCE = SAMPLES / "dl1.jpg"
BANK = SAMPLES / "bank_hdfc_new.pdf"

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "boundary.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


@pytest.fixture
def kyc_tripwire(monkeypatch):
    """
    Fails the test if anything runs KYC.

    Patched at the flow's own reference, which is the ONE place the FOS
    upload could reach it from. A future call added anywhere on this path
    trips it rather than quietly returning identity comparisons again.
    """
    from app.agents.los import flow

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("KYC was executed from the FOS upload path")

    monkeypatch.setattr(flow, "run_kyc", forbidden)
    return calls


def open_case(client, product="PERSONAL_LOAN") -> tuple[str, str]:
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Boundary Test", "mobile": "9876543210",
                      "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": product, "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def upload(client, applicant_id, case_id, items, types=None):
    files = []
    for name, source in items:
        content = (Path(source).read_bytes()
                   if isinstance(source, (str, Path)) else source)
        files.append(("files", (name, content, "application/octet-stream")))
    data: dict[str, object] = {
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT",
    }
    if types is not None:
        data["document_types"] = list(types)
    return client.post("/api/v1/fos/copilot", data=data, files=files)


def slot(body, name):
    return next(e for e in body["checklist"] if e["slot"] == name)


def blank_image() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 560), (235, 235, 235)).save(buffer, format="PNG")
    return buffer.getvalue()


# ==========================================================================
# A, B, C -- ONE DOCUMENT AT A TIME
# ==========================================================================

def test_uploading_a_pan_verifies_it_and_runs_no_kyc(client, kyc_tripwire):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN)], ["PAN"]).json()

    assert body["verification"]["documents_processed"][0]["verification"] == "PASS"
    assert slot(body, "PAN")["status"] == "VERIFIED"
    assert body["kyc"] is None
    assert not kyc_tripwire


def test_uploading_a_licence_satisfies_address_proof_and_runs_no_kyc(
    client, kyc_tripwire
):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("dl.jpg", LICENCE)], ["ADDRESS_PROOF"]).json()

    assert body["verification"]["documents_processed"][0]["verification"] == "PASS"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"
    assert body["kyc"] is None
    assert not kyc_tripwire


def test_a_bank_statement_gets_basic_verification_only(client, kyc_tripwire):
    """
    A statement is verified as a DOCUMENT here, and nothing more.

    No transaction analysis, no income estimate, no financial scoring reaches
    the response. Whatever verdict basic verification reached is reported as
    it stands -- a REVIEW is a document-verification state, not a financial
    rejection, and is never converted into one.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("bank.pdf", BANK)], ["BANK_STATEMENT"]).json()

    outcome = body["verification"]["documents_processed"][0]
    assert outcome["document_type"] == "BANK_STATEMENT"
    assert outcome["verification"] in {"PASS", "REVIEW", "FAIL"}
    assert body["kyc"] is None
    assert not kyc_tripwire

    # No financial analysis anywhere in the response.
    #
    # THE MARKERS ARE ANALYSIS OUTPUTS, NOT THE WORD "INCOME". A bare
    # substring search for "income" also matched INCOME_PROOF -- a
    # CHECKLIST SLOT naming a document to collect, which is a FOS-stage
    # requirement and the opposite of a leak. Broadening a guard until it
    # catches the thing it is protecting makes it useless: the next person
    # to hit it deletes it.
    #
    # What must never appear is a FIGURE DERIVED FROM the statement -- an
    # estimated income, a total, a score. `income_estimate` and
    # `monthly_income` are those; `income_proof` is not.
    import json

    blob = json.dumps(body).lower()
    for forbidden in ("average_monthly_credit", "monthly_net_salary",
                      "total_credit", "total_debit", "transactions",
                      "income_estimate", "estimated_income", "monthly_income",
                      "income_analysis", "risk_score", "creditworth",
                      "affordability", "cash_flow", "spending"):
        assert forbidden not in blob, (
            f"{forbidden!r} leaked financial analysis into a FOS response"
        )

    # And the word "income" may appear ONLY as a document slot name.
    for occurrence in blob.split('"'):
        if "income" in occurrence:
            assert occurrence.strip().lower() in {"income_proof"}, (
                f"{occurrence!r} mentions income and is not a document slot"
            )


def test_a_reviewed_document_is_never_reported_as_rejected(client):
    """
    REVIEW means a person has to look. It is not a refusal, and inventing a
    rejection reason for it would turn a verification state into a decision
    FOS has no authority to make.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("bank.pdf", BANK)], ["BANK_STATEMENT"]).json()

    outcome = body["verification"]["documents_processed"][0]
    if outcome["verification"] != "REVIEW":
        pytest.skip("this sample does not review on this build")

    assert outcome["status"] != "REJECTED"
    assert "reject" not in body["answer"].lower()
    assert body["next_action"]["action"] in {
        "RESOLVE_DOCUMENT_REVIEW", "COLLECT_DOCUMENT",
    }
    assert body["readiness"]["status"] == "NOT_READY"


# ==========================================================================
# D -- ALL THREE TOGETHER
# ==========================================================================

def test_three_documents_are_verified_independently_with_no_kyc(
    client, kyc_tripwire
):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [
        ("pan.jpg", PAN), ("dl.jpg", LICENCE), ("bank.pdf", BANK),
    ], ["PAN", "ADDRESS_PROOF", "BANK_STATEMENT"]).json()

    outcomes = {o["source_id"]: o
                for o in body["verification"]["documents_processed"]}
    assert len(outcomes) == 3
    assert outcomes["pan.jpg"]["verification"] == "PASS"
    assert outcomes["dl.jpg"]["verification"] == "PASS"

    assert slot(body, "PAN")["status"] == "VERIFIED"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"

    # THE POINT: two identity documents passed and nothing compared them.
    assert body["kyc"] is None
    assert not kyc_tripwire


def test_no_identity_comparison_appears_in_an_upload_response(client):
    """
    Name, date of birth and father's name across documents are a KYC answer.
    A FOS upload must not produce one, however well the documents verified.
    """
    import json

    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  ["PAN", "DRIVING_LICENCE"]).json()

    blob = json.dumps(body)
    for forbidden in ("NAME_MISMATCH", "DOB_MISMATCH", "FATHER_NAME",
                      "match_score", "overall_confidence",
                      "PAN_NUMBER", "DATE_OF_BIRTH"):
        assert forbidden not in blob, (
            f"{forbidden!r} is a KYC field and appeared on a FOS upload"
        )


def test_the_upload_answer_describes_documents_not_identity(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  ["PAN", "DRIVING_LICENCE"]).json()

    answer = body["answer"].lower()
    assert "verification" in answer or "uploaded" in answer
    for forbidden in ("kyc", "mismatch", "same person", "identity match"):
        assert forbidden not in answer


# ==========================================================================
# THE LOS ENDPOINT IS UNCHANGED
# ==========================================================================

def test_the_los_endpoint_still_runs_kyc(make_token):
    """
    The boundary is on the FOS caller, not on KYC.

    /api/v1/los/process is the stage that owns cross-document checks and must
    keep producing them, or this change would have removed a capability
    rather than moved it.
    """
    import main

    client = TestClient(main.app)
    token = make_token(scopes=["documents:read", "documents:write",
                               "kyc:read", "agents:execute"])
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "expected_types": "PAN,DRIVING_LICENCE"},
        files=[("files", ("pan.jpg", PAN.read_bytes(), "image/jpeg")),
               ("files", ("dl.jpg", LICENCE.read_bytes(), "image/jpeg"))],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert "kyc" in body, "the LOS endpoint stopped reporting KYC"
    assert body["kyc"]["status"] in {"PASS", "REVIEW", "FAIL", "SKIPPED"}
    assert "fields" in body["kyc"]


def test_the_stage_flags_default_to_running_everything():
    """
    Both default to True, so every existing caller is unaffected. Only a
    caller that opts out -- the FOS upload -- skips a stage.
    """
    import inspect

    from app.agents.los.flow import process_application

    signature = inspect.signature(process_application)
    assert signature.parameters["cross_document_checks"].default is True
    assert signature.parameters["summarise"].default is True


# ==========================================================================
# I -- READINESS STILL BEHAVES
# ==========================================================================

def test_a_missing_mandatory_document_blocks_readiness(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN)], ["PAN"]).json()

    assert body["readiness"]["status"] == "NOT_READY"
    assert any(i["code"] == "DOCUMENT_MISSING" for i in body["pending_items"])
    assert body["next_action"]["action"] == "COLLECT_DOCUMENT"


def test_a_rejected_document_blocks_readiness(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("dummy.png", blank_image())], ["PAN"]).json()

    assert body["readiness"]["status"] == "NOT_READY"
    assert body["kyc"] is None
