"""
Document verification, exercised through the FOS surface on real files.

WHY THIS SUITE EXISTS. The verification gate could mark a dummy or wrong
document as PASS. A PASS is what releases extracted fields, satisfies a
checklist slot and moves a case toward the CPA handoff, so a PASS that is not
earned is a case handed on with nothing behind it.

WHAT IT DEFENDS, and these are the rules, not the assertions:

    a document that is not what the caller asserted never passes
    a document that cannot be read never passes
    a document with a required field unread never passes
    nothing short of a pass releases extracted fields
    a checklist slot is satisfied by the classes it accepts, and no others
    one case never sees another case's documents

EVERY DOCUMENT HERE IS REAL, and every verdict is the pipeline's own. Nothing
is seeded into the store behind the API and no verdict is stubbed: a test that
writes the verdict it then asserts is testing nothing. The two negatives -- a
blank image and an unreadable scan -- are generated rather than sampled,
because a corpus of genuine documents contains no counterfeits to draw on.

The store is a real repository on a temporary file, so no test sees data left
by another test or by earlier manual use of the service.
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

#: A genuine PAN whose every required field is legible.
PAN_COMPLETE = SAMPLES / "pan_bw2.jpg"

#: A genuine PAN photographed on a phone. Its printed date of birth did not
#: survive the photo, and the only date the page carries is the camera's own
#: gallery caption. Kept deliberately: this is the case where a real document
#: must still not pass, because a required field was never read.
PAN_DOB_UNREADABLE = SAMPLES / "pan_bw1.jpg"

#: A genuine driving licence.
DRIVING_LICENCE = SAMPLES / "dl1.jpg"

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]

#: Verdicts that release extracted fields. Exactly one.
PASSING = {"PASS"}


# ==========================================================================
# ISOLATION
# ==========================================================================

@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    """A repository of its own per test. No shared state, no prior data."""
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "verification_test.sqlite3")
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


def open_case(client, product="PERSONAL_LOAN") -> tuple[str, str]:
    """A case, opened through the public endpoint like any other caller."""
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {
            "full_name": "Test Applicant", "mobile": "9876543210",
            "date_of_birth": "1990-04-12", "address": "Mumbai, Maharashtra",
        },
        "application": {"product": product, "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def upload(client, applicant_id, case_id, path_or_bytes, *,
           document_type=None, filename=None):
    """Push a file through POST /api/v1/fos/copilot as a FOS would."""
    if isinstance(path_or_bytes, (str, Path)):
        content = Path(path_or_bytes).read_bytes()
        name = filename or Path(path_or_bytes).name
    else:
        content, name = path_or_bytes, filename or "upload.png"

    data = {"applicant_id": applicant_id, "case_id": case_id,
            "action": "UPLOAD_DOCUMENT"}
    if document_type:
        data["document_type"] = document_type

    return client.post(
        "/api/v1/fos/copilot",
        data=data,
        files={"file": (name, content, "application/octet-stream")},
    )


def ask(client, applicant_id, case_id, action):
    """A structured copilot call."""
    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id, "action": action,
    })
    assert response.status_code == 200, response.text
    return response.json()


# -- generated negatives ----------------------------------------------------

def blank_image(size=(900, 560)) -> bytes:
    """A plain grey rectangle. Carries no document and never should pass."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (235, 235, 235)).save(buffer, format="PNG")
    return buffer.getvalue()


