"""
MCP tool layer.

The point of these tests is not that the tools return something. It is that
the layer stays THIN: that it forwards to the existing services, that it never
computes a verdict of its own, and that it cannot be talked into reading a
file outside the upload sandbox.

The OCR-backed success cases carry the `ocr` marker, in line with the rest of
the suite, because they run a real extraction pass over a real sample.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from app.mcp import capabilities
from app.mcp.errors import ToolStatus

RB = Path("samples/real_batch")
SD = Path("samples/documents")


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """
    Put a real sample inside the upload sandbox.

    Mirrors the fixture in tests/agents/test_document_verification_service.py
    so the MCP layer is exercised against the same sandbox the service uses.
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


def kyc_payload() -> dict:
    """A KYC request whose documents agree with each other."""
    return {
        "applicant_id": "APP-1",
        "documents": [
            {
                "source_id": "pan",
                "document_type": "PAN",
                "name": "RISHABH AJIT SINGH",
                "date_of_birth": "2002-06-12",
                "pan": "NUHPS4875K",
            },
            {
                "source_id": "itr",
                "document_type": "ITR",
                "name": "RISHABH AJIT SINGH",
                "pan": "NUHPS4875K",
                "income": {"declared_annual_income": "960000"},
            },
        ],
    }


# ==========================================================================
# TOOL SURFACE
# ==========================================================================


async def test_the_declared_capabilities_are_registered():
    """
    The advertised surface is a contract; a model picks tools from it.

    Asserted against TOOLS rather than a second hardcoded list, so adding a
    capability is one edit rather than two -- and so a tool registered but
    left out of TOOLS (or the reverse) still fails here.
    """
    from app.mcp.server import TOOLS, mcp

    registered = sorted(t.name for t in await mcp.list_tools())

    assert registered == sorted(TOOLS)

    # The capabilities that must not silently disappear.
    for required in (
        "document.verify",
        "document.extract",
        "financial.analyze",
        "kyc.run",
        "sale_deed.analyze",
    ):
        assert required in registered, required


async def test_every_tool_declares_a_typed_input_schema():
    """An untyped tool is one a model calls wrongly."""
    from app.mcp.server import mcp

    for tool in await mcp.list_tools():
        schema = tool.inputSchema
        assert schema.get("type") == "object", tool.name
        assert schema.get("properties"), f"{tool.name} declares no parameters"
        assert tool.description, f"{tool.name} has no description"


# ==========================================================================
# SUCCESS PATHS
# ==========================================================================


@pytest.mark.ocr
async def test_document_verify_returns_the_service_verdict(sandboxed):
    """The verdict is the workflow's, carried through untouched."""
    from app.agents.document_verification.service import verify_document

    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    envelope = await capabilities.document_verify("PAN", str(path))
    direct = await verify_document("PAN", file_path=str(path))

    assert envelope.ok is True
    assert envelope.status is ToolStatus.OK
    assert envelope.result["decision"] == direct.decision
    assert envelope.result["decision"] in {"PASS", "REVIEW", "FAIL", "SKIPPED"}
    assert envelope.result["checks"], "a verdict must show its checks"
    assert envelope.result["source"] == "in_house"


@pytest.mark.ocr
async def test_document_extract_returns_fields_and_no_verdict(sandboxed):
    """Extraction reports what it read. Deciding is a different capability."""
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    envelope = await capabilities.document_extract("PAN", str(path))

    assert envelope.ok is True
    assert envelope.status is ToolStatus.OK
    assert "decision" not in envelope.result


@pytest.mark.ocr
async def test_financial_analyze_returns_income_signals(sandboxed):
    path = sandboxed("statement.pdf", SD / "sbi_wa.pdf")

    envelope = await capabilities.financial_analyze(str(path))

    assert envelope.ok is True
    assert envelope.status is ToolStatus.OK
    assert "document_type" in envelope.result
    assert "status" in envelope.result


async def test_kyc_run_returns_the_agents_status():
    """KYC needs no file, so this costs nothing and runs everywhere."""
    from app.agents.kyc.agent import run_kyc
    from app.agents.kyc.schemas import KycRequest

    envelope = await capabilities.kyc_run(kyc_payload())
    direct = run_kyc(KycRequest.model_validate(kyc_payload()))

    assert envelope.ok is True
    assert envelope.status is ToolStatus.OK
    assert envelope.result["status"] == direct.status.value
    assert envelope.result["checks"], "a status must show the checks behind it"


async def test_document_get_reports_metadata_without_reading_the_file(sandboxed):
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    envelope = await capabilities.document_get(str(path))

    assert envelope.ok is True
    assert envelope.result["filename"] == "pan.jpg"
    assert envelope.result["size_bytes"] > 0
    assert envelope.result["content_read"] is False


