"""
Multiple documents in one upload, and what must survive a bad one.

THE RULE THIS SUITE EXISTS FOR: ONE BAD FILE MUST NOT POISON THE BATCH.

A field officer sitting with an applicant photographs three documents and
sends them together. If a blurred shot of the third makes the request fail,
they re-photograph everything and send everything again — and the two good
documents are re-OCR'd for nothing. Worse, if the batch reports a single
verdict, the officer cannot tell which file to redo.

So every file is classified and verified on its own, every file gets its own
entry in the response, and the failures are NAMED rather than counted.

Everything else this suite defends:

    an asserted type is checked, never applied
    an omitted type is classified, not guessed by the caller
    the same filename in two cases stays two documents
    an empty file fails alone
    the checklist, pending items, next action and readiness all move together

Cases are opened through the public endpoint and documents go through the
public endpoint. Nothing is written behind the API, because a test that seeds
the store is not testing the upload path.
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
BANK_STATEMENT = SAMPLES / "bank_hdfc_new.pdf"

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    """A repository per test. No test sees another test's documents."""
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "multi_upload.sqlite3")
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
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {
            "full_name": "Batch Test Applicant", "mobile": "9876543210",
            "date_of_birth": "1990-04-12", "address": "Mumbai, Maharashtra",
        },
        "application": {"product": product, "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def blank_image() -> bytes:
    """A plain rectangle. Not a document, and must never pass as one."""
    buffer = io.BytesIO()
    Image.new("RGB", (900, 560), (235, 235, 235)).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, applicant_id, case_id, items, *, types=None):
    """
    Push files through the public endpoint.

    `items` is a list of (filename, bytes-or-Path). `types` is the positional
    document_types list, or None to let the pipeline classify.
    """
    files = []
    for name, source in items:
        content = (Path(source).read_bytes()
                   if isinstance(source, (str, Path)) else source)
        files.append(("files", (name, content, "application/octet-stream")))

    # A dict with a list value is what repeats a multipart field in this
    # client. A list of (key, value) tuples produces no content-type at all,
    # which silently sends the request as JSON.
    data: dict[str, object] = {
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT",
    }
    if types is not None:
        data["document_types"] = list(types)

    return client.post("/api/v1/fos/copilot", data=data, files=files)


def outcomes(body) -> dict[str, dict]:
    """Per-file results, keyed by the filename that was sent."""
    return {o["source_id"]: o
            for o in body["verification"]["documents_processed"]}


def slot(body, name):
    return next(e for e in body["checklist"] if e["slot"] == name)


# ==========================================================================
# ONE FILE STILL WORKS
# ==========================================================================

def test_a_single_file_upload_is_unchanged(client):
    """The single-file shape older callers were written against still holds."""
    applicant_id, case_id = open_case(client)
    response = client.post("/api/v1/fos/copilot", data={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT", "document_type": "PAN",
    }, files={"file": ("pan.jpg", PAN.read_bytes(), "image/jpeg")})

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["verification"]["verification"] == "PASS"
    assert body["verification"]["document_type"] == "PAN"
    assert body["verification"]["extraction_released"] is True
    # And the new per-file list is present alongside it.
    assert body["verification"]["total"] == 1
    assert len(body["verification"]["documents_processed"]) == 1


# ==========================================================================
# SEVERAL FILES, ONE REQUEST
# ==========================================================================

