"""
Each party is processed on their own terms.

THE DEFECT THIS CLOSES. `files` was a required form field, so a request
carrying ONLY `co_applicant_files` was rejected at the door with 400
NO_DOCUMENTS. A co-applicant's documents could not be assessed until the
primary applicant supplied theirs -- one party's readiness gating the
other's, before any processing began.

THE SECOND DEFECT, found while testing the first. A party with no
documents reported `status: REVIEW`, because the roll-up over an empty
document list correctly finds that nothing was verified. True, and
completely misleading: a reviewer reading REVIEW beside an empty
`document_ids` concludes that party was assessed and found wanting, when
nobody has sent anything to assess. Absent input is now NOT_PROVIDED.

WHAT DID NOT CHANGE. The flow was already party-independent underneath:
KYC runs once per party over `parties.owned_by(...)`, and the two never
meet. Both fixes are at the edges -- what the API accepts, and what the
response calls an absent party.
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

#: Each run is real OCR; several tests assert on the same one.
_RUNS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "independence.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read', 'los.write'])}"})
    return c


def upload(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "application/octet-stream"))


def post(client, files, **form):
    return client.post("/api/v1/los/process", data=form, files=files)


def run(client, key, files, **form):
    if key not in _RUNS:
        response = post(client, files, **form)
        assert response.status_code == 200, response.text
        _RUNS[key] = response.json()
    return _RUNS[key]


@pytest.fixture
def primary_only(client):
    """1, 7. The primary applicant alone."""
    return run(client, "primary_only",
               [upload("files", "pan.jpg", PRIMARY_PAN),
                upload("files", "dl.jpg", PRIMARY_DL)],
               operation="PROCESS", applicant_id="APP-1",
               case_id="IND-PRIMARY", expected_types="PAN,DRIVING_LICENCE")


@pytest.fixture
def co_only(client):
    """2, 6. The co-applicant alone -- NO primary documents at all."""
    return run(client, "co_only",
               [upload("co_applicant_files", "copan.jpg", CO_PAN)],
               operation="PROCESS", applicant_id="APP-1",
               co_applicant_id="COAPP-9", case_id="IND-CO",
               co_applicant_expected_types="PAN")


@pytest.fixture
def both(client):
    """3. Both parties present."""
    return run(client, "both",
               [upload("files", "pan.jpg", PRIMARY_PAN),
                upload("files", "dl.jpg", PRIMARY_DL),
                upload("co_applicant_files", "copan.jpg", CO_PAN)],
               operation="PROCESS", applicant_id="APP-1",
               co_applicant_id="COAPP-9", case_id="IND-BOTH",
               expected_types="PAN,DRIVING_LICENCE",
               co_applicant_expected_types="PAN")


def ids(section) -> list[str]:
    return list(section.get("document_ids") or [])


def cited(kyc) -> set[str]:
    return {source["source_id"]
            for field in (kyc or {}).get("fields") or []
            for source in (field.get("sources") or [])}


# ==========================================================================
# 1, 7. PRIMARY ONLY
# ==========================================================================


def test_a_primary_only_application_is_processed(primary_only):
    assert primary_only["status"] in {"SUCCESS", "PARTIAL", "REVIEW"}
    assert ids(primary_only["primary_applicant"]) == ["pan.jpg", "dl.jpg"]


def test_an_absent_co_applicant_is_omitted_entirely(primary_only):
    """
    Not null, not an empty object -- absent. A null there would read as
    "there is a second party and we do not know who".
    """
    assert "co_applicant" not in primary_only


def test_a_missing_co_applicant_produces_no_error(primary_only):
    """Nobody was expected, so nothing is missing."""
    codes = {e.get("code") for e in primary_only.get("errors") or []}

    assert "NO_DOCUMENTS" not in codes
    assert not [c for c in codes if "CO_APPLICANT" in str(c)]


# ==========================================================================
# 2, 6. CO-APPLICANT ONLY -- THE CASE THAT WAS REJECTED AT THE DOOR
# ==========================================================================


def test_a_co_applicant_only_application_is_accepted(client):
    """
    THE DEFECT. This request used to be refused with 400 NO_DOCUMENTS
    because `files` was mandatory.
    """
    response = post(client,
                    [upload("co_applicant_files", "copan.jpg", CO_PAN)],
                    operation="PROCESS", applicant_id="APP-1",
                    co_applicant_id="COAPP-2", case_id="IND-CO-ACCEPT",
                    co_applicant_expected_types="PAN")

    assert response.status_code == 200, response.text


def test_the_co_applicants_documents_are_actually_processed(co_only):
    assert ids(co_only["co_applicant"]) == ["copan.jpg"]
    assert co_only["documents"], "no document was processed"
    assert co_only["documents"][0]["source_id"] == "copan.jpg"


def test_an_absent_primary_is_not_reported_as_a_verdict(co_only):
    """
    ABSENT INPUT IS NOT A VERIFICATION OUTCOME. This section used to
    say REVIEW -- a party who sent nothing, reported as one who was
    assessed and found wanting.
    """
    primary = co_only["primary_applicant"]

    assert ids(primary) == []
    assert primary["status"] == "NOT_PROVIDED"


def test_an_absent_primary_claims_no_kyc_and_no_profile_match(co_only):
    """
    Publishing `SKIPPED / INSUFFICIENT_SOURCES` would say a check ran
    and reached a verdict. None did.
    """
    primary = co_only["primary_applicant"]

    assert primary["kyc"] is None
    assert "profile_match" not in primary


def test_an_absent_primary_does_not_fail_the_application(co_only):
    """
    The overall status reflects the co-applicant's real result, not an
    invented primary failure.
    """
    assert co_only["status"] in {"SUCCESS", "PARTIAL", "REVIEW"}
    assert co_only["status"] != "FAILED"


def test_neither_party_missing_is_still_refused(client):
    """The requirement is that SOMEBODY sent a document."""
    response = client.post("/api/v1/los/process",
                           data={"operation": "PROCESS",
                                 "applicant_id": "APP-1",
                                 "case_id": "IND-NONE"})

    assert response.status_code in {400, 422}
    if response.status_code == 400:
        assert response.json()["detail"]["error"] == "NO_DOCUMENTS"


# ==========================================================================
# 3. BOTH PARTIES
# ==========================================================================


def test_both_parties_are_processed(both):
    assert ids(both["primary_applicant"]) == ["pan.jpg", "dl.jpg"]
    assert ids(both["co_applicant"]) == ["copan.jpg"]


def test_neither_party_is_marked_not_provided_when_both_sent_documents(both):
    for section in ("primary_applicant", "co_applicant"):
        assert both[section]["status"] != "NOT_PROVIDED"


# ==========================================================================
# 8, 9, 10. PARTY SCOPING SURVIVES ALL OF IT
# ==========================================================================


def test_primary_kyc_never_cites_a_co_applicant_document(both):
    assert cited(both["primary_applicant"].get("kyc")) <= {"pan.jpg", "dl.jpg"}


def test_co_applicant_kyc_never_cites_a_primary_document(both):
    assert cited(both["co_applicant"].get("kyc")) <= {"copan.jpg"}


def test_a_co_applicant_changes_nothing_about_the_primarys_kyc(
        primary_only, both):
    """
    THE CONTROL EXPERIMENT, and the real proof of isolation.

    ASSERTED AS A COMPARISON, NOT AS CLEANLINESS. An earlier version of
    this test asserted the primary reports no identity mismatch -- and
    failed, because `rpan.jpg` and `dl1.jpg` genuinely disagree on name
    and date of birth. That is a WITHIN-party finding about the sample
    documents, correctly reported, and has nothing to do with the
    co-applicant. Asserting on it would have pinned an accident of the
    fixtures rather than the property under test.

    The property is that adding a second party changes nothing. If one
    field of the co-applicant's leaked into the primary's comparison,
    these two would differ.
    """
    # WHERE THE PRIMARY'S KYC LIVES DIFFERS BY SHAPE, deliberately. On a
    # single-applicant case it is the case's KYC and sits at top level;
    # a party section repeats it only when there is a second party to
    # tell it apart from. Same result, published once either way.
    alone = primary_only["kyc"]
    joint = both["primary_applicant"]["kyc"]

    assert set(alone.get("reason_codes") or []) == set(
        joint.get("reason_codes") or [])

    assert alone.get("overall_score") == joint.get("overall_score")


def test_each_partys_findings_come_only_from_their_own_documents(both):
    """
    Whatever either party's verdict is, it was reached WITHIN that
    party's own uploads. Two people on one application legitimately
    differ from each other, and nothing here compares them.
    """
    assert cited(both["primary_applicant"].get("kyc")) <= {"pan.jpg", "dl.jpg"}
    assert cited(both["co_applicant"].get("kyc")) <= {"copan.jpg"}


def test_every_cross_document_check_stays_with_its_own_party(both):
    owned = {"APP-1": set(ids(both["primary_applicant"])),
             "COAPP-9": set(ids(both["co_applicant"]))}

    for check in both["cross_document"]["checks"]:
        party = check.get("party_id")
        assert party in owned, f"unattributed check: {check}"
        assert set(check.get("sources") or []) <= owned[party], check


def test_the_cross_document_section_is_not_blanked_by_an_absent_party(co_only):
    """
    One party being absent must not silence the other party's checks.
    """
    published = co_only["cross_document"]

    assert published["checks"], "an absent primary silenced the co-applicant"

    # NO `party_id` STAMP HERE, AND THAT IS CORRECT. Only one party
    # actually ran, so `_case_kyc` returns that party's result untouched
    # -- the byte-for-byte single-applicant contract. The stamp appears
    # only when two parties' rows share one list and have to be told
    # apart. What must hold either way is the scoping:
    for check in published["checks"]:
        assert set(check.get("sources") or []) <= {"copan.jpg"}, check


# ==========================================================================
# 11, 12. WHAT MUST NOT HAVE MOVED
# ==========================================================================


def test_a_primary_only_case_looks_exactly_like_a_single_applicant_case(
        primary_only):
    """
    12. The existing single-applicant contract: a primary section with
    a real status, no co-applicant key, and the documents published
    once.
    """
    assert primary_only["primary_applicant"]["status"] != "NOT_PROVIDED"
    assert "co_applicant" not in primary_only
    assert len(primary_only["documents"]) == 2


def test_adding_a_co_applicant_does_not_change_the_primarys_documents(
        primary_only, both):
    """
    11. THE CONTROL. The same primary uploads, run alone and run
    alongside a second party, are processed identically.
    """
    alone = {d["source_id"]: d["verification"]
             for d in primary_only["documents"]}
    joint = {d["source_id"]: d["verification"]
             for d in both["documents"] if d["source_id"] in alone}

    assert alone == joint


def test_the_request_contract_still_carries_every_documented_field(client):
    """9. No field was removed to make the parties independent."""
    import main

    body = main.app.openapi()["paths"]["/api/v1/los/process"]["post"][
        "requestBody"]["content"]["multipart/form-data"]["schema"]
    ref = body.get("$ref", "")
    schema = (main.app.openapi()["components"]["schemas"][ref.split("/")[-1]]
              if ref else body)

    for field in ("files", "operation", "applicant_id", "case_id",
                  "expected_types", "co_applicant_files",
                  "co_applicant_expected_types", "co_applicant_id"):
        assert field in schema["properties"], field


def test_co_applicant_files_still_need_an_owner(client):
    """
    The one dependency that MUST remain: a document with no owner
    cannot be filed against a case.
    """
    response = post(client,
                    [upload("co_applicant_files", "copan.jpg", CO_PAN)],
                    operation="PROCESS", applicant_id="APP-1",
                    case_id="IND-NO-OWNER",
                    co_applicant_expected_types="PAN")

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "CO_APPLICANT_ID_REQUIRED"


# ==========================================================================
# THE CLEAN RESPONSE CONTRACT
#
# Four scenarios, end to end through the real endpoint -- the same four
# a reviewer would try in Swagger. `overall` and the party `verification`
# / `kyc` objects are ADDITIVE: every legacy key is still there, so an
# existing consumer sees no change.
# ==========================================================================


OVERALL_KEYS = {"status", "decision", "next_action", "issues", "missing",
                "summary", "processing_summary"}

LEGACY_KEYS = {"request_id", "applicant_id", "case_id", "status",
               "documents", "cross_document", "decision", "next_action",
               "summary", "summary_source", "processing_ms", "errors"}


def test_a_the_primary_only_response_is_clean(primary_only):
    """A. PRIMARY ONLY."""
    assert set(primary_only["overall"]) == OVERALL_KEYS
    assert primary_only["overall"]["status"] == primary_only["status"]
    assert "co_applicant" not in primary_only


def test_b_the_co_applicant_only_response_is_clean(co_only):
    """B. CO-APPLICANT ONLY."""
    assert set(co_only["overall"]) == OVERALL_KEYS

    primary = co_only["primary_applicant"]
    assert primary["status"] == "NOT_PROVIDED"
    assert primary["verification"] is None
    assert primary["kyc"] is None
    assert primary["document_ids"] == []


def test_c_the_both_party_response_is_clean(both):
    """C. BOTH."""
    for key in ("primary_applicant", "co_applicant"):
        section = both[key]
        assert section["verification"] is not None
        assert set(section["verification"]) == {"status", "issues"}
        if section.get("kyc") is not None:
            # ADDITIVE: the readable `issues` sit beside everything the
            # section already published.
            assert {"status", "reason_codes", "issues"} <= set(section["kyc"])


def test_d_a_co_applicant_review_is_attributed_to_the_co_applicant(both):
    """
    D. BOTH, WITH A CO-APPLICANT KYC FINDING.

    Every issue in `overall` names the party it came from, and a
    co-applicant's finding is never filed under the primary.
    """
    for issue in both["overall"]["issues"]:
        assert issue["party"] in {"PRIMARY_APPLICANT", "CO_APPLICANT"}
        assert issue["code"]

    co_codes = {i["code"] for i in both["overall"]["issues"]
                if i["party"] == "CO_APPLICANT"}
    section_codes = {i["code"]
                     for i in (both["co_applicant"].get("kyc") or {}).get(
                         "issues", [])}

    assert section_codes <= co_codes


# -- what must not have moved --------------------------------------------


def test_every_legacy_top_level_key_survives(both):
    """11. An existing consumer reads the same keys it always did."""
    assert LEGACY_KEYS <= set(both)


def test_the_overall_verdicts_repeat_the_top_level_ones(both):
    """
    `overall` is a regrouping, not a second opinion. If these ever
    disagreed, a client would have two answers to one question.
    """
    assert both["overall"]["status"] == both["status"]
    assert both["overall"]["decision"] == both["decision"]
    assert both["overall"]["next_action"] == both["next_action"]


def test_the_overall_summary_is_deterministic_and_its_own(both):
    """
    Separate from the top-level `summary`, which a model may write.
    This one is assembled from the party statuses.
    """
    assert both["overall"]["summary"]
    assert both["overall"]["summary"].startswith("Primary applicant")


def test_no_missing_items_are_invented(primary_only, co_only, both):
    """
    This endpoint has no checklist. An invented gap sends a reviewer
    chasing a document nobody asked for.
    """
    for response in (primary_only, co_only, both):
        assert response["overall"]["missing"] == []


def test_the_absent_party_is_not_blamed_in_the_summary(co_only):
    summary = co_only["overall"]["summary"].lower()

    assert "not provided" in summary
    for word in ("fail", "reject"):
        assert word not in summary


def test_no_internal_detail_reaches_the_clean_response(both):
    import json

    blob = json.dumps(both)

    for forbidden in ("Traceback", "content_hash", "bounding", "prompt",
                      "C:\\\\", "/tmp/", "confidence_factors", "payload"):
        assert forbidden not in blob, forbidden


def test_the_documented_schema_matches_the_live_overall(both):
    """12. OpenAPI."""
    from app.agents.los.schemas import ApplicationOverview

    model = ApplicationOverview.model_validate(both["overall"])

    assert model.status == both["status"]
    assert model.processing_summary.documents_received == len(both["documents"])


def test_swagger_publishes_the_new_objects():
    """13. A reviewer can see the shape before calling it."""
    import main

    schemas = main.app.openapi()["components"]["schemas"]

    # No `PartyKyc`: the party's KYC stays the documented `KycSummary`
    # with `issues` added, rather than a second, narrower model that
    # would have dropped `fields` and `reason_codes`.
    for name in ("ApplicationOverview", "PartyVerification", "PartyIssue",
                 "ProcessingSummary"):
        assert name in schemas, name

    assert "issues" in schemas["KycSummary"]["properties"]

    assert "overall" in schemas["LosProcessResponse"]["properties"]
    assert "verification" in schemas["PartySection"]["properties"]


# ==========================================================================
# WHAT SWAGGER ACTUALLY SENDS
#
# Swagger UI submits a file input the user never touched as an EMPTY
# STRING part rather than omitting it. An optional `list[UploadFile]`
# field was handed `""` and FastAPI rejected the whole request with
# 422 "Expected UploadFile, received: <class 'str'>" -- so a
# primary-only application was unsubmittable from the very UI the team
# tests with, and once `files` became optional, so was a
# co-applicant-only one.
# ==========================================================================


def test_an_untouched_co_applicant_file_input_does_not_break_the_request(
        client):
    """The exact body Swagger sends for PRIMARY ONLY."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "SWAG-PRIMARY", "expected_types": "PAN",
              # every co-applicant field left blank in the form
              "co_applicant_files": "", "co_applicant_expected_types": "",
              "co_applicant_id": ""},
        files=[upload("files", "pan.jpg", PRIMARY_PAN)])

    assert response.status_code == 200, response.text
    assert "co_applicant" not in response.json()


