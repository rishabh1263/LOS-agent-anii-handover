"""
End-to-end: specialist capabilities through the public LOS entry point.

The path under test is the whole one:

    client -> POST /api/v1/los/process -> LOS flow -> orchestrator
           -> registered handler -> capability -> evidence in the response

The point of these tests is that a client uploading a shop photo, a bank sign
card or a sale deed alongside its PAN gets all of them assessed in ONE call.
If any capability needed a second request to become part of the case, it
would be an orphan however well its own unit tests pass.

FIXTURES ARE SYNTHETIC except the sale deed, which is a real scanned sample
from samples/real_batch. See the capability suites for what that means.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest

RB = Path("samples/real_batch")


# ==========================================================================
# SYNTHETIC UPLOAD BUILDERS
# ==========================================================================


def photo_bytes(size: tuple[int, int] = (1280, 960)) -> bytes:
    """A SYNTHETIC premises photograph with enough detail to be in focus."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (210, 210, 210))
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 24):
        draw.line([(x, 0), (x, size[1])], fill=(10, 10, 10), width=3)
    for y in range(0, size[1], 24):
        draw.line([(0, y), (size[0], y)], fill=(10, 10, 10), width=3)

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def sign_card_bytes(phase: float = 0.0) -> bytes:
    """A SYNTHETIC bank sign card."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (640, 240), 250)
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 630, 230), outline=120)
    points = [
        (60 + i * 18, 120 + int(46 * math.sin(i * 0.7 + phase))) for i in range(32)
    ]
    draw.line(points, fill=0, width=8)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def deed_bytes() -> bytes:
    source = RB / "sale_deed_test.pdf"
    if not source.exists():
        pytest.skip("sale deed sample not available")
    return source.read_bytes()


@pytest.fixture(autouse=True)
def upload_sandbox(tmp_path, monkeypatch):
    """
    Give the flow a writable sandbox.

    The specialists take paths and refuse anything outside the upload root,
    so the flow stages bytes inside it. Pointing that at tmp_path keeps the
    test from writing into the developer's real runtime directory.
    """
    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    return root


def post_los(client, files, expected_types, operation="EXTRACT"):
    return client.post(
        "/api/v1/los/process",
        files=files,
        data={
            "operation": operation,
            "applicant_id": "APP-E2E",
            "expected_types": ",".join(expected_types),
        },
    )


def document_for(body: dict, source_id: str) -> dict:
    return next(d for d in body["documents"] if d["source_id"] == source_id)


# ==========================================================================
# BUSINESS PROOF 1 AND 2
# ==========================================================================


@pytest.mark.parametrize("slot", ["BUSINESS_PROOF_1", "BUSINESS_PROOF_2"])
def test_business_proof_reaches_the_case_through_los_process(app_client, slot):
    """Both slots, one call, no separate Business Proof API."""
    response = post_los(
        app_client,
        [("files", ("shop.jpg", photo_bytes(), "image/jpeg"))],
        [slot],
    )

    assert response.status_code == 200, response.text
    body = response.json()

    document = document_for(body, "shop.jpg")
    assert document["type"] == slot
    assert document["specialist"]["decision"] in {"PASS", "REVIEW", "FAIL"}
    assert document["status"] in ("PASS", "REVIEW", "FAIL")
    assert document["evidence_refs"]


def test_business_proof_never_claims_the_business_exists(app_client):
    response = post_los(
        app_client,
        [("files", ("shop.jpg", photo_bytes(), "image/jpeg"))],
        ["BUSINESS_PROOF_1"],
    )

    specialist = document_for(response.json(), "shop.jpg")["specialist"]

    assert specialist.get("business_existence_verified") is False
    assert specialist.get("ownership_verified") is False
    assert "BUSINESS_EXISTENCE_NOT_ESTABLISHED" in specialist["reason_codes"]


# ==========================================================================
# BANK SIGNATURE
# ==========================================================================


def test_bank_signature_reaches_the_case_through_los_process(app_client):
    response = post_los(
        app_client,
        [("files", ("bank.png", sign_card_bytes(), "image/png"))],
        ["BANK_SIGNATURE"],
    )

    assert response.status_code == 200, response.text
    document = document_for(response.json(), "bank.png")

    assert document["type"] == "BANK_SIGNATURE"
    specialist = document["specialist"]
    # No reference travelled with the upload, so the honest answer is REVIEW.
    # The four signature answers live in one nested block in the public
    # response rather than being spread across the envelope.
    assert specialist["signature"]["comparison"] == "NOT_COMPARABLE"
    assert document["status"] == "REVIEW"
    assert specialist.get("authenticity_verified") is False


# ==========================================================================
# STANDALONE SIGNATURE UPLOAD
# ==========================================================================


def standalone_signature_bytes(phase: float = 0.0) -> bytes:
    """
    A SYNTHETIC standalone signature with scan grain.

    Grain is deliberate: a flat rendering trips the synthetic-risk heuristic,
    which is the heuristic working but would make this test about the wrong
    thing.
    """
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(23)
    image = Image.new("L", (640, 240), 250)
    draw = ImageDraw.Draw(image)
    draw.line(
        [(60 + i * 18, 120 + int(46 * math.sin(i * 0.7 + phase))) for i in range(30)],
        fill=20,
        width=8,
    )

    pixels = image.load()
    for _ in range((640 * 240) // 6):
        x = rng.randrange(640)
        y = rng.randrange(240)
        pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-18, 18)))

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("expected", ["SIGNATURE", "STANDALONE_SIGNATURE"])
def test_a_standalone_signature_reaches_the_case_through_los_process(
    app_client, expected
):
    """
    No separate signature API.

    "SIGNATURE" is the name a caller naturally uses; it must route to the
    same capability as the explicit type.
    """
    response = post_los(
        app_client,
        [("files", ("sig.png", standalone_signature_bytes(), "image/png"))],
        [expected],
    )

    assert response.status_code == 200, response.text
    document = document_for(response.json(), "sig.png")
    specialist = document["specialist"]

    assert document["type"] == expected
    assert specialist["signature"]["present"] is True

    # No reference travelled with the upload, so PASS is not available.
    assert specialist["signature"]["comparison"] == "NOT_COMPARABLE"
    assert document["status"] == "REVIEW"
    assert specialist.get("authenticity_verified") is False


def test_a_blank_signature_upload_fails_through_los_process(app_client):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (640, 240), 255).save(buffer, "PNG")

    response = post_los(
        app_client,
        [("files", ("blank.png", buffer.getvalue(), "image/png"))],
        ["SIGNATURE"],
    )

    assert response.status_code == 200
    document = document_for(response.json(), "blank.png")

    assert document["status"] == "FAIL"
    assert "SIGNATURE_BLANK" in document["specialist"]["reason_codes"]


async def test_the_signature_mcp_tool_handles_a_standalone_upload(
    tmp_path, monkeypatch
):
    from app.mcp import capabilities

    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    path = root / "sig.png"
    path.write_bytes(standalone_signature_bytes())

    envelope = await capabilities.signature_verify(
        str(path), "STANDALONE_SIGNATURE", "", "sig-1", "REQ-MCP"
    )

    assert envelope.ok is True
    assert envelope.result["input_mode"] == "STANDALONE_SIGNATURE"
    assert envelope.result["signature"]["comparison"] == "NOT_COMPARABLE"


# ==========================================================================
# SALE DEED
# ==========================================================================


@pytest.mark.ocr
def test_sale_deed_reaches_the_case_through_los_process(app_client):
    """Classify -> extract -> verify -> evidence, in the one LOS call."""
    response = post_los(
        app_client,
        [("files", ("deed.pdf", deed_bytes(), "application/pdf"))],
        ["SALE_DEED"],
    )

    assert response.status_code == 200, response.text
    document = document_for(response.json(), "deed.pdf")

    assert document["type"] == "SALE_DEED"
    specialist = document["specialist"]
    assert specialist["decision"] in {"PASS", "REVIEW", "FAIL"}
    assert specialist.get("ownership_verified") is False
    assert document["evidence_refs"]


# ==========================================================================
# MIXED APPLICATION
# ==========================================================================


@pytest.mark.ocr
def test_one_application_carries_every_evidence_type(app_client):
    """
    The case is complete after a single request.

    A mixed upload is the real shape of an application, and it is where a
    specialist that quietly failed to route would show up.
    """
    response = post_los(
        app_client,
        [
            ("files", ("shop1.jpg", photo_bytes(), "image/jpeg")),
            ("files", ("shop2.jpg", photo_bytes(), "image/jpeg")),
            ("files", ("bank.png", sign_card_bytes(), "image/png")),
            ("files", ("deed.pdf", deed_bytes(), "application/pdf")),
        ],
        ["BUSINESS_PROOF_1", "BUSINESS_PROOF_2", "BANK_SIGNATURE", "SALE_DEED"],
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["documents"]) == 4
    verdicts = {
        d["source_id"]: (d.get("specialist") or {}).get("decision")
        for d in body["documents"]
    }
    assert set(verdicts) == {"shop1.jpg", "shop2.jpg", "bank.png", "deed.pdf"}
    for source, decision in verdicts.items():
        assert decision in {"PASS", "REVIEW", "FAIL"}, source

    for document in body["documents"]:
        assert document.get("errors", []) == [], document["source_id"]
        assert document["evidence_refs"], document["source_id"]


def test_specialist_uploads_do_not_disturb_the_application_envelope(app_client):
    """Backward compatibility: the response shape callers already parse."""
    response = post_los(
        app_client,
        [("files", ("shop.jpg", photo_bytes(), "image/jpeg"))],
        ["BUSINESS_PROOF_1"],
    )

    body = response.json()
    for key in ("request_id", "applicant_id", "status", "documents", "kyc",
                "cross_document", "processing_ms", "errors"):
        assert key in body, key


# ==========================================================================
# ERRORS AND EDGE CASES
# ==========================================================================


def test_an_unusable_business_photo_is_reported_not_crashed(app_client):
    """A PDF in the Business Proof slot is wrong, and must fail cleanly."""
    response = post_los(
        app_client,
        [("files", ("notaphoto.pdf", b"%PDF-1.4 nonsense", "application/pdf"))],
        ["BUSINESS_PROOF_1"],
    )

    assert response.status_code == 200
    document = document_for(response.json(), "notaphoto.pdf")

    assert document["status"] == "FAIL"
    assert "UNSUPPORTED_FILE_TYPE" in document["specialist"]["reason_codes"]


def test_a_tiny_image_goes_to_review_not_pass(app_client):
    response = post_los(
        app_client,
        [("files", ("tiny.jpg", photo_bytes((120, 90)), "image/jpeg"))],
        ["BUSINESS_PROOF_1"],
    )

    document = document_for(response.json(), "tiny.jpg")

    assert document["status"] == "REVIEW"
    assert "IMAGE_TOO_SMALL" in document["specialist"]["reason_codes"]


def test_a_specialist_failure_does_not_fail_the_application(
    app_client, monkeypatch
):
    """One bad document must not take the whole case with it."""
    from app.agents.los import flow

    async def explode(*args, **kwargs):
        raise RuntimeError("capability exploded")

    monkeypatch.setattr(flow, "run_agent", explode, raising=False)

    response = post_los(
        app_client,
        [
            ("files", ("shop.jpg", photo_bytes(), "image/jpeg")),
            ("files", ("pan.jpg", photo_bytes(), "image/jpeg")),
        ],
        ["BUSINESS_PROOF_1", "AUTO"],
    )

    assert response.status_code == 200
    assert len(response.json()["documents"]) == 2


def test_an_unknown_expected_type_falls_through_to_the_document_agent(app_client):
    """
    Routing must not swallow types it does not own.

    Anything not in the specialist table has to keep reaching the Document
    Agent, or adding a capability would quietly break ordinary documents.
    """
    response = post_los(
        app_client,
        [("files", ("thing.jpg", photo_bytes(), "image/jpeg"))],
        ["SOMETHING_UNKNOWN"],
    )

    assert response.status_code == 200
    document = document_for(response.json(), "thing.jpg")
    assert document.get("specialist") is None


# ==========================================================================
# SECURITY
# ==========================================================================


def test_los_process_requires_authentication(unauthenticated_client):
    response = unauthenticated_client.post(
        "/api/v1/los/process",
        files=[("files", ("shop.jpg", photo_bytes(), "image/jpeg"))],
        data={"expected_types": "BUSINESS_PROOF_1"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "capability,args",
    [
        ("business_evidence_analyze", ("BUSINESS_PROOF_1",)),
        ("signature_verify", ("BANK_SIGNATURE",)),
        ("sale_deed_analyze", ()),
    ],
)
async def test_no_specialist_mcp_tool_reads_outside_the_sandbox(
    capability, args, tmp_path, monkeypatch
):
    from app.mcp import capabilities as mcp_capabilities
    from app.mcp.errors import ToolStatus

    # The autouse sandbox fixture has already made this directory.
    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(photo_bytes((600, 400)))

    func = getattr(mcp_capabilities, capability)
    envelope = await func(str(outside), *args)

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH


async def test_a_signature_reference_path_is_sandboxed_too(tmp_path, monkeypatch):
    """
    The second path is a path too.

    An unchecked reference argument would reopen arbitrary file read through
    the back door while the subject path looked properly guarded.
    """
    from app.mcp import capabilities as mcp_capabilities
    from app.mcp.errors import ToolStatus

    # The autouse sandbox fixture has already made this directory.
    root = tmp_path / "uploads"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    inside = root / "card.png"
    inside.write_bytes(sign_card_bytes())
    outside = tmp_path / "reference.png"
    outside.write_bytes(sign_card_bytes())

    envelope = await mcp_capabilities.signature_verify(
        str(inside), "BANK_SIGNATURE", str(outside)
    )

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH


# ==========================================================================
# ORCHESTRATION REACHABILITY
# ==========================================================================


@pytest.mark.parametrize(
    "agent_id", ["business_evidence", "signature_verification", "sale_deed"]
)
def test_every_new_capability_is_registered_and_executable(agent_id):
    """No dead registry entries: each must resolve to a real handler."""
    from app.orchestration import registry

    assert agent_id in registry.registered_agents()
    handler, config = registry.resolve(agent_id)
    assert config.enabled is True
    assert callable(handler)


async def test_the_specialist_mcp_tools_are_registered():
    from app.mcp.server import TOOLS, mcp

    names = {t.name for t in await mcp.list_tools()}
    for tool in (
        "business_evidence.analyze",
        "signature.verify",
        "sale_deed.analyze",
    ):
        assert tool in names, tool
        assert tool in TOOLS, tool