def test_two_documents_are_each_verified_on_their_own(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  types=["PAN", "DRIVING_LICENCE"]).json()

    results = outcomes(body)
    assert set(results) == {"pan.jpg", "dl.jpg"}
    assert results["pan.jpg"]["document_type"] == "PAN"
    assert results["dl.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert results["pan.jpg"]["verification"] == "PASS"
    assert results["dl.jpg"]["verification"] == "PASS"
    assert body["verification"]["passed"] == 2
    assert body["verification"]["not_passed"] == 0


def test_three_documents_satisfy_three_slots_in_one_request(client):
    """PAN, address proof and a bank statement — a whole personal loan file."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [
        ("pan.jpg", PAN),
        ("dl.jpg", LICENCE),
        ("bank.pdf", BANK_STATEMENT),
    ], types=["PAN", "ADDRESS_PROOF", "BANK_STATEMENT"]).json()

    results = outcomes(body)
    assert len(results) == 3
    assert results["pan.jpg"]["verification"] == "PASS"
    assert results["dl.jpg"]["verification"] == "PASS"
    # The statement's own verdict is whatever verification decided; what this
    # asserts is that it was PROCESSED and reported, not that it passed.
    assert results["bank.pdf"]["verification"] in {"PASS", "REVIEW", "FAIL"}
    assert results["bank.pdf"]["document_type"] is not None

    assert slot(body, "PAN")["status"] == "VERIFIED"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"


def test_document_types_are_matched_by_position(client):
    """The second type belongs to the second file, not to whichever matches."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("first.jpg", LICENCE), ("second.jpg", PAN)],
                  types=["DRIVING_LICENCE", "PAN"]).json()

    results = outcomes(body)
    assert results["first.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert results["second.jpg"]["document_type"] == "PAN"
    assert all(o["verification"] == "PASS" for o in results.values())


# ==========================================================================
# ONE BAD FILE MUST NOT POISON THE BATCH
# ==========================================================================

def test_a_failing_document_does_not_stop_the_others(client):
    """
    THE CENTRAL TEST. Two genuine documents and a blank image, together.

    The blank must fail, the other two must pass, and the response must say
    which was which.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [
        ("pan.jpg", PAN),
        ("dl.jpg", LICENCE),
        ("dummy.png", blank_image()),
    ], types=["PAN", "DRIVING_LICENCE", ""]).json()

    results = outcomes(body)
    assert len(results) == 3

    assert results["pan.jpg"]["verification"] == "PASS"
    assert results["pan.jpg"]["extraction_released"] is True
    assert results["dl.jpg"]["verification"] == "PASS"

    assert results["dummy.png"]["verification"] != "PASS"
    assert results["dummy.png"]["extraction_released"] is False

    assert body["verification"]["passed"] == 2
    assert body["verification"]["not_passed"] == 1

    # The good documents still moved the case on.
    assert slot(body, "PAN")["status"] == "VERIFIED"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"


def test_the_answer_names_which_file_failed(client):
    """
    Counting is not enough.

    "One document did not pass" leaves the officer opening every file to find
    out which one to photograph again.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [
        ("good_pan.jpg", PAN),
        ("bad_scan.png", blank_image()),
    ], types=["PAN", ""]).json()

    assert "bad_scan.png" in body["answer"]
    assert "good_pan.jpg" not in body["answer"]


# ==========================================================================
# ASSERTED TYPES ARE CHECKED, NEVER APPLIED
# ==========================================================================

def test_a_wrong_asserted_type_fails_only_that_file(client):
    """
    The PAN is genuine and the assertion is wrong.

    It must fail with DOCUMENT_TYPE_MISMATCH and must not be silently
    reclassified — and the licence beside it must be unaffected.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  types=["DRIVING_LICENCE", "DRIVING_LICENCE"]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in results["pan.jpg"]["reason_codes"]
    assert results["pan.jpg"]["extraction_released"] is False
    assert results["pan.jpg"]["expected_type"] == "DRIVING_LICENCE"

    assert results["dl.jpg"]["verification"] == "PASS"


def test_an_omitted_type_is_classified_by_the_pipeline(client):
    """The frontend is never responsible for classification."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("mystery1.jpg", PAN), ("mystery2.jpg", LICENCE)]).json()

    results = outcomes(body)
    assert results["mystery1.jpg"]["document_type"] == "PAN"
    assert results["mystery2.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert all(o["expected_type"] is None for o in results.values())


def test_some_types_may_be_supplied_and_others_left_blank(client):
    """An officer who knows one of two types should not have to guess both."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("unknown.jpg", LICENCE)],
                  types=["PAN", ""]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["expected_type"] == "PAN"
    assert results["unknown.jpg"]["expected_type"] is None
    assert results["unknown.jpg"]["document_type"] == "DRIVING_LICENCE"


def test_more_types_than_files_is_refused(client):
    """Positional matching is only meaningful if the lists can be aligned."""
    applicant_id, case_id = open_case(client)
    response = upload(client, applicant_id, case_id, [("pan.jpg", PAN)],
                      types=["PAN", "DRIVING_LICENCE"])
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "DOCUMENT_TYPES_MISALIGNED"


def test_an_unknown_asserted_type_is_refused_before_any_processing(client):
    applicant_id, case_id = open_case(client)
    response = upload(client, applicant_id, case_id, [("pan.jpg", PAN)],
                      types=["NOT_A_REAL_TYPE"])
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "UNSUPPORTED_DOCUMENT_TYPE"


# ==========================================================================
# EDGE CASES
# ==========================================================================

def test_no_files_at_all_is_refused(client):
    applicant_id, case_id = open_case(client)
    response = client.post("/api/v1/fos/copilot", data={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT",
    })
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "FILE_REQUIRED"


def test_an_empty_file_beside_a_good_one_fails_only_itself(client):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("empty.jpg", b"")]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["verification"] == "PASS"
    assert results["empty.jpg"]["verification"] == "FAIL"
    assert "EMPTY_FILE" in results["empty.jpg"]["reason_codes"]


def test_every_file_empty_is_refused(client):
    applicant_id, case_id = open_case(client)
    response = upload(client, applicant_id, case_id,
                      [("a.jpg", b""), ("b.jpg", b"")])
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "EMPTY_FILE"


def test_an_unreadable_file_type_fails_without_taking_the_batch_down(client):
    """Something that is not an image or a PDF at all."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [
        ("pan.jpg", PAN),
        ("notes.txt", b"this is not a document, it is a text file" * 20),
    ]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["verification"] == "PASS"
    assert results["notes.txt"]["verification"] != "PASS"
    assert results["notes.txt"]["extraction_released"] is False


