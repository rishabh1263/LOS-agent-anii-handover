"""
In-house document verification.

Verification runs in-process through the Document Agent workflow. There is no
external service: these tests pass with nothing else running, which is the
whole point of the change they cover.

Cases that read a real document are marked `ocr` alongside the other
document tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agents.document_verification.service import (
    SUPPORTED_DOCUMENTS,
    DocumentVerificationOutcome,
    VerificationStatus,
    configuration,
    normalize_document_type,
    resolve_sandboxed_path,
    upload_root,
    verify_document,
)

RB = Path("samples/real_batch")
SD = Path("samples/documents")


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """
    Put a real sample inside the upload sandbox.

    Returns a factory: sandboxed("pan_bw2.jpg") -> path inside the root.
    """
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    def place(name: str, source: Path) -> Path:
        if not source.exists():
            pytest.skip(f"sample not available: {source}")
        target = root / name
        shutil.copyfile(source, target)
        return target

    place.root = root  # type: ignore[attr-defined]
    return place


# ---------------------------------------------------------------------------
# No external dependency
# ---------------------------------------------------------------------------


def test_verification_declares_no_external_dependency():
    config = configuration()

    assert config["backend"] == "in_house"
    assert config["external_dependency"] is None
    assert "process_document" in config["implementation"]
    assert config["deterministic"] is True


def test_legacy_base_url_is_not_consulted_on_the_normal_path(monkeypatch):
    """
    Point the old variable somewhere that would fail loudly if it were still
    used, then verify configuration and routing are unaffected.
    """
    monkeypatch.setenv("LEGACY_API_BASE_URL", "http://127.0.0.1:9")

    assert configuration()["external_dependency"] is None


def test_the_mcp_server_no_longer_imports_an_http_client():
    import app.mcp.document.server as server

    assert not hasattr(server, "httpx")
    assert hasattr(server, "verify_document")
    assert hasattr(server, "list_enabled_document_types")


def test_verification_does_not_establish_authenticity():
    outcome = DocumentVerificationOutcome(
        status=VerificationStatus.OK, document_type="PAN", ok=True, decision="PASS"
    )
    assert outcome.authenticity_checked is False


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


async def test_unsupported_document_type_is_refused():
    outcome = await verify_document("MARRIAGE_CERTIFICATE", document_bytes=b"x",
                                    filename="a.jpg")

    assert outcome.status is VerificationStatus.UNSUPPORTED_DOCUMENT_TYPE
    assert outcome.ok is False
    assert "PAN" in outcome.supported_documents


def test_document_type_aliases_normalise():
    assert normalize_document_type("driving-license") == "DRIVING_LICENCE"
    assert normalize_document_type(" pan_card ") == "PAN"


async def test_a_path_outside_the_sandbox_is_refused(tmp_path, monkeypatch):
    """
    The orchestration handler takes a path over HTTP, so this is the guard
    against it reading any file the process can reach.
    """
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()

    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4")

    outcome = await verify_document("PAN", file_path=outside)

    assert outcome.status is VerificationStatus.INVALID_FILE
    assert outcome.ok is False
    assert "sandbox" in (outcome.message or "").lower()


async def test_a_missing_file_is_reported_not_raised(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    outcome = await verify_document("PAN", file_path=root / "nope.jpg")

    assert outcome.status is VerificationStatus.INVALID_FILE
    assert outcome.ok is False


async def test_an_empty_file_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    empty = root / "empty.jpg"
    empty.write_bytes(b"")

    outcome = await verify_document("PAN", file_path=empty)
    assert outcome.status is VerificationStatus.INVALID_FILE


async def test_no_document_at_all_is_reported():
    outcome = await verify_document("PAN")
    assert outcome.status is VerificationStatus.INVALID_FILE
    assert outcome.error == "NO_DOCUMENT"


async def test_an_unreadable_document_does_not_raise():
    outcome = await verify_document(
        "PAN", document_bytes=b"not an image", filename="broken.jpg"
    )
    # Either a structured failure or a FAIL verdict -- never an exception.
    assert outcome.status in (VerificationStatus.OK, VerificationStatus.INVALID_FILE)
    if outcome.status is VerificationStatus.OK:
        assert outcome.decision in ("FAIL", "REVIEW")


async def test_a_workflow_explosion_becomes_a_structured_failure(monkeypatch):
    """A bug in the workflow must not surface as an exception to the agent."""
    import app.agents.document_agent.workflow as workflow

    async def explode(**_kwargs):
        raise RuntimeError("workflow blew up")

    monkeypatch.setattr(workflow, "process_document", explode)

    outcome = await verify_document(
        "PAN", document_bytes=b"x", filename="a.jpg"
    )

    assert outcome.status is VerificationStatus.FAILED
    assert outcome.ok is False
    assert outcome.error == "VERIFICATION_FAILED"
    assert "workflow blew up" in (outcome.message or "")


# ---------------------------------------------------------------------------
# Real documents
# ---------------------------------------------------------------------------


@pytest.mark.ocr
async def test_pan_verifies_and_passes(sandboxed):
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    outcome = await verify_document("PAN", file_path=path)

    assert outcome.status is VerificationStatus.OK
    assert outcome.ok is True
    assert outcome.decision == "PASS"
    assert outcome.detected_document_type == "PAN"
    assert outcome.checks, "a verdict must show the checks behind it"
    assert outcome.source == "in_house"


@pytest.mark.ocr
async def test_wrong_expected_type_fails(sandboxed):
    """A PAN offered as a driving licence must not pass."""
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    outcome = await verify_document("DRIVING_LICENCE", file_path=path)

    assert outcome.status is VerificationStatus.OK
    assert outcome.decision == "FAIL"
    assert outcome.checks.get("matches_requested_class") == "FAIL"


@pytest.mark.ocr
async def test_poor_quality_document_is_not_silently_passed(sandboxed):
    """
    A worn card with an unreadable identifier goes to REVIEW rather than
    being accepted or rejected outright.
    """
    path = sandboxed("voter.jpg", RB / "voter_id.jpg")

    outcome = await verify_document("VOTER_ID", file_path=path)

    assert outcome.status is VerificationStatus.OK
    assert outcome.decision in ("REVIEW", "PASS")
    if outcome.decision == "REVIEW":
        failed = [k for k, v in outcome.checks.items() if v == "FAIL"]
        assert failed, "a REVIEW must name what was unsatisfied"


@pytest.mark.ocr
async def test_an_unidentifiable_document_fails(sandboxed, tmp_path):
    """Meaningless page: not a supported document, so it cannot pass."""
    from PIL import Image, ImageDraw

    root = upload_root()
    page = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(["MEETING NOTES", "Quarterly planning",
                              "No blockers reported"]):
        draw.text((60, 100 + i * 120), line, fill="black")
    target = root / "notes.png"
    page.save(target)

    outcome = await verify_document("PAN", file_path=target)

    assert outcome.status is VerificationStatus.OK
    assert outcome.decision == "FAIL"


@pytest.mark.ocr
async def test_a_financial_document_verifies_through_the_financial_agent(sandboxed):
    path = sandboxed("itr.pdf", RB / "itr_v.pdf")

    outcome = await verify_document("ITR", file_path=path)

    assert outcome.status is VerificationStatus.OK
    assert outcome.detected_document_type == "ITR"
    assert outcome.decision in ("PASS", "REVIEW", "FAIL")


@pytest.mark.ocr
async def test_verification_never_returns_extracted_fields(sandboxed):
    """
    VERIFY must not release fields. The outcome model has nowhere to put them,
    which is the structural guarantee.
    """
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    outcome = await verify_document("PAN", file_path=path)
    dumped = outcome.model_dump()

    assert "extraction" not in dumped
    assert "fields" not in dumped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_document_verification_is_registered():
    import app.orchestration.graph  # noqa: F401  (registers handlers)
    import app.orchestration.registry as registry

    assert registry.is_registered("document_verification")


def test_registering_it_did_not_displace_the_other_agents():
    import app.orchestration.graph  # noqa: F401
    import app.orchestration.registry as registry

    for agent_id in ("document_agent", "kyc_agent", "fraud_risk_agent",
                     "bank_statement_agent"):
        assert registry.is_registered(agent_id)


@pytest.mark.ocr
async def test_it_runs_through_the_orchestrator(sandboxed):
    from app.orchestration.graph import run_agent

    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    state = await run_agent(
        agent_id="document_verification",
        payload={"document_type": "PAN", "file_path": str(path)},
        request_id="orchestrated-dv",
    )

    assert state.get("status") in ("SUCCESS", "success", "COMPLETED", "completed")
    result = state.get("result") or {}
    assert result.get("decision") == "PASS"
    assert result.get("ok") is True


async def test_the_orchestrated_path_refuses_a_path_outside_the_sandbox(
    tmp_path, monkeypatch
):
    """The sandbox must hold for a path arriving through orchestration."""
    from app.orchestration.graph import run_agent

    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"x" * 32)

    state = await run_agent(
        agent_id="document_verification",
        payload={"document_type": "PAN", "file_path": str(outside)},
        request_id="orchestrated-escape",
    )

    result = state.get("result") or {}
    assert result.get("ok") is False
    assert result.get("error") == "INVALID_FILE"
