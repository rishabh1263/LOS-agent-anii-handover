"""
The FOS integration surface: two endpoints, one response shape.

WHAT THIS DEFENDS. A field-officer frontend integrates against
POST /api/v1/fos/applicants and POST /api/v1/fos/copilot and nothing else, so
those two contracts have to hold: the same envelope for every action, the
action list complete, unsupported input refused cleanly, and authorisation
enforced on both.

The store is a real repository on a temporary file. Cases are opened through
the public endpoint rather than seeded, because a test that writes rows behind
the API is not testing the API.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.api.routes.fos_api import FosAction
from app.store import set_repository
from app.store.models import Document, DocumentStatus, status_for_verdict
from app.store.sqlite_repo import SQLiteRepository

#: Every field the unified contract promises. Missing or extra is a break.
ENVELOPE = {
    "request_id", "applicant_id", "case_id", "action", "intent", "answer",
    "applicant", "application", "stage", "documents", "checklist",
    "required_documents", "policy", "pending_items", "verification", "kyc",
    "knowledge", "category", "next_action", "readiness", "actions",
    "route_to", "response_source", "processing_ms", "errors",
    # the frontend contract
    "query_type", "case_state", "suggested_questions", "available_actions",
    "document_highlights", "clarification_required",
    # conversation plumbing -- the caller carries it, this service does not
    "followed_up", "context",
}

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "fos_test.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(fos_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {fos_token}"})
    return c


@pytest.fixture
def fos_token(make_token):
    return make_token(scopes=FOS_SCOPES)


@pytest.fixture
def readonly_token(make_token):
    return make_token(scopes=["los.read"])


@pytest.fixture
def case(client):
    """A real case, opened through the public endpoint."""
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {
            "full_name": "Rahul Sharma", "mobile": "9876543210",
            "email": "rahul@example.com", "date_of_birth": "1990-04-12",
            "address": "Mumbai, Maharashtra",
        },
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    return body["applicant_id"], body["case_id"]


def verify(store, case_id, applicant_id, doc_type, verdict, codes=()):
    """Record a pipeline verdict, exactly as app/store/ingest.py would."""
    store.save_document(Document(
        document_id=f"{case_id}:{doc_type}", case_id=case_id,
        applicant_id=applicant_id, document_type=doc_type,
        status=status_for_verdict(verdict), verification_status=verdict,
        reason_codes=list(codes), source_id=f"{doc_type.lower()}.jpg",
    ))


def ask(client, applicant_id, case_id, action, message=None):
    body = {"applicant_id": applicant_id, "case_id": case_id, "action": action}
    if message:
        body["message"] = message
    return client.post("/api/v1/fos/copilot", json=body)


# ==========================================================================
# API 1 -- OPEN A CASE
# ==========================================================================

def test_one_call_creates_the_applicant_and_the_application(client):
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210",
                      "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    })
    assert response.status_code == 201
    body = response.json()

    assert body["applicant_id"]
    assert body["case_id"]
    assert body["applicant"]["full_name"] == "Rahul Sharma"
    assert body["application"]["product"] == "PERSONAL_LOAN"
    assert body["stage"] == "APPLICATION_CREATED"


def test_opening_a_case_returns_the_unified_envelope(client):
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Envelope Test"},
    })
    assert set(response.json()) == ENVELOPE


def test_the_checklist_is_initialised_from_the_product(client):
    """
    The checklist comes from the product's CONFIGURED policy.

    This used to assert a literal list copied out of applicant_agent.yaml,
    which made the test a second copy of the configuration: editing the
    policy broke the test whether or not the service had done anything
    wrong. What matters is that the API returns what the policy engine
    resolves rather than a list of its own, so that is what is asserted --
    plus the one thing that is true of a personal loan under any policy
    worth the name, that identity and address evidence are required.
    """
    from app.agents.policy import engine as policy

    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Checklist Test"},
        "application": {"product": "PERSONAL_LOAN"},
    })
    body = response.json()

    resolved = policy.resolve("PERSONAL_LOAN")
    assert body["required_documents"] == [
        r.slot for r in resolved.requirements if r.mandatory]
    assert "PAN" in body["required_documents"]
    assert "ADDRESS_PROOF" in body["required_documents"]
    assert all(e["status"] == "MISSING" for e in body["checklist"])
    assert body["readiness"]["status"] == "NOT_READY"


def test_optional_slots_appear_but_are_not_required(client):
    body = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Optional Test"},
        "application": {"product": "PERSONAL_LOAN"},
    }).json()

    optional = [e for e in body["checklist"] if not e["mandatory"]]

    assert optional, "no optional slot is configured, so nothing is tested"
    assert all(e["requirement"] == "OPTIONAL" for e in optional)
    assert not any(e["slot"] in body["required_documents"] for e in optional)


def test_a_case_without_a_product_still_has_a_checklist(client):
    """Otherwise a case with no product looks complete by accident."""
    body = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "No Product"},
    }).json()
    assert body["required_documents"] == ["PAN", "ADDRESS_PROOF"]


def test_identifiers_can_be_supplied(client):
    body = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Fixed Ids"},
        "applicant_id": "APP-FIXED", "case_id": "CASE-FIXED",
    }).json()
    assert body["applicant_id"] == "APP-FIXED"
    assert body["case_id"] == "CASE-FIXED"


# ==========================================================================
# API 2 -- THE COPILOT
# ==========================================================================

@pytest.mark.parametrize("action", [
    a.value for a in FosAction
    if a not in (FosAction.UPLOAD_DOCUMENT, FosAction.CUSTOM_QUERY)
])
def test_every_dropdown_action_answers(client, case, action):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, action)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == action
    assert body["answer"]
    assert body["intent"] not in (None, "UNKNOWN")


@pytest.mark.parametrize("action", [
    a.value for a in FosAction
    if a not in (FosAction.UPLOAD_DOCUMENT, FosAction.CUSTOM_QUERY)
])
def test_every_action_returns_the_same_envelope(client, case, action):
    """One response model for the frontend, not eleven."""
    applicant_id, case_id = case
    assert set(ask(client, applicant_id, case_id, action).json()) == ENVELOPE


def test_a_custom_query_is_answered(client, case):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, "CUSTOM_QUERY",
                   "What documents are pending?")

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "DOCUMENTS_PENDING"
    assert body["response_source"] == "STRUCTURED"


def test_a_custom_query_needs_a_message(client, case):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, "CUSTOM_QUERY")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "MESSAGE_REQUIRED"


def test_an_unsupported_action_is_refused_cleanly(client, case):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, "DELETE_EVERYTHING")
    assert response.status_code == 422


def test_upload_over_json_is_refused_with_guidance(client, case):
    applicant_id, case_id = case
    response = ask(client, applicant_id, case_id, "UPLOAD_DOCUMENT")
    assert response.status_code == 415
    assert response.json()["detail"]["error"] == "UPLOAD_REQUIRES_MULTIPART"


# ==========================================================================
# THE STORED VERDICT IS WHAT THE FOS SEES
# ==========================================================================

def test_a_verified_document_shows_as_verified(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")

    body = ask(client, applicant_id, case_id, "GET_DOCUMENTS").json()
    assert [(d["document_type"], d["status"]) for d in body["documents"]] == \
        [("PAN", "VERIFIED")]


def test_a_missing_document_is_pending_and_blocks_readiness(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")

    pending = ask(client, applicant_id, case_id, "GET_PENDING_ITEMS").json()
    codes = {i["code"] for i in pending["pending_items"]}
    assert "DOCUMENT_MISSING" in codes

    readiness = ask(client, applicant_id, case_id, "CHECK_CPA_READINESS").json()
    assert readiness["readiness"]["status"] == "NOT_READY"


def test_readiness_flips_when_everything_is_satisfied(client, case, _store):
    applicant_id, case_id = case
    for doc_type in ("PAN", "BANK_STATEMENT", "DRIVING_LICENCE"):
        verify(_store, case_id, applicant_id, doc_type, "PASS")

    body = ask(client, applicant_id, case_id, "CHECK_CPA_READINESS").json()
    assert body["readiness"]["status"] == "READY_FOR_CPA"
    assert body["readiness"]["blocking_items"] == []


def test_an_optional_document_never_blocks(client, case, _store):
    applicant_id, case_id = case
    for doc_type in ("PAN", "BANK_STATEMENT", "PASSPORT"):
        verify(_store, case_id, applicant_id, doc_type, "PASS")

    # SALARY_SLIP and PHOTO are configured optional and are absent.
    body = ask(client, applicant_id, case_id, "CHECK_CPA_READINESS").json()
    assert body["readiness"]["status"] == "READY_FOR_CPA"


def test_a_review_verdict_blocks_the_handoff(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "REVIEW",
           ["IDENTIFIER_NOT_FOUND"])
    verify(_store, case_id, applicant_id, "BANK_STATEMENT", "PASS")
    verify(_store, case_id, applicant_id, "DRIVING_LICENCE", "PASS")

    body = ask(client, applicant_id, case_id, "CHECK_CPA_READINESS").json()
    assert body["readiness"]["status"] == "NOT_READY"
    assert any(i["code"] == "DOCUMENT_UNDER_REVIEW"
               for i in body["readiness"]["blocking_items"])


def test_a_skipped_check_is_not_treated_as_a_pass():
    """SKIPPED established nothing. Treating it as PASS would release fields
    the verification gate withheld."""
    assert status_for_verdict("SKIPPED") is DocumentStatus.UPLOADED
    assert status_for_verdict("SKIPPED") is not DocumentStatus.VERIFIED


def test_verification_status_reports_what_needs_attention(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")
    verify(_store, case_id, applicant_id, "BANK_STATEMENT", "FAIL",
           ["DOCUMENT_TYPE_MISMATCH"])

    body = ask(client, applicant_id, case_id, "GET_VERIFICATION_STATUS").json()
    assert body["verification"]["needs_attention"] == ["BANK_STATEMENT"]


def test_the_next_action_names_what_to_collect(client, case, _store):
    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")

    body = ask(client, applicant_id, case_id, "GET_NEXT_ACTION").json()
    assert body["next_action"]["action"] in {
        "COLLECT_DOCUMENT", "REQUEST_CORRECT_DOCUMENT",
    }


# ==========================================================================
# OUT-OF-SCOPE ROUTING
# ==========================================================================

@pytest.mark.parametrize("message,route", [
    ("Should we approve this loan?", "DECISION_AGENT"),
    ("What is the applicant's credit score?", "CREDIT_AGENT"),
    ("Calculate the risk for this case.", "RISK_AGENT"),
    ("Do a complete KYC.", "KYC_AGENT"),
    ("Run an RCU check.", "RCU_AGENT"),
])
def test_downstream_questions_are_routed_not_answered(client, case, message, route):
    applicant_id, case_id = case
    body = ask(client, applicant_id, case_id, "CUSTOM_QUERY", message).json()

    assert body["intent"] == "OUT_OF_SCOPE"
    assert body["route_to"] == route

    # And nothing was read on the way to saying so. The fields are now
    # ABSENT rather than null, which is the stronger guarantee: a routed
    # answer that carried the applicant record and every document would have
    # disclosed the case while refusing to discuss it.
    assert not body.get("documents")
    assert body.get("readiness") is None
    assert body.get("applicant") is None


def test_no_approval_recommendation_is_ever_made(client, case):
    applicant_id, case_id = case
    body = ask(client, applicant_id, case_id, "CUSTOM_QUERY",
               "Should we approve this loan?").json()

    lowered = body["answer"].lower()
    assert "approve" not in lowered or "handled by" in lowered
    assert body.get("readiness") is None


# ==========================================================================
# SECURITY
# ==========================================================================

def test_no_token_is_refused(case):
    import main

    applicant_id, case_id = case
    response = TestClient(main.app).post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_DOCUMENTS",
    })
    assert response.status_code == 401


def test_an_invalid_token_is_refused(case):
    import main

    applicant_id, case_id = case
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": "GET_DOCUMENTS"},
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert response.status_code == 401


def test_a_read_only_token_cannot_open_a_case(readonly_token):
    import main

    response = TestClient(main.app).post(
        "/api/v1/fos/applicants",
        json={"applicant": {"full_name": "Refused"}},
        headers={"Authorization": f"Bearer {readonly_token}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "INSUFFICIENT_SCOPE"


def test_a_read_only_token_can_still_read(client, case, readonly_token):
    import main

    applicant_id, case_id = case
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": "GET_DOCUMENTS"},
        headers={"Authorization": f"Bearer {readonly_token}"},
    )
    assert response.status_code == 200


def test_another_applicants_case_is_refused(client, case):
    _, case_id = case
    response = ask(client, "APP-SOMEONE-ELSE", case_id, "GET_DOCUMENTS")
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "CASE_NOT_ACCESSIBLE"


def test_an_expired_token_is_refused(case, make_token):
    """
    A token that was valid yesterday is not valid now.

    Distinct from an unparseable token: this one is correctly signed by the
    right issuer and would pass every check but the clock.
    """
    import main

    applicant_id, case_id = case
    expired = make_token(scopes=FOS_SCOPES, expires_in=-60)
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": "GET_DOCUMENTS"},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert response.status_code == 401


def test_a_token_signed_by_another_key_is_refused(case):
    """
    Right issuer, right audience, right scopes, wrong signing key.

    The one that matters: everything a forger can copy from a real token is
    copied here, and only the signature differs. A verifier that reads the
    claims before checking the signature -- or that trusts the `kid` header to
    pick a key without confirming the key is one of ours -- lets this through.
    """
    import jwt as pyjwt
    import main
    from datetime import datetime, timedelta, timezone

    import app.security.auth as auth
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign_pem = foreign.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    now = datetime.now(timezone.utc)
    forged = pyjwt.encode(
        {"sub": "attacker", "iss": auth.JWT_ISSUER, "aud": auth.JWT_AUDIENCE,
         "iat": now, "nbf": now, "exp": now + timedelta(seconds=900),
         "scope": " ".join(FOS_SCOPES)},
        foreign_pem, algorithm="RS256",
        headers={"kid": "test-signing-key"},
    )

    applicant_id, case_id = case
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": "GET_DOCUMENTS"},
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert response.status_code == 401


def test_a_token_with_no_signature_is_refused(case):
    """The alg=none attack, stated plainly."""
    import base64
    import json

    import main

    def part(payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    unsigned = ".".join([
        part({"alg": "none", "typ": "JWT"}),
        part({"sub": "attacker", "scope": " ".join(FOS_SCOPES)}),
        "",
    ])

    applicant_id, case_id = case
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": "GET_DOCUMENTS"},
        headers={"Authorization": f"Bearer {unsigned}"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("action, withheld", [
    ("GET_APPLICANT", "read_applicant"),
    ("GET_DOCUMENTS", "read_documents"),
    ("GET_VERIFICATION_STATUS", "read_verification"),
    ("GET_PENDING_ITEMS", "read_pending_items"),
    ("GET_NEXT_ACTION", "read_next_action"),
])
def test_each_action_needs_its_own_scope(case, make_token, action, withheld):
    """
    Scope is checked per action, not once at the door.

    A token good for documents is not thereby good for verification. Each
    parameter removes exactly one scope and asks for exactly the thing it
    covered, so a check that passes for the wrong reason still fails here.
    """
    import main

    applicant_id, case_id = case
    partial = [s for s in FOS_SCOPES if s != withheld]
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        json={"applicant_id": applicant_id, "case_id": case_id,
              "action": action},
        headers={"Authorization": f"Bearer {make_token(scopes=partial)}"},
    )
    assert response.status_code == 403, (
        f"{action} answered without {withheld}"
    )
    assert response.json()["detail"]["error"] == "INSUFFICIENT_SCOPE"


def test_an_upload_without_the_upload_scope_is_refused(case, make_token):
    """
    A read token must not be able to put a document on a case.

    Refused before the file is read, so a caller who cannot upload also
    cannot use the endpoint to push bytes through the OCR pipeline.
    """
    import main

    applicant_id, case_id = case
    partial = [s for s in FOS_SCOPES if s != "upload_document"]
    response = TestClient(main.app).post(
        "/api/v1/fos/copilot",
        data={"applicant_id": applicant_id, "case_id": case_id,
              "action": "UPLOAD_DOCUMENT", "document_type": "PAN"},
        files={"file": ("pan.jpg", bytes([0xFF, 0xD8, 0xFF]) + b"0" * 64,
                        "image/jpeg")},
        headers={"Authorization": f"Bearer {make_token(scopes=partial)}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "INSUFFICIENT_SCOPE"


def test_a_case_belonging_to_another_applicant_is_refused(client):
    """
    Two real cases. Neither applicant may read the other's.

    Ownership is asked of the store, not inferred from what the caller sent,
    so pairing a real applicant with a real case they do not own is refused.
    """
    first = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "First Applicant", "mobile": "9000000001"},
        "application": {"product": "PERSONAL_LOAN"},
    }).json()
    second = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Second Applicant", "mobile": "9000000002"},
        "application": {"product": "PERSONAL_LOAN"},
    }).json()

    crossed = ask(client, first["applicant_id"], second["case_id"],
                  "GET_DOCUMENTS")
    assert crossed.status_code == 403
    assert crossed.json()["detail"]["error"] == "CASE_NOT_ACCESSIBLE"

    # And each still reads its own.
    assert ask(client, first["applicant_id"], first["case_id"],
               "GET_DOCUMENTS").status_code == 200
    assert ask(client, second["applicant_id"], second["case_id"],
               "GET_DOCUMENTS").status_code == 200


def test_an_unknown_case_is_refused_without_confirming_it_exists(client, case):
    """
    A caller must not learn which case identifiers are real.

    The refusal for a case that does not exist and the refusal for one that
    belongs to someone else are the same refusal.
    """
    applicant_id, _ = case
    unknown = ask(client, applicant_id, "CASE-DOES-NOT-EXIST", "GET_DOCUMENTS")
    assert unknown.status_code == 403
    assert unknown.json()["detail"]["error"] == "CASE_NOT_ACCESSIBLE"


def test_the_response_carries_no_internals(client, case, _store):
    from app.agents.applicant.validate import validate_response_shape

    applicant_id, case_id = case
    verify(_store, case_id, applicant_id, "PAN", "PASS")

    body = ask(client, applicant_id, case_id, "GET_CASE_360").json()
    assert validate_response_shape(body) == []


# ==========================================================================
# SUPPORTING ENDPOINTS
# ==========================================================================

def test_the_action_list_matches_the_enum(client):
    """A dropdown rendered from this must offer everything the API accepts."""
    body = client.get("/api/v1/fos/actions").json()
    assert {a["value"] for a in body["actions"]} == {a.value for a in FosAction}


def test_the_action_list_says_how_each_action_is_sent(client):
    body = client.get("/api/v1/fos/actions").json()
    by_value = {a["value"]: a for a in body["actions"]}

    assert by_value["UPLOAD_DOCUMENT"]["requires_file"] is True
    assert by_value["UPLOAD_DOCUMENT"]["content_type"] == "multipart/form-data"
    assert by_value["CUSTOM_QUERY"]["requires_message"] is True
    assert by_value["GET_DOCUMENTS"]["content_type"] == "application/json"


def test_the_config_endpoint_describes_the_taxonomy(client):
    body = client.get("/api/v1/fos/config").json()

    assert "PERSONAL_LOAN" in body["products"]
    assert "PAN" in body["document_types"]
    # Declaring a type must not make it required anywhere.
    assert "AADHAAR" in body["document_types"]
    personal = {e["slot"] for e in body["checklists"]["PERSONAL_LOAN"]
                if e["mandatory"]}
    assert "AADHAAR" not in personal


# ==========================================================================
# OPENAPI -- the frontend's contract
# ==========================================================================

def test_the_two_endpoints_are_documented_under_fos():
    import main

    spec = main.app.openapi()
    for path in ("/api/v1/fos/applicants", "/api/v1/fos/copilot"):
        operation = spec["paths"][path]["post"]
        assert operation["tags"] == ["FOS"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert operation["summary"]


def test_the_copilot_documents_both_request_forms():
    import main

    body = main.app.openapi()["paths"]["/api/v1/fos/copilot"]["post"]["requestBody"]
    assert "application/json" in body["content"]
    assert "multipart/form-data" in body["content"]

    upload = body["content"]["multipart/form-data"]["schema"]["properties"]

    # The published upload form is the MULTI-file one. `file` and
    # `document_type` still work but are no longer shown: two file pickers
    # side by side, one of which takes a single document, sent a person
    # demoing this straight to the wrong one.
    assert upload["files"]["items"]["format"] == "binary"
    assert upload["document_types"]["type"] == "array"
    assert "file" not in upload
    assert "document_type" not in upload

    # And the arrays must be declared as one part per item, or the generated
    # curl joins them with commas.
    encoding = body["content"]["multipart/form-data"]["encoding"]
    assert encoding["files"]["explode"] is True
    assert encoding["document_types"]["explode"] is True


def test_the_copilot_carries_examples_for_the_frontend():
    import main

    examples = (main.app.openapi()["paths"]["/api/v1/fos/copilot"]["post"]
                ["requestBody"]["content"]["application/json"]["examples"])
    assert len(examples) >= 6
    assert all("summary" in e and "value" in e for e in examples.values())


def test_the_superseded_routes_are_marked_deprecated():
    """They still work; Swagger just stops recommending them."""
    import main

    spec = main.app.openapi()
    applicant_agent = [
        spec["paths"][p][m]
        for p in spec["paths"] if "applicant-agent" in p
        for m in spec["paths"][p]
    ]
    assert applicant_agent
    assert all(op.get("deprecated") for op in applicant_agent)


def test_the_los_endpoint_is_untouched():
    """The refactor must not have moved or changed the LOS contract."""
    import main

    operation = main.app.openapi()["paths"]["/api/v1/los/process"]["post"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("LosProcessResponse")