def test_the_same_filename_twice_in_one_request_keeps_both(client):
    """Two photographs both called `image.jpg` are two documents."""
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("image.jpg", PAN), ("image.jpg", LICENCE)]).json()

    assert body["verification"]["total"] == 2
    kinds = {o["document_type"]
             for o in body["verification"]["documents_processed"]}
    assert kinds == {"PAN", "DRIVING_LICENCE"}


def test_the_same_file_in_two_cases_stays_isolated(client):
    """Identity is the case, never the filename."""
    first_applicant, first_case = open_case(client)
    second_applicant, second_case = open_case(client)

    upload(client, first_applicant, first_case, [("PAN.pdf", PAN)],
           types=["PAN"])
    upload(client, second_applicant, second_case, [("PAN.pdf", PAN)],
           types=["PAN"])

    def documents(applicant_id, case_id):
        response = client.post("/api/v1/fos/copilot", json={
            "applicant_id": applicant_id, "case_id": case_id,
            "action": "GET_DOCUMENTS",
        })
        assert response.status_code == 200
        return response.json()["documents"]

    first = documents(first_applicant, first_case)
    second = documents(second_applicant, second_case)

    assert len(first) == 1 and len(second) == 1
    assert first[0]["document_id"] != second[0]["document_id"]


def test_re_uploading_the_same_document_does_not_duplicate_the_slot(client):
    """
    A second upload of the same type replaces rather than accumulates.

    An officer who re-photographs a document should end with one PAN on the
    case, not two competing records.
    """
    applicant_id, case_id = open_case(client)
    upload(client, applicant_id, case_id, [("pan.jpg", PAN)], types=["PAN"])
    body = upload(client, applicant_id, case_id, [("pan.jpg", PAN)],
                  types=["PAN"]).json()

    pan_documents = [d for d in body["documents"]
                     if d.get("document_type") == "PAN"]
    assert len(pan_documents) == 1
    assert slot(body, "PAN")["status"] == "VERIFIED"


# ==========================================================================
# THE CASE MOVES
# ==========================================================================

def test_the_whole_case_state_advances_on_one_batch(client):
    """
    Checklist, pending items, next action and readiness all move together.

    They are computed from the same records, so a batch that satisfies two
    slots must be reflected in all four or they are disagreeing about the
    same case.
    """
    applicant_id, case_id = open_case(client)

    before = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_CASE_360",
    }).json()
    assert before["readiness"]["status"] == "NOT_READY"
    assert {e["status"] for e in before["checklist"]} == {"MISSING"}

    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  types=["PAN", "ADDRESS_PROOF"]).json()

    assert slot(body, "PAN")["status"] == "VERIFIED"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"

    outstanding = {i.get("code") for i in body["pending_items"]}
    assert "DOCUMENT_MISSING" in outstanding
    assert body["next_action"]["action"] == "COLLECT_DOCUMENT"
    assert body["next_action"]["target"] == "BANK_STATEMENT"
    assert body["readiness"]["status"] == "NOT_READY"

    blocking = {i["code"] for i in body["readiness"]["blocking_items"]}
    assert "DOCUMENT_MISSING" in blocking