async def test_policy_get_reads_the_live_policy():
    from app.agents.fraud_risk.config import get_policy

    envelope = await capabilities.policy_get("risk")

    assert envelope.ok is True
    assert envelope.result["content"] == get_policy()


# ==========================================================================
# INVALID INPUT
# ==========================================================================


@pytest.mark.parametrize("bad", ["", "   "])
async def test_an_empty_document_type_is_rejected(bad):
    envelope = await capabilities.document_verify(bad, "x.jpg")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.INVALID_INPUT
    assert envelope.error.code == "INVALID_INPUT"


async def test_an_empty_file_path_is_rejected():
    envelope = await capabilities.document_verify("PAN", "")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.INVALID_INPUT


async def test_a_malformed_kyc_request_is_rejected_with_field_errors():
    """A model that sent the wrong shape needs to know which field."""
    envelope = await capabilities.kyc_run({"documents": []})

    assert envelope.ok is False
    assert envelope.status is ToolStatus.INVALID_INPUT
    assert envelope.error.context["errors"], "the failing field must be named"


async def test_an_unknown_policy_is_reported_with_the_available_ones():
    envelope = await capabilities.policy_get("nonexistent")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.NOT_FOUND
    assert "risk" in envelope.error.context["available_policies"]


# ==========================================================================
# UNSUPPORTED DOCUMENT
# ==========================================================================


async def test_an_unsupported_document_type_names_what_is_supported():
    envelope = await capabilities.document_verify("MARRIAGE_CERTIFICATE", "x.jpg")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.UNSUPPORTED_DOCUMENT
    assert envelope.error.code == "UNSUPPORTED_DOCUMENT_TYPE"
    assert "PAN" in envelope.error.context["supported_documents"]


async def test_an_unsupported_financial_hint_is_refused(sandboxed):
    path = sandboxed("statement.pdf", SD / "sbi_wa.pdf")

    envelope = await capabilities.financial_analyze(str(path), "PASSPORT")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.UNSUPPORTED_DOCUMENT


# ==========================================================================
# SECURITY BOUNDARY
# ==========================================================================


@pytest.mark.parametrize(
    "capability",
    ["document_verify", "document_extract", "document_get"],
)
async def test_no_tool_reads_outside_the_upload_sandbox(
    capability, tmp_path, monkeypatch
):
    """
    Every path-taking tool must refuse a file outside the sandbox.

    Parametrised deliberately: a new tool that forgets the check is the way
    arbitrary file read comes back, and this catches it by name.
    """
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 confidential")

    func = getattr(capabilities, capability)
    args = (
        (str(outside),)
        if capability == "document_get"
        else ("PAN", str(outside))
    )
    envelope = await func(*args)

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH
    assert envelope.error.code == "PATH_NOT_ALLOWED"


async def test_financial_analyze_also_refuses_a_path_outside_the_sandbox(
    tmp_path, monkeypatch
):
    """
    Load-bearing rather than defensive.

    process_financial_document takes a bare path and checks only that it
    exists, so if this layer did not sandbox, the tool would read anything.
    """
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    outside = tmp_path / "payroll.pdf"
    outside.write_bytes(b"%PDF-1.4 confidential")

    envelope = await capabilities.financial_analyze(str(outside))

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH


async def test_a_traversal_path_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 confidential")

    envelope = await capabilities.document_get(
        str(root / ".." / "secret.pdf")
    )

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FORBIDDEN_PATH


async def test_a_sandbox_refusal_does_not_echo_the_resolved_path(
    tmp_path, monkeypatch
):
    """An error message that quotes back absolute paths is a probe primitive."""
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 confidential")

    envelope = await capabilities.document_get(str(outside))

    assert "secret.pdf" not in envelope.error.message
    assert str(tmp_path) not in envelope.error.message


async def test_policy_get_cannot_be_used_to_read_arbitrary_yaml():
    """Policies are named, not addressed by path."""
    for attempt in ("../../.env", "/etc/passwd", "app/config/agents.yaml"):
        envelope = await capabilities.policy_get(attempt)
        assert envelope.ok is False
        assert envelope.status is ToolStatus.NOT_FOUND


async def test_a_missing_file_inside_the_sandbox_is_not_found(
    tmp_path, monkeypatch
):
    """Absent, not forbidden -- the two are different answers."""
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("AGENT_UPLOAD_ROOT", str(root))

    envelope = await capabilities.document_get(str(root / "absent.jpg"))

    assert envelope.ok is False
    assert envelope.status is ToolStatus.NOT_FOUND


