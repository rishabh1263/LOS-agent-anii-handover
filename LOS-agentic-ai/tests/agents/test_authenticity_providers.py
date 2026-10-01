"""
AUTHENTICITY PROVIDERS (app/agents/verification/authenticity_providers.py) --
real signatures, real verification, test keys (no real issuer material is in
the repository).

  pdf_signature       a signed PDF built here with a genuine CMS signature:
                      intact + trusted + expected issuer -> CONFIRMED; one
                      changed byte -> MISMATCH; no anchor / wrong signer /
                      signed-then-modified / unsigned -> NOT_ESTABLISHED
  aadhaar_secure_qr   a Secure-QR payload signed with a test "UIDAI" key:
                      valid -> CONFIRMED with the matching fields; forged ->
                      MISMATCH; a field that disagrees -> MISMATCH naming it;
                      no certificate configured -> NOT_ESTABLISHED
  http:<name>         a contracted API behind a mock transport
  chain               first decisive answer wins
  the issuer layer    a confirmation lifts nothing it should not, a mismatch
                      fails the document, the default config confirms nothing
"""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from app.agents.verification import authenticity_providers as ap
from app.agents.verification import issuer
from app.agents.verification.issuer import IssuerStatus, IssuerVerificationRequest

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "documents"


# ---- keys and certificates (test material only) --------------------------------------
def _cert(cn, org, issuer_cert=None, issuer_key=None, ca=False, days=30):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name)
            .issuer_name(issuer_cert.subject if issuer_cert else name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(issuer_key or key, hashes.SHA256()))
    return cert, key


@pytest.fixture(scope="module")
def pki():
    root, root_key = _cert("Test Root CA", "Test CCA", ca=True)
    uidai, uidai_key = _cert("e-Aadhaar Signer", "UNIQUE IDENTIFICATION AUTHORITY OF INDIA", root, root_key)
    stranger, stranger_key = _cert("Someone", "SOME OTHER COMPANY", root, root_key)
    return {"root": root, "uidai": (uidai, uidai_key), "stranger": (stranger, stranger_key)}


def signed_pdf(cert, key, body=b"1 0 obj << /Type /Catalog >> endobj\n") -> bytes:
    """A PDF with one signature over everything but its /Contents value."""
    width = 10
    head = b"%PDF-1.7\n" + body + b"2 0 obj << /Type /Sig /Filter /Adobe.PPKLite /ByteRange ["
    placeholder_range = b" ".join([b"0" * width] * 4)
    head += placeholder_range + b"] /Contents <"
    reserved = 16384
    tail = b"> >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    start = len(head) - 1                         # the '<'
    end = start + 1 + reserved + 1                # just past the '>'
    total = len(head) + reserved + len(tail)
    numbers = [0, start, end, total - end]
    byte_range = b" ".join(str(n).rjust(width, "0").encode() for n in numbers)
    head = head.replace(placeholder_range, byte_range)
    covered = head[:start] + tail[1:]             # everything but "<hex>"
    der = (pkcs7.PKCS7SignatureBuilder().set_data(covered).add_signer(cert, key, hashes.SHA256())
           # BINARY: the raw bytes, as PDF signers sign them (no S/MIME line-ending rewrite)
           .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))
    hexed = der.hex().encode().ljust(reserved, b"0")
    pdf = head + hexed + tail
    assert len(pdf) == total
    return pdf


@pytest.fixture
def settings(tmp_path, monkeypatch, pki):
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    (anchors / "root.pem").write_bytes(pki["root"].public_bytes(serialization.Encoding.PEM))
    uidai_cert = tmp_path / "uidai.pem"
    uidai_cert.write_bytes(pki["uidai"][0].public_bytes(serialization.Encoding.PEM))
    values = {"trust_anchors_dir": str(anchors), "aadhaar_qr_certificate": str(uidai_cert),
              "expected_signers": {"AADHAAR": ["UNIQUE IDENTIFICATION AUTHORITY OF INDIA"], "BANK_STATEMENT": []},
              "http": {}}
    monkeypatch.setattr(ap, "_settings", lambda: values)
    return values


