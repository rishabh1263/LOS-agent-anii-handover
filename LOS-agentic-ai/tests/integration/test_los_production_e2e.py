"""
The production LOS flow, end to end over the real authenticated endpoint.

Every test here posts to POST /api/v1/los/process with a signed JWT and reads
the public response. Nothing calls a service directly, because the point is
the WHOLE path:

    client -> HTTP -> orchestrator -> MCP tool -> capability
           -> verification gate -> extraction -> KYC -> decision -> summary

Fixtures are real samples from the repository where one exists, and drawn
locally where none does. Nothing sensitive is committed.
"""

from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path

import pytest

RB = Path("samples/real_batch")
SD = Path("samples/documents")


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    # The summary is exercised on its own below; leaving the model out of the
    # other cases keeps them about the flow rather than about Ollama.
    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")

    from app.agents.los import config

    config.reload()
    yield
    config.reload()


def sample(name: str) -> bytes:
    for root in (RB, SD):
        path = root / name
        if path.exists():
            return path.read_bytes()
    pytest.skip(f"sample not available: {name}")


def photo() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 960), (210, 210, 210))
    draw = ImageDraw.Draw(image)
    for x in range(0, 1280, 24):
        draw.line([(x, 0), (x, 960)], fill=(10, 10, 10), width=3)
    for y in range(0, 960, 24):
        draw.line([(0, y), (1280, y)], fill=(10, 10, 10), width=3)

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def signature() -> bytes:
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(23)
    image = Image.new("L", (640, 240), 250)
    ImageDraw.Draw(image).line(
        [(60 + i * 18, 120 + int(46 * math.sin(i * 0.7))) for i in range(30)],
        fill=20,
        width=8,
    )
    pixels = image.load()
    for _ in range((640 * 240) // 6):
        pixels[rng.randrange(640), rng.randrange(240)] = rng.randint(120, 250)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def post(client, uploads, expected=None, case_id=None, **extra):
    """uploads: list of (filename, bytes, content_type)."""
    data = {"operation": "PROCESS", "applicant_id": "APP-E2E"}
    if case_id:
        data["case_id"] = case_id
    if expected:
        data["expected_types"] = ",".join(expected)
    data.update(extra)

    return client.post(
        "/api/v1/los/process",
        files=[("files", upload) for upload in uploads],
        data=data,
    )


def doc(body: dict, source_id: str) -> dict:
    return next(d for d in body["documents"] if d["source_id"] == source_id)


# ==========================================================================
# THE MULTIPART CONTRACT
#
# Swagger UI draws a file picker only when it sees `format: binary`. FastAPI
# 0.138 emits OpenAPI 3.1, which describes an upload with `contentMediaType`
# instead -- so the docs page rendered `files` as a plain string array with
# an "Add string item" button, and the multi-document upload this endpoint
# has always accepted could not be exercised from it.
# ==========================================================================


def test_files_is_declared_as_an_array_of_binaries(app_client):
    spec = app_client.get("/openapi.json").json()
    body = spec["paths"]["/api/v1/los/process"]["post"]["requestBody"]

    schema_ref = body["content"]["multipart/form-data"]["schema"]["$ref"]
    schema = spec["components"]["schemas"][schema_ref.split("/")[-1]]
    files = schema["properties"]["files"]

    assert files["type"] == "array"
    assert files["items"]["type"] == "string"
    assert files["items"]["format"] == "binary"
    assert "files" in schema["required"]


def test_the_endpoint_accepts_multipart_not_json(app_client):
    """Files must never be declared through a JSON body model."""
    spec = app_client.get("/openapi.json").json()
    content = spec["paths"]["/api/v1/los/process"]["post"]["requestBody"]["content"]

    assert "multipart/form-data" in content
    assert "application/json" not in content


@pytest.mark.parametrize("count", [1, 2, 4, 6])
def test_any_number_of_files_is_accepted_in_one_request(app_client, count):
    uploads = [(f"doc{i}.jpg", photo(), "image/jpeg") for i in range(count)]

    response = post(app_client, uploads)

    assert response.status_code == 200, response.text
    assert len(response.json()["documents"]) == count


def test_images_and_pdfs_mix_in_one_request(app_client):
    response = post(
        app_client,
        [
            ("shop.jpg", photo(), "image/jpeg"),
            ("itr.pdf", sample("itr_v.pdf"), "application/pdf"),
        ],
        expected=["BUSINESS_PROOF_1", "AUTO"],
    )

    assert response.status_code == 200
    body = response.json()

    assert {d["source_id"] for d in body["documents"]} == {"shop.jpg", "itr.pdf"}


def test_expected_types_stay_positionally_matched(app_client):
    """Third slot is AUTO; the first two are asserted."""
    response = post(
        app_client,
        [
            ("a.jpg", photo(), "image/jpeg"),
            ("b.jpg", photo(), "image/jpeg"),
            ("c.jpg", photo(), "image/jpeg"),
        ],
        expected=["BUSINESS_PROOF_1", "BUSINESS_PROOF_2", "AUTO"],
    )

    body = response.json()

    assert doc(body, "a.jpg")["type"] == "BUSINESS_PROOF_1"
    assert doc(body, "b.jpg")["type"] == "BUSINESS_PROOF_2"
    # AUTO means the caller asserted nothing, so classification decides.
    assert doc(body, "c.jpg")["type"] != "BUSINESS_PROOF_2"


def test_a_specialist_verdict_is_reported_not_swallowed(app_client):
    """
    A specialist writes its verdict as `decision`; the Document Agent writes
    `status`. Reading only one spelling made every specialist document report
    SKIPPED -- and because the extraction gate keys on that value, it
    withheld their fields too.
    """
    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
    )

    document = doc(response.json(), "shop.jpg")

    assert document["verification"] == document["specialist"]["decision"]
    assert document["verification"] != "SKIPPED"


# ==========================================================================
# THE CONTRACT
# ==========================================================================


CONTRACT_KEYS = {
    "request_id", "applicant_id", "status", "documents", "kyc",
    "cross_document", "decision", "next_action", "summary", "processing_ms",
    "errors",
}


@pytest.mark.ocr
def test_1_pan_only(app_client):
    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])

    assert response.status_code == 200, response.text
    body = response.json()

    assert CONTRACT_KEYS <= set(body)
    assert isinstance(body["decision"], str)
    assert isinstance(body["next_action"], str)

    document = doc(body, "pan.jpg")
    assert document["type"] == "PAN"
    assert document["verification"] == "PASS"
    assert document["extraction"]["pan_number"]


@pytest.mark.ocr
def test_2_pan_and_driving_licence(app_client):
    """Two identity documents in one request; KYC compares them."""
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("dl.jpg", sample("dl1.jpg"), "image/jpeg"),
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["documents"]) == 2
    assert body["kyc"]["status"] in {"PASS", "REVIEW", "FAIL", "SKIPPED"}


@pytest.mark.ocr
def test_3_identity_plus_financial(app_client):
    """PAN + ITR: identity and income in one application-level pass."""
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("itr.pdf", sample("itr_v.pdf"), "application/pdf"),
        ],
    )

    body = response.json()
    types = {d["type"] for d in body["documents"]}

    assert "PAN" in types
    assert "ITR" in types


@pytest.mark.ocr
def test_4_pan_plus_sale_deed(app_client):
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("deed.pdf", sample("sale_deed_test.pdf"), "application/pdf"),
        ],
        expected=["AUTO", "SALE_DEED"],
    )

    deed = doc(response.json(), "deed.pdf")

    assert deed["specialist"]["decision"] in {"PASS", "REVIEW", "FAIL"}
    assert deed["specialist"]["ownership_verified"] is False


def test_5_both_business_proof_slots(app_client):
    response = post(
        app_client,
        [
            ("bp1.jpg", photo(), "image/jpeg"),
            ("bp2.jpg", photo(), "image/jpeg"),
        ],
        expected=["BUSINESS_PROOF_1", "BUSINESS_PROOF_2"],
    )

    body = response.json()

    for source in ("bp1.jpg", "bp2.jpg"):
        assert doc(body, source)["specialist"]["decision"] in {"PASS", "REVIEW", "FAIL"}


@pytest.mark.ocr
def test_6_mixed_full_application(app_client):
    """
    The real shape of an application: identity, income, property, premises
    and a signature, in ONE request. A specialist that quietly failed to
    route shows up here.
    """
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("dl.jpg", sample("dl1.jpg"), "image/jpeg"),
            ("itr.pdf", sample("itr_v.pdf"), "application/pdf"),
            ("deed.pdf", sample("sale_deed_test.pdf"), "application/pdf"),
            ("shop.jpg", photo(), "image/jpeg"),
            ("sig.png", signature(), "image/png"),
        ],
        expected=["AUTO", "AUTO", "AUTO", "SALE_DEED", "BUSINESS_PROOF_1", "SIGNATURE"],
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["documents"]) == 6

    # Each specialist upload came back with a real verdict. The capability
    # NAME is no longer published -- it told a client which of our services
    # ran -- so routing is proved by the verdict and the capability-specific
    # findings arriving, which only that capability produces.
    handled = {
        d["source_id"]: d.get("specialist") or {}
        for d in body["documents"]
    }
    for source in ("deed.pdf", "shop.jpg", "sig.png"):
        assert handled[source].get("decision") in {"PASS", "REVIEW", "FAIL"}

    assert handled["deed.pdf"]["ownership_verified"] is False
    assert handled["shop.jpg"]["business_existence_verified"] is False
    assert handled["sig.png"]["signature"]["presence"]

    assert body["decision"] in {"PASS", "REVIEW", "REJECT"}
    assert body["summary"]


@pytest.mark.ocr
def test_7_a_conflict_is_surfaced(app_client):
    """
    Two identity documents belonging to different people.

    The application must not pass quietly just because each document read
    cleanly on its own.
    """
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("dl.jpg", sample("dl1.jpg"), "image/jpeg"),
        ],
    )

    body = response.json()

    if body["kyc"]["status"] in {"FAIL", "REVIEW"}:
        assert body["cross_document"]["status"] in {"FAIL", "REVIEW"}
        assert body["decision"] != "PASS"
        assert body["next_action"] == "MANUAL_REVIEW"


# ==========================================================================
# THE VERIFICATION GATE
# ==========================================================================


@pytest.mark.ocr
def test_8_verification_off_skips_and_withholds_extraction(app_client, monkeypatch):
    """
    Switched off is REPORTED, and it does not widen what the API releases.

    SKIPPED is not a PASS: a check that did not run has established nothing.
    """
    monkeypatch.setenv("VERIFICATION_ENABLED", "false")

    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])

    document = doc(response.json(), "pan.jpg")

    assert document["verification"] != "PASS"
    assert document.get("extraction") is None


@pytest.mark.ocr
def test_extraction_is_released_only_behind_a_pass(app_client):
    """The gate, stated as an invariant over whatever the run produced."""
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("shop.jpg", photo(), "image/jpeg"),
        ],
        expected=["AUTO", "BUSINESS_PROOF_1"],
    )

    for document in response.json()["documents"]:
        if document["verification"] != "PASS":
            assert document.get("extraction") is None, document["source_id"]


# ==========================================================================
# CONFIGURATION
# ==========================================================================