def illegible(path: Path, factor: int = 14) -> bytes:
    """
    A real document destroyed by resampling.

    Built from a genuine card rather than from noise so the failure under test
    is legibility and nothing else -- the layout, the proportions and the
    print are all still those of a real PAN.
    """
    image = Image.open(path).convert("RGB")
    width, height = image.size
    small = image.resize((max(1, width // factor), max(1, height // factor)))
    buffer = io.BytesIO()
    small.resize((width, height)).save(buffer, format="JPEG", quality=12)
    return buffer.getvalue()


def slot(body, name):
    """One checklist row by slot name."""
    return next(e for e in body["checklist"] if e["slot"] == name)


# ==========================================================================
# A FRESH CASE HAS NOTHING IN IT
# ==========================================================================

def test_a_fresh_case_has_no_documents_and_no_verified_anything(client):
    """
    A case that was just opened must not look partly complete.

    This is the test that catches a store returning another case's rows, or a
    checklist built from a default that pretends a slot is filled.
    """
    applicant_id, case_id = open_case(client)
    body = ask(client, applicant_id, case_id, "GET_CASE_360")

    assert body["documents"] == []
    assert body["readiness"]["status"] == "NOT_READY"
    assert {e["status"] for e in body["checklist"]} == {"MISSING"}
    assert body["required_documents"], "a case with no required documents"


# ==========================================================================
# A GENUINE DOCUMENT PASSES
# ==========================================================================

def test_a_genuine_pan_passes_and_releases_its_fields(client):
    applicant_id, case_id = open_case(client)
    response = upload(client, applicant_id, case_id, PAN_COMPLETE,
                      document_type="PAN")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["verification"]["document_type"] == "PAN"
    assert body["verification"]["verification"] == "PASS"
    assert body["verification"]["reason_codes"] == []
    assert body["verification"]["extraction_released"] is True
    assert slot(body, "PAN")["status"] == "VERIFIED"


def test_a_genuine_licence_passes_on_its_own_class(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, DRIVING_LICENCE,
                  document_type="DRIVING_LICENCE").json()

    assert body["verification"]["document_type"] == "DRIVING_LICENCE"
    assert body["verification"]["verification"] == "PASS"


# ==========================================================================
# THE WRONG DOCUMENT NEVER PASSES
# ==========================================================================

def test_a_pan_uploaded_as_a_licence_fails_and_withholds_its_fields(client):
    """
    The document is genuine. It is the wrong one.

    A PAN that passes as a driving licence satisfies the address-proof slot
    with a document that proves no address.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, PAN_COMPLETE,
                  document_type="DRIVING_LICENCE").json()

    verification = body["verification"]
    assert verification["verification"] not in PASSING
    assert "DOCUMENT_TYPE_MISMATCH" in verification["reason_codes"]
    assert verification["extraction_released"] is False

    # The upload is recorded against the class it actually is, rejected. The
    # slot it was DECLARED for stays empty: a misdeclared document never
    # satisfies the slot the caller aimed it at.
    assert slot(body, "PAN")["status"] == "REJECTED"
    assert slot(body, "ADDRESS_PROOF")["status"] == "MISSING"
    assert body["readiness"]["status"] == "NOT_READY"


# ==========================================================================
# A DUMMY IMAGE NEVER PASSES
# ==========================================================================

@pytest.mark.parametrize("asserted", [None, "PAN", "DRIVING_LICENCE"])
def test_a_dummy_image_never_passes_whatever_it_is_called(client, asserted):
    """
    Calling a blank rectangle a PAN does not make it one.

    Asserted and unasserted are both covered because they take different
    paths: one is refused by the type check, the other has to be refused by
    verification on the document's own merits.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, blank_image(),
                  document_type=asserted, filename="dummy.png").json()

    verification = body["verification"]
    assert verification["verification"] not in PASSING, (
        f"a blank image asserted as {asserted} passed verification"
    )
    assert verification["extraction_released"] is False
    assert all(e["status"] == "MISSING" for e in body["checklist"])


# ==========================================================================
# AN UNREADABLE DOCUMENT NEVER PASSES
# ==========================================================================

def test_an_unreadable_pan_does_not_pass(client):
    """A real PAN nobody can read is not a verified PAN."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, illegible(PAN_COMPLETE),
                  document_type="PAN", filename="unreadable_pan.jpg").json()

    verification = body["verification"]
    assert verification["verification"] not in PASSING
    assert verification["extraction_released"] is False
    assert verification["reason_codes"], "refused with no reason given"


# ==========================================================================
# A CHECKLIST SLOT ACCEPTS WHAT IT ACCEPTS, AND NOTHING ELSE
# ==========================================================================

def test_a_licence_satisfies_the_address_proof_slot(client):
    """
    ADDRESS_PROOF is a slot, not a document class.

    The checklist shows the FOS a slot called ADDRESS_PROOF, so uploading
    against that name has to work -- otherwise the API is arguing with its own
    screen. A licence is one of the classes the slot accepts.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, DRIVING_LICENCE,
                  document_type="ADDRESS_PROOF").json()

    verification = body["verification"]
    assert verification["document_type"] == "DRIVING_LICENCE"
    assert verification["verification"] == "PASS"
    assert "DOCUMENT_TYPE_MISMATCH" not in (verification["reason_codes"] or [])
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"


def test_a_pan_does_not_satisfy_the_address_proof_slot(client):
    """The slot is not a wildcard. A PAN proves no address."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, PAN_COMPLETE,
                  document_type="ADDRESS_PROOF").json()

    verification = body["verification"]
    assert verification["verification"] not in PASSING
    assert "DOCUMENT_TYPE_MISMATCH" in verification["reason_codes"]
    assert verification["extraction_released"] is False
    assert slot(body, "ADDRESS_PROOF")["status"] == "MISSING"


# ==========================================================================
# A REQUIRED FIELD THAT WAS NOT READ IS NOT A PASS
# ==========================================================================

def test_a_genuine_pan_with_an_unread_date_of_birth_is_not_passed(client):
    """
    THIS IS THE LIMIT OF WHAT CAN BE ESTABLISHED LOCALLY, STATED HONESTLY.

    The card is genuine. Its date of birth did not survive being photographed,
    and the only date on the page is the camera's gallery caption, which the
    pipeline refuses to adopt as a date of birth. With a required field unread
    there is nothing to pass on, so the verdict is REVIEW: a person should
    look at it. Returning PASS here would mean certifying a PAN whose date of
    birth was never read.

    There is no external PAN authority wired into this service, so a REVIEW
    that a human resolves is the strongest honest answer available.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, PAN_DOB_UNREADABLE,
                  document_type="PAN").json()

    verification = body["verification"]
    assert verification["document_type"] == "PAN"
    assert verification["verification"] == "REVIEW"
    assert "REQUIRED_FIELD_MISSING" in verification["reason_codes"]
    assert verification["extraction_released"] is False, (
        "fields released behind a verdict that is not a pass"
    )
    assert slot(body, "PAN")["status"] == "REVIEW"