def test_an_untouched_primary_file_input_does_not_break_the_request(client):
    """The same body Swagger sends for CO-APPLICANT ONLY."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "SWAG-CO", "co_applicant_id": "COAPP-9",
              "files": "", "expected_types": "",
              "co_applicant_expected_types": "PAN"},
        files=[upload("co_applicant_files", "copan.jpg", CO_PAN)])

    assert response.status_code == 200, response.text
    assert response.json()["primary_applicant"]["status"] == "NOT_PROVIDED"


def test_omitting_the_co_applicant_fields_entirely_still_works(client):
    response = post(client, [upload("files", "pan.jpg", PRIMARY_PAN)],
                    operation="PROCESS", applicant_id="APP-1",
                    case_id="SWAG-OMIT", expected_types="PAN")

    assert response.status_code == 200, response.text


def test_a_real_co_applicant_upload_is_unaffected(client):
    response = post(client,
                    [upload("files", "pan.jpg", PRIMARY_PAN),
                     upload("co_applicant_files", "copan.jpg", CO_PAN)],
                    operation="PROCESS", applicant_id="APP-1",
                    co_applicant_id="COAPP-9", case_id="SWAG-REAL",
                    expected_types="PAN", co_applicant_expected_types="PAN")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert body["co_applicant"]["document_ids"] == ["copan.jpg"]


def test_two_blank_file_fields_still_report_no_documents(client):
    """The NO_DOCUMENTS rule is unchanged: somebody must send something."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "SWAG-NEITHER", "files": "",
              "co_applicant_files": ""})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "NO_DOCUMENTS"


