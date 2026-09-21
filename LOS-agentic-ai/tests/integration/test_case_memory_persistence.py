"""
Case memory, written from a real request.

THE TWO PROPERTIES THAT MATTER, and most of this file is about them.

  IT CHANGES NOTHING. Persistence runs after the response is built,
  behind a flag that is off by default, inside a handler that cannot
  propagate. Turning it on must not alter one byte of what the caller
  receives, and a failure inside it must not cost them their answer.

  IT OBEYS THE GATE. A document whose fields verification withheld is
  recorded as having been withheld -- its verdict is kept, its fields
  are not. Writing unreleased extraction here would make the database a
  way around the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.models import FindingKind
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/los/process"

PRIMARY_PAN = Path("samples/documents/rpan.jpg")
CO_PAN = Path("samples/documents/lPan.jpg")

pytestmark = pytest.mark.skipif(
    not (PRIMARY_PAN.exists() and CO_PAN.exists()),
    reason="sample documents not available",
)


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "memory.sqlite3")
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


def two_party(client, case_id: str):
    return call(
        client,
        [up("files", "pan.jpg", PRIMARY_PAN),
         up("co_applicant_files", "pan.jpg", CO_PAN)],
        operation="PROCESS", applicant_id="MEM-APP",
        co_applicant_id="MEM-CO", case_id=case_id,
        expected_types="PAN", co_applicant_expected_types="PAN")


def enabled():
    return patch("app.agents.los.config.case_memory_enabled",
                 return_value=True)


# ==========================================================================
# 17. DISABLED — THE DEFAULT
# ==========================================================================


def test_the_flag_is_off_by_default():
    from app.agents.los.config import case_memory_enabled

    assert case_memory_enabled() is False


def test_nothing_is_written_when_disabled(client, repo):
    body = call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
                operation="PROCESS", applicant_id="MEM-APP",
                case_id="MEM-OFF", expected_types="PAN")

    assert body["status"]
    assert repo.get_case_findings("MEM-OFF") == []
    assert repo.get_case_decisions("MEM-OFF") == []
    assert repo.get_case_timeline("MEM-OFF") == []


def test_the_response_is_identical_either_way(client, repo):
    """
    THE WHOLE POINT. The same request, with persistence off and on,
    must produce the same answer.
    """
    volatile = {"request_id", "case_id", "processing_ms"}

    off = call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
               operation="PROCESS", applicant_id="MEM-APP",
               case_id="MEM-A", expected_types="PAN")

    with enabled():
        on = call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
                  operation="PROCESS", applicant_id="MEM-APP",
                  case_id="MEM-B", expected_types="PAN")

    assert {k: v for k, v in off.items() if k not in volatile} == \
           {k: v for k, v in on.items() if k not in volatile}


# ==========================================================================
# 18. ENABLED — FINDINGS OUTLIVE THE REQUEST
# ==========================================================================


@pytest.fixture
def written(client, repo):
    with enabled():
        body = two_party(client, "MEM-TWO")
    return body, repo


def test_findings_survive_the_request(written):
    _, repo = written

    assert repo.get_case_findings("MEM-TWO")


def test_verification_is_recorded_per_document(written):
    _, repo = written
    verifications = repo.get_case_findings("MEM-TWO",
                                           kind=FindingKind.VERIFICATION)

    assert len(verifications) == 2
    assert {f.source_id for f in verifications} == {"pan.jpg"}
    assert {f.party_id for f in verifications} == {"MEM-APP", "MEM-CO"}


def test_kyc_is_recorded_per_party(written):
    _, repo = written
    kyc = repo.get_case_findings("MEM-TWO", kind=FindingKind.KYC)

    assert {f.party_id for f in kyc} == {"MEM-APP", "MEM-CO"}
    for row in kyc:
        assert row.status


def test_the_decision_is_recorded(written):
    body, repo = written
    decisions = repo.get_case_decisions("MEM-TWO")

    assert len(decisions) == 1
    assert decisions[0].decision == body["decision"]
    assert decisions[0].next_action == body["next_action"]
    assert decisions[0].status == body["status"]


def test_the_case_is_on_the_timeline(written):
    _, repo = written
    timeline = repo.get_case_timeline("MEM-TWO")

    assert [e.event_type for e in timeline] == ["LOS_PROCESSED"]


def test_document_versions_are_recorded(written):
    _, repo = written
    findings = repo.get_case_findings("MEM-TWO",
                                      kind=FindingKind.VERIFICATION)

    for row in findings:
        assert repo.get_document_versions(row.document_id)


# ==========================================================================
# 16. THE GATE IS NOT WORKED AROUND
# ==========================================================================


def test_a_failed_document_records_its_verdict_but_not_its_fields(client, repo):
    """
    THE ONE THAT MATTERS. A PAN declared as a licence fails and releases
    nothing. The failure is worth recording; the withheld fields are
    not, and writing them here would make the database a way past the
    gate.
    """
    with enabled():
        call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
             operation="PROCESS", applicant_id="MEM-APP",
             case_id="MEM-FAIL", expected_types="DRIVING_LICENCE")

    verifications = repo.get_case_findings("MEM-FAIL",
                                           kind=FindingKind.VERIFICATION)
    extractions = repo.get_case_findings("MEM-FAIL",
                                         kind=FindingKind.EXTRACTION)

    assert len(verifications) == 1
    assert verifications[0].status == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in verifications[0].reason_codes
    assert extractions == []


def test_a_passing_document_does_record_its_released_fields(client, repo):
    with enabled():
        call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
             operation="PROCESS", applicant_id="MEM-APP",
             case_id="MEM-PASS", expected_types="PAN")

    extractions = repo.get_case_findings("MEM-PASS",
                                         kind=FindingKind.EXTRACTION)

    assert len(extractions) == 1
    assert extractions[0].payload.get("pan_number")


# ==========================================================================
# 14-15. ISOLATION, THROUGH THE REAL PIPELINE
# ==========================================================================


def test_each_partys_findings_carry_their_own_party(written):
    _, repo = written

    for party in ("MEM-APP", "MEM-CO"):
        rows = repo.get_case_findings("MEM-TWO", party_id=party)
        assert rows
        assert {f.party_id for f in rows} <= {party, None}


def test_two_cases_do_not_share_findings(client, repo):
    with enabled():
        call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
             operation="PROCESS", applicant_id="MEM-APP",
             case_id="MEM-X", expected_types="PAN")
        call(client, [up("files", "pan.jpg", CO_PAN)],
             operation="PROCESS", applicant_id="MEM-APP",
             case_id="MEM-Y", expected_types="PAN")

    x = repo.get_case_findings("MEM-X")
    y = repo.get_case_findings("MEM-Y")

    assert x and y
    assert {f.case_id for f in x} == {"MEM-X"}
    assert {f.case_id for f in y} == {"MEM-Y"}


# ==========================================================================
# WHAT IS NOT STORED
# ==========================================================================


def test_no_internals_reach_the_database(written):
    _, repo = written
    blob = json.dumps([f.payload for f in repo.get_case_findings("MEM-TWO")])

    for forbidden in ("bbox", "raw_text", "ocr_text", "tokens", "prompt",
                      "Traceback", "file_path", "samples/", "Decimal(",
                      "candidates"):
        assert forbidden not in blob


def test_kyc_source_values_are_not_copied_into_the_payload(written):
    """
    A KYC row carries the values each document showed. Those are already
    in the response; duplicating them here would put extracted identity
    data in a second place for no reader.
    """
    _, repo = written

    for row in repo.get_case_findings("MEM-TWO", kind=FindingKind.KYC):
        for field in row.payload.get("fields") or []:
            assert "sources" not in field
            assert set(field) <= {"field", "status", "match_score",
                                  "confidence", "reason_code"}


# ==========================================================================
# FAIL-SOFT
# ==========================================================================


def test_a_persistence_failure_does_not_reach_the_caller(client, repo):
    """
    Observational means observational. If the write breaks, the caller
    still gets their answer.
    """
    with enabled(), patch(
        "app.store.ingest._write_case_memory",
        side_effect=RuntimeError("disk on fire"),
    ):
        body = call(client, [up("files", "pan.jpg", PRIMARY_PAN)],
                    operation="PROCESS", applicant_id="MEM-APP",
                    case_id="MEM-BOOM", expected_types="PAN")

    assert body["status"]
    assert body["decision"]
    assert repo.get_case_findings("MEM-BOOM") == []


def test_reprocessing_a_case_does_not_duplicate_findings(client, repo):
    """
    The same documents processed twice is one case, not two opinions.
    """
    with enabled():
        two_party(client, "MEM-REPEAT")
        first = len(repo.get_case_findings("MEM-REPEAT"))
        two_party(client, "MEM-REPEAT")

    assert len(repo.get_case_findings("MEM-REPEAT")) == first