def _ask(provider, document_type, content=None, fields=None, filename="doc.pdf"):
    return provider.verify(IssuerVerificationRequest(document_type=document_type, content=content,
                                                     fields=fields or {}, filename=filename))


# ---- PDF digital signature -----------------------------------------------------------
def test_a_signed_pdf_from_the_expected_issuer_is_confirmed(settings, pki):
    answer = _ask(ap.PdfSignatureProvider(), "AADHAAR", signed_pdf(*pki["uidai"]))
    assert answer.status is IssuerStatus.ISSUER_CONFIRMED
    assert answer.reference_id and answer.verified_at and answer.verified_fields == ["document_signature"]
    assert "REVOCATION_NOT_CHECKED" in answer.reason_codes           # honest about what was not checked


def test_one_changed_byte_breaks_the_signature_and_fails(settings, pki):
    pdf = bytearray(signed_pdf(*pki["uidai"]))
    pdf[12] ^= 0x01                                                    # inside the signed range
    answer = _ask(ap.PdfSignatureProvider(), "AADHAAR", bytes(pdf))
    assert answer.status is IssuerStatus.ISSUER_MISMATCH
    assert "DOCUMENT_SIGNATURE_INVALID" in answer.reason_codes


def test_bytes_added_after_signing_are_not_confirmed(settings, pki):
    pdf = signed_pdf(*pki["uidai"]) + b"3 0 obj << /Injected true >> endobj\n%%EOF\n"
    answer = _ask(ap.PdfSignatureProvider(), "AADHAAR", pdf)
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["PDF_MODIFIED_AFTER_SIGNING"]


def test_a_valid_signature_from_someone_else_is_not_the_issuer(settings, pki):
    answer = _ask(ap.PdfSignatureProvider(), "AADHAAR", signed_pdf(*pki["stranger"]))
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["SIGNER_NOT_EXPECTED_ISSUER"]


def test_a_type_with_no_expected_signer_confirms_nothing(settings, pki):
    answer = _ask(ap.PdfSignatureProvider(), "BANK_STATEMENT", signed_pdf(*pki["uidai"]))
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["EXPECTED_SIGNER_NOT_CONFIGURED"]


def test_without_a_trust_anchor_nothing_is_confirmed(settings, pki, tmp_path):
    settings["trust_anchors_dir"] = str(tmp_path / "empty")
    answer = _ask(ap.PdfSignatureProvider(), "AADHAAR", signed_pdf(*pki["uidai"]))
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and "TRUST_ANCHOR_NOT_CONFIGURED" in answer.reason_codes


def test_an_unsigned_real_statement_is_not_signed(settings):
    sample = SAMPLES / "Canara Bank Statement.pdf"
    if not sample.exists():
        pytest.skip("sample not available")
    answer = _ask(ap.PdfSignatureProvider(), "BANK_STATEMENT", sample.read_bytes())
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["DOCUMENT_NOT_DIGITALLY_SIGNED"]


def test_an_image_is_not_a_pdf_signature_case(settings):
    answer = _ask(ap.PdfSignatureProvider(), "PAN", b"\xff\xd8\xff\xe0 jpeg", filename="pan.jpg")
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["AUTHENTICITY_CHECK_NOT_APPLICABLE"]


# ---- Aadhaar Secure QR ---------------------------------------------------------------
def secure_qr_text(key, name=b"ASHA RAO", dob=b"12-04-1990", gender=b"F", forge=False) -> str:
    fields = [b"V2", b"3", b"123420240101120000000", name, dob, gender, b"C/O X", b"MUMBAI"]
    signed = b"\xff".join(fields) + b"\xff"
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    if forge:
        signed = signed.replace(b"ASHA", b"USHA")
    payload = gzip.compress(signed + signature)
    return str(int.from_bytes(payload, "big"))