def test_text_in_a_file_field_is_refused_rather_than_ignored(client):
    """
    VALIDATION IS NOT WEAKENED. An empty part means the field was left
    blank. A non-empty string is a caller sending something that is not
    a file, and silently dropping it would let them believe they had
    uploaded a document that never existed.
    """
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "SWAG-JUNK", "files": "not-a-file"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "INVALID_FILE_FIELD"


def test_swagger_documents_both_file_fields_as_uploads(client):
    """
    THE TOLERANCE MUST NOT REACH THE CONTRACT. The endpoint accepts
    `UploadFile | str` at runtime; left alone that publishes
    `anyOf: [binary, string]`, and Swagger UI renders a TEXT BOX -- so
    the fix for the upload widget would have removed the upload widget.
    """
    import main

    schema = main.app.openapi()["paths"]["/api/v1/los/process"]["post"][
        "requestBody"]["content"]["multipart/form-data"]["schema"]
    ref = schema.get("$ref", "")
    body = (main.app.openapi()["components"]["schemas"][ref.split("/")[-1]]
            if ref else schema)

    for field in ("files", "co_applicant_files"):
        published = body["properties"][field]
        array = [o for o in published["anyOf"] if o.get("type") == "array"][0]

        assert array["items"]["format"] == "binary"
        assert "anyOf" not in array["items"], field

    assert not body.get("required")