# ==========================================================================
# CASES DO NOT LEAK INTO EACH OTHER
# ==========================================================================

def test_one_case_never_sees_another_cases_documents(client):
    """
    Two cases, one document, and the same file name in both.

    A store keyed on the file name rather than on the case would collapse
    these into one document and show a verified PAN on a case that has none.
    """
    first_applicant, first_case = open_case(client)
    second_applicant, second_case = open_case(client)
    assert first_case != second_case

    upload(client, first_applicant, first_case, PAN_COMPLETE,
           document_type="PAN", filename="pan.jpg")

    empty = ask(client, second_applicant, second_case, "GET_CASE_360")
    assert empty["documents"] == []
    assert slot(empty, "PAN")["status"] == "MISSING"

    # The same file name again, in the second case.
    upload(client, second_applicant, second_case, PAN_COMPLETE,
           document_type="PAN", filename="pan.jpg")

    first = ask(client, first_applicant, first_case, "GET_DOCUMENTS")
    second = ask(client, second_applicant, second_case, "GET_DOCUMENTS")

    assert len(first["documents"]) == 1
    assert len(second["documents"]) == 1
    assert (first["documents"][0]["document_id"]
            != second["documents"][0]["document_id"])


# ==========================================================================
# THE CHECKLIST MOVES, AND READINESS FOLLOWS IT
# ==========================================================================

def test_the_checklist_advances_only_on_a_pass(client):
    """
    A slot goes from MISSING to VERIFIED because a document passed, and for
    no other reason. The rejected upload in the middle must not move it.
    """
    applicant_id, case_id = open_case(client)

    before = ask(client, applicant_id, case_id, "GET_DOCUMENT_CHECKLIST")
    assert slot(before, "PAN")["status"] == "MISSING"

    rejected = upload(client, applicant_id, case_id, blank_image(),
                      document_type="PAN", filename="dummy.png").json()
    assert slot(rejected, "PAN")["status"] == "MISSING"

    passed = upload(client, applicant_id, case_id, PAN_COMPLETE,
                    document_type="PAN").json()
    assert slot(passed, "PAN")["status"] == "VERIFIED"


def test_readiness_flips_only_when_every_mandatory_slot_is_satisfied(client):
    """
    Readiness is derived from the slots, not stored.

    PERSONAL_LOAN requires PAN, BANK_STATEMENT and ADDRESS_PROOF. Two of three
    is still NOT_READY, and the outstanding slot is named.
    """
    applicant_id, case_id = open_case(client)

    upload(client, applicant_id, case_id, PAN_COMPLETE, document_type="PAN")
    body = upload(client, applicant_id, case_id, DRIVING_LICENCE,
                  document_type="ADDRESS_PROOF").json()

    assert body["readiness"]["status"] == "NOT_READY"
    outstanding = {i.get("code") for i in body["pending_items"]}
    assert "DOCUMENT_MISSING" in outstanding
    assert body["next_action"]["action"] == "COLLECT_DOCUMENT"
    assert body["next_action"]["target"] == "BANK_STATEMENT"


# ==========================================================================
# NOTHING IN THE ANSWER OUTRUNS THE VERDICT
# ==========================================================================

def test_the_copilot_never_reports_a_document_as_verified_that_is_not(client):
    """
    The narrated answer and the stored verdict cannot disagree.

    The copilot phrases what the pipeline decided. A summary that calls a
    rejected document verified is the failure this catches, and it is the one
    a reader of the answer would never notice.
    """
    applicant_id, case_id = open_case(client)
    upload(client, applicant_id, case_id, blank_image(),
           document_type="PAN", filename="dummy.png")

    body = ask(client, applicant_id, case_id, "GET_VERIFICATION_STATUS")

    for document in body["verification"]["documents"]:
        assert document["verification"] not in PASSING

    # The checklist travels with every case-scoped answer, so the screen
    # behind this one renders from the same call -- and it agrees.
    assert slot(body, "PAN")["status"] != "VERIFIED"
