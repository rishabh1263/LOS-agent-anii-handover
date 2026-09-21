"""
Party-scoped KYC, through the real endpoint, with real documents.

THE CONTROL EXPERIMENT. The clearest possible statement of isolation is
that adding a co-applicant changes NOTHING about the primary applicant's
KYC. So the same primary documents are run twice — once alone, once with
a second party's card alongside — and the two KYC results are compared
for equality. If a single field of the co-applicant's leaked into the
primary's comparison, the two would differ.

THE DOCUMENTS. `rpan.jpg` and `lPan.jpg` are PAN cards belonging to two
different people, with different names, dates of birth, PANs and fathers'
names. That is what a joint application looks like, and before this phase
it produced NAME_MISMATCH, DOB_MISMATCH, PAN_MISMATCH and
FATHER_NAME_MISMATCH on a perfectly clean case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

PRIMARY_PAN = Path("samples/documents/rpan.jpg")
PRIMARY_DL = Path("samples/real_batch/dl1.jpg")
CO_PAN = Path("samples/documents/lPan.jpg")

pytestmark = pytest.mark.skipif(
    not (PRIMARY_PAN.exists() and PRIMARY_DL.exists() and CO_PAN.exists()),
    reason="sample documents not available",
)

IDENTITY_MISMATCHES = {"NAME_MISMATCH", "DOB_MISMATCH", "PAN_MISMATCH",
                       "FATHER_NAME_MISMATCH"}

#: Response bodies, keyed by scenario. Each run is real OCR over two or
#: three documents and several tests assert on the same one.
_RUNS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "party_kyc.sqlite3")
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


def upload(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "application/octet-stream"))


def post(client, files, **form):
    response = client.post("/api/v1/los/process", data=form, files=files)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def alone(client):
    """The primary applicant's documents, with nobody else on the case."""
    if "alone" not in _RUNS:
        _RUNS["alone"] = post(
            client,
            [upload("files", "pan.jpg", PRIMARY_PAN),
             upload("files", "dl.jpg", PRIMARY_DL)],
            operation="PROCESS", applicant_id="APP-1", case_id="KYC-ALONE",
            expected_types="PAN,DRIVING_LICENCE",
        )
    return _RUNS["alone"]


@pytest.fixture
def joint(client):
    """The very same primary documents, plus a co-applicant's PAN."""
    if "joint" not in _RUNS:
        _RUNS["joint"] = post(
            client,
            [upload("files", "pan.jpg", PRIMARY_PAN),
             upload("files", "dl.jpg", PRIMARY_DL),
             upload("co_applicant_files", "pan.jpg", CO_PAN)],
            operation="PROCESS", applicant_id="APP-1",
            co_applicant_id="COAPP-9", case_id="KYC-JOINT",
            expected_types="PAN,DRIVING_LICENCE",
            co_applicant_expected_types="PAN",
        )
    return _RUNS["joint"]


def sources_cited(kyc) -> set[str]:
    return {
        source["source_id"]
        for field in kyc.get("fields") or []
        for source in (field.get("sources") or [])
        if source.get("source_id")
    }


# ==========================================================================
# THE CONTROL EXPERIMENT
# ==========================================================================


def test_adding_a_co_applicant_does_not_change_the_primarys_kyc(alone, joint):
    """
    THE MANDATORY GUARD, END TO END.

    Identical primary documents, identical primary KYC. A single field
    of Person B's reaching Person A's comparison would show up here.
    """
    assert joint["primary_applicant"]["kyc"] == alone["kyc"]


def test_the_primarys_kyc_cites_only_the_primarys_documents(joint):
    primary = joint["primary_applicant"]

    cited = sources_cited(primary["kyc"])

    assert cited <= set(primary["document_ids"])


def test_the_co_applicants_kyc_cites_only_their_own_documents(joint):
    co = joint["co_applicant"]

    assert sources_cited(co["kyc"]) <= set(co["document_ids"])


def test_neither_party_is_measured_against_the_other(joint):
    """
    The two people genuinely differ on every identity field. Neither
    party's KYC may report that as a disagreement, because neither
    party's OWN documents disagree about it.
    """
    for section in ("primary_applicant", "co_applicant"):
        owned = set(joint[section]["document_ids"])
        for field in joint[section]["kyc"].get("fields") or []:
            for source in field.get("sources") or []:
                assert source["source_id"] in owned


def test_a_two_party_case_is_not_pushed_to_review_by_being_joint(joint, alone):
    """
    THE DEFECT, STATED AS AN OUTCOME. The case verdict must be no worse
    for having a second party on it than the primary's own documents
    already made it.
    """
    severity = {"PASS": 0, "SKIPPED": 0, "REVIEW": 1, "FAIL": 2}

    assert severity[joint["kyc"]["status"]] <= max(
        severity[alone["kyc"]["status"]],
        severity[joint["co_applicant"]["kyc"]["status"]],
    )