# ==========================================================================
# EVERY SHAPE A BROWSER SENDS FOR AN UNTOUCHED FILE INPUT
#
# There is more than one, and only some are strings. A browser may send
# the field as a plain empty form value, or as an empty FILE part whose
# filename is blank. All of them mean the same thing -- the user
# selected nothing -- and none may reject the request.
#
# WHAT MUST STILL BE REFUSED is a non-empty text value. Swagger renders
# this field as a text box when it is serving a spec built before
# `files` became optional, and its placeholder is the literal word
# `string`; accepting that would file a document nobody uploaded.
# ==========================================================================


def blank(client, case_id, **extra):
    return client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": case_id, "expected_types": "PAN", **extra},
        files=[upload("files", "pan.jpg", PRIMARY_PAN)])


def test_a_co_applicant_file_field_sent_as_an_empty_string_is_absent(client):
    """B. The plain empty form value."""
    response = blank(client, "BLANK-STR", co_applicant_files="")

    assert response.status_code == 200, response.text
    assert "co_applicant" not in response.json()


def test_a_co_applicant_file_field_sent_as_an_empty_part_is_absent(client):
    """
    B, the other encoding. An empty FILE part with a blank filename --
    what a browser sends for a file input the user never touched.
    """
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "BLANK-PART", "expected_types": "PAN"},
        files=[upload("files", "pan.jpg", PRIMARY_PAN),
               ("co_applicant_files", ("", b"", "application/octet-stream"))])

    assert response.status_code == 200, response.text
    assert "co_applicant" not in response.json()


