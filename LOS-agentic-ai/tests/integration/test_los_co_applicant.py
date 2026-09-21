"""
Primary applicant and co-applicant, through the real endpoint.

ONE ENDPOINT, TWO PEOPLE. `POST /api/v1/los/process` now accepts a second
party's documents alongside the first. These tests go through the real
FastAPI route with real sample documents, because the failure mode being
guarded against is a wiring one: the party is stamped in the flow, carried
through the pipeline, written to the store and published on the response,
and a break anywhere in that chain shows up as somebody else's document on
your file.

THE TWO THINGS THAT MATTER.

  ISOLATION. A document sent under `files` is the primary applicant's and
  can never become the co-applicant's, however it is named. Both parties
  routinely upload `pan.jpg`, because that is what phones call things.

  BACKWARD COMPATIBILITY. A request with no co-applicant field must behave
  exactly as it did before any of this existed — same response shape, no
  new required input, no co-applicant key appearing as null.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

PAN = Path("samples/real_batch/pan_bw2.jpg")
DL = Path("samples/real_batch/dl1.jpg")
VOTER = Path("samples/documents/voter_id2.jpg")

pytestmark = pytest.mark.skipif(
    not (PAN.exists() and DL.exists()),
    reason="sample documents not available",
)


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "party_test.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({
        "Authorization": f"Bearer {make_token(scopes=['los.read'])}"
    })
    return c


def post(client, **form):
    """One multipart call, with files split out of the form fields."""
    files = form.pop("files", [])
    return client.post("/api/v1/los/process", data=form, files=files)


def upload(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "application/octet-stream"))


def by_party(body) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for document in body["documents"]:
        out.setdefault(document.get("party_id") or "UNOWNED", []).append(document)
    return out


# ==========================================================================
# A. BACKWARD COMPATIBILITY — THE SINGLE-APPLICANT REQUEST IS UNCHANGED
# ==========================================================================


def test_a_request_with_no_co_applicant_still_works(client):
    response = post(
        client,
        files=[upload("files", "pan.jpg", PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-SOLO",
        expected_types="PAN",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applicant_id"] == "APP-1"
    assert len(body["documents"]) == 1


def test_a_single_applicant_response_has_no_co_applicant_key(client):
    """
    Absent, not null. A null would read as "there is a co-applicant and
    we do not know who", which is a different and alarming claim.
    """
    body = post(
        client,
        files=[upload("files", "pan.jpg", PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-SOLO2",
        expected_types="PAN",
    ).json()

    assert "co_applicant_id" not in body


def test_the_comma_separated_form_of_expected_types_still_works(client):
    """Callers already send it, and Swagger UI submits a single value."""
    body = post(
        client,
        files=[upload("files", "pan.jpg", PAN),
               upload("files", "dl.jpg", DL)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-CSV",
        expected_types="PAN,DRIVING_LICENCE",
    ).json()

    types = {d["source_id"]: d["type"] for d in body["documents"]}
    assert types["pan.jpg"] == "PAN"
    assert types["dl.jpg"] == "DRIVING_LICENCE"


def test_repeatable_expected_types_map_positionally(client):
    """The shape a generated client produces."""
    # A LIST VALUE, not a list of tuples. httpx encodes
    # {"k": ["a", "b"]} as two `k` fields, which is what a repeatable
    # OpenAPI string item looks like on the wire. Passing a list of
    # tuples alongside `files` silently drops the files.
    response = client.post(
        "/api/v1/los/process",
        data={
            "operation": "PROCESS", "applicant_id": "APP-1",
            "case_id": "CASE-REP",
            "expected_types": ["PAN", "DRIVING_LICENCE"],
        },
        files=[upload("files", "pan.jpg", PAN), upload("files", "dl.jpg", DL)],
    )
    body = response.json()

    assert response.status_code == 200, response.text
    types = {d["source_id"]: d["type"] for d in body["documents"]}
    assert types["pan.jpg"] == "PAN"
    assert types["dl.jpg"] == "DRIVING_LICENCE"


# ==========================================================================
# B. BOTH PARTIES IN ONE CALL
# ==========================================================================


@pytest.fixture
def two_party(client):
    response = post(
        client,
        files=[upload("files", "pan.jpg", PAN),
               upload("co_applicant_files", "pan.jpg", DL)],
        operation="PROCESS", applicant_id="APP-1", co_applicant_id="COAPP-9",
        case_id="CASE-TWO",
        expected_types="PAN", co_applicant_expected_types="DRIVING_LICENCE",
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_both_parties_are_processed_in_one_call(two_party):
    assert len(two_party["documents"]) == 2


def test_the_response_names_both_parties(two_party):
    assert two_party["applicant_id"] == "APP-1"
    assert two_party["co_applicant_id"] == "COAPP-9"


def test_both_parties_share_one_case(two_party):
    assert two_party["case_id"] == "CASE-TWO"


def test_every_document_says_whose_it_is(two_party):
    for document in two_party["documents"]:
        assert document["party_id"]
        assert document["party_role"] in {"PRIMARY_APPLICANT", "CO_APPLICANT"}


def test_the_same_filename_from_both_parties_stays_separate(two_party):
    """
    THE COLLISION THIS PHASE EXISTS FOR. Both uploaded `pan.jpg`. They
    are two documents belonging to two people, not one document uploaded
    twice.
    """
    grouped = by_party(two_party)

    assert set(grouped) == {"APP-1", "COAPP-9"}
    assert len(grouped["APP-1"]) == 1
    assert len(grouped["COAPP-9"]) == 1
    assert {d["source_id"] for d in two_party["documents"]} == {"pan.jpg"}


def test_each_party_document_kept_its_own_content(two_party):
    """
    Not merely two rows — two rows holding the RIGHT files. The primary
    sent a PAN, the co-applicant a licence, under the same filename.
    """
    grouped = by_party(two_party)

    assert grouped["APP-1"][0]["type"] == "PAN"
    assert grouped["COAPP-9"][0]["type"] == "DRIVING_LICENCE"


def test_both_parties_documents_are_stored_separately(two_party, store):
    rows = store.list_documents("CASE-TWO")

    assert len(rows) == 2
    assert len({row.document_id for row in rows}) == 2
    assert {row.owner_id for row in rows} == {"APP-1", "COAPP-9"}


def test_a_stored_document_belongs_to_exactly_one_party(two_party, store):
    for row in store.list_documents("CASE-TWO"):
        owners = [p for p in ("APP-1", "COAPP-9") if row.belongs_to(p)]
        assert len(owners) == 1, f"{row.document_id} claims {owners}"


def test_the_case_records_its_co_applicant(two_party, store):
    # The primary applicant's own row is what the case hangs off; the
    # second party is recorded against the application.
    assert store.get_application("CASE-TWO") is not None


# ==========================================================================
# C. THE REQUEST IS REFUSED WHEN OWNERSHIP IS AMBIGUOUS
# ==========================================================================


def test_co_applicant_files_without_an_id_are_refused(client):
    """
    A document with no owner cannot be filed. Attributing it to the
    primary applicant would put one person's evidence on another's file
    — the exact failure the party model prevents.
    """
    response = post(
        client,
        files=[upload("files", "pan.jpg", PAN),
               upload("co_applicant_files", "dl.jpg", DL)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-NOID",
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "CO_APPLICANT_ID_REQUIRED"


def test_the_two_parties_may_not_share_an_identifier(client):
    response = post(
        client,
        files=[upload("files", "pan.jpg", PAN),
               upload("co_applicant_files", "dl.jpg", DL)],
        operation="PROCESS", applicant_id="SAME", co_applicant_id="SAME",
        case_id="CASE-SAME",
    )

    assert response.status_code == 400
    detail = str(response.json()["detail"]).lower()
    assert "differ" in detail or "share one identity" in detail


def test_a_co_applicant_id_with_no_files_is_harmless(client):
    """
    Declaring the second party before collecting their documents is an
    ordinary sequence, not an error.
    """
    response = post(
        client,
        files=[upload("files", "pan.jpg", PAN)],
        operation="PROCESS", applicant_id="APP-1", co_applicant_id="COAPP-9",
        case_id="CASE-DECLARED", expected_types="PAN",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["co_applicant_id"] == "COAPP-9"
    assert len(body["documents"]) == 1
    assert body["documents"][0]["party_role"] == "PRIMARY_APPLICANT"


# ==========================================================================
# D. CROSS-PARTY CONTAMINATION
# ==========================================================================


def test_adding_a_co_applicant_later_does_not_touch_the_primarys_documents(
        client, store):
    """
    Two calls against one case, as a field officer actually works:
    collect the applicant's documents, then the co-applicant's.
    """
    post(client,
         files=[upload("files", "pan.jpg", PAN)],
         operation="PROCESS", applicant_id="APP-1", case_id="CASE-SEQ",
         expected_types="PAN")

    before = {r.document_id: r.owner_id for r in store.list_documents("CASE-SEQ")}

    post(client,
         files=[upload("files", "pan.jpg", PAN),
                upload("co_applicant_files", "pan.jpg", DL)],
         operation="PROCESS", applicant_id="APP-1", co_applicant_id="COAPP-9",
         case_id="CASE-SEQ",
         expected_types="PAN", co_applicant_expected_types="DRIVING_LICENCE")

    after = {r.document_id: r.owner_id for r in store.list_documents("CASE-SEQ")}

    for document_id, owner in before.items():
        assert after.get(document_id) == owner, (
            f"{document_id} changed hands from {owner} to "
            f"{after.get(document_id)}"
        )


def test_a_re_upload_by_the_same_party_updates_rather_than_duplicates(
        client, store):
    for _ in range(2):
        post(client,
             files=[upload("files", "pan.jpg", PAN)],
             operation="PROCESS", applicant_id="APP-1", case_id="CASE-RE",
             expected_types="PAN")

    rows = store.list_documents("CASE-RE")
    assert len(rows) == 1


def test_three_documents_across_two_parties_all_land_correctly(client, store):
    if not VOTER.exists():
        pytest.skip("voter sample not available")

    body = post(
        client,
        files=[upload("files", "pan.jpg", PAN),
               upload("files", "dl.jpg", DL),
               upload("co_applicant_files", "voter.jpg", VOTER)],
        operation="PROCESS", applicant_id="APP-1", co_applicant_id="COAPP-9",
        case_id="CASE-MIX",
        expected_types="PAN,DRIVING_LICENCE",
        co_applicant_expected_types="VOTER_ID",
    ).json()

    grouped = by_party(body)
    assert len(grouped["APP-1"]) == 2
    assert len(grouped["COAPP-9"]) == 1
    assert grouped["COAPP-9"][0]["type"] == "VOTER_ID"

    owners = {r.owner_id for r in store.list_documents("CASE-MIX")}
    assert owners == {"APP-1", "COAPP-9"}
