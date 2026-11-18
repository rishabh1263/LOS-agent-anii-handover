"""
The identity-PDF scan reads the PDF's STRUCTURE, not its binary payloads (2026-10-22).

A real PAN phone scan made with Adobe Scan for Android was refused PDF_ACTIVE_CONTENT twice over:
  - the 3 bytes "/JS" occurred by chance inside the compressed page image;
  - Adobe Scan attaches a 42-byte "pageEntities.json" to every PDF it makes.
Both are allowed now. Everything that runs something stays refused -- and JavaScript hidden in a compressed object
stream, which the raw-bytes check never saw, is now caught. Synthetic PDFs only.
"""

from __future__ import annotations

import zlib

import pytest

from app.security import malware_scan as M


def _pdf(*objects: bytes, tail: bytes = b"") -> bytes:
    body = b"%PDF-1.7\n"
    for i, o in enumerate(objects, 1):
        body += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    return body + b"trailer\n<</Root 1 0 R>>\n" + tail + b"%%EOF\n"


def _stream(dictionary: bytes, payload: bytes) -> bytes:
    return dictionary + b"\nstream\n" + payload + b"\nendstream"


CATALOG = b"<</Type/Catalog>>"
JSON_ATTACHMENT = _stream(b"<</Type/EmbeddedFile/Subtype/application#2Fjson/Length 42>>",
                          b'{ "type": "ID Card", "isBackSide": false }')


def _scan(content: bytes):
    return M.builtin_scan(content, "pan.pdf", identity=True)


@pytest.mark.parametrize("name, content", [
    ("chance /JS bytes inside an image", _pdf(CATALOG, _stream(b"<</Type/XObject/Subtype/Image/Filter/DCTDecode>>",
                                                                b"\xff\xd8\x00/JS \x10\xff\xd9"))),
    ("an Adobe Scan style json attachment", _pdf(CATALOG, JSON_ATTACHMENT)),
    ("chance bytes AND the json attachment", _pdf(CATALOG, _stream(b"<</Type/XObject/Subtype/Image>>",
                                                                    b"\x00/JS\n\x01"), JSON_ATTACHMENT)),
], ids=lambda v: v if isinstance(v, str) else "")
def test_a_phone_scan_is_not_mistaken_for_active_content(name, content):
    assert _scan(content) is None, name


@pytest.mark.parametrize("name, content", [
    ("javascript action", _pdf(b"<</Type/Catalog/OpenAction 2 0 R>>", b"<</S/JavaScript/JS(app.alert(1))>>")),
    ("javascript in a compressed object stream",
     _pdf(CATALOG, _stream(b"<</Type/ObjStm/Filter/FlateDecode/N 1/First 4>>",
                           zlib.compress(b"3 0 <</S/JavaScript/JS(x)>>")))),
    ("escaped name", _pdf(CATALOG, b"<</S/J#61vaScript>>")),
    ("binary attachment", _pdf(CATALOG, _stream(b"<</Type/EmbeddedFile/Subtype/application#2Foctet-stream>>",
                                                 b"\x00\x01"))),
    ("attachment with no type", _pdf(CATALOG, _stream(b"<</Type/EmbeddedFile/Length 2>>", b"{}"))),
    ("oversized json attachment", _pdf(CATALOG, _stream(b"<</Type/EmbeddedFile/Subtype/application#2Fjson>>",
                                                        b"{" + b" " * 70_000 + b"}"))),
    ("json next to an executable attachment",
     _pdf(CATALOG, JSON_ATTACHMENT, _stream(b"<</Type/EmbeddedFile/Subtype/application#2Fx-msdownload>>", b"MZ"))),
    ("automatic action", _pdf(b"<</Type/Catalog/AA<</O 2 0 R>>>>", b"<</S/URI>>")),
    ("encrypted", _pdf(CATALOG, tail=b"trailer\n<</Encrypt 3 0 R>>\n")),
    ("launch", _pdf(b"<</Type/Catalog/OpenAction<</S/Launch/F(cmd.exe)>>>>")),
    ("a stream that never ends: the raw check decides", b"%PDF-1.7\n1 0 obj\n<<>>\nstream\n/JS "),
], ids=lambda v: v if isinstance(v, str) else "")
def test_active_content_is_still_refused(name, content):
    found = _scan(content)
    assert found is not None and found.code == "PDF_ACTIVE_CONTENT", name


def test_the_allowed_attachment_types_and_size_come_from_the_environment(monkeypatch):
    content = _pdf(CATALOG, JSON_ATTACHMENT)
    monkeypatch.setenv("UPLOAD_PDF_ALLOWED_ATTACHMENT_TYPES", "text/plain")
    assert _scan(content).code == "PDF_ACTIVE_CONTENT"
    monkeypatch.setenv("UPLOAD_PDF_ALLOWED_ATTACHMENT_TYPES", "application/json")
    monkeypatch.setenv("UPLOAD_PDF_MAX_ATTACHMENT_BYTES", "10")
    assert _scan(content).code == "PDF_ACTIVE_CONTENT"
    monkeypatch.setenv("UPLOAD_PDF_MAX_ATTACHMENT_BYTES", "1000")
    assert _scan(content) is None


def test_launch_is_refused_for_every_document_not_only_identity():
    assert M.builtin_scan(_pdf(b"<</Type/Catalog/OpenAction<</S/Launch>>>>"), "slip.pdf").code == "PDF_ACTIVE_CONTENT"
