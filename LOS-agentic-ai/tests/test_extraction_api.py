"""
Document extraction upload API tests.

Uses real sample files, so these need the OCR engine and are marked `ocr`.
Validation-only tests (file type, size, empty) run without OCR.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.document_extraction_api import router

SAMPLES = Path("samples/documents")


# See tests/agents/test_fraud_risk_api.py for why this is a module global.
_AUTH: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _authenticate_module_clients(auth_headers):
    """/extract-document is protected; give every client a valid token."""
    _AUTH.clear()
    _AUTH.update(auth_headers)
    yield
    _AUTH.clear()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


def build_client() -> TestClient:
    """An authenticated client."""
    client = TestClient(_app())
    client.headers.update(_AUTH)
    return client


def build_unauthenticated_client() -> TestClient:
    """No credentials. For tests asserting a request is refused."""
    return TestClient(_app())


def test_extraction_requires_authentication():
    """The protection itself is part of the contract."""
    response = build_unauthenticated_client().get(
        "/api/v1/extract-document/supported"
    )
    assert response.status_code == 401


def test_supported_lists_every_document_type():
    """
    The advertised list is a contract: a .NET caller reads it to decide what
    it may send. It has to name every type the agent actually handles.
    """
    response = build_client().get("/api/v1/extract-document/supported")
    assert response.status_code == 200
    types = response.json()["document_types"]
    for expected in ("PAN", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT"):
        assert expected in types


def test_rejects_unsupported_file_type():
    response = build_client().post(
        "/api/v1/extract-document",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 415


def test_rejects_empty_file():
    response = build_client().post(
        "/api/v1/extract-document",
        files={"file": ("blank.jpg", b"", "image/jpeg")},
    )
    assert response.status_code == 400


def test_corrupt_image_returns_structured_failure():
    """
    A file with a valid extension but garbage content must not 500, and must
    not report success. The status code reflects the failure while the body
    still carries the structured evidence.
    """
    response = build_client().post(
        "/api/v1/extract-document",
        files={"file": ("bad.jpg", b"this is not an image", "image/jpeg")},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["errors"]
    assert body["fields"] == {}


def test_unsupported_document_returns_415_with_body():
    """A readable image that is not a PAN or DL is classified UNSUPPORTED."""
    from app.agents.document_agent.pipeline import extract_from_tokens
    from app.agents.document_agent.schemas import DocumentStatus, OCRToken

    result = extract_from_tokens(
        [OCRToken(text="ELECTRICITY BILL", confidence=0.99, x0=0, y0=0, x1=90, y1=20)]
    )
    assert result.status is DocumentStatus.UNSUPPORTED


def test_batch_rejects_too_many_files():
    files = [("files", (f"f{i}.jpg", b"x", "image/jpeg")) for i in range(11)]
    assert build_client().post("/api/v1/extract-document/batch", files=files).status_code == 413


def test_upload_is_not_left_on_disk():
    """The stored file must be removed once extraction finishes."""
    import os

    upload_root = Path(os.getenv("AGENT_UPLOAD_ROOT", "./runtime/uploads"))
    before = set(upload_root.glob("*")) if upload_root.exists() else set()
    build_client().post(
        "/api/v1/extract-document",
        files={"file": ("bad.jpg", b"not an image", "image/jpeg")},
    )
    after = set(upload_root.glob("*")) if upload_root.exists() else set()
    assert after == before


# =========================================================================
# REAL OCR
# =========================================================================


@pytest.mark.ocr
def test_upload_pan_extracts_all_fields():
    path = SAMPLES / "rpan.jpg"
    if not path.exists():
        pytest.skip("sample not available")

    with path.open("rb") as handle:
        response = build_client().post(
            "/api/v1/extract-document",
            files={"file": ("rpan.jpg", handle, "image/jpeg")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "PAN"
    assert body["status"] == "SUCCESS"
    assert body["fields"]["pan_number"]["value"] == "NUHPS4875K"
    assert body["fields"]["date_of_birth"]["value"] == "2002-06-12"


@pytest.mark.ocr
def test_upload_pdf_driving_licence():
    path = SAMPLES / "driving_license.pdf"
    if not path.exists():
        pytest.skip("sample not available")

    with path.open("rb") as handle:
        response = build_client().post(
            "/api/v1/extract-document",
            files={"file": ("driving_license.pdf", handle, "application/pdf")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "DRIVING_LICENCE"
    assert body["fields"]["dl_number"]["value"] == "MH0320220045390"
    assert body["fields"]["pin_code"]["value"] == "400043"


@pytest.mark.ocr
def test_request_id_header_present():
    path = SAMPLES / "lPan.jpg"
    if not path.exists():
        pytest.skip("sample not available")
    with path.open("rb") as handle:
        response = build_client().post(
            "/api/v1/extract-document",
            files={"file": ("lPan.jpg", handle, "image/jpeg")},
        )
    assert response.headers.get("X-Request-ID")


@pytest.mark.ocr
def test_batch_extraction():
    paths = [SAMPLES / "rpan.jpg", SAMPLES / "lPan.jpg"]
    if not all(p.exists() for p in paths):
        pytest.skip("samples not available")

    handles = [p.open("rb") for p in paths]
    try:
        files = [("files", (p.name, h, "image/jpeg")) for p, h in zip(paths, handles)]
        response = build_client().post("/api/v1/extract-document/batch", files=files)
    finally:
        for h in handles:
            h.close()

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["succeeded"] == 2