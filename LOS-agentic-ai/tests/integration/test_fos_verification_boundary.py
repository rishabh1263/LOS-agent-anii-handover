"""
The FOS verification boundary, end to end: response, store and Copilot.

WHAT test_fos_verification.py ALREADY PROVES, over HTTP on real files: a
genuine PAN passes and releases its fields; a PAN uploaded as a licence
does not pass; blank and unreadable images never pass; an unread date of
birth is REVIEW. It proves them at the RESPONSE.

WHAT THIS ADDS: the same gate must hold everywhere a released field could
later be read from --

    the persisted case state   an unreleased extraction is never stored
    the Universal Copilot      it never quotes a field the gate withheld
    the LOS route              the same gate on /api/v1/los/process
    SKIPPED                    verification switched off releases nothing

-- and a wrong document type is a FAIL, not merely "not a pass".

Every document is real and every verdict is the pipeline's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.agents.los import config as los_config
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "real_batch"
PAN_COMPLETE = SAMPLES / "pan_bw2.jpg"          # passes as PAN
PAN_DOB_UNREADABLE = SAMPLES / "pan_bw1.jpg"    # REVIEW: DOB unread

pytestmark = pytest.mark.skipif(
    not (PAN_COMPLETE.exists() and PAN_DOB_UNREADABLE.exists()),
    reason="sample documents not available")

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    agent_config.reload()
    los_config.reload()
    repository = SQLiteRepository(tmp_path / "boundary.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    agent_config.reload()
    los_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def open_case(client) -> tuple[str, str]:
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Boundary Test", "mobile": "9876543210"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000}})
    assert response.status_code == 201, response.text
    return response.json()["applicant_id"], response.json()["case_id"]


def upload(client, applicant_id, case_id, path, document_type):
    response = client.post("/api/v1/fos/copilot", data={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT", "document_type": document_type},
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()["verification"]


def ask(client, applicant_id, case_id, message) -> dict:
    response = client.post("/api/v1/copilot/query", json={
        "applicant_id": applicant_id, "case_id": case_id, "message": message})
    assert response.status_code == 200, response.text
    return response.json()


def extractions(store, case_id) -> list[dict]:
    """Every extraction the store holds for the case, history included."""
    return [f.payload for f in store.get_case_findings(case_id)
            if f.finding_kind.value == "EXTRACTION"]


def pipeline_names(path) -> set[str]:
    """What the pipeline itself reads from the card, to prove none leaks."""
    from app.agents.document_agent import preprocess
    from app.agents.document_agent.ocr import get_engine
    from app.agents.document_agent.pipeline import recognise
    from app.agents.document_agent.schemas import DocumentType

    rec = recognise(get_engine(), preprocess.load(str(path)),
                    force_type=DocumentType.PAN)
    return {str(f.value) for k, f in rec.result.fields.items()
            if k in ("name", "father_name") and f.value}


# ==========================================================================
# PASS RELEASES -- AND WHAT IS RELEASED IS WHAT IS STORED AND QUOTED
# ==========================================================================


def test_a_pass_releases_stores_and_is_quoted_exactly(client, store):
    applicant_id, case_id = open_case(client)

    verification = upload(client, applicant_id, case_id, PAN_COMPLETE, "PAN")

    assert verification["verification"] == "PASS"
    assert verification["extraction_released"] is True
    stored = extractions(store, case_id)
    assert len(stored) == 1 and stored[0].get("name")
    answer = ask(client, applicant_id, case_id, "What is my PAN name?")["answer"]
    assert stored[0]["name"] in answer


# ==========================================================================
# NOTHING SHORT OF A PASS REACHES THE STORE OR THE COPILOT
# ==========================================================================


def test_a_wrong_document_type_is_a_fail_and_leaves_nothing_behind(client, store):
    """A genuine PAN declared as a driving licence: FAIL, not merely REVIEW."""
    applicant_id, case_id = open_case(client)

    verification = upload(client, applicant_id, case_id, PAN_COMPLETE,
                          "DRIVING_LICENCE")

    assert verification["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in verification["reason_codes"]
    assert verification["extraction_released"] is False
    assert extractions(store, case_id) == []

    names = pipeline_names(PAN_COMPLETE)
    for question in ("What is the name on my driving licence?",
                     "What is my PAN name?"):
        body = ask(client, applicant_id, case_id, question)
        assert not any(n in body["answer"] for n in names), (question, body["answer"])
        assert not [s for s in body["sources"] if s.get("finding_kind") == "EXTRACTION"]


def test_a_review_withholds_from_the_store_and_the_copilot(client, store):
    applicant_id, case_id = open_case(client)

    verification = upload(client, applicant_id, case_id, PAN_DOB_UNREADABLE, "PAN")

    assert verification["verification"] == "REVIEW"
    assert verification["extraction_released"] is False
    assert extractions(store, case_id) == []

    body = ask(client, applicant_id, case_id, "What is my PAN name?")
    assert "under review" in body["answer"]  # the recorded REVIEW, in words
    assert not any(n in body["answer"] for n in pipeline_names(PAN_DOB_UNREADABLE))


def test_skipped_verification_releases_nothing(client, store, monkeypatch):
    """
    Verification switched off established nothing. Reading SKIPPED as
    permission would make turning a check OFF silently widen what the API
    releases.
    """
    monkeypatch.setenv("LOS_VERIFICATION_ENABLED", "false")
    los_config.reload()
    applicant_id, case_id = open_case(client)

    verification = upload(client, applicant_id, case_id, PAN_COMPLETE, "PAN")

    assert verification["verification"] != "PASS"
    assert verification["extraction_released"] is False
    assert extractions(store, case_id) == []


# ==========================================================================
# THE SAME GATE ON THE LOS ROUTE
# ==========================================================================


def test_the_los_route_applies_the_same_gate(make_token, store):
    import main

    c = TestClient(main.app)
    # /los/process writes a case: the service writer scope, not read-only.
    c.headers.update({"Authorization":
                      f"Bearer {make_token(scopes=['los.read', 'los.write'])}"})

    def process(path, case_id):
        response = c.post("/api/v1/los/process", data={
            "operation": "PROCESS", "applicant_id": "APP-LOS", "case_id": case_id,
            "expected_types": "PAN"},
            files=[("files", (path.name, path.read_bytes(), "image/jpeg"))])
        assert response.status_code == 200, response.text
        return response.json()["documents"][0]

    passed = process(PAN_COMPLETE, "CASE-LOS-PASS")
    held = process(PAN_DOB_UNREADABLE, "CASE-LOS-REVIEW")

    assert passed["verification"] == "PASS" and passed.get("extraction")
    assert held["verification"] == "REVIEW" and not held.get("extraction")
    assert extractions(store, "CASE-LOS-REVIEW") == []


# ==========================================================================
# THE BOUNDARY'S OWN ACCESS CONTROL
# ==========================================================================


def test_upload_needs_the_upload_scope(make_token, client):
    import main

    applicant_id, case_id = open_case(client)
    reader = TestClient(main.app)
    reader.headers.update({"Authorization": f"Bearer {make_token(scopes=['read_documents'])}"})

    response = reader.post("/api/v1/fos/copilot", data={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT", "document_type": "PAN"},
        files={"file": ("pan.jpg", PAN_COMPLETE.read_bytes(), "image/jpeg")})

    assert response.status_code == 403


def test_upload_to_another_applicants_case_is_refused(client, store):
    _, case_a = open_case(client)
    applicant_b, _ = open_case(client)

    response = client.post("/api/v1/fos/copilot", data={
        "applicant_id": applicant_b, "case_id": case_a,
        "action": "UPLOAD_DOCUMENT", "document_type": "PAN"},
        files={"file": ("pan.jpg", PAN_COMPLETE.read_bytes(), "image/jpeg")})

    assert response.status_code == 403
    assert store.list_documents(case_a) == []