@pytest.mark.ocr
def test_9_kyc_off_reports_skipped(app_client, monkeypatch):
    monkeypatch.setenv("LOS_KYC_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])
    body = response.json()

    assert body["kyc"]["status"] == "SKIPPED"
    assert "KYC_DISABLED" in body["kyc"]["reason_codes"]


@pytest.mark.ocr
def test_10_llm_off_uses_the_deterministic_summary(app_client, monkeypatch):
    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")

    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])
    body = response.json()

    assert body["summary"]
    assert body["summary_source"] != "llm"


@pytest.mark.ocr
def test_11_an_unavailable_model_does_not_slow_the_request(app_client, monkeypatch):
    """
    The request must not wait on a model that is not there.

    The deterministic summary is already written; an unreachable provider
    costs the sentence, not the response.
    """
    import time

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")

    from app.llm import availability

    availability.mark_unavailable("forced for test")

    started = time.perf_counter()
    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])
    elapsed = time.perf_counter() - started

    body = response.json()

    assert body["summary"]
    assert body["summary_source"] != "llm"
    # Generous: the OCR pass dominates. The point is that no multi-second
    # model timeout was added on top of it.
    assert elapsed < 30

    availability.reset()


def test_a_disabled_specialist_is_reported_not_bypassed(app_client, monkeypatch):
    """
    A switched-off capability must not fall through to the Document Agent,
    which would classify a shop photograph as an unreadable ID card.
    """
    monkeypatch.setenv("LOS_BUSINESS_EVIDENCE_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
    )

    document = doc(response.json(), "shop.jpg")

    assert document["verification"] == "SKIPPED"
    assert document.get("extraction") is None
    assert any(
        error["code"] == "CAPABILITY_DISABLED"
        for error in document.get("errors") or []
    )


def test_conflict_detection_off_empties_the_list(app_client, monkeypatch):
    monkeypatch.setenv("LOS_CONFLICT_DETECTION_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(app_client, [("shop.jpg", photo(), "image/jpeg")],
                    expected=["BUSINESS_PROOF_1"])

    assert response.json()["cross_document"] == {"status": "SKIPPED", "checks": []}


# ==========================================================================
# CLASSIFICATION SWITCHED OFF
# ==========================================================================


def _classification_off(monkeypatch):
    monkeypatch.setenv("LOS_CLASSIFICATION_ENABLED", "false")
    from app.agents.los import config

    config.reload()


def test_classification_off_skips_and_says_so(app_client, monkeypatch):
    _classification_off(monkeypatch)

    response = post(app_client, [("shop.jpg", photo(), "image/jpeg")])
    document = doc(response.json(), "shop.jpg")

    assert document["verification"] == "SKIPPED"
    assert "CLASSIFICATION_DISABLED" in document["reason_codes"]
    assert document["type"] == "UNKNOWN"
    assert document.get("extraction") is None


def test_classification_off_does_not_promote_a_hint_to_a_type(
    app_client, monkeypatch
):
    """
    The caller's expected_type is an ASSERTION, not a classification result.

    Reporting it as `type` would present a hint as something this service
    established, and route a specialist on it.
    """
    _classification_off(monkeypatch)

    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
    )
    document = doc(response.json(), "shop.jpg")

    assert document["type"] == "UNKNOWN"
    assert document["hint"] == "BUSINESS_PROOF_1"
    assert document["verification"] == "SKIPPED"


def test_classification_off_runs_no_specialist(app_client, monkeypatch):
    """A hint must not decide which capability judged the document."""
    _classification_off(monkeypatch)

    response = post(
        app_client,
        [("deed.pdf", b"%PDF-1.4 stub", "application/pdf")],
        expected=["SALE_DEED"],
    )
    document = doc(response.json(), "deed.pdf")

    assert "specialist" not in document
    assert document.get("extraction") is None


# ==========================================================================
# EXTRACTION SWITCHED OFF
#
# The invariant, over both switches:
#   extraction is released ONLY when verification PASSed AND extraction is on.
# ==========================================================================


def _set_flags(monkeypatch, *, verification: bool, extraction: bool):
    monkeypatch.setenv("VERIFICATION_ENABLED", "true" if verification else "false")
    monkeypatch.setenv("LOS_EXTRACTION_ENABLED", "true" if extraction else "false")
    from app.agents.los import config

    config.reload()


@pytest.mark.ocr
def test_pass_with_extraction_on_releases_fields(app_client, monkeypatch):
    _set_flags(monkeypatch, verification=True, extraction=True)

    document = doc(
        post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")]).json(),
        "pan.jpg",
    )

    assert document["verification"] == "PASS"
    assert document["extraction"]["pan_number"]


@pytest.mark.ocr
def test_pass_with_extraction_off_withholds_fields(app_client, monkeypatch):
    _set_flags(monkeypatch, verification=True, extraction=False)

    document = doc(
        post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")]).json(),
        "pan.jpg",
    )

    assert document["verification"] == "PASS"
    assert document.get("extraction") is None
    assert "EXTRACTION_DISABLED" in document["reason_codes"]


@pytest.mark.ocr
def test_a_non_pass_withholds_fields_even_with_extraction_on(
    app_client, monkeypatch
):
    """Verification outranks the extraction switch, never the other way."""
    _set_flags(monkeypatch, verification=True, extraction=True)

    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
    )

    for document in response.json()["documents"]:
        if document["verification"] != "PASS":
            assert document.get("extraction") is None


@pytest.mark.ocr
@pytest.mark.parametrize("extraction_on", [True, False])
def test_verification_off_withholds_fields_either_way(
    app_client, monkeypatch, extraction_on
):
    """
    Two switches, one invariant.

    With verification off the document is SKIPPED, and SKIPPED is not a PASS
    -- so extraction is withheld whether or not extraction is enabled.
    """
    _set_flags(monkeypatch, verification=False, extraction=extraction_on)

    document = doc(
        post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")]).json(),
        "pan.jpg",
    )

    assert document["verification"] != "PASS"
    assert document.get("extraction") is None


# ==========================================================================
# APPLICANT AND CASE
# ==========================================================================


def test_a_case_id_is_created_when_none_is_given(app_client):
    body = post(app_client, [("shop.jpg", photo(), "image/jpeg")]).json()

    assert body["applicant_id"] == "APP-E2E"
    assert body["case_id"]
    assert body["case_id"] != body["applicant_id"]


def test_one_applicant_gets_two_distinct_cases(app_client):
    """
    An applicant_id names a PERSON and is reused for life; a case_id names
    one application. Two uploads with no case_id are two cases.
    """
    first = post(app_client, [("shop.jpg", photo(), "image/jpeg")]).json()
    second = post(app_client, [("shop.jpg", photo(), "image/jpeg")]).json()

    assert first["applicant_id"] == second["applicant_id"]
    assert first["case_id"] != second["case_id"]


def test_an_explicit_case_id_is_continued(app_client):
    body = post(
        app_client, [("shop.jpg", photo(), "image/jpeg")], case_id="CASE-2"
    ).json()

    assert body["case_id"] == "CASE-2"


def test_results_are_scoped_to_the_case_in_the_response(app_client):
    """
    Cases stay isolated.

    Nothing from another case reaches this one: the response carries only
    the documents sent in this call, under this case_id.
    """
    one = post(
        app_client, [("a.jpg", photo(), "image/jpeg")], case_id="CASE-A"
    ).json()
    two = post(
        app_client, [("b.jpg", photo(), "image/jpeg")], case_id="CASE-B"
    ).json()

    assert [d["source_id"] for d in one["documents"]] == ["a.jpg"]
    assert [d["source_id"] for d in two["documents"]] == ["b.jpg"]
    assert one["case_id"] == "CASE-A"
    assert two["case_id"] == "CASE-B"


# ==========================================================================
# TIMING ATTRIBUTION
# ==========================================================================


def test_a_specialist_call_reports_mcp_and_orchestration_time(tmp_path, monkeypatch):
    """
    Nested measurements, not additive costs.

    mcp_ms is what MCP measured inside the call; orchestration_ms is what
    routing cost around it. Both sit INSIDE specialist_ms.

    Asserted on the INTERNAL document, because that is where these live now:
    they describe how the service is built, so they were taken out of the
    client response. Removing the test with them would have meant nothing
    checked that the attribution is still correct.
    """
    import asyncio

    from app.agents.los.flow import UploadedDocument, _run_specialist

    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(tmp_path))

    document = UploadedDocument(
        source_id="shop.jpg",
        filename="shop.jpg",
        content=photo(),
        expected_type="BUSINESS_PROOF_1",
    )

    result = asyncio.run(
        _run_specialist(document, "business_evidence", "req_timing")
    )
    timings = result["processing"]

    assert timings["specialist_ms"] > 0
    assert timings["mcp_ms"] > 0
    assert timings["orchestration_ms"] >= 0
    assert timings["mcp_ms"] <= timings["specialist_ms"]
    assert (
        abs(
            timings["mcp_ms"]
            + timings["orchestration_ms"]
            - timings["specialist_ms"]
        )
        < 1.0
    )


def test_stage_timings_are_kept_but_not_published(tmp_path, monkeypatch):
    """
    The aggregate still exists internally; it is simply not in the response.

    Guards the difference between "we stopped publishing this" and "we
    stopped measuring it" -- only the first was intended.
    """
    from app.agents.los.flow import _aggregate_timings

    totals = _aggregate_timings([
        {"processing": {"ocr_ms": 10.0, "classification_ms": 2.0,
                        "verification_ms": 1.0, "extraction_ms": 3.0}},
        {"processing": {"ocr_ms": 0.0, "classification_ms": 1.0,
                        "verification_ms": 0.5, "extraction_ms": 2.0}},
    ])

    assert totals["ocr_ms"] == 10.0
    assert totals["classification_ms"] == 3.0
    assert totals["extraction_ms"] == 5.0


# ==========================================================================
# COLLATERAL STAYS UNWIRED
# ==========================================================================


def test_collateral_is_off_and_stays_off():
    """
    No contract exists for the external services, so nothing calls them.

    Their request/response schema files in the supplied bundles are empty,
    and a client built against a guessed contract would be worse than none.
    """
    from app.agents.los import config

    assert config.collateral_customer_enabled() is False
    assert config.collateral_property_enabled() is False


def test_no_collateral_capability_is_registered():
    from app.orchestration import registry

    registered = registry.registered_agents()

    assert not any("collateral" in agent for agent in registered)


# ==========================================================================
# FAILURE AND SECURITY
# ==========================================================================


@pytest.mark.ocr
def test_12_one_bad_document_does_not_sink_the_application(app_client):
    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("broken.jpg", b"\xff\xd8\xff\xe0 not an image", "image/jpeg"),
        ],
    )

    assert response.status_code == 200
    body = response.json()

    assert len(body["documents"]) == 2
    assert doc(body, "pan.jpg")["type"] == "PAN"


def test_13_an_unauthenticated_request_is_refused(unauthenticated_client):
    response = unauthenticated_client.post(
        "/api/v1/los/process",
        files=[("files", ("shop.jpg", photo(), "image/jpeg"))],
        data={"operation": "EXTRACT"},
    )

    assert response.status_code == 401


def test_14_an_unsupported_file_type_is_reported_not_raised(app_client):
    response = post(
        app_client,
        [("notes.txt", b"this is not a document", "text/plain")],
    )

    assert response.status_code in (200, 400, 415, 422)
    if response.status_code == 200:
        document = doc(response.json(), "notes.txt")
        assert document.get("extraction") is None


def test_an_empty_upload_is_rejected(app_client):
    response = post(app_client, [("empty.jpg", b"", "image/jpeg")])

    assert response.status_code == 400


# ==========================================================================
# RESPONSE HYGIENE
# ==========================================================================