def test_a_uidai_signed_qr_matching_the_card_is_confirmed(settings, pki, monkeypatch):
    monkeypatch.setattr(ap, "_qr_text", lambda content, filename: secure_qr_text(pki["uidai"][1]))
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", b"image",
                  fields={"name": "Asha Rao", "date_of_birth": "1990-04-12", "gender": "Female"})
    assert answer.status is IssuerStatus.ISSUER_CONFIRMED
    assert set(answer.verified_fields) == {"secure_qr", "name", "date_of_birth", "gender"}
    assert "1234" not in str(answer.model_dump())                    # never the Aadhaar reference digits


def test_a_forged_qr_fails(settings, pki, monkeypatch):
    monkeypatch.setattr(ap, "_qr_text", lambda c, f: secure_qr_text(pki["uidai"][1], forge=True))
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", b"image", fields={"name": "Usha Rao"})
    assert answer.status is IssuerStatus.ISSUER_MISMATCH and "AADHAAR_SECURE_QR_SIGNATURE_INVALID" in answer.reason_codes


def test_a_card_whose_printed_name_differs_from_its_qr_fails_naming_the_field(settings, pki, monkeypatch):
    monkeypatch.setattr(ap, "_qr_text", lambda c, f: secure_qr_text(pki["uidai"][1]))
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", b"image",
                  fields={"name": "Someone Else", "date_of_birth": "12/04/1990"})
    assert answer.status is IssuerStatus.ISSUER_MISMATCH
    assert answer.mismatched_fields == ["name"] and "date_of_birth" in answer.verified_fields


def test_without_the_uidai_certificate_nothing_is_confirmed(settings, pki, monkeypatch):
    settings["aadhaar_qr_certificate"] = ""
    monkeypatch.setattr(ap, "_qr_text", lambda c, f: secure_qr_text(pki["uidai"][1]))
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", b"image")
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["UIDAI_CERTIFICATE_NOT_CONFIGURED"]


def test_no_qr_in_the_image_is_not_established(settings):
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", b"\xff\xd8\xff\xe0 not an image", filename="a.jpg")
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and answer.reason_codes == ["AADHAAR_SECURE_QR_NOT_FOUND"]


# ---- contracted issuer API -------------------------------------------------------------
SPEC = {"url_env": "TEST_ISSUER_URL", "auth_env": "TEST_ISSUER_KEY",
        "request": {"pan": "pan_number", "name": "name"},
        "response": {"status": "status", "reference": "reference_id", "fields": "fields",
                     "confirmed": ["VALID"], "mismatch": ["INVALID"]}}


def _api(reply, seen=None, status_code=200):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status_code, json=reply)
    return ap.HttpIssuerProvider("pan_test", SPEC, transport=httpx.MockTransport(handler))


def test_the_api_confirms_and_is_sent_only_the_mapped_fields(monkeypatch):
    monkeypatch.setenv("TEST_ISSUER_URL", "https://issuer.example/verify")
    monkeypatch.setenv("TEST_ISSUER_KEY", "Bearer t")
    seen = []
    answer = _ask(_api({"status": "VALID", "reference_id": "R-1", "fields": {"name": True}}, seen), "PAN",
                  fields={"pan_number": "ABCDE1234F", "name": "Asha", "address": "not sent"})
    assert answer.status is IssuerStatus.ISSUER_CONFIRMED and answer.reference_id == "R-1"
    import json

    assert json.loads(seen[0].content) == {"pan": "ABCDE1234F", "name": "Asha"}
    assert seen[0].headers["Authorization"] == "Bearer t"


def test_the_api_saying_a_field_differs_is_a_mismatch(monkeypatch):
    monkeypatch.setenv("TEST_ISSUER_URL", "https://issuer.example/verify")
    answer = _ask(_api({"status": "VALID", "reference_id": "R-2", "fields": {"name": False}}), "PAN")
    assert answer.status is IssuerStatus.ISSUER_MISMATCH and answer.mismatched_fields == ["name"]