# ==========================================================================
# ERROR HANDLING
# ==========================================================================


async def test_a_service_explosion_becomes_a_structured_error(
    sandboxed, monkeypatch
):
    """No exception may cross the MCP boundary as a traceback."""
    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")

    async def explode(*args, **kwargs):
        raise RuntimeError("workflow exploded")

    monkeypatch.setattr(capabilities, "_verify_document", explode)

    envelope = await capabilities.document_verify("PAN", str(path))

    assert envelope.ok is False
    assert envelope.status is ToolStatus.FAILED
    assert envelope.error.code == "SERVICE_ERROR"
    assert envelope.error.context["exception"] == "RuntimeError"
    assert "workflow exploded" in envelope.error.message


async def test_every_envelope_is_json_serialisable():
    """The transport carries JSON, so a Decimal or date would break it."""
    import json

    for envelope in (
        await capabilities.kyc_run(kyc_payload()),
        await capabilities.policy_get("risk"),
        await capabilities.policy_get("verification"),
        await capabilities.case_get("C-1"),
    ):
        json.dumps(envelope.as_tool_payload())


async def test_a_failed_call_is_never_reported_as_a_result():
    """`ok` false must mean no result, or a model will read an empty one."""
    envelope = await capabilities.document_verify("PAN", "")

    payload = envelope.as_tool_payload()
    assert payload["ok"] is False
    assert "result" not in payload
    assert payload["error"]["code"]


async def test_case_get_reports_that_no_case_store_exists():
    """Honest unavailability beats a fabricated case."""
    envelope = await capabilities.case_get("CASE-1")

    assert envelope.ok is False
    assert envelope.status is ToolStatus.UNAVAILABLE
    assert envelope.error.code == "CASE_STORE_UNAVAILABLE"
    assert capabilities.CASE_STORE_AVAILABLE is False


# ==========================================================================
# NO DUPLICATED BUSINESS LOGIC
# ==========================================================================


def _executable_string_constants(path: str) -> set[str]:
    """
    Every string literal in a module except its docstrings.

    Parsed rather than grepped so that prose describing the rule -- this layer
    must not decide PASS/REVIEW/FAIL -- does not read as a breach of it.
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def test_the_mcp_layer_contains_no_ocr_or_decision_logic():
    """
    The layer forwards; it does not decide.

    Guards the design constraint directly: if someone implements a verdict or
    an OCR pass in here, the MCP layer has stopped being an integration layer.
    """
    source = Path("app/mcp/capabilities.py").read_text(encoding="utf-8")

    # No OCR engine is imported or referenced anywhere in the module.
    for engine in ("rapidocr", "pytesseract", "easyocr", "cv2"):
        assert engine not in source, (
            f"{engine!r} appears in the MCP layer: OCR belongs in the "
            "deterministic services"
        )

    # No verdict is constructed or compared against in executable code.
    literals = _executable_string_constants("app/mcp/capabilities.py")
    for verdict in ("PASS", "REVIEW", "FAIL", "SKIPPED"):
        assert verdict not in literals, (
            f"the MCP layer handles the verdict {verdict!r} in code; verdicts "
            "are computed by the services and passed through untouched"
        )


def test_the_mcp_layer_calls_the_existing_services():
    """Reuse is the requirement, so assert on it rather than trusting it."""
    source = Path("app/mcp/capabilities.py").read_text(encoding="utf-8")

    for expected in (
        "app.agents.document_verification.service",
        # The same extraction path the HTTP route and the orchestration
        # handler use, rather than a second one reachable only over MCP.
        "app.agents.document_agent.pipeline",
        "app.agents.financial",
        "app.agents.kyc.agent",
        "app.agents.fraud_risk.config",
    ):
        assert expected in source, f"{expected} is not reused"


def test_the_server_is_registration_only():
    """Logic in server.py is logic that cannot be tested without a transport."""
    source = Path("app/mcp/server.py").read_text(encoding="utf-8")

    assert "capabilities." in source
    for forbidden in ("resolve_sandboxed_path", "run_ocr", "run_kyc"):
        assert forbidden not in source, (
            f"{forbidden} is called directly in server.py; it belongs in the "
            "capability layer"
        )


async def test_document_get_does_not_trigger_an_extraction_pass(
    sandboxed, monkeypatch
):
    """The cheap tool must stay cheap, or agents will stop calling it first."""
    from app.agents.document_agent import pipeline

    called = False

    def spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("document.get must not extract")

    monkeypatch.setattr(pipeline, "extract_document", spy)

    path = sandboxed("pan.jpg", RB / "pan_bw2.jpg")
    envelope = await capabilities.document_get(str(path))

    assert envelope.ok is True
    assert called is False
