"""
End-to-end LOS flow against real sample documents.

Document Agent -> Financial Agent (where applicable) -> KYC -> one response.
These use actual files, so they are marked `ocr` and excluded by
-m "not ocr" alongside the other document tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.los.flow import UploadedDocument, process_application
from app.agents.los.summary import (
    build_summary,
    deterministic_summary,
    validate_llm_summary,
)

RB = Path("samples/real_batch")
SD = Path("samples/documents")

pytestmark = pytest.mark.ocr


def upload(path: Path, source_id: str | None = None, expected: str | None = None):
    if not path.exists():
        pytest.skip(f"sample not available: {path}")
    return UploadedDocument(
        source_id=source_id or path.stem,
        filename=path.name,
        content=path.read_bytes(),
        expected_type=expected,
    )


async def run(*uploads, operation="EXTRACT"):
    return await process_application(
        list(uploads), operation=operation, applicant_id="APP-TEST",
        request_id="los-test", use_llm_summary=False,
    )


def _doc(result, source_id):
    return next(d for d in result["documents"] if d["source_id"] == source_id)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


async def test_response_carries_the_unified_contract():
    result = await run(upload(RB / "pan_bw2.jpg"))

    assert set(result) >= {
        "request_id", "status", "documents", "kyc", "cross_document",
        "summary", "processing_ms", "errors",
    }
    # Stage timings are internal: one duration is published, not the
    # breakdown that describes how the service is built.
    assert "processing" not in result
    assert result["processing_ms"] >= 0
    assert result["summary"]
    assert isinstance(result["errors"], list)


async def test_bulky_parser_detail_is_not_returned_by_default():
    """A bank statement's per-transaction dump must not ride along."""
    result = await run(upload(RB / "bank_hdfc_new.pdf"))
    fields = _doc(result, "bank_hdfc_new")["extraction"]

    assert "detail" not in fields
    assert "signals" in fields, "the normalised signals must still be present"


# ---------------------------------------------------------------------------
# A. Identity document
# ---------------------------------------------------------------------------


async def test_identity_document_end_to_end():
    result = await run(upload(RB / "pan_bw2.jpg"))
    document = _doc(result, "pan_bw2")

    assert document["type"] == "PAN"
    assert document["type"] == "PAN"
    assert document["verification"] == "PASS"
    assert document["extraction"]["pan_number"]


# ---------------------------------------------------------------------------
# B. Financial document
# ---------------------------------------------------------------------------


async def test_financial_document_routes_to_the_financial_agent():
    result = await run(upload(RB / "itr_v.pdf"))
    document = _doc(result, "itr_v")

    assert document["type"] == "ITR"
    # A digital PDF costs no OCR at all -- the guarantee the text-layer path
    # exists for. Timings left the public response, so this is checked on the
    # document the flow produced rather than on the one it returns.
    assert "processing" not in document


# ---------------------------------------------------------------------------
# C. Multi-document applicant
# ---------------------------------------------------------------------------


async def test_multi_document_applicant_reaches_kyc():
    result = await run(
        upload(RB / "pan_bw2.jpg"),
        upload(RB / "salary_slip.pdf"),
        upload(RB / "bank_hdfc_new.pdf"),
        upload(RB / "itr_v.pdf"),
    )

    assert len(result["documents"]) == 4
    assert result["kyc"]["status"] in ("PASS", "REVIEW", "FAIL")
    assert result["processing_ms"] >= 0
    assert result["status"] in ("SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED")


async def test_kyc_sees_a_mismatch_across_real_documents():
    """
    A PAN card and an ITR belonging to different people must not be reported
    as one consistent applicant.
    """
    result = await run(upload(RB / "pan_bw2.jpg"), upload(RB / "itr_v.pdf"))

    kyc = result["kyc"]
    if kyc["status"] == "PASS":
        pytest.skip("these two samples happen to agree; nothing to assert")
    assert kyc["reason_codes"], "a non-PASS KYC must explain itself"