@pytest.mark.parametrize("reply,code", [({"status": "WEIRD"}, "ISSUER_API_UNEXPECTED_RESPONSE"), ({}, None)])
def test_an_unexpected_or_failed_api_answer_is_not_established(monkeypatch, reply, code):
    monkeypatch.setenv("TEST_ISSUER_URL", "https://issuer.example/verify")
    provider = _api(reply, status_code=200 if code else 503)
    answer = _ask(provider, "PAN")
    assert answer.status is IssuerStatus.NOT_ESTABLISHED
    assert answer.reason_codes == [code or "ISSUER_API_HTTP_503"]


def test_an_api_with_no_url_configured_calls_nothing(monkeypatch):
    monkeypatch.delenv("TEST_ISSUER_URL", raising=False)
    seen = []
    answer = _ask(_api({"status": "VALID"}, seen), "PAN")
    assert answer.status is IssuerStatus.NOT_ESTABLISHED and seen == []


# ---- chain + the issuer layer --------------------------------------------------------------
def test_a_chain_answers_with_the_first_decisive_provider(settings, pki):
    chain = ap.ChainProvider([ap.AadhaarSecureQrProvider(), ap.PdfSignatureProvider()])
    answer = _ask(chain, "AADHAAR", signed_pdf(*pki["uidai"]))
    assert answer.status is IssuerStatus.ISSUER_CONFIRMED and answer.provider == "pdf_signature"


def test_through_the_issuer_layer_a_broken_signature_fails_the_document(settings, pki):
    issuer.set_provider("AADHAAR", ap.PdfSignatureProvider())
    try:
        pdf = bytearray(signed_pdf(*pki["uidai"]))
        pdf[12] ^= 0x01
        result = {"status": "SUCCESS", "document": {"type": "AADHAAR"},
                  "verification": {"status": "PASS", "reason_codes": []}}
        issuer.apply(result, case_id="C", content=bytes(pdf), filename="e-aadhaar.pdf")
        assert result["verification"]["status"] == "FAIL" and result["status"] == "REJECTED"
        assert result["verification"]["issuer_verified"] is False

        good = {"status": "SUCCESS", "document": {"type": "AADHAAR"},
                "verification": {"status": "PASS", "reason_codes": []}}
        issuer.apply(good, case_id="C", content=signed_pdf(*pki["uidai"]), filename="e-aadhaar.pdf")
        assert good["verification"]["issuer_verified"] is True
        assert good["verification"]["authenticity"] == "ISSUER_CONFIRMED"
    finally:
        issuer.set_provider("AADHAAR", None)


def test_the_shipped_configuration_confirms_nothing_by_default():
    issuer.reset_providers()
    for document_type in ("PAN", "AADHAAR", "DRIVING_LICENCE", "PASSPORT", "VOTER_ID", "BANK_STATEMENT", "ITR"):
        assert issuer.provider_for(document_type).name == "none"


def test_a_secure_qr_image_is_decoded_verified_and_matched_end_to_end(settings, pki):
    """No shortcut: the QR is rendered to a PNG and read back from the bytes."""
    import cv2

    encoder = cv2.QRCodeEncoder.create()
    image = encoder.encode(secure_qr_text(pki["uidai"][1]))
    image = cv2.resize(image, (image.shape[1] * 6, image.shape[0] * 6), interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(image, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    png = cv2.imencode(".png", image)[1].tobytes()
    answer = _ask(ap.AadhaarSecureQrProvider(), "AADHAAR", png, filename="aadhaar.png",
                  fields={"name": "ASHA RAO", "date_of_birth": "12-04-1990", "gender": "F"})
    assert answer.status is IssuerStatus.ISSUER_CONFIRMED
    assert {"secure_qr", "name", "date_of_birth", "gender"} == set(answer.verified_fields)