# ==========================================================================
# WHAT EACH SECTION PUBLISHES
# ==========================================================================


def test_each_party_publishes_its_own_kyc(joint):
    for section in ("primary_applicant", "co_applicant"):
        kyc = joint[section]["kyc"]
        assert set(kyc) == {"status", "reason_codes", "overall_score",
                            "overall_confidence", "fields"}


def test_the_case_kyc_is_compact_on_a_two_party_case(joint):
    """
    CHANGED DELIBERATELY. The case object used to repeat both parties'
    field rows, which put the whole of both parties' KYC in the
    response twice. The rows live under the party they belong to; the
    case object keeps the verdict that is only stated here.
    """
    assert set(joint["kyc"]) == {"status", "reason_codes", "overall_score",
                                 "overall_confidence"}
    assert joint["primary_applicant"]["kyc"]["fields"]
    assert joint["co_applicant"]["kyc"]["fields"]


def test_the_case_verdict_is_the_worst_partys(joint):
    severity = {"PASS": 0, "SKIPPED": 0, "REVIEW": 1, "FAIL": 2}
    parties = [joint[s]["kyc"]["status"]
               for s in ("primary_applicant", "co_applicant")]

    assert severity[joint["kyc"]["status"]] == max(
        severity[s] for s in parties)


def test_the_case_reason_codes_are_the_union_of_the_parties(joint):
    case = set(joint["kyc"]["reason_codes"])
    parties = set()
    for section in ("primary_applicant", "co_applicant"):
        parties |= set(joint[section]["kyc"]["reason_codes"])

    assert case == parties


# ==========================================================================
# BACKWARD COMPATIBILITY
# ==========================================================================


def test_a_single_applicant_case_publishes_the_same_kyc_as_always(alone):
    assert set(alone["kyc"]) == {"status", "reason_codes", "overall_score",
                                 "overall_confidence", "fields"}
    assert isinstance(alone["kyc"]["overall_score"], int)
    assert isinstance(alone["kyc"]["reason_codes"], list)


def test_a_single_applicant_row_carries_no_owner(alone):
    """
    The owner is added only where two parties make it necessary, so a
    single-applicant response carries exactly the keys it carried before.
    """
    for row in alone["kyc"]["fields"]:
        assert "party_id" not in row


def test_the_case_kyc_is_the_primarys_kyc_when_alone(alone):
    """
    Not repeated inside the section: on a single-applicant case the
    top-level object already IS this party's KYC, and a second copy
    carries no information while doubling that part of the payload.
    """
    assert "kyc" not in alone["primary_applicant"]
    assert alone["kyc"]["status"] in {"PASS", "REVIEW", "FAIL", "SKIPPED"}


def test_the_cross_document_object_still_reports(alone, joint):
    for body in (alone, joint):
        assert body["cross_document"]["status"] in {
            "PASS", "REVIEW", "FAIL", "SKIPPED"}


def test_every_existing_top_level_key_survives(joint):
    for key in ("request_id", "applicant_id", "case_id", "status",
                "documents", "kyc", "cross_document", "decision",
                "next_action", "summary", "summary_source",
                "processing_ms", "errors"):
        assert key in joint, key


def test_the_response_validates_against_its_contract(joint):
    from app.agents.los.schemas import LosProcessResponse

    parsed = LosProcessResponse.model_validate(joint)

    assert parsed.primary_applicant.kyc is not None
    assert parsed.co_applicant.kyc is not None


def test_profile_matching_is_unaffected(client):
    """
    A separate layer. KYC asks whether the documents agree with each
    other; profile matching asks whether they match what was declared.
    """
    body = post(
        client,
        [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="KYC-PROFILE",
        expected_types="PAN", applicant_pan="NUHPS4875K",
    )
    section = body["primary_applicant"]

    assert section["profile_match"]["party_id"] == "APP-1"
    pan = next(f for f in section["profile_match"]["fields"]
               if f["field"] == "PAN_NUMBER")
    assert pan["status"] == "PASS"


def test_no_internals_leak_through_the_party_kyc(joint):
    import json

    blob = json.dumps({s: joint[s]["kyc"]
                       for s in ("primary_applicant", "co_applicant")})

    assert "samples/" not in blob
    for forbidden in ("confidence_factors", "bbox", "raw_text", "prompt",
                      "file_path", "Traceback", "ran"):
        assert forbidden not in blob