def test_every_blank_co_applicant_field_together_is_still_primary_only(client):
    """A. The whole co-applicant half of the form, left untouched."""
    response = blank(client, "BLANK-ALL", co_applicant_files="",
                     co_applicant_expected_types="", co_applicant_id="")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "co_applicant" not in body
    assert body["primary_applicant"]["document_ids"] == ["pan.jpg"]


@pytest.mark.parametrize("junk", ["fake.pdf", "string", "undefined", "null"])
def test_text_in_the_co_applicant_file_field_is_refused(client, junk):
    """
    D. NOT ACCEPTED, however plausible it looks. `fake.pdf` names a
    document that was never uploaded, and `string` is Swagger's own
    placeholder -- filing either would record evidence nobody sent.
    """
    response = blank(client, f"JUNK-{junk}", co_applicant_files=junk)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "INVALID_FILE_FIELD"
    assert junk in detail["message"]


def test_the_refusal_explains_the_swagger_text_box(client):
    """
    A reader seeing this went looking for a bug in their upload. The
    real cause is a cached OpenAPI document, and the message says so.
    """
    detail = blank(client, "JUNK-HINT",
                   co_applicant_files="string").json()["detail"]

    assert "text box" in detail["message"]
    assert "restart" in detail["message"]


def test_a_real_co_applicant_upload_is_still_validated(client):
    """C. The genuine case is untouched."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "co_applicant_id": "COAPP-9", "case_id": "BLANK-REAL",
              "expected_types": "PAN", "co_applicant_expected_types": "PAN"},
        files=[upload("files", "pan.jpg", PRIMARY_PAN),
               upload("co_applicant_files", "copan.jpg", CO_PAN)])

    assert response.status_code == 200, response.text
    assert response.json()["co_applicant"]["document_ids"] == ["copan.jpg"]


def test_blank_everywhere_is_still_no_documents(client):
    """E. Unchanged."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "case_id": "BLANK-NONE", "files": "",
              "co_applicant_files": ""})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "NO_DOCUMENTS"


def test_the_primary_file_field_behaves_identically(client):
    """6. Primary validation was not changed -- it was made consistent."""
    response = client.post(
        "/api/v1/los/process",
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "co_applicant_id": "COAPP-9", "case_id": "BLANK-PRIMARY",
              "files": "", "co_applicant_expected_types": "PAN"},
        files=[upload("co_applicant_files", "copan.jpg", CO_PAN)])

    assert response.status_code == 200, response.text