def test_a_multi_document_upload_runs_no_kyc(client):
    """
    THIS ASSERTION IS THE REVERSE OF WHAT IT WAS, deliberately.

    It used to require a two-document upload to return a KYC result, on the
    reasoning that a batch is the only thing that gives KYC two sources to
    compare. That reasoning was about what KYC NEEDS, and it never asked
    whether the FOS stage should be running it at all.

    It should not. Document verification asks "is this upload usable as the
    type it claims?"; KYC asks "do these documents describe the same
    person?". Different questions, different stages — and an officer adding
    a licence to a case was being handed a cross-document name and
    date-of-birth comparison from an upload endpoint.

    KYC itself is unchanged and still runs on /api/v1/los/process. What
    changed is who calls it.
    """
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                  types=["PAN", "DRIVING_LICENCE"]).json()

    # Both verified — and still no identity comparison between them.
    outcomes = {o["source_id"]: o
                for o in body["verification"]["documents_processed"]}
    assert outcomes["pan.jpg"]["verification"] == "PASS"
    assert outcomes["dl.jpg"]["verification"] == "PASS"

    assert body["kyc"] is None, "a FOS upload produced a KYC result"

    import json

    blob = json.dumps(body)
    for field in ("NAME_MISMATCH", "DOB_MISMATCH", "match_score",
                  "overall_confidence"):
        assert field not in blob, f"{field!r} is a KYC field on a FOS upload"


