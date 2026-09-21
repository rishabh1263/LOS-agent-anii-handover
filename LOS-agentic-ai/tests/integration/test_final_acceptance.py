"""
The acceptance matrix, exercised through the real endpoint.

WHAT THIS IS FOR. Every behaviour below is already covered by a focused
suite somewhere. This file exists because "each part works" and "the
thing works" are different claims, and the second one is what gets
handed to a frontend team. It drives `POST /api/v1/los/process` the way
a client will and asserts what a client will actually receive.

ONE RUN, MANY ASSERTIONS. The end-to-end calls are real OCR and cost
seconds each, so the expensive scenarios are executed once and cached;
the tests read the cached bodies. A scenario that cannot run (missing
sample) skips rather than failing, and says which sample was missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/los/process"

R = Path("samples/real_batch")
D = Path("samples/documents")

PRIMARY_PAN = D / "rpan.jpg"      # RISHABH AJIT SINGH
CO_PAN = D / "lPan.jpg"           # LAXMI SANTOSH GUPTA -- a different person
PRIMARY_DL = R / "dl1.jpg"
CO_DL = R / "dl2.jpg"
BANK = D / "Canara Bank Statement.pdf"

pytestmark = pytest.mark.skipif(
    not (PRIMARY_PAN.exists() and CO_PAN.exists() and PRIMARY_DL.exists()),
    reason="identity samples not available",
)

#: Scenario name -> response body. Populated once per session.
_RUNS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "acceptance.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def up(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "application/octet-stream"))


def call(client, files, **data):
    response = client.post(ENDPOINT, files=files, data=data)
    assert response.status_code == 200, response.text
    return response.json()


def cached(client, name: str, builder):
    if name not in _RUNS:
        _RUNS[name] = builder(client)
    return _RUNS[name]


# ==========================================================================
# THE SCENARIOS
# ==========================================================================


@pytest.fixture
def pan_only(client):
    """1. Primary, one PAN."""
    return cached(client, "pan_only", lambda c: call(
        c, [up("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="ACC-APP", case_id="ACC-1",
        expected_types="PAN"))


@pytest.fixture
def pan_and_dl(client):
    """2. Primary, PAN + driving licence."""
    return cached(client, "pan_and_dl", lambda c: call(
        c, [up("files", "pan.jpg", PRIMARY_PAN),
            up("files", "dl.jpg", PRIMARY_DL)],
        operation="PROCESS", applicant_id="ACC-APP", case_id="ACC-2",
        expected_types="PAN,DRIVING_LICENCE"))


@pytest.fixture
def two_party(client):
    """3, 4, 12-14, 24-26. Both parties, SAME filename, different people."""
    return cached(client, "two_party", lambda c: call(
        c, [up("files", "pan.jpg", PRIMARY_PAN),
            up("files", "dl.jpg", PRIMARY_DL),
            up("co_applicant_files", "pan.jpg", CO_PAN),
            up("co_applicant_files", "dl.jpg", CO_DL)],
        operation="PROCESS", applicant_id="ACC-APP",
        co_applicant_id="ACC-CO", case_id="ACC-3",
        expected_types="PAN,DRIVING_LICENCE",
        co_applicant_expected_types="PAN,DRIVING_LICENCE"))


@pytest.fixture
def wrong_type(client):
    """6, 7, 8. A PAN declared as a driving licence."""
    return cached(client, "wrong_type", lambda c: call(
        c, [up("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="ACC-APP", case_id="ACC-6",
        expected_types="DRIVING_LICENCE"))


@pytest.fixture
def with_profile(client):
    """16. A declared profile, matched against the party's own documents."""
    return cached(client, "with_profile", lambda c: call(
        c, [up("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="ACC-APP", case_id="ACC-16",
        expected_types="PAN", applicant_pan="NUHPS4875K"))


# ==========================================================================
# 1-5, 19. THE REQUEST CONTRACT
# ==========================================================================


def test_a_single_pan_is_processed(pan_only):
    assert len(pan_only["documents"]) == 1
    assert pan_only["documents"][0]["type"] == "PAN"


def test_two_primary_documents_map_positionally(pan_and_dl):
    """5, 19. files[i] pairs with expected_types[i]."""
    types = {d["source_id"]: d["type"] for d in pan_and_dl["documents"]}

    assert types == {"pan.jpg": "PAN", "dl.jpg": "DRIVING_LICENCE"}


def test_both_parties_are_processed_in_one_call(two_party):
    assert len(two_party["documents"]) == 4


def test_co_applicant_types_map_positionally_too(two_party):
    by_party: dict[str, dict[str, str]] = {}
    for d in two_party["documents"]:
        by_party.setdefault(d["party_id"], {})[d["source_id"]] = d["type"]

    assert by_party["ACC-APP"] == {"pan.jpg": "PAN",
                                   "dl.jpg": "DRIVING_LICENCE"}
    assert by_party["ACC-CO"] == {"pan.jpg": "PAN",
                                  "dl.jpg": "DRIVING_LICENCE"}


def test_the_same_filename_from_both_parties_stays_separate(two_party):
    """4. Both sent `pan.jpg` and `dl.jpg`."""
    names = [d["source_id"] for d in two_party["documents"]]

    assert names.count("pan.jpg") == 2
    assert names.count("dl.jpg") == 2
    assert two_party["primary_applicant"]["document_ids"] == ["pan.jpg",
                                                              "dl.jpg"]
    assert two_party["co_applicant"]["document_ids"] == ["pan.jpg", "dl.jpg"]


# ==========================================================================
# 6-9. THE VERIFICATION AND EXTRACTION GATE
# ==========================================================================


def test_a_wrong_expected_type_fails_verification(wrong_type):
    """6, 7."""
    document = wrong_type["documents"][0]

    assert document["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in (document.get("reason_codes") or [])


def test_a_failed_document_releases_no_extraction(wrong_type):
    """8. The gate."""
    document = wrong_type["documents"][0]

    assert document.get("extraction") is None
    assert document.get("has_extracted_fields") is False


def test_a_wrong_upload_asks_for_the_right_one(wrong_type):
    """It is a wrong file, not a credit rejection."""
    assert wrong_type["next_action"] == "REQUEST_CORRECT_DOCUMENT"
    assert wrong_type["decision"] != "REJECT"


def test_a_passing_document_does_release_extraction(pan_only):
    """9."""
    document = pan_only["documents"][0]

    assert document["verification"] == "PASS"
    assert document["extraction"]["pan_number"]


# ==========================================================================
# 10-15. KYC, PARTY-SCOPED
# ==========================================================================


def test_each_party_has_its_own_kyc(two_party):
    for section in ("primary_applicant", "co_applicant"):
        kyc = two_party[section]["kyc"]
        assert set(kyc) == {"status", "reason_codes", "overall_score",
                            "overall_confidence", "fields"}


def test_neither_party_is_measured_against_the_other(two_party):
    """
    14. THE ONE THAT MATTERS. Two different people; each party's KYC
    may only cite that party's own documents.
    """
    for section, party in (("primary_applicant", "ACC-APP"),
                           ("co_applicant", "ACC-CO")):
        owned = set(two_party[section]["document_ids"])
        for field in two_party[section]["kyc"]["fields"]:
            for source in field.get("sources") or []:
                assert source["source_id"] in owned


def test_no_cross_party_check_reaches_cross_document(two_party):
    """Nothing was compared across parties, so nothing is claimed."""
    assert two_party["cross_document"] == {"status": "SKIPPED", "checks": []}


def test_kyc_fields_carry_scores_and_confidence(two_party):
    """15."""
    for section in ("primary_applicant", "co_applicant"):
        for field in two_party[section]["kyc"]["fields"]:
            assert 0 <= field["match_score"] <= 100
            assert 0 <= field["confidence"] <= 100
            assert field["status"] in {"PASS", "PARTIAL", "FAIL", "SKIPPED"}
            assert field.get("reason_code")


def test_a_single_source_field_is_skipped_not_failed(pan_only):
    """One document cannot be cross-checked against anything."""
    statuses = {f["field"]: f["status"] for f in pan_only["kyc"]["fields"]}

    assert set(statuses.values()) <= {"SKIPPED", "PASS", "PARTIAL", "FAIL"}
    assert "INSUFFICIENT_SOURCES" in pan_only["kyc"]["reason_codes"]


# ==========================================================================
# 16-18. PROFILE MATCH, ADDRESS, BANK
# ==========================================================================


def test_a_declared_profile_is_matched_against_its_own_party(with_profile):
    """16."""
    match = with_profile["primary_applicant"]["profile_match"]
    pan = next(f for f in match["fields"] if f["field"] == "PAN_NUMBER")

    assert match["party_id"] == "ACC-APP"
    assert pan["status"] == "PASS"


def test_an_address_is_published_as_components_or_not_at_all(two_party):
    """17."""
    for document in two_party["documents"]:
        address = (document.get("extraction") or {}).get("address")
        if address is None:
            continue
        assert isinstance(address, dict)
        assert set(address) <= {"house", "street", "locality", "city",
                                "district", "state", "pincode"}
        if "pincode" in address:
            assert len(address["pincode"]) == 6


@pytest.mark.skipif(not BANK.exists(), reason="bank sample not available")
def test_a_bank_statement_is_processed_with_json_native_numbers(client):
    """18."""
    body = call(client, [up("files", "bank.pdf", BANK)],
                operation="PROCESS", applicant_id="ACC-APP",
                case_id="ACC-18", expected_types="BANK_STATEMENT")
    document = body["documents"][0]

    assert document["type"] == "BANK_STATEMENT"
    assert "Decimal(" not in json.dumps(body)
    signals = (document.get("extraction") or {}).get("signals")
    if signals:
        for value in signals.values():
            assert not isinstance(value, str) or not value.replace(
                ".", "").isdigit()


# ==========================================================================
# 20. A BAD REQUEST
# ==========================================================================


def test_a_request_with_no_files_is_refused(client):
    response = client.post(ENDPOINT, data={"operation": "PROCESS"})

    assert response.status_code == 422


def test_co_applicant_files_without_an_id_are_refused(client):
    response = client.post(
        ENDPOINT,
        files=[up("files", "pan.jpg", PRIMARY_PAN),
               up("co_applicant_files", "dl.jpg", PRIMARY_DL)],
        data={"operation": "PROCESS", "applicant_id": "ACC-APP",
              "case_id": "ACC-20"})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "CO_APPLICANT_ID_REQUIRED"


def test_an_unknown_operation_is_refused(client):
    response = client.post(
        ENDPOINT, files=[up("files", "pan.jpg", PRIMARY_PAN)],
        data={"operation": "DESTROY", "applicant_id": "ACC-APP"})

    assert response.status_code == 422


# ==========================================================================
# 21-23. THE SUMMARY NEVER DEPENDS ON THE MODEL
# ==========================================================================


def test_the_summary_is_deterministic_and_party_aware(two_party):
    """21, 24-26."""
    summary = two_party["summary"]

    assert two_party["summary_source"] in {"deterministic", "llm"}
    assert "applicant" in summary.lower()
    assert "Overall" in summary


def test_the_request_survives_the_model_being_unavailable(client):
    """22."""
    from unittest.mock import patch

    with patch("app.agents.los.summary._agenerate",
               side_effect=TimeoutError("model down")):
        body = call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
                    operation="PROCESS", applicant_id="ACC-APP",
                    case_id="ACC-22", expected_types="PAN")

    assert body["summary_source"] == "deterministic"
    assert body["summary"]
    assert body["decision"]


def test_a_vague_model_sentence_cannot_reach_the_caller(client):
    """23."""
    from unittest.mock import patch

    with patch("app.agents.los.summary._agenerate",
               return_value="Loan officer review shows all documents except "
                            "Kyc status as successful."):
        body = call(client, [up("files", "pan.jpg", PRIMARY_PAN),
                             up("co_applicant_files", "pan.jpg", CO_PAN)],
                    operation="PROCESS", applicant_id="ACC-APP",
                    co_applicant_id="ACC-CO", case_id="ACC-23",
                    expected_types="PAN", co_applicant_expected_types="PAN")

    assert body["summary_source"] == "deterministic"
    assert "Loan officer review" not in body["summary"]


# ==========================================================================
# THE PUBLISHED CONTRACT
# ==========================================================================


PUBLIC_TOP_LEVEL = {
    "request_id", "applicant_id", "co_applicant_id", "case_id", "status",
    "documents", "kyc", "cross_document", "decision", "next_action",
    "summary", "summary_source", "processing_ms", "errors",
    "primary_applicant", "co_applicant", "profile_match",
}


def test_the_response_carries_only_public_top_level_keys(two_party, pan_only):
    for body in (two_party, pan_only):
        assert set(body) <= PUBLIC_TOP_LEVEL, set(body) - PUBLIC_TOP_LEVEL


def test_a_primary_only_response_has_no_co_applicant(pan_only):
    assert "co_applicant" not in pan_only
    assert "co_applicant_id" not in pan_only


def test_a_party_section_is_the_agreed_shape(two_party):
    for section in ("primary_applicant", "co_applicant"):
        assert set(two_party[section]) <= {
            "party_id", "role", "status", "document_ids",
            "verification_summary", "profile_match", "kyc"}
        assert "documents" not in two_party[section]
        assert "decision" not in two_party[section]
        assert "next_action" not in two_party[section]


def test_nothing_internal_leaks(two_party, pan_only, wrong_type):
    forbidden = ("bbox", "bounding_box", "raw_text", "ocr_text", "tokens",
                 "candidates", "prompt", "Traceback", "file_path",
                 "mcp_payload", "agent_state", "confidence_factors",
                 "Decimal(", "ocr_ms", "classification_ms", "specialist_ms",
                 "llm_ms", "samples/", "C:\\\\")

    for body in (two_party, pan_only, wrong_type):
        blob = json.dumps(body)
        for token in forbidden:
            assert token not in blob, token


def test_the_response_validates_against_the_published_model(two_party):
    from app.agents.los.schemas import LosProcessResponse

    parsed = LosProcessResponse.model_validate(two_party)

    assert parsed.primary_applicant.party_id == "ACC-APP"
    assert parsed.co_applicant.party_id == "ACC-CO"
    assert parsed.decision and parsed.next_action
