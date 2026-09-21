"""
The published contract, as OpenAPI actually generates it.

WHY THESE EXIST. The co-applicant fields were implemented, tested and
working for three phases while the Swagger a client actually opened
showed five fields. The code was right; the running process was stale,
and nothing in the suite would ever have noticed, because every test
went through the ASGI app directly and none of them read
`/openapi.json`.

So these tests read the GENERATED SPEC, not the route signature and not
the description text. A field documented only in prose is a field a
generated client cannot send.

THE OTHER HALF IS WHAT MUST NOT BE THERE. No party-level `decision` or
`next_action` — one beside a case-level `MANUAL_REVIEW` reads as
permission to proceed, and there is one decision on a loan. And nothing
internal: no OCR text, no bounding boxes, no prompts, no filesystem
paths, no stage timings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

PRIMARY_PAN = Path("samples/documents/rpan.jpg")
CO_PAN = Path("samples/documents/lPan.jpg")

ENDPOINT = "/api/v1/los/process"


@pytest.fixture(scope="module")
def spec() -> dict:
    import main

    return main.app.openapi()


@pytest.fixture(scope="module")
def request_schema(spec) -> dict:
    operation = spec["paths"][ENDPOINT]["post"]
    ref = operation["requestBody"]["content"]["multipart/form-data"][
        "schema"]["$ref"].split("/")[-1]
    return spec["components"]["schemas"][ref]


@pytest.fixture(scope="module")
def response_schema(spec) -> dict:
    operation = spec["paths"][ENDPOINT]["post"]
    ref = operation["responses"]["200"]["content"]["application/json"][
        "schema"]["$ref"].split("/")[-1]
    return spec["components"]["schemas"][ref]


def optional(schema: dict, field: str) -> bool:
    return field in schema["properties"] and field not in schema.get(
        "required", [])


# ==========================================================================
# A-E. THE REQUEST BODY
# ==========================================================================


@pytest.mark.parametrize("field", [
    "files", "expected_types", "co_applicant_id", "co_applicant_files",
    "co_applicant_expected_types",
])
def test_the_multipart_schema_declares_the_field(request_schema, field):
    """
    A, B, C. Read off the generated spec: this is what Swagger UI
    renders and what a generated client can send.
    """
    assert field in request_schema["properties"], field


def test_only_the_primary_files_are_required(request_schema):
    """D and E together. Adding a required field would break every
    existing caller."""
    assert request_schema["required"] == ["files"]


@pytest.mark.parametrize("field", [
    "co_applicant_id", "co_applicant_files", "co_applicant_expected_types",
])
def test_no_co_applicant_field_is_required(request_schema, field):
    assert optional(request_schema, field)


def test_both_file_fields_accept_several_binaries(request_schema):
    """
    Swagger UI renders a multi-file picker from `array` + `format:
    binary`. A plain string renders one text box and the co-applicant
    could not upload at all.
    """
    for field in ("files", "co_applicant_files"):
        spec = request_schema["properties"][field]
        # The optional one is `anyOf: [array, null]`.
        array = spec if spec.get("type") == "array" else next(
            branch for branch in spec.get("anyOf", [])
            if branch.get("type") == "array")
        assert array["items"]["format"] == "binary", field


def test_both_expected_type_fields_are_repeatable_arrays(request_schema):
    for field in ("expected_types", "co_applicant_expected_types"):
        spec = request_schema["properties"][field]
        types = {spec.get("type")} | {
            b.get("type") for b in spec.get("anyOf", [])}
        assert "array" in types, field


def test_the_operation_enum_is_the_real_one(request_schema):
    assert request_schema["properties"]["operation"]["enum"] == [
        "PROCESS", "EXTRACT", "VERIFY"]


def test_the_description_names_both_parties(spec):
    description = spec["paths"][ENDPOINT]["post"]["description"]

    for field in ("files", "expected_types", "co_applicant_id",
                  "co_applicant_files", "co_applicant_expected_types"):
        assert field in description, field


# ==========================================================================
# I-N. THE RESPONSE BODY
# ==========================================================================


@pytest.mark.parametrize("field", [
    "request_id", "applicant_id", "case_id", "status", "documents", "kyc",
    "cross_document", "decision", "next_action", "summary", "summary_source",
    "processing_ms", "errors",
])
def test_the_existing_top_level_contract_is_documented(response_schema, field):
    """P. Nothing renamed, nothing removed."""
    assert field in response_schema["properties"], field


def test_the_case_decision_and_action_are_documented(response_schema):
    """M and N. These are the authoritative ones."""
    assert "decision" in response_schema["required"]
    assert "next_action" in response_schema["required"]


def test_the_party_sections_are_documented(response_schema):
    """I and J."""
    assert "primary_applicant" in response_schema["properties"]
    assert optional(response_schema, "co_applicant")


def test_a_party_section_carries_the_agreed_shape(spec):
    section = spec["components"]["schemas"]["PartySection"]["properties"]

    assert set(section) == {"party_id", "role", "status", "document_ids",
                            "verification_summary", "profile_match", "kyc"}


@pytest.mark.parametrize("field", ["decision", "next_action"])
def test_a_party_section_exposes_no_decision_or_action(spec, field):
    """
    K and L. A party-level CONTINUE beside a case-level MANUAL_REVIEW
    reads as permission to proceed. There is ONE decision on a loan.
    """
    assert field not in spec["components"]["schemas"]["PartySection"][
        "properties"]


def test_only_the_party_id_and_role_are_required_of_a_section(spec):
    """
    A declared co-applicant who has uploaded nothing still has a
    section; everything measured about them may legitimately be absent.
    """
    assert set(spec["components"]["schemas"]["PartySection"]["required"]) <= {
        "party_id", "role", "verification_summary"}


def test_the_verification_summary_is_documented(spec):
    assert set(spec["components"]["schemas"]["PartyVerificationSummary"][
        "properties"]) == {"total_documents", "passed", "review", "failed",
                           "skipped"}


# ==========================================================================
# THE NESTED SCHEMAS
# ==========================================================================


def test_the_kyc_summary_is_documented(spec):
    assert set(spec["components"]["schemas"]["KycSummary"]["properties"]) == {
        "status", "reason_codes", "overall_score", "overall_confidence",
        "fields"}


def test_a_kyc_field_row_is_documented(spec):
    row = spec["components"]["schemas"]["KycFieldResult"]["properties"]

    assert {"field", "status", "match_score", "confidence", "reason_code",
            "reason", "sources"} <= set(row)
    # Phase 6: whose row this is, on a two-party case.
    assert "party_id" in row


def test_the_kyc_party_id_is_optional(spec):
    """Absent on a single-applicant response, where it would say nothing."""
    assert optional(spec["components"]["schemas"]["KycFieldResult"],
                    "party_id")


def test_the_cross_document_object_is_documented(spec):
    assert set(spec["components"]["schemas"]["CrossDocument"][
        "properties"]) == {"status", "checks"}
    assert set(spec["components"]["schemas"]["CrossDocumentCheck"][
        "properties"]) == {"check", "status", "reason_codes", "sources",
                           "details"}


def test_the_document_object_is_documented(spec):
    document = spec["components"]["schemas"]["ProcessedDocument"]["properties"]

    for field in ("source_id", "type", "status", "verification", "extraction",
                  "reason_codes", "verification_score",
                  "verification_confidence", "reasons", "has_extracted_fields",
                  "authenticity", "advisories", "specialist", "expected_type",
                  "hint", "evidence_refs", "errors"):
        assert field in document, field


def test_a_document_says_whose_it_is(spec):
    document = spec["components"]["schemas"]["ProcessedDocument"]

    assert optional(document, "party_id")
    assert optional(document, "party_role")


# ==========================================================================
# O. NOTHING INTERNAL
# ==========================================================================


#: Property names that would mean an internal detail had escaped.
FORBIDDEN = {
    "tokens", "bbox", "bounding_box", "boxes", "raw_text", "raw", "prompt",
    "prompts", "ocr_text", "candidates", "payload", "mcp", "agent_state",
    "file_path", "staged", "confidence_factors", "field_quality", "ran",
    "party_kyc", "party_status", "processing", "timings", "ocr_ms",
    "classification_ms", "specialist_ms", "mcp_ms", "orchestration_ms",
    "llm_ms", "summary_ms",
}


def reachable(spec: dict, root: str) -> dict[str, list[str]]:
    """Every schema the response can reach, and its property names."""
    schemas = spec["components"]["schemas"]
    found: dict[str, list[str]] = {}
    pending = [root]

    while pending:
        name = pending.pop()
        if name in found or name not in schemas:
            continue
        found[name] = sorted(schemas[name].get("properties", {}))
        for ref in set(re.findall(r'"#/components/schemas/([A-Za-z0-9_]+)"',
                                  json.dumps(schemas[name]))):
            pending.append(ref)

    return found


def test_no_internal_field_is_reachable_from_the_response(spec):
    leaked = {
        name: [p for p in props if p in FORBIDDEN]
        for name, props in reachable(spec, "LosProcessResponse").items()
    }

    assert not {n: v for n, v in leaked.items() if v}


def test_the_spec_names_no_filesystem_path(spec):
    blob = json.dumps(spec)

    assert "samples/" not in blob
    assert "C:\\\\" not in blob
    assert "/tmp/" not in blob


def test_the_documented_next_actions_are_all_reachable(spec, response_schema):
    """
    A documented value the code cannot emit is as misleading as an
    undocumented one it can. `REQUEST_CORRECT_DOCUMENT` was missing.
    """
    documented = set(response_schema["properties"]["next_action"]["examples"])

    assert documented == {"CONTINUE", "MANUAL_REVIEW",
                          "REQUEST_VALID_DOCUMENT",
                          "REQUEST_CORRECT_DOCUMENT"}


def test_the_documented_statuses_match_the_roll_up(spec, response_schema):
    from app.agents.los import flow

    assert set(response_schema["properties"]["status"]["examples"]) == set(
        flow._PUBLIC)


def test_the_documented_party_roles_are_the_real_ones(spec):
    from app.agents.los.parties import PartyRole

    role = spec["components"]["schemas"]["PartySection"]["properties"]["role"]

    assert set(role["examples"]) == {r.value for r in PartyRole}


# ==========================================================================
# F-H. THE CONTRACT, EXERCISED
# ==========================================================================


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "contract.sqlite3")
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


samples_present = pytest.mark.skipif(
    not (PRIMARY_PAN.exists() and CO_PAN.exists()),
    reason="sample documents not available",
)


def upload(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "image/jpeg"))


@samples_present
def test_the_documented_primary_only_request_works(client):
    """F. Exactly the fields the old Swagger showed."""
    response = client.post(ENDPOINT, files=[upload("files", "pan.jpg",
                                                   PRIMARY_PAN)],
                           data={"operation": "PROCESS",
                                 "applicant_id": "APP-1",
                                 "case_id": "CONTRACT-SOLO",
                                 "expected_types": "PAN"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "co_applicant" not in body
    assert body["primary_applicant"]["party_id"] == "APP-1"


@samples_present
def test_the_documented_two_party_request_works(client):
    """G and H. Both parties, and both call their file `pan.jpg`."""
    response = client.post(
        ENDPOINT,
        files=[upload("files", "pan.jpg", PRIMARY_PAN),
               upload("co_applicant_files", "pan.jpg", CO_PAN)],
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "co_applicant_id": "COAPP-9", "case_id": "CONTRACT-TWO",
              "expected_types": "PAN", "co_applicant_expected_types": "PAN"})

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["primary_applicant"]["party_id"] == "APP-1"
    assert body["co_applicant"]["party_id"] == "COAPP-9"
    assert body["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert body["co_applicant"]["document_ids"] == ["pan.jpg"]
    assert {d["source_id"] for d in body["documents"]} == {"pan.jpg"}
    assert len(body["documents"]) == 2


@samples_present
def test_a_live_response_validates_against_the_published_contract(client):
    """
    The strongest form of the audit: what the service actually returns,
    parsed by the model the spec is generated from.
    """
    from app.agents.los.schemas import LosProcessResponse

    body = client.post(
        ENDPOINT,
        files=[upload("files", "pan.jpg", PRIMARY_PAN),
               upload("co_applicant_files", "pan.jpg", CO_PAN)],
        data={"operation": "PROCESS", "applicant_id": "APP-1",
              "co_applicant_id": "COAPP-9", "case_id": "CONTRACT-VALID",
              "expected_types": "PAN",
              "co_applicant_expected_types": "PAN"}).json()

    parsed = LosProcessResponse.model_validate(body)

    assert parsed.primary_applicant.status
    assert parsed.co_applicant.status
    assert parsed.decision
    assert parsed.next_action


@samples_present
def test_a_live_response_carries_no_undocumented_top_level_key(client):
    """
    The spec must not be narrower than the response either. A key the
    service sends and the contract omits is a key a generated client
    silently drops.
    """
    from app.agents.los.schemas import LosProcessResponse

    body = client.post(ENDPOINT, files=[upload("files", "pan.jpg",
                                               PRIMARY_PAN)],
                       data={"operation": "PROCESS", "applicant_id": "APP-1",
                             "case_id": "CONTRACT-KEYS",
                             "expected_types": "PAN"}).json()

    assert set(body) <= set(LosProcessResponse.model_fields), (
        set(body) - set(LosProcessResponse.model_fields))