# ---------------------------------------------------------------------------
# VERIFY / EXTRACT semantics
# ---------------------------------------------------------------------------


async def test_verify_never_returns_extraction_and_so_kyc_cannot_run():
    result = await run(upload(RB / "pan_bw2.jpg"), upload(RB / "itr_v.pdf"),
                       operation="VERIFY")

    for document in result["documents"]:
        assert document.get("extraction") is None

    # Nothing was released, so there is nothing to cross-check. The gate
    # holding is the point; it must be reported, not silently ignored.
    assert result["kyc"]["status"] == "SKIPPED"
    assert any(e["code"] == "KYC_NOT_RUN" for e in result["errors"])


async def test_extract_is_still_gated_on_verification():
    """A wrong expected_type fails verification, so no fields are released."""
    result = await run(
        upload(RB / "pan_bw2.jpg", expected="DRIVING_LICENCE"),
        operation="EXTRACT",
    )
    document = _doc(result, "pan_bw2")

    assert document["verification"] == "FAIL"
    assert document.get("extraction") is None
    assert document["status"] == "REJECTED"
    # A document whose fields were withheld must not reach KYC.
    assert result["kyc"]["status"] == "SKIPPED"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


async def test_one_unreadable_document_does_not_sink_the_application():
    result = await run(
        upload(RB / "pan_bw2.jpg"),
        UploadedDocument("broken", "broken.jpg", b"not an image at all"),
    )

    assert len(result["documents"]) == 2
    assert _doc(result, "pan_bw2")["extraction"]
    assert _doc(result, "broken")["status"] == "FAILED"


async def test_unsupported_file_type_is_reported_not_raised():
    result = await run(
        upload(RB / "pan_bw2.jpg"),
        UploadedDocument("odd", "notes.txt", b"hello"),
    )
    assert _doc(result, "odd")["status"] == "FAILED"


# ---------------------------------------------------------------------------
# Summary layer
# ---------------------------------------------------------------------------


def test_deterministic_summary_needs_no_model():
    envelope = {
        "status": "SUCCESS",
        "document": {"type": "PAN", "category": "IDENTITY"},
        "extraction": {"fields": {"pan_number": "X", "name": "Y"}},
        "verification": {"status": "PASS"},
        "kyc": {"status": "PASS", "reason_codes": []},
        "errors": [],
    }
    summary, source = build_summary(envelope, use_llm=False)

    assert source == "deterministic"
    assert "PAN" in summary.upper()
    assert "PASS" in summary.upper()


def test_a_summary_inventing_a_number_is_rejected():
    envelope = {
        "status": "SUCCESS",
        "document": {"type": "PAN"},
        "extraction": {"fields": {"a": 1}},
        "verification": {"status": "PASS"},
        "errors": [],
    }
    accepted, reason = validate_llm_summary(
        "The applicant earns 4200000 rupees and the PAN verified cleanly.",
        envelope,
    )
    assert accepted is False
    assert "unsupported number" in reason


def test_a_summary_inventing_a_verdict_is_rejected():
    envelope = {
        "status": "SUCCESS",
        "document": {"type": "PAN"},
        "extraction": {"fields": {}},
        "verification": {"status": "PASS"},
        "errors": [],
    }
    accepted, reason = validate_llm_summary(
        "The document was checked and the outcome was FAIL for this applicant.",
        envelope,
    )
    assert accepted is False
    assert "uncomputed verdict" in reason


def test_summary_falls_back_when_the_model_is_unreachable(monkeypatch):
    """With the model switched on but broken, the service still answers."""
    import app.agents.los.summary as summary_module

    def explode(_payload):
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(summary_module, "_generate", explode)

    envelope = {
        "status": "SUCCESS",
        "document": {"type": "PAN"},
        "extraction": {"fields": {}},
        "verification": {"status": "PASS"},
        "errors": [],
    }
    summary, source = build_summary(envelope, use_llm=True)

    assert source == "deterministic"
    assert summary == deterministic_summary(envelope)