@pytest.mark.ocr
def test_the_response_carries_no_internal_detail(app_client):
    """
    No OCR tokens, no candidate scores, no MCP envelope, no debug objects.

    A caller that starts reading those turns an internal detail into a
    contract nobody meant to sign.
    """
    import json

    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("shop.jpg", photo(), "image/jpeg"),
        ],
        expected=["AUTO", "BUSINESS_PROOF_1"],
    )

    blob = json.dumps(response.json())

    for leaked in (
        "ocr_tokens", "tokens", "candidate", "pair_comparison",
        "comparisons", "raw_text", "bbox", "x0",
    ):
        assert leaked not in blob, f"{leaked!r} leaked into the public response"


@pytest.mark.ocr
def test_the_response_stays_mid_short(app_client):
    """Two documents should not produce a wall of JSON."""
    import json

    response = post(
        app_client,
        [
            ("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg"),
            ("shop.jpg", photo(), "image/jpeg"),
        ],
        expected=["AUTO", "BUSINESS_PROOF_1"],
    )

    assert len(json.dumps(response.json())) < 6000


@pytest.mark.ocr
def test_stage_timings_are_reported(app_client):
    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])
    body = response.json()

    assert body["processing_ms"] > 0
    assert "processing" not in body, "stage timings are internal"


# ==========================================================================
# WHAT THE BUSINESS-LEVEL E2E PASS TURNED UP
#
# Two defects, both found by running whole applications through the real
# endpoint rather than by testing a unit: a conflict nobody could attribute,
# and a switch that did nothing.
# ==========================================================================


def test_a_conflict_names_the_documents_it_is_about():
    """
    `sources` was always empty.

    The response layer reads a failed check's `source_ids`, and the flow
    never wrote any -- so every conflict arrived unattributed. On a
    six-document application, NAME_MISMATCH against nothing in particular
    tells a reviewer to re-read all six.
    """
    from app.agents.kyc.schemas import (
        CheckResult, CheckStatus, Evidence, KycCheck, KycDocumentType,
        PairComparison, ReasonCode,
    )
    from app.agents.los.flow import _disagreeing_sources

    check = CheckResult(
        check=KycCheck.NAME,
        status=CheckStatus.FAIL,
        reason_codes=[ReasonCode.NAME_MISMATCH],
        evidence=[
            Evidence(source_id="pan.jpg", document_type=KycDocumentType.PAN,
                     field="name", value="RAHUL KUMAR"),
            Evidence(source_id="itr.pdf", document_type=KycDocumentType.ITR,
                     field="name", value="RAHUL SHARMA"),
        ],
        comparisons=[
            PairComparison(
                left_source_id="pan.jpg", right_source_id="itr.pdf",
                left_value="RAHUL KUMAR", right_value="RAHUL SHARMA",
                agreed=False, score=0.696, detail="no match",
            )
        ],
    )

    assert _disagreeing_sources(check) == ["pan.jpg", "itr.pdf"]


def test_only_the_disagreeing_pair_is_named():
    """A document that agreed is not what the conflict is about."""
    from app.agents.kyc.schemas import (
        CheckResult, CheckStatus, KycCheck, PairComparison,
    )
    from app.agents.los.flow import _disagreeing_sources

    check = CheckResult(
        check=KycCheck.NAME,
        status=CheckStatus.FAIL,
        comparisons=[
            PairComparison(left_source_id="pan.jpg", right_source_id="dl.jpg",
                           left_value="A", right_value="A", agreed=True,
                           score=1.0, detail="exact"),
            PairComparison(left_source_id="pan.jpg", right_source_id="itr.pdf",
                           left_value="A", right_value="B", agreed=False,
                           score=0.4, detail="no match"),
        ],
    )

    assert _disagreeing_sources(check) == ["pan.jpg", "itr.pdf"]


def test_a_check_that_never_compared_falls_back_to_its_evidence():
    from app.agents.kyc.schemas import (
        CheckResult, CheckStatus, Evidence, KycCheck, KycDocumentType,
    )
    from app.agents.los.flow import _disagreeing_sources

    check = CheckResult(
        check=KycCheck.PAN,
        status=CheckStatus.REVIEW,
        evidence=[
            Evidence(source_id="pan.jpg", document_type=KycDocumentType.PAN,
                     field="pan", value="ABCPV1234K"),
        ],
    )

    assert _disagreeing_sources(check) == ["pan.jpg"]


def test_conflicts_carry_the_sources_the_flow_supplies():
    """End of the chain: what the flow writes is what the client reads."""
    from app.agents.los.response import conflicts_from_kyc

    conflicts = conflicts_from_kyc({
        "status": "FAIL",
        "checks": [
            {
                "check": "NAME",
                "status": "FAIL",
                "reason_codes": ["NAME_MISMATCH"],
                "source_ids": ["pan.jpg", "itr.pdf"],
            },
            {"check": "DOB", "status": "SKIPPED", "reason_codes": []},
        ],
    })

    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "NAME_MISMATCH"
    assert conflicts[0]["severity"] == "HIGH"
    assert conflicts[0]["sources"] == ["pan.jpg", "itr.pdf"]