def test_an_upload_response_carries_no_internals(client):
    """No OCR token, box, prompt, path or stack trace reaches a caller."""
    import json

    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id,
                  [("pan.jpg", PAN), ("dl.jpg", LICENCE)]).json()

    blob = json.dumps(body).lower()
    for forbidden in ("bounding", "bbox", "ocr_confidence", "field_quality",
                      "confidence_factors", "traceback", "system_prompt",
                      "select ", "sqlite", "c:\\\\"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the response"


# ==========================================================================
# HOWEVER THE CLIENT SPELLS document_types
#
# Swagger UI serialises an array field in a multipart body by joining it with
# commas. Three types picked in the UI arrived as ONE part:
#
#     -F 'document_types= PAN,DRIVING_LICENCE,BANK_STATEMENT'
#
# Read literally that asserts a single document is of type
# "PAN,DRIVING_LICENCE,BANK_STATEMENT", and the endpoint refused it with
# UNSUPPORTED_DOCUMENT_TYPE -- correctly, and uselessly, because the person
# had done exactly what the form asked of them.
# ==========================================================================

def upload_raw(client, applicant_id, case_id, items, type_parts):
    """
    Upload with document_types sent EXACTLY as given.

    `type_parts` is the list of raw multipart values, so a test can send one
    joined string or several separate ones and get the wire format it asked
    for rather than whatever the helper prefers.
    """
    files = []
    for name, source in items:
        content = (Path(source).read_bytes()
                   if isinstance(source, (str, Path)) else source)
        files.append(("files", (name, content, "application/octet-stream")))

    data: dict[str, object] = {
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "UPLOAD_DOCUMENT",
    }
    if type_parts is not None:
        data["document_types"] = list(type_parts)

    return client.post("/api/v1/fos/copilot", data=data, files=files)


def test_a_repeated_document_types_field_is_accepted(client):
    """The correct multipart spelling: one part per file."""
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id,
                      [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                      ["PAN", "DRIVING_LICENCE"]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["expected_type"] == "PAN"
    assert results["dl.jpg"]["expected_type"] == "DRIVING_LICENCE"
    assert all(o["verification"] == "PASS" for o in results.values())


def test_a_comma_joined_document_types_value_is_split(client):
    """
    THE BUG. One part carrying every type, which is what Swagger UI sends.

    Before the fix this was refused with UNSUPPORTED_DOCUMENT_TYPE.
    """
    applicant_id, case_id = open_case(client)
    response = upload_raw(client, applicant_id, case_id,
                          [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                          ["PAN,DRIVING_LICENCE"])

    assert response.status_code == 200, response.text
    results = outcomes(response.json())
    assert results["pan.jpg"]["expected_type"] == "PAN"
    assert results["dl.jpg"]["expected_type"] == "DRIVING_LICENCE"
    assert all(o["verification"] == "PASS" for o in results.values())


def test_a_comma_joined_value_with_the_spacing_swagger_emits(client):
    """The generated curl carries a leading space: `= PAN,DL,BANK`."""
    applicant_id, case_id = open_case(client)
    response = upload_raw(client, applicant_id, case_id, [
        ("pan.jpg", PAN), ("dl.jpg", LICENCE), ("bank.pdf", BANK_STATEMENT),
    ], [" PAN, ADDRESS_PROOF , BANK_STATEMENT"])

    assert response.status_code == 200, response.text
    results = outcomes(response.json())
    assert results["pan.jpg"]["expected_type"] == "PAN"
    assert results["dl.jpg"]["expected_type"] == "ADDRESS_PROOF"
    assert results["bank.pdf"]["expected_type"] == "BANK_STATEMENT"


def test_three_files_and_three_joined_types_map_positionally(client):
    """The Nth type belongs to the Nth file, joined or not."""
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id, [
        ("first.jpg", LICENCE), ("second.jpg", PAN),
        ("third.pdf", BANK_STATEMENT),
    ], ["DRIVING_LICENCE,PAN,BANK_STATEMENT"]).json()

    results = outcomes(body)
    assert results["first.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert results["second.jpg"]["document_type"] == "PAN"
    assert results["first.jpg"]["verification"] == "PASS"
    assert results["second.jpg"]["verification"] == "PASS"


def test_a_blank_entry_inside_a_joined_value_still_classifies_that_file(client):
    """
    `PAN,,BANK_STATEMENT` keeps its middle gap.

    If the gap collapsed, the third assertion would slide onto the second
    file and a correct upload would fail as a type mismatch.
    """
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id, [
        ("pan.jpg", PAN), ("dl.jpg", LICENCE), ("bank.pdf", BANK_STATEMENT),
    ], ["PAN,,BANK_STATEMENT"]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["expected_type"] == "PAN"
    assert results["dl.jpg"]["expected_type"] is None
    assert results["dl.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert results["bank.pdf"]["expected_type"] == "BANK_STATEMENT"


def test_an_untouched_empty_document_types_field_asserts_nothing(client):
    """
    Swagger sends an empty part for a field the user did not fill in.

    It must not claim the first file's slot -- that would assert "" against
    the first document and shift everything after it.
    """
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id,
                      [("pan.jpg", PAN), ("dl.jpg", LICENCE)], [""]).json()

    results = outcomes(body)
    assert all(o["expected_type"] is None for o in results.values())
    assert results["pan.jpg"]["document_type"] == "PAN"
    assert results["dl.jpg"]["document_type"] == "DRIVING_LICENCE"


def test_a_joined_value_still_refuses_an_unknown_type(client):
    """Splitting must not become a way to smuggle a bad type through."""
    applicant_id, case_id = open_case(client)
    response = upload_raw(client, applicant_id, case_id,
                          [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                          ["PAN,NOT_A_REAL_TYPE"])
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "UNSUPPORTED_DOCUMENT_TYPE"


def test_a_joined_value_still_enforces_the_type_assertion(client):
    """
    The comma path must be exactly as strict as the repeated path.

    A wrong assertion still fails, and is never quietly reclassified.
    """
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id,
                      [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                      ["DRIVING_LICENCE,DRIVING_LICENCE"]).json()

    results = outcomes(body)
    assert results["pan.jpg"]["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in results["pan.jpg"]["reason_codes"]
    assert results["pan.jpg"]["extraction_released"] is False
    assert results["dl.jpg"]["verification"] == "PASS"


def test_a_joined_value_still_rejects_more_types_than_files(client):
    applicant_id, case_id = open_case(client)
    response = upload_raw(client, applicant_id, case_id, [("pan.jpg", PAN)],
                          ["PAN,DRIVING_LICENCE"])
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "DOCUMENT_TYPES_MISALIGNED"


def test_address_proof_works_through_a_joined_value(client):
    """The logical slot resolves the same way whichever spelling arrived."""
    applicant_id, case_id = open_case(client)
    body = upload_raw(client, applicant_id, case_id,
                      [("pan.jpg", PAN), ("dl.jpg", LICENCE)],
                      ["PAN,ADDRESS_PROOF"]).json()

    results = outcomes(body)
    assert results["dl.jpg"]["document_type"] == "DRIVING_LICENCE"
    assert results["dl.jpg"]["verification"] == "PASS"
    assert slot(body, "ADDRESS_PROOF")["status"] == "VERIFIED"


def test_the_openapi_schema_tells_swagger_to_explode_the_arrays():
    """
    The generated curl must be right, not merely absorbed.

    A form whose example shows a comma-joined value teaches every reader the
    wrong shape, even once the backend tolerates it.
    """
    import main

    spec = main.app.openapi()
    content = (spec["paths"]["/api/v1/fos/copilot"]["post"]["requestBody"]
               ["content"]["multipart/form-data"])

    encoding = content.get("encoding") or {}
    assert encoding.get("document_types", {}).get("explode") is True
    assert encoding.get("files", {}).get("explode") is True