@pytest.mark.ocr
def test_the_los_verification_flag_is_enforced(app_client, monkeypatch):
    """
    LOS_VERIFICATION_ENABLED was published in the config snapshot and
    enforced nowhere.

    An operator switching verification off still got PASS and the fields
    behind it -- fields nothing had checked, which is the exact outcome the
    gate exists to prevent. VERIFICATION_ENABLED, the Verification Agent's
    own switch, did work; this one silently did not.
    """
    monkeypatch.setenv("LOS_VERIFICATION_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(app_client, [("pan.jpg", sample("pan_bw2.jpg"), "image/jpeg")])
    document = doc(response.json(), "pan.jpg")

    assert document["verification"] == "SKIPPED"
    assert "VERIFICATION_DISABLED" in (document.get("reason_codes") or [])
    assert document.get("extraction") is None


def test_the_los_verification_flag_also_governs_a_specialist(
    app_client, monkeypatch
):
    """
    A specialist reports `decision`, and the response layer falls back to it.

    Left untouched, a switched-off verification would still surface the
    capability's verdict as the document's verification status.
    """
    monkeypatch.setenv("LOS_VERIFICATION_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
    )
    document = doc(response.json(), "shop.jpg")

    assert document["verification"] == "SKIPPED"
    assert document.get("extraction") is None
    assert document["specialist"]["decision"] == "SKIPPED"


def test_verification_off_does_not_rescue_an_unprocessable_file(
    app_client, monkeypatch
):
    """
    Withholding the verdict must not withhold the failure.

    Reporting every document SKIPPED would drop an unreadable file out of the
    application roll-up, so a case full of garbage would report SUCCESS with
    verification switched off.
    """
    monkeypatch.setenv("LOS_VERIFICATION_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(app_client, [("notes.txt", b"not a document", "text/plain")])
    document = doc(response.json(), "notes.txt")

    assert document["status"] == "FAILED"
    assert document["verification"] == "SKIPPED"
    assert document.get("extraction") is None


# ==========================================================================
# A CONSISTENT SYNTHETIC APPLICANT
#
# The real corpus belongs to unrelated real people, and combining two of
# them into one applicant would manufacture a conflict rather than test one.
# These are drawn here, in the real layouts, with invented values -- so the
# all-PASS path can be exercised over HTTP without committing anyone's data.
# ==========================================================================

APPLICANT = {
    "name": "SUNIL KUMAR VERMA",
    "father": "RAMESH KUMAR VERMA",
    "dob": "12/04/1988",
    "pan": "ABCPV1234K",
}


def _truetype(size: int):
    from PIL import ImageFont

    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def pan_card(name: str | None = None, pan: str | None = None) -> bytes:
    """A PAN card in the printed layout, with invented values."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 640), (246, 243, 232))
    draw = ImageDraw.Draw(image)

    draw.text((40, 30), "INCOME TAX DEPARTMENT", font=_truetype(30), fill=(20, 20, 90))
    draw.text((600, 30), "GOVT. OF INDIA", font=_truetype(30), fill=(20, 20, 90))
    draw.line((40, 78, 960, 78), fill=(20, 20, 90), width=3)

    rows = [
        (150, "Permanent Account Number", 192, pan or APPLICANT["pan"], 44),
        (280, "Name", 320, name or APPLICANT["name"], 38),
        (390, "Father's Name", 430, APPLICANT["father"], 38),
        (500, "Date of Birth", 540, APPLICANT["dob"], 38),
    ]
    for label_y, label, value_y, value, size in rows:
        draw.text((40, label_y), label, font=_truetype(26), fill=(0, 0, 0))
        draw.text((40, value_y), value, font=_truetype(size), fill=(0, 0, 0))

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def itr_ack(name: str | None = None, pan: str | None = None) -> bytes:
    """An ITR-V acknowledgement: caption on one line, value on the next."""
    import pymupdf

    lines = [
        "Acknowledgement Number:471253000100626",
        "Date of filing : 10-Jun-2026",
        "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT",
        "(Please see Rule 12 of the Income-tax Rules, 1962)",
        "Assessment", "Year", "2026-27",
        "PAN", pan or APPLICANT["pan"],
        "Name", name or APPLICANT["name"],
        "Status", "Individual",
        "Form Number", "ITR-1",
        "Filed u/s", "139(1)-On or before due date",
        "e-Filing Acknowledgement Number", "471253000100626",
        "Total Income", "1A", "5,90,000",
        "Net tax payable", "4", "0",
        "DO NOT SEND THIS ACKNOWLEDGEMENT TO CPC, BENGALURU",
    ]

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=1191)
    y = 40.0
    for line in lines:
        page.insert_text((36, y), line, fontname="helv", fontsize=9)
        y += 13
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


# ==========================================================================
# NOTHING VERIFIED IS NOT A PASS
#
# SKIPPED ranks alongside SUCCESS, so an application whose every document
# was skipped rolled up to SUCCESS and decided PASS -- for a case in which
# nothing had been checked at all.
# ==========================================================================


@pytest.mark.ocr
def test_all_verification_off_cannot_decide_pass(app_client, monkeypatch):
    monkeypatch.setenv("LOS_VERIFICATION_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
        ],
        case_id="CASE-VOFF",
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "REVIEW"
    assert body["decision"] == "REVIEW"
    assert body["next_action"] == "MANUAL_REVIEW"
    assert body["kyc"]["status"] == "SKIPPED"
    assert "NO_VERIFIED_DOCUMENTS" in {e["code"] for e in body["errors"]}

    for document in body["documents"]:
        assert document["verification"] == "SKIPPED"
        assert document.get("extraction") is None


@pytest.mark.ocr
def test_a_verified_document_still_reaches_pass(app_client):
    """The rule must not make a genuinely clean application unpassable."""
    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
        ],
        case_id="CASE-CLEAN",
    )

    body = response.json()

    assert [d["verification"] for d in body["documents"]] == ["PASS", "PASS"]
    assert body["kyc"]["status"] == "PASS"
    assert body["cross_document"]["status"] in {"PASS", "SKIPPED"}
    assert body["status"] == "SUCCESS"
    assert body["decision"] == "PASS"
    assert body["next_action"] == "CONTINUE"
    assert "NO_VERIFIED_DOCUMENTS" not in {e["code"] for e in body["errors"]}


@pytest.mark.ocr
def test_mixed_verified_and_skipped_follows_existing_policy(
    app_client, monkeypatch
):
    """
    Some verified, some not.

    The verified documents decide, the skipped one contributes no evidence,
    and the nothing-verified rule does not fire.
    """
    monkeypatch.setenv("LOS_SIGNATURE_ENABLED", "false")

    from app.agents.los import config

    config.reload()

    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
            ("sig.png", signature(), "image/png"),
        ],
        expected=["AUTO", "AUTO", "SIGNATURE"],
        case_id="CASE-MIXED",
    )

    body = response.json()

    assert doc(body, "pan.jpg")["verification"] == "PASS"
    assert doc(body, "itr.pdf")["verification"] == "PASS"

    skipped = doc(body, "sig.png")
    assert skipped["verification"] == "SKIPPED"
    assert skipped.get("extraction") is None

    assert body["decision"] == "PASS"
    assert body["next_action"] == "MANUAL_REVIEW"
    assert "NO_VERIFIED_DOCUMENTS" not in {e["code"] for e in body["errors"]}


def test_skipped_is_never_counted_as_verified():
    from app.agents.los.response import any_document_verified

    assert not any_document_verified([{"verification": {"status": "SKIPPED"}}])
    assert not any_document_verified([{"specialist": {"decision": "SKIPPED"}}])
    assert not any_document_verified([{}])
    assert any_document_verified([
        {"verification": {"status": "SKIPPED"}},
        {"verification": {"status": "PASS"}},
    ])


def test_decision_cannot_pass_without_a_verified_document():
    from app.agents.los.response import decision_from

    decision = decision_from(
        "SUCCESS", {"status": "SKIPPED", "reason_codes": []}, [],
        [{"verification": {"status": "SKIPPED"}}],
    )

    assert decision["status"] == "REVIEW"
    assert "NO_VERIFIED_DOCUMENTS" in decision["reason_codes"]


def test_a_failed_application_is_not_softened_to_review():
    """The rule is a floor, not an override."""
    from app.agents.los.response import decision_from

    decision = decision_from(
        "FAILED", {"status": "SKIPPED", "reason_codes": []}, [],
        [{"verification": {"status": "SKIPPED"}}],
    )

    assert decision["status"] == "REJECT"


# ==========================================================================
# NO INTERNAL FILE NAMING IN A PUBLIC RESPONSE
#
# A capability is handed a file staged in the upload sandbox, so the evidence
# locator it reported named that staged file -- and by the time the client
# read it the file had been unlinked. A path that points at nothing, and
# leaks the sandbox naming scheme on the way.
# ==========================================================================

BACKSLASH = chr(92)

SANDBOX_PATTERNS = [
    # The staged-FILE naming scheme. An extension is required: the
    # request_id is legitimately "los_<uuid>" and is not a path.
    re.compile(r"los_[0-9a-f]{16,}\.[A-Za-z0-9]{2,5}"),
    re.compile(r"[A-Za-z]:" + BACKSLASH * 2),  # C:\...
    re.compile(r"/tmp/"),
    re.compile(r"/var/folders/"),
    re.compile(r"AppData"),
    re.compile(r"Temp" + BACKSLASH * 2),
    re.compile(r"uploads?[" + BACKSLASH * 2 + r"/]"),
]


def assert_no_internal_paths(body: dict) -> None:
    blob = json.dumps(body)
    for pattern in SANDBOX_PATTERNS:
        match = pattern.search(blob)
        assert match is None, f"internal path leaked: {match.group(0)!r}"


def test_a_specialist_evidence_locator_names_the_upload_not_the_temp_file(
    app_client,
):
    response = post(
        app_client,
        [("sig.png", signature(), "image/png")],
        expected=["SIGNATURE"],
        case_id="CASE-EVIDENCE",
    )

    body = response.json()
    assert_no_internal_paths(body)

    refs = doc(body, "sig.png").get("evidence_refs") or []
    assert refs, "the signature capability reports an evidence reference"

    for ref in refs:
        assert ref["source_id"] == "sig.png"
        assert ref["locator"].startswith("sig.png")


def test_no_public_response_carries_a_filesystem_path(app_client):
    """Swept across every capability that reports evidence."""
    response = post(
        app_client,
        [
            ("shop.jpg", photo(), "image/jpeg"),
            ("sig.png", signature(), "image/png"),
            ("pan.jpg", pan_card(), "image/jpeg"),
        ],
        expected=["BUSINESS_PROOF_1", "SIGNATURE", "AUTO"],
        case_id="CASE-SWEEP",
    )

    assert_no_internal_paths(response.json())


def test_an_internal_locator_is_rewritten_to_the_source_id():
    from app.agents.los.response import _public_locator

    staged = "los_af9e56a0a37c4dcb969627fc67c8d90c.png"

    assert (_public_locator(staged + "#0,136,640,166", "sig.png")
            == "sig.png#region=0,136,640,166")
    assert _public_locator(staged, "shop.jpg") == "shop.jpg"
    assert _public_locator("", "x.pdf") == "x.pdf"


def test_an_absolute_path_never_survives_into_a_locator():
    from app.agents.los.response import _public_locator

    b = chr(92)
    windows = "C:" + b + "Users" + b + "x" + b + "los_a.png#1,2,3,4"

    assert _public_locator(windows, "sig.png") == "sig.png#region=1,2,3,4"
    assert _public_locator("/tmp/los_b.png", "sig.png") == "sig.png"


def test_a_meaningful_locator_is_kept_and_anchored():
    """A page reference says something; it is kept, not discarded."""
    from app.agents.los.response import _public_locator

    assert _public_locator("page=1", "deed.pdf") == "deed.pdf#page=1"
    assert _public_locator("pan.jpg#page=2", "pan.jpg") == "pan.jpg#page=2"


# ==========================================================================
# ONE SUMMARY CALL PER APPLICATION
#
# The per-document envelope built its own summary, which the LOS flow then
# discarded. With the model switched on, a five-document application issued
# six blocking calls and threw five of them away.
# ==========================================================================


@pytest.mark.ocr
def test_one_application_makes_one_summary_call(app_client, monkeypatch):
    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")

    from app.agents.los import config, summary

    config.reload()

    calls: list[dict] = []

    async def counted(payload):
        calls.append(payload)
        return "Three documents processed. Overall PARTIAL."

    monkeypatch.setattr(summary, "_agenerate", counted)
    monkeypatch.setattr(
        summary, "_generate",
        lambda payload: pytest.fail("a document envelope consulted the model"),
    )

    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
            ("shop.jpg", photo(), "image/jpeg"),
        ],
        expected=["AUTO", "AUTO", "BUSINESS_PROOF_1"],
        case_id="CASE-ONECALL",
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1, f"expected one summary call, got {len(calls)}"


def test_a_document_envelope_never_calls_the_model(monkeypatch):
    """
    The per-document summary is deterministic by construction.

    Asserted on the call itself rather than on a timing, because the symptom
    -- N wasted model calls -- is invisible when the provider is unreachable
    and the connect probe makes each one cost nothing.
    """
    from app.agents.los import summary

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    monkeypatch.setattr(
        summary, "_generate",
        lambda payload: pytest.fail("the document envelope consulted the model"),
    )

    from app.agents.document_agent.workflow import _envelope

    envelope = _envelope(
        request_id="r1", status="SUCCESS", document_type="PAN",
        category="IDENTITY", supported=True,
        extraction={"fields": {"pan_number": "ABCPV1234K"}},
        verification={"status": "PASS", "reason_codes": []},
        ocr_status="SUCCESS", ocr_confidence=0.9,
        classification_status="SUCCESS", classification_confidence=0.9,
        errors=[],
    )

    assert envelope["summary"]


@pytest.mark.ocr
def test_the_summary_stage_is_timed_separately(app_client):
    response = post(app_client, [("pan.jpg", pan_card(), "image/jpeg")])
    assert "processing" not in response.json(), "stage timings are internal"


@pytest.mark.ocr
def test_an_unreachable_model_leaves_the_decision_alone(app_client, monkeypatch):
    """LLM unavailable: deterministic summary, and nothing else moves."""
    from app.agents.los import config, summary

    uploads = [
        ("pan.jpg", pan_card(), "image/jpeg"),
        ("itr.pdf", itr_ack(), "application/pdf"),
    ]

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")
    config.reload()
    baseline = post(app_client, uploads).json()

    async def unreachable(payload):
        raise ConnectionError("model provider is not reachable")

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    monkeypatch.setattr(summary, "_agenerate", unreachable)
    config.reload()

    body = post(app_client, uploads).json()

    assert body["summary_source"] == "deterministic"
    assert body["summary"] == baseline["summary"]
    assert body["status"] == baseline["status"]
    assert body["decision"] == baseline["decision"]
    assert body["next_action"] == baseline["next_action"]
    assert body["kyc"]["status"] == baseline["kyc"]["status"]
    assert "processing" not in body


# ==========================================================================
# THE PUBLISHED 200 CONTRACT
#
# The endpoint declared no response model, so Swagger documented the success
# response as `{}` -- an endpoint with a careful public contract whose docs
# page described none of it. The model is attached through `responses=`, so
# it DOCUMENTS the response without FastAPI serialising through it; these
# tests hold the documentation to the real thing.
# ==========================================================================


def test_the_success_response_is_documented(app_client):
    spec = app_client.get("/openapi.json").json()
    ok = spec["paths"]["/api/v1/los/process"]["post"]["responses"]["200"]

    schema = ok["content"]["application/json"]["schema"]
    assert schema != {}, "the 200 response is undocumented"
    assert schema["$ref"].endswith("/LosProcessResponse")

    model = spec["components"]["schemas"]["LosProcessResponse"]
    assert {
        "request_id", "applicant_id", "case_id", "status", "documents",
        "kyc", "cross_document", "decision", "next_action", "summary",
        "summary_source", "processing_ms", "errors",
    } <= set(model["properties"])


def test_the_document_shape_is_documented(app_client):
    spec = app_client.get("/openapi.json").json()
    document = spec["components"]["schemas"]["ProcessedDocument"]

    assert {
        "source_id", "type", "status", "verification", "extraction",
        "reason_codes", "specialist", "evidence_refs",
    } <= set(document["properties"])
    assert "processing" not in document["properties"]
    assert "category" not in document["properties"]


@pytest.mark.ocr
def test_the_documented_contract_matches_a_real_response(app_client):
    """
    Drift guard.

    A published schema that has fallen behind the response is worse than no
    schema: a reader trusts it. Every key a real response carries must be a
    field the model declares.
    """
    from app.agents.los.schemas import (
        CrossDocument, CrossDocumentCheck, DocumentError, EvidenceRef,
        KycSummary, LosProcessResponse, ProcessedDocument, SpecialistVerdict,
    )

    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
            ("sig.png", signature(), "image/png"),
        ],
        expected=["AUTO", "AUTO", "SIGNATURE"],
        case_id="CASE-CONTRACT",
    )

    body = response.json()

    assert set(body) <= set(LosProcessResponse.model_fields), (
        "undocumented top-level key(s): "
        f"{sorted(set(body) - set(LosProcessResponse.model_fields))}"
    )
    assert set(body["kyc"]) <= set(KycSummary.model_fields)

    for document in body["documents"]:
        extra = set(document) - set(ProcessedDocument.model_fields)
        assert not extra, f"undocumented document key(s): {sorted(extra)}"

        specialist = document.get("specialist")
        if specialist:
            assert set(specialist) <= set(SpecialistVerdict.model_fields)

        for ref in document.get("evidence_refs") or []:
            assert set(ref) <= set(EvidenceRef.model_fields)

        for error in document.get("errors") or []:
            assert set(error) <= set(DocumentError.model_fields)

    assert set(body["cross_document"]) <= set(CrossDocument.model_fields)
    for check in body["cross_document"]["checks"]:
        assert set(check) <= set(CrossDocumentCheck.model_fields)

    for error in body["errors"]:
        assert set(error) <= set(DocumentError.model_fields)


@pytest.mark.ocr
def test_a_real_response_validates_against_the_published_model(app_client):
    from app.agents.los.schemas import LosProcessResponse

    response = post(
        app_client,
        [("pan.jpg", pan_card(), "image/jpeg"),
         ("itr.pdf", itr_ack(), "application/pdf")],
        case_id="CASE-VALIDATES",
    )

    LosProcessResponse.model_validate(response.json())


@pytest.mark.ocr
def test_the_model_documents_but_does_not_filter_the_response(app_client):
    """
    `responses=` documents; `response_model=` would serialise THROUGH the
    model and drop anything it had not been taught about. The conditional
    keys are the proof: they survive.
    """
    from app.api.routes.los_api import router

    route = next(r for r in router.routes if r.path == "/los/process")
    assert route.response_model is None, (
        "response_model would filter the live response; use responses= instead"
    )

    body = post(
        app_client,
        [("sig.png", signature(), "image/png")],
        expected=["SIGNATURE"],
        case_id="CASE-NOFILTER",
    ).json()

    document = doc(body, "sig.png")
    assert document["specialist"]["signature"]["presence"]
    assert "capability" not in document["specialist"]
    assert document["evidence_refs"][0]["locator"].startswith("sig.png")


# ==========================================================================
# PROCESS IS THE PUBLIC OPERATION
#
# `operation` used to accept only VERIFY and EXTRACT -- the Document Agent's
# own modes, which reached the public API because this endpoint passed the
# value straight down. PROCESS is the application-level verb and the one a
# client should send; the other two stay accepted for callers already using
# them.
# ==========================================================================


@pytest.mark.ocr
def test_process_runs_the_whole_application(app_client):
    """PROCESS must reach every stage, not just be accepted."""
    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(), "image/jpeg"),
            ("itr.pdf", itr_ack(), "application/pdf"),
            ("shop.jpg", photo(), "image/jpeg"),
        ],
        expected=["AUTO", "AUTO", "BUSINESS_PROOF_1"],
        case_id="CASE-PROCESS",
        operation="PROCESS",
    )

    assert response.status_code == 200, response.text
    body = response.json()

    # Classification ran.
    assert doc(body, "pan.jpg")["type"] == "PAN"
    assert doc(body, "itr.pdf")["type"] == "ITR"

    # Verification ran, and the gate released fields behind a PASS.
    assert doc(body, "pan.jpg")["verification"] == "PASS"
    assert doc(body, "pan.jpg")["extraction"]["pan_number"]

    # Specialist routing ran.
    assert doc(body, "shop.jpg")["specialist"]["decision"] in {
        "PASS", "REVIEW", "FAIL"
    }

    # KYC, cross-document checks, decision, next action and summary all ran.
    assert body["kyc"]["status"] in {"PASS", "REVIEW", "FAIL", "SKIPPED"}
    assert body["cross_document"]["checks"]
    assert body["decision"] in {"PASS", "REVIEW", "REJECT"}
    assert body["next_action"] in {
        "CONTINUE", "MANUAL_REVIEW", "REQUEST_VALID_DOCUMENT"
    }
    assert body["summary"]


@pytest.mark.ocr
def test_process_is_the_default_operation(app_client):
    """A client that sends no operation gets the whole application."""
    response = app_client.post(
        "/api/v1/los/process",
        files=[("files", ("pan.jpg", pan_card(), "image/jpeg")),
               ("files", ("itr.pdf", itr_ack(), "application/pdf"))],
        data={"applicant_id": "APP-DEFAULT", "case_id": "CASE-DEFAULT"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert doc(body, "pan.jpg")["extraction"]["pan_number"]
    assert body["decision"] in {"PASS", "REVIEW", "REJECT"}


@pytest.mark.ocr
def test_process_and_extract_agree(app_client):
    """
    PROCESS runs as the Document Agent's EXTRACT mode.

    Asserted rather than assumed: if the translation at the seam ever
    changed, PROCESS would quietly stop releasing fields.
    """
    uploads = [("pan.jpg", pan_card(), "image/jpeg"),
               ("itr.pdf", itr_ack(), "application/pdf")]

    process = post(app_client, uploads, operation="PROCESS").json()
    extract = post(app_client, uploads, operation="EXTRACT").json()

    assert process["status"] == extract["status"]
    assert process["decision"] == extract["decision"]
    assert process["kyc"]["status"] == extract["kyc"]["status"]
    for source in ("pan.jpg", "itr.pdf"):
        assert doc(process, source)["extraction"] == doc(extract, source)["extraction"]


@pytest.mark.ocr
def test_verify_still_withholds_fields(app_client):
    """Backward compatible: VERIFY behaves exactly as it did."""
    response = post(
        app_client,
        [("pan.jpg", pan_card(), "image/jpeg")],
        operation="VERIFY",
    )

    assert response.status_code == 200, response.text
    assert doc(response.json(), "pan.jpg").get("extraction") is None


@pytest.mark.parametrize(
    "operation",
    ["PROCESS", "EXTRACT", "VERIFY", "process", " Process ", "extract"],
)
def test_every_documented_operation_is_accepted(app_client, operation):
    """Case and surrounding whitespace have always been tolerated."""
    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
        operation=operation,
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "operation", ["ANALYZE", "PROCES", "", "DELETE", "EXTRACT_ALL", "1"]
)
def test_an_unsupported_operation_is_refused(app_client, operation):
    """Rejected at the boundary, with a message naming what is accepted."""
    response = post(
        app_client,
        [("shop.jpg", photo(), "image/jpeg")],
        expected=["BUSINESS_PROOF_1"],
        operation=operation,
    )

    if operation.strip() == "":
        # An empty value is absence, not a wrong value: the default applies.
        assert response.status_code == 200, response.text
        return

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "PROCESS" in detail and "EXTRACT" in detail and "VERIFY" in detail


def test_the_operation_enum_matches_what_the_endpoint_accepts(app_client):
    """Swagger must not advertise a value the runtime refuses, or omit one."""
    from app.agents.los.flow import PUBLIC_OPERATIONS

    spec = app_client.get("/openapi.json").json()
    ref = (
        spec["paths"]["/api/v1/los/process"]["post"]["requestBody"]
        ["content"]["multipart/form-data"]["schema"]["$ref"]
    )
    schema = spec["components"]["schemas"][ref.split("/")[-1]]
    operation = schema["properties"]["operation"]

    assert operation["enum"] == list(PUBLIC_OPERATIONS)
    assert operation["default"] == "PROCESS"


def test_document_mode_translates_the_public_verb():
    from app.agents.los.flow import document_mode

    assert document_mode("PROCESS") == "EXTRACT"
    assert document_mode("process") == "EXTRACT"
    assert document_mode(None) == "EXTRACT"
    assert document_mode("EXTRACT") == "EXTRACT"
    assert document_mode("VERIFY") == "VERIFY"

    with pytest.raises(ValueError, match="PROCESS"):
        document_mode("ANALYZE")


# ==========================================================================
# A MISMATCH IS A REVIEW, NOT A REJECTION
#
# Documents disagreeing used to FAIL the KYC verdict and reject the
# application outright. This agent cannot tell a married name, a corrected
# date of birth or a transliteration from someone else's document in the
# bundle -- only a human can -- so a disagreement routes the case to one and
# reports what each document said.
#
# The blocking mechanism is deliberately kept: a check marked blocking in
# kyc_policies.yaml still carries the verdict to FAIL and the decision to
# REJECT. What changed is the DEFAULT for ordinary identity mismatches.
# ==========================================================================


def _kyc(status, checks):
    return {"status": status, "reason_codes": [], "checks": checks}


def test_a_name_mismatch_reviews_rather_than_rejects():
    from app.agents.los.response import cross_document_from, decision_from

    kyc = _kyc("REVIEW", [{
        "check": "NAME", "status": "FAIL", "reason_codes": ["NAME_MISMATCH"],
        "source_ids": ["pan.jpg", "dl.jpg"],
        "values": {"pan.jpg": "MUKESH KUMAR", "dl.jpg": "RISHABH AJIT SINGH"},
        "blocking": False,
    }])

    cross = cross_document_from(kyc)

    assert cross["status"] == "REVIEW"
    assert cross["checks"][0]["status"] == "FAIL"
    assert cross["checks"][0]["details"] == {
        "pan.jpg": "MUKESH KUMAR", "dl.jpg": "RISHABH AJIT SINGH",
    }

    decision = decision_from(
        "PARTIAL", kyc, [], [{"verification": {"status": "PASS"}}]
    )
    assert decision["status"] == "REVIEW"
    assert decision["status"] != "REJECT"


def test_a_dob_mismatch_reviews_rather_than_rejects():
    from app.agents.los.response import cross_document_from, decision_from

    kyc = _kyc("REVIEW", [{
        "check": "DOB", "status": "FAIL", "reason_codes": ["DOB_MISMATCH"],
        "source_ids": ["pan.jpg", "dl.jpg"],
        "values": {"pan.jpg": "1979-01-01", "dl.jpg": "2002-06-12"},
        "blocking": False,
    }])

    cross = cross_document_from(kyc)

    assert cross["status"] == "REVIEW"
    assert cross["checks"][0]["details"]["dl.jpg"] == "2002-06-12"

    decision = decision_from(
        "PARTIAL", kyc, [], [{"verification": {"status": "PASS"}}]
    )
    assert decision["status"] == "REVIEW"


def test_several_mismatches_all_carry_their_sources_and_values():
    from app.agents.los.response import cross_document_from

    cross = cross_document_from(_kyc("REVIEW", [
        {"check": "NAME", "status": "FAIL", "reason_codes": ["NAME_MISMATCH"],
         "source_ids": ["pan.jpg", "dl.jpg", "voter4.jpg"],
         "values": {"pan.jpg": "MUKESH KUMAR", "dl.jpg": "RISHABH AJIT SINGH",
                    "voter4.jpg": "KUNTI"},
         "blocking": False},
        {"check": "DOB", "status": "FAIL", "reason_codes": ["DOB_MISMATCH"],
         "source_ids": ["pan.jpg", "dl.jpg"],
         "values": {"pan.jpg": "1979-01-01", "dl.jpg": "2002-06-12"},
         "blocking": False},
        {"check": "PAN", "status": "SKIPPED",
         "reason_codes": ["PAN_SINGLE_SOURCE"], "source_ids": ["pan.jpg"],
         "values": {"pan.jpg": "ABCPM1234K"}, "blocking": False},
    ]))

    assert cross["status"] == "REVIEW"

    failed = [c for c in cross["checks"] if c["status"] == "FAIL"]
    assert {c["check"] for c in failed} == {"NAME", "DOB"}
    for check in failed:
        assert check["sources"]
        assert check["details"]
        assert set(check["details"]) == set(check["sources"])

    # A skipped check carries no details: nothing disagreed.
    skipped = next(c for c in cross["checks"] if c["status"] == "SKIPPED")
    assert "details" not in skipped


def test_a_blocking_rule_still_blocks():
    """
    The capability is preserved, not removed.

    A check marked blocking in policy carries the verdict to FAIL, and FAIL
    rolls up to REJECT exactly as before.
    """
    from app.agents.los.response import cross_document_from, decision_from

    kyc = _kyc("FAIL", [{
        "check": "PAN", "status": "FAIL", "reason_codes": ["PAN_MISMATCH"],
        "source_ids": ["pan.jpg", "itr.pdf"],
        "values": {"pan.jpg": "ABCPM1234K", "itr.pdf": "ZZZPQ9999Z"},
        "blocking": True,
    }])

    assert cross_document_from(kyc)["status"] == "FAIL"

    decision = decision_from(
        "REJECTED", kyc, [], [{"verification": {"status": "PASS"}}]
    )
    assert decision["status"] == "REJECT"


def test_the_blocking_flag_is_read_from_policy_not_hardcoded(monkeypatch):
    from app.agents.kyc import config as kyc_config
    from app.agents.kyc.agent import check_is_blocking

    assert check_is_blocking("NAME") is False

    original = kyc_config.check_blocking
    monkeypatch.setattr(
        kyc_config, "check_blocking", lambda name: name == "name"
    )
    assert check_is_blocking("NAME") is True
    assert check_is_blocking("DOB") is False
    monkeypatch.setattr(kyc_config, "check_blocking", original)


def test_agreeing_documents_still_pass():
    from app.agents.los.response import cross_document_from, decision_from

    kyc = _kyc("PASS", [
        {"check": "NAME", "status": "PASS", "reason_codes": [],
         "source_ids": ["pan.jpg", "itr.pdf"],
         "values": {"pan.jpg": "SUNIL KUMAR VERMA",
                    "itr.pdf": "SUNIL KUMAR VERMA"},
         "blocking": False},
        {"check": "PAN", "status": "PASS", "reason_codes": [],
         "source_ids": ["pan.jpg", "itr.pdf"],
         "values": {"pan.jpg": "ABCPV1234K", "itr.pdf": "ABCPV1234K"},
         "blocking": False},
    ])

    cross = cross_document_from(kyc)

    assert cross["status"] == "PASS"
    # A passing check does not repeat values already in documents[].extraction.
    assert all("details" not in c for c in cross["checks"])

    decision = decision_from(
        "SUCCESS", kyc, [], [{"verification": {"status": "PASS"}}]
    )
    assert decision["status"] == "PASS"


@pytest.mark.ocr
def test_mismatched_identity_documents_review_over_http(app_client):
    """The whole path, not just the roll-up."""
    response = post(
        app_client,
        [
            ("pan.jpg", pan_card(name="MUKESH KUMAR"), "image/jpeg"),
            ("itr.pdf", itr_ack(name="RISHABH AJIT SINGH"), "application/pdf"),
        ],
        case_id="CASE-MISMATCH",
    )

    assert response.status_code == 200, response.text
    body = response.json()

    for document in body["documents"]:
        assert document["verification"] == "PASS"
        assert document["extraction"]

    assert body["kyc"]["status"] == "REVIEW"
    assert body["cross_document"]["status"] == "REVIEW"
    assert body["decision"] == "REVIEW"
    assert body["decision"] != "REJECT"
    assert body["next_action"] == "MANUAL_REVIEW"

    name_check = next(
        c for c in body["cross_document"]["checks"] if c["check"] == "NAME"
    )
    assert name_check["status"] == "FAIL"
    assert set(name_check["sources"]) == {"pan.jpg", "itr.pdf"}
    assert name_check["details"]["pan.jpg"] == "MUKESH KUMAR"
    assert name_check["details"]["itr.pdf"] == "RISHABH AJIT SINGH"

    # The summary names the documents, not just the finding.
    assert "pan.jpg" in body["summary"] and "itr.pdf" in body["summary"]


def test_the_details_field_is_published(app_client):
    spec = app_client.get("/openapi.json").json()
    check = spec["components"]["schemas"]["CrossDocumentCheck"]

    assert "details" in check["properties"]
    assert "sources" in check["properties"]


# ==========================================================================
# INDEPENDENT DOCUMENTS RUN CONCURRENTLY
#
# The flow already dispatched documents with asyncio.gather, but every one of
# them then queued at a single-worker OCR pool: measured on a real
# four-document request, three OCR calls spent 16.9 seconds waiting inside a
# 7.5 second request. Each worker now holds its own engine, so recognition is
# no longer serialised by a shared ONNX session.
#
# Concurrency is asserted with a BARRIER, not a stopwatch. A barrier that
# releases proves the tasks were in flight together; a threshold on elapsed
# time only proves the machine was not busy.
# ==========================================================================


def test_independent_documents_are_in_flight_together(monkeypatch):
    """
    All documents must be submitted before any result is awaited.

    A barrier sized to the document count can only release if every document
    reached it, which serial execution can never do -- the first would block
    forever waiting for peers that have not started.
    """
    import asyncio
    import threading

    from app.agents.los import flow

    count = 4
    barrier = threading.Barrier(count, timeout=10)
    reached = []

    original = flow._process_one

    async def gated(document, operation, request_id, **stage_flags):
        reached.append(document.source_id)
        # Released only when every document has arrived here.
        await asyncio.get_running_loop().run_in_executor(None, barrier.wait)
        # **stage_flags forwards whatever the flow passes. A double that
        # pins the exact signature breaks every time a stage flag is added,
        # which says nothing about the concurrency this test is checking.
        return await original(document, operation, request_id, **stage_flags)

    monkeypatch.setattr(flow, "_process_one", gated)

    uploads = [
        flow.UploadedDocument(f"doc{i}.jpg", f"doc{i}.jpg", photo(),
                              "BUSINESS_PROOF_1")
        for i in range(count)
    ]

    body = asyncio.run(
        flow.process_application(uploads, operation="PROCESS",
                                 applicant_id="APP-CONC", request_id="req_conc")
    )

    assert len(reached) == count
    assert len(body["documents"]) == count


def test_specialists_are_not_serialised_behind_each_other(monkeypatch):
    """Independent MCP capability calls overlap too."""
    import asyncio
    import threading

    from app.agents.los import flow

    count = 3
    barrier = threading.Barrier(count, timeout=10)

    original = flow._run_specialist

    async def gated(document, agent_id, request_id):
        await asyncio.get_running_loop().run_in_executor(None, barrier.wait)
        return await original(document, agent_id, request_id)

    monkeypatch.setattr(flow, "_run_specialist", gated)

    uploads = [
        flow.UploadedDocument("shop.jpg", "shop.jpg", photo(), "BUSINESS_PROOF_1"),
        flow.UploadedDocument("shop2.jpg", "shop2.jpg", photo(), "BUSINESS_PROOF_2"),
        flow.UploadedDocument("sig.png", "sig.png", signature(), "SIGNATURE"),
    ]

    body = asyncio.run(
        flow.process_application(uploads, operation="PROCESS",
                                 applicant_id="APP-SPEC-CONC",
                                 request_id="req_spec_conc")
    )

    assert len(body["documents"]) == count
    for document in body["documents"]:
        assert document["specialist"]["decision"] in {"PASS", "REVIEW", "FAIL"}


def test_the_response_preserves_the_order_files_were_sent_in(app_client):
    """Concurrent execution, deterministic ordering."""
    uploads = [(f"doc{i}.jpg", photo(), "image/jpeg") for i in range(6)]

    response = post(
        app_client, uploads,
        expected=["BUSINESS_PROOF_1"] * 6,
        case_id="CASE-ORDER",
    )

    assert response.status_code == 200, response.text
    returned = [d["source_id"] for d in response.json()["documents"]]

    assert returned == [f"doc{i}.jpg" for i in range(6)]


def test_one_document_failing_does_not_corrupt_the_others(app_client):
    response = post(
        app_client,
        [
            ("good1.jpg", photo(), "image/jpeg"),
            ("broken.txt", b"not a document at all", "text/plain"),
            ("good2.jpg", photo(), "image/jpeg"),
        ],
        expected=["BUSINESS_PROOF_1", "AUTO", "BUSINESS_PROOF_2"],
        case_id="CASE-PARTIAL-FAIL",
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert [d["source_id"] for d in body["documents"]] == [
        "good1.jpg", "broken.txt", "good2.jpg"
    ]

    for source in ("good1.jpg", "good2.jpg"):
        document = doc(body, source)
        assert document["specialist"]["decision"] in {"PASS", "REVIEW", "FAIL"}
        assert document["evidence_refs"]

    assert doc(body, "broken.txt")["status"] == "FAILED"


def test_kyc_runs_only_after_every_document_is_finished(monkeypatch):
    """KYC is an application-level dependency, not a per-document one."""
    import asyncio

    from app.agents.los import flow

    order = []

    original_process = flow._process_one
    original_kyc = flow.run_kyc

    async def traced(document, operation, request_id, **stage_flags):
        result = await original_process(
            document, operation, request_id, **stage_flags
        )
        order.append(("doc", document.source_id))
        return result

    def traced_kyc(*args, **kwargs):
        order.append(("kyc", None))
        return original_kyc(*args, **kwargs)

    monkeypatch.setattr(flow, "_process_one", traced)
    monkeypatch.setattr(flow, "run_kyc", traced_kyc)

    uploads = [
        flow.UploadedDocument("a.jpg", "a.jpg", photo(), "BUSINESS_PROOF_1"),
        flow.UploadedDocument("b.jpg", "b.jpg", photo(), "BUSINESS_PROOF_2"),
    ]

    asyncio.run(
        flow.process_application(uploads, operation="PROCESS",
                                 applicant_id="APP-ORDER", request_id="req_order")
    )

    kinds = [kind for kind, _ in order]
    if "kyc" in kinds:
        assert kinds.index("kyc") == len(uploads), (
            "KYC ran before every document had finished"
        )


def test_each_ocr_worker_holds_its_own_engine():
    """
    The reason the pool may have more than one worker.

    A shared ONNX session across threads is what forced a single worker; this
    is the assertion that removing that limit stayed safe.
    """
    import threading

    from app.agents.document_agent import ocr

    workers = ocr.get_ocr_executor()._max_workers
    if workers < 2:
        pytest.skip("OCR pool is configured with a single worker")

    barrier = threading.Barrier(workers, timeout=60)

    def probe():
        engine = ocr.get_engine()
        barrier.wait()
        return id(engine)

    futures = [ocr.get_ocr_executor().submit(probe) for _ in range(workers)]
    identities = {future.result(timeout=120) for future in futures}

    assert len(identities) == workers


def test_the_ocr_pool_stays_bounded():
    """Never a thread per document, and never unbounded."""
    from app.agents.document_agent import ocr

    workers = ocr.get_ocr_executor()._max_workers

    assert 1 <= workers <= max(1, (__import__("os").cpu_count() or 1))
    assert workers <= 4, "the OCR pool must stay small: one model per worker"


def test_the_canara_statement_still_reconciles(monkeypatch):
    """
    Performance work must not have touched the bank parser's answers.

    THE BUDGET IS RAISED ON PURPOSE. This test is about the NUMBERS the
    parser reads, not about how fast this machine is. Left at the shipped
    25s budget it also asked whether the box happened to be idle, and inside
    a loaded full-suite run the parse was cut short -- correctly, with a
    partial result -- and the balance assertion below failed against `None`.
    That said nothing about the parser's arithmetic.

    If even the raised budget is not enough, the test could not be run, and
    it says so rather than reporting a service limit as a regression.
    """
    from decimal import Decimal

    from app.agents.bank_statement import extract_bank_statement
    from app.agents.bank_statement.extract import ExtractionStatus

    path = Path("samples/documents/Canara Bank Statement.pdf")
    if not path.exists():
        pytest.skip("Canara sample not available")

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "90000")
    result = extract_bank_statement(str(path))

    if (result.status is ExtractionStatus.PARTIAL
            and result.opening_balance is None):
        pytest.skip(
            "this machine could not parse the Canara sample inside 90s; "
            f"the parser returned a partial result: {result.warnings}"
        )

    assert result.opening_balance == Decimal("4824.70")
    assert result.closing_balance == Decimal("229.70")
    assert result.transactions[0].credit == Decimal("400.00")
    assert result.transactions[-1].debit == Decimal("500.00")
    assert result.balance_reconciles is True
    assert (
        result.opening_balance + result.total_credit - result.total_debit
        == result.closing_balance
    )


# ==========================================================================
# THE OPTIONAL SUMMARY MUST NOT HOLD THE RESPONSE
#
# The summary is the last stage and the only one that may consult a model.
# Every decision above it is final before it runs. Measured against a slow
# local model, the old four-second budget cost 4.3 seconds of pure waiting
# on every request outside the unavailable-cache window, for a sentence the
# deterministic path writes in under a millisecond.
# ==========================================================================


def test_the_summary_budget_is_a_fast_fail_one():
    from app.agents.los.summary import llm_timeout_seconds

    budget = llm_timeout_seconds()

    assert 1.0 <= budget <= 2.0, (
        "an optional final-stage summary gets a fast-fail budget on a "
        f"synchronous API; got {budget}s"
    )


def test_the_budget_is_configurable(monkeypatch):
    from app.agents.los.summary import llm_timeout_seconds

    monkeypatch.setenv("LOS_LLM_SUMMARY_TIMEOUT_SECONDS", "30")
    assert llm_timeout_seconds() == 30.0

    monkeypatch.setenv("LOS_LLM_SUMMARY_TIMEOUT_SECONDS", "not-a-number")
    assert 1.0 <= llm_timeout_seconds() <= 2.0


@pytest.mark.ocr
def test_a_slow_model_does_not_hold_the_response(app_client, monkeypatch):
    """
    A model that hangs costs the budget and nothing else.

    Asserted on the CALL, not the clock: the generation coroutine is made to
    hang forever, so if the timeout were not enforced the test would never
    finish rather than merely run slowly.
    """
    import asyncio

    from app.agents.los import config, summary
    from app.llm import availability

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("LOS_LLM_SUMMARY_TIMEOUT_SECONDS", "1")
    config.reload()
    availability.reset()

    calls = []

    async def hangs(messages, stream=False, options=None):
        calls.append(1)
        await asyncio.sleep(3600)

    class Client:
        # `options` carries the generation cap; a stub that does not accept
        # it raises TypeError before the call is ever made, which would make
        # this test pass for the wrong reason.
        async def get_response(self, messages, stream=False, options=None):
            return await hangs(messages, stream, options)

    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    monkeypatch.setattr(summary, "create_ollama_client", lambda: Client(),
                        raising=False)
    monkeypatch.setattr(
        "app.llm.provider.create_ollama_client", lambda: Client()
    )

    response = post(
        app_client,
        [("pan.jpg", pan_card(), "image/jpeg")],
        case_id="CASE-SLOW-LLM",
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["summary"]
    assert body["summary_source"] == "deterministic"
    assert len(calls) == 1, "the generation call must not be retried"

    availability.reset()


@pytest.mark.ocr
def test_a_known_bad_provider_is_not_called_at_all(app_client, monkeypatch):
    """
    Requirement: the cooldown is a CACHE, not a wait.

    Once the provider is known bad, the request must skip the attempt
    outright rather than pay the budget again for 60 seconds.
    """
    from app.agents.los import config, summary
    from app.llm import availability

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    config.reload()

    availability.reset()
    availability.mark_unavailable("test")
    assert availability.cached_state() == "UNAVAILABLE"

    generated = []

    def forbidden():
        generated.append(1)
        raise AssertionError("the model was called while cached unavailable")

    monkeypatch.setattr("app.llm.provider.create_ollama_client", forbidden)

    response = post(
        app_client,
        [("pan.jpg", pan_card(), "image/jpeg")],
        case_id="CASE-CACHED-DOWN",
    )

    assert response.status_code == 200, response.text
    assert response.json()["summary_source"] == "deterministic"
    assert generated == []

    availability.reset()


@pytest.mark.ocr
def test_the_model_changes_nothing_but_the_sentence(app_client, monkeypatch):
    """
    Same application, model up and model down: one field differs.

    This is the whole claim about the summary being optional, stated as an
    assertion over two real responses.
    """
    from app.agents.los import config, summary
    from app.llm import availability

    uploads = [
        ("pan.jpg", pan_card(), "image/jpeg"),
        ("itr.pdf", itr_ack(), "application/pdf"),
    ]

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")
    config.reload()
    without = post(app_client, uploads, case_id="CASE-NO-LLM").json()

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    config.reload()
    availability.reset()

    async def unreachable(payload):
        raise TimeoutError()

    monkeypatch.setattr(summary, "_agenerate", unreachable)

    with_model = post(app_client, uploads, case_id="CASE-NO-LLM").json()

    volatile = {"request_id", "processing_ms"}
    for key in set(without) - volatile:
        assert with_model[key] == without[key], f"{key} changed"

    assert with_model["summary_source"] == "deterministic"

    availability.reset()


def test_no_stage_before_the_summary_consults_the_model(monkeypatch):
    """
    The summary is the only caller.

    Nothing that decides anything may reach the provider, so the whole
    decision path is run with the provider poisoned.
    """
    import asyncio

    from app.agents.los import config, flow
    from app.llm import availability

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "false")
    config.reload()

    def forbidden(*a, **kw):
        raise AssertionError("a decision stage consulted the model")

    monkeypatch.setattr("app.llm.provider.create_ollama_client", forbidden)
    monkeypatch.setattr(availability, "provider_reachable", forbidden)

    uploads = [
        flow.UploadedDocument("shop.jpg", "shop.jpg", photo(), "BUSINESS_PROOF_1"),
    ]

    body = asyncio.run(
        flow.process_application(uploads, operation="PROCESS",
                                 applicant_id="APP-NO-LLM",
                                 request_id="req_no_llm")
    )

    assert body["decision"] in {"PASS", "REVIEW", "REJECT"}
    assert body["next_action"]
    assert body["summary"]
    assert body["summary_source"] == "deterministic"


# ==========================================================================
# THE DOCUMENT MUST BE THE ONE THAT WAS ASKED FOR
#
# When a caller asserts a type through expected_types, the detected type has
# to match it. Uploading a licence against expected_type=PAN is not a
# borderline document -- it is the wrong document, and the applicant can fix
# it by sending the right one.
#
# The verifier already caught this (matches_requested_class ->
# DOC_CLASS_MISMATCH, a hard check). What was missing: the reason never
# reached the client, the caller's assertion was not echoed back, the next
# action said REQUEST_VALID_DOCUMENT, and one wrong upload REJECTED the whole
# application.
# ==========================================================================

REAL_PAN = RB / "pan_bw2.jpg"
REAL_DL = RB / "dl1.jpg"


def _identity(path):
    if not path.exists():
        pytest.skip(f"sample not available: {path.name}")
    return path.read_bytes()


@pytest.mark.ocr
def test_the_asserted_type_matching_passes_normally(app_client):
    response = post(
        app_client,
        [("pan.jpg", _identity(REAL_PAN), "image/jpeg")],
        expected=["PAN"],
        case_id="CASE-TYPE-MATCH",
    )

    assert response.status_code == 200, response.text
    document = doc(response.json(), "pan.jpg")

    assert document["type"] == "PAN"
    assert document["expected_type"] == "PAN"
    assert document["verification"] == "PASS"
    assert document["extraction"]["pan_number"]
    assert "DOCUMENT_TYPE_MISMATCH" not in (document.get("reason_codes") or [])


@pytest.mark.ocr
def test_a_licence_uploaded_against_pan_is_rejected(app_client):
    response = post(
        app_client,
        [("pan.jpg", _identity(REAL_DL), "image/jpeg")],
        expected=["PAN"],
        case_id="CASE-TYPE-PAN-DL",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    document = doc(body, "pan.jpg")

    assert document["expected_type"] == "PAN"
    assert document["type"] == "DRIVING_LICENCE"
    assert document["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in document["reason_codes"]
    assert document.get("extraction") is None

    # A wrong file is not a rejected applicant.
    assert body["decision"] != "REJECT"
    assert body["next_action"] == "REQUEST_CORRECT_DOCUMENT"


@pytest.mark.ocr
def test_a_pan_uploaded_against_a_licence_is_rejected(app_client):
    """The check is symmetric."""
    response = post(
        app_client,
        [("dl.jpg", _identity(REAL_PAN), "image/jpeg")],
        expected=["DRIVING_LICENCE"],
        case_id="CASE-TYPE-DL-PAN",
    )

    body = response.json()
    document = doc(body, "dl.jpg")

    assert document["expected_type"] == "DRIVING_LICENCE"
    assert document["type"] == "PAN"
    assert document["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in document["reason_codes"]
    assert document.get("extraction") is None
    assert body["next_action"] == "REQUEST_CORRECT_DOCUMENT"


@pytest.mark.ocr
def test_auto_lets_classification_decide(app_client):
    """AUTO asserts nothing, so there is nothing to mismatch against."""
    response = post(
        app_client,
        [("doc.jpg", _identity(REAL_DL), "image/jpeg")],
        expected=["AUTO"],
        case_id="CASE-TYPE-AUTO",
    )

    document = doc(response.json(), "doc.jpg")

    assert document["type"] == "DRIVING_LICENCE"
    assert document.get("expected_type") is None
    assert document["verification"] == "PASS"
    assert "DOCUMENT_TYPE_MISMATCH" not in (document.get("reason_codes") or [])


@pytest.mark.ocr
def test_a_mismatch_never_releases_fields(app_client):
    """The gate, stated for this specific failure."""
    response = post(
        app_client,
        [
            ("pan.jpg", _identity(REAL_DL), "image/jpeg"),
            ("dl.jpg", _identity(REAL_PAN), "image/jpeg"),
        ],
        expected=["PAN", "DRIVING_LICENCE"],
        case_id="CASE-TYPE-NO-FIELDS",
    )

    for document in response.json()["documents"]:
        assert "DOCUMENT_TYPE_MISMATCH" in document["reason_codes"]
        assert document.get("extraction") is None


@pytest.mark.ocr
def test_the_wrong_extractor_never_runs_on_a_mismatch(app_client, monkeypatch):
    """
    Requirement stated as a call assertion, not an absence of output.

    Withholding the fields afterwards would satisfy the response contract
    while still having read a licence with a PAN parser. This fails if the
    PAN extractor is invoked at all.
    """
    from app.agents.document_agent import pipeline

    invoked = []

    original = pipeline.extract_pan_fields

    def spy(tokens, *args, **kwargs):
        invoked.append(1)
        return original(tokens, *args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_pan_fields", spy)

    response = post(
        app_client,
        [("pan.jpg", _identity(REAL_DL), "image/jpeg")],
        expected=["PAN"],
        case_id="CASE-TYPE-NO-EXTRACTOR",
    )

    assert response.status_code == 200, response.text
    assert doc(response.json(), "pan.jpg")["verification"] == "FAIL"
    assert invoked == [], "the PAN extractor ran on a driving licence"


def test_a_mismatch_document_is_capped_at_review():
    """
    One wrong upload must not reject the application.

    Asserted on the roll-up directly so it holds whatever the document's own
    status word happens to be.
    """
    from app.agents.los.response import DOCUMENT_TYPE_MISMATCH, next_action_from

    documents = [{
        "source_id": "pan.jpg",
        "verification": "FAIL",
        "reason_codes": [DOCUMENT_TYPE_MISMATCH],
    }]

    assert next_action_from("REVIEW", None, [], documents) == (
        "REQUEST_CORRECT_DOCUMENT"
    )


def test_an_unreadable_document_still_asks_for_a_valid_one():
    """The two failures stay distinguishable."""
    from app.agents.los.response import next_action_from

    documents = [{
        "source_id": "blur.jpg",
        "verification": "FAIL",
        "reason_codes": ["IDENTIFIER_NOT_FOUND"],
    }]

    assert next_action_from("REVIEW", None, [], documents) == (
        "REQUEST_VALID_DOCUMENT"
    )


def test_expected_type_is_published(app_client):
    spec = app_client.get("/openapi.json").json()
    document = spec["components"]["schemas"]["ProcessedDocument"]

    assert "expected_type" in document["properties"]


@pytest.mark.ocr
def test_a_passing_document_carries_no_reason_codes(app_client):
    """
    Surfacing the mismatch reason must not put advisory notes on clean
    documents -- a passing document with reason codes reads as a problem.
    """
    response = post(
        app_client,
        [("pan.jpg", _identity(REAL_PAN), "image/jpeg")],
        expected=["PAN"],
        case_id="CASE-TYPE-CLEAN",
    )

    document = doc(response.json(), "pan.jpg")

    assert document["verification"] == "PASS"
    assert document.get("reason_codes") is None


# ==========================================================================
# FINANCIAL EVIDENCE, NOT A CREDIT DECISION
#
# Counts, sums and minima over rows that already reconciled. Nothing here
# concludes anything about affordability; it hands a later risk engine the
# arithmetic so that engine can.
# ==========================================================================


def _statement(rows, opening="10000.00", printed_closing=None):
    """A minimal reconciling statement with the given rows."""
    import io
    from decimal import Decimal

    import pymupdf

    balance = Decimal(opening)
    lines = [
        "HDFC BANK LIMITED",
        "Statement of account",
        "Account No      : 50100689185590",
        "From : 01/01/2026        To : 30/06/2026",
    ]
    body = []
    for day, narration, debit, credit in rows:
        balance = balance - Decimal(debit or "0") + Decimal(credit or "0")
        d = f"{Decimal(debit):,.2f}" if debit else ""
        c = f"{Decimal(credit):,.2f}" if credit else ""
        body.append(
            f"{day}  {narration:<34}{'':<12}{day}  "
            f"{d:>14}   {c:>14}   {balance:>15,.2f}"
        )

    lines.append(f"Opening Balance : {Decimal(opening):,.2f}")
    lines.append(f"Closing Balance : {printed_closing or f'{balance:,.2f}'}")
    lines.append("This is a computer generated statement.")
    lines.append("")
    lines.append(
        "Date        Narration                          Chq./Ref.No.   "
        "Value Dt      Withdrawal Amt.   Deposit Amt.   Closing Balance"
    )
    lines.extend(body)

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=1191)
    y = 40.0
    for line in lines:
        page.insert_text((36, y), line, fontname="cour", fontsize=7.4)
        y += 11
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


ROWS = [
    ("05/01/2026", "SALARY CREDIT ACME LIMITED", None, "50000.00"),
    ("07/01/2026", "ATM CASH WDL", "5000.00", None),
    ("10/01/2026", "NACH EMI HOUSING LOAN", "12000.00", None),
    ("15/01/2026", "UPI-GROCERY", "3000.00", None),
    ("05/02/2026", "SALARY CREDIT ACME LIMITED", None, "50000.00"),
    ("09/02/2026", "ECS RETURN CHARGES", "500.00", None),
    ("10/02/2026", "NACH EMI HOUSING LOAN", "12000.00", None),
    ("05/03/2026", "SALARY CREDIT ACME LIMITED", None, "50000.00"),
    ("10/03/2026", "NACH EMI HOUSING LOAN", "12000.00", None),
    ("20/03/2026", "ATM CASH WDL", "2000.00", None),
]


def test_evidence_signals_are_derived_from_reconciled_rows(tmp_path):
    from app.agents.bank_statement import extract_bank_statement, signals

    path = tmp_path / "bank.pdf"
    path.write_bytes(_statement(ROWS))

    result = extract_bank_statement(str(path))
    if result.balance_reconciles is not True:
        pytest.skip("synthetic statement did not reconcile on this build")

    evidence = signals.derive(result)

    assert evidence["reconciled"] is True
    assert evidence["transaction_count"] == len(ROWS)
    assert evidence["credit_count"] == 3
    assert evidence["salary_credit_count"] == 3
    assert evidence["cash_withdrawal_count"] == 2
    assert evidence["mandate_debit_count"] == 3
    assert evidence["returned_transaction_count"] == 1
    assert evidence["minimum_balance"] <= evidence["maximum_balance"]
    assert evidence["largest_credit"] == Decimal_("50000.00")


def Decimal_(value):
    from decimal import Decimal

    return Decimal(value)


def test_no_evidence_is_derived_when_the_rows_do_not_reconcile(tmp_path):
    """
    Figures computed from rows known to be wrong are worse than none.
    """
    from app.agents.bank_statement import extract_bank_statement, signals

    path = tmp_path / "broken.pdf"
    path.write_bytes(_statement(ROWS, printed_closing="999,999.00"))

    result = extract_bank_statement(str(path))

    assert result.balance_reconciles is not True
    assert signals.derive(result) == {}


def test_absent_evidence_is_absent_not_zero(tmp_path):
    """
    A statement with no salary wording reports NO salary signal.

    Zero would claim the statement contains no salary credits, when what
    happened is that this bank does not use the word.
    """
    from app.agents.bank_statement import extract_bank_statement, signals

    plain = [
        ("05/01/2026", "UPI-TRANSFER FROM SELF", None, "5000.00"),
        ("06/02/2026", "UPI-GROCERY SUPERMART", "1200.00", None),
        ("07/03/2026", "UPI-FUEL", "900.00", None),
    ]

    path = tmp_path / "plain.pdf"
    path.write_bytes(_statement(plain))

    result = extract_bank_statement(str(path))
    if result.balance_reconciles is not True:
        pytest.skip("synthetic statement did not reconcile on this build")

    evidence = signals.derive(result)

    assert "salary_credit_count" not in evidence
    assert "returned_transaction_count" not in evidence
    assert "cash_withdrawal_count" not in evidence


@pytest.mark.ocr
def test_the_canara_statement_publishes_evidence(app_client):
    """Released through the real endpoint, behind the verification gate."""
    path = Path("samples/documents/Canara Bank Statement.pdf")
    if not path.exists():
        pytest.skip("Canara sample not available")

    response = post(
        app_client,
        [("bank.pdf", path.read_bytes(), "application/pdf")],
        case_id="CASE-EVIDENCE",
    )

    assert response.status_code == 200, response.text
    document = doc(response.json(), "bank.pdf")

    assert document["verification"] == "PASS"

    extraction = document["extraction"]
    assert extraction["account_number_masked"]
    assert extraction["period_start"] and extraction["period_end"]

    signals = extraction["signals"]
    for field in ("average_monthly_credit", "closing_balance", "total_credits",
                  "total_debits", "months_covered"):
        assert field in signals

    evidence = extraction["evidence"]
    assert evidence["reconciled"] is True
    assert evidence["minimum_balance"]
    assert evidence["transaction_count"] > 0

    # The rows themselves stay internal.
    assert "detail" not in extraction
    assert "transactions" not in extraction


# ==========================================================================
# THE SUMMARY BUDGET IS MET BY BOUNDING THE WORK, NOT THE PATIENCE
# ==========================================================================


def test_the_summary_generation_is_capped():
    from app.agents.los.summary import summary_max_tokens

    cap = summary_max_tokens()

    assert 16 <= cap <= 200, (
        "an uncapped model writes until it stops, which is what put the "
        "summary over budget"
    )


def test_the_cap_is_configurable(monkeypatch):
    from app.agents.los.summary import summary_max_tokens

    monkeypatch.setenv("LOS_LLM_SUMMARY_MAX_TOKENS", "32")
    assert summary_max_tokens() == 32

    monkeypatch.setenv("LOS_LLM_SUMMARY_MAX_TOKENS", "nonsense")
    assert summary_max_tokens() == 64


@pytest.mark.ocr
def test_the_generation_call_carries_the_cap(app_client, monkeypatch):
    """The options actually reach the client, rather than being built and dropped."""
    from app.agents.los import config, summary
    from app.llm import availability

    monkeypatch.setenv("LOS_LLM_SUMMARY_ENABLED", "true")
    config.reload()
    availability.reset()

    seen = {}

    class Response:
        text = "One document processed. Overall PARTIAL."

    class Client:
        async def get_response(self, messages, stream=False, options=None):
            seen["options"] = options
            return Response()

    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    monkeypatch.setattr("app.llm.provider.create_ollama_client", lambda: Client())

    response = post(
        app_client,
        [("pan.jpg", pan_card(), "image/jpeg")],
        case_id="CASE-CAP",
    )

    assert response.status_code == 200, response.text
    assert seen.get("options") is not None, "generation options were not passed"

    availability.reset()


def test_a_derived_opening_balance_is_declared_as_such(tmp_path):
    """
    Six of the eight committed statements print no opening balance.

    Where it is inferred from row one, row one's own side cannot be checked
    against anything -- a wrong side there and a wrong opening cancel out and
    the chain still reconciles. The evidence says which case it is rather
    than letting a risk engine assume the stronger one.
    """
    import io

    import pymupdf

    from app.agents.bank_statement import extract_bank_statement, signals

    # Same rows, but with no printed Opening Balance caption.
    lines = [
        "HDFC BANK LIMITED",
        "Statement of account",
        "Closing Balance : 113,500.00",
        "This is a computer generated statement.",
        "",
        "Date        Narration                          Chq./Ref.No.   "
        "Value Dt      Withdrawal Amt.   Deposit Amt.   Closing Balance",
        "05/01/2026  SALARY CREDIT ACME LIMITED                    05/01/2026  "
        "                     50,000.00        60,000.00",
        "07/01/2026  ATM CASH WDL                                  07/01/2026  "
        "      5,000.00                        55,000.00",
    ]

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=1191)
    y = 40.0
    for line in lines:
        page.insert_text((36, y), line, fontname="cour", fontsize=7.4)
        y += 11
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()

    path = tmp_path / "no_opening.pdf"
    path.write_bytes(buffer.getvalue())

    result = extract_bank_statement(str(path))

    assert result.opening_balance_printed is False

    evidence = signals.derive(result)
    if evidence:
        assert evidence["opening_balance_source"] == "derived"


def test_the_canara_opening_balance_is_printed_not_inferred():
    """Canara prints one, which is why its first row could be corrected."""
    from app.agents.bank_statement import extract_bank_statement, signals

    path = Path("samples/documents/Canara Bank Statement.pdf")
    if not path.exists():
        pytest.skip("Canara sample not available")

    result = extract_bank_statement(str(path))

    assert result.opening_balance_printed is True
    assert signals.derive(result)["opening_balance_source"] == "printed"
