"""
AUTHENTICITY PROVIDERS -- real ways for an issuer to confirm a document,
plugged into the existing issuer layer (issuer.py: register_provider ->
issuer_verification.yaml). Each returns ISSUER_CONFIRMED only with the
provider, a reference and a timestamp; issuer.verify() refuses anything less.

  pdf_signature       a digitally signed PDF (bank e-statement, e-Aadhaar PDF,
                      ITR acknowledgement, DigiLocker-issued document): the
                      signed bytes are unchanged, the signer's key signed them,
                      the signer chains to a CONFIGURED trust anchor and is the
                      EXPECTED issuer for the document type.
  aadhaar_secure_qr   the UIDAI Secure QR: its payload is signed by UIDAI; the
                      signature is checked with UIDAI's CONFIGURED certificate,
                      and name / date of birth / gender are compared with what
                      was extracted. The Aadhaar number is never read.
  http:<name>         a contracted issuer / registry API (PAN, DL, Voter ID,
                      Passport, ...), configured in YAML: URL and credentials
                      come from the environment, never from the code.
  chain               several of the above for one type, in order.

WHAT EACH ANSWER MEANS
  ISSUER_CONFIRMED   the issuer's signature / API confirmed THIS document
  ISSUER_MISMATCH    the issuer's evidence contradicts it (a broken signature
                     over altered bytes, a forged QR, an API saying no)
  NOT_ESTABLISHED    everything else -- unsigned, no trust anchor, unexpected
                     signer, not configured, unreachable -- with the reason

NOTHING IS CONFIRMED BY DEFAULT. With no trust anchors, no UIDAI certificate
and no API configured, every provider answers NOT_ESTABLISHED and says why.
Revocation (CRL / OCSP) is not checked; a confirmation says so in its codes.
Values never leave this module: results carry field NAMES only.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import unicodedata
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.verification import cms
from app.agents.verification.issuer import (
    IssuerStatus, IssuerVerificationProvider, IssuerVerificationRequest, IssuerVerificationResult,
    ProviderMode, _config, register_provider)

logger = logging.getLogger(__name__)

NOT_SIGNED = "DOCUMENT_NOT_DIGITALLY_SIGNED"
NOT_APPLICABLE = "AUTHENTICITY_CHECK_NOT_APPLICABLE"
MODIFIED_AFTER_SIGNING = "PDF_MODIFIED_AFTER_SIGNING"
SIGNATURE_BROKEN = "DOCUMENT_SIGNATURE_INVALID"
UNEXPECTED_SIGNER = "SIGNER_NOT_EXPECTED_ISSUER"
SIGNER_NOT_CONFIGURED = "EXPECTED_SIGNER_NOT_CONFIGURED"
REVOCATION_NOT_CHECKED = "REVOCATION_NOT_CHECKED"
QR_NOT_FOUND = "AADHAAR_SECURE_QR_NOT_FOUND"
QR_UNREADABLE = "AADHAAR_SECURE_QR_UNREADABLE"
QR_SIGNATURE_INVALID = "AADHAAR_SECURE_QR_SIGNATURE_INVALID"
UIDAI_CERT_MISSING = "UIDAI_CERTIFICATE_NOT_CONFIGURED"
HTTP_NOT_CONFIGURED = "ISSUER_API_NOT_CONFIGURED"
HTTP_UNEXPECTED = "ISSUER_API_UNEXPECTED_RESPONSE"


def _settings() -> dict[str, Any]:
    return dict(_config().get("authenticity_providers") or {})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else _root() / p


def _answer(status: IssuerStatus, name: str, codes: list[str], **extra: Any) -> IssuerVerificationResult:
    return IssuerVerificationResult(status=status, provider=name, mode=ProviderMode.ISSUER.value,
                                    reason_codes=codes, **extra)


# =========================================================================================
# PDF DIGITAL SIGNATURE
# =========================================================================================
_BYTE_RANGE = re.compile(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]")


def pdf_signatures(content: bytes) -> list[tuple[bytes, bytes, bool]]:
    """
    Every signature in a PDF as (CMS DER, the bytes it covers, whether it
    covers the file to its end). The signature value sits in the gap the
    ByteRange leaves: `<hex>` between the two covered ranges.
    """
    out = []
    size = len(content.rstrip(b"\r\n\x00 "))
    for match in _BYTE_RANGE.finditer(content):
        a, b, c, d = (int(g) for g in match.groups())
        if a != 0 or b <= 0 or c < b or c + d > len(content):
            continue
        gap = content[a + b:c].strip()
        if not (gap.startswith(b"<") and gap.endswith(b">")):
            continue
        try:
            der = bytes.fromhex(gap[1:-1].decode("ascii"))
        except ValueError:
            continue
        out.append((der, content[a:a + b] + content[c:c + d], c + d >= size))
    return out


def _anchors() -> list:
    directory = _resolve(_settings().get("trust_anchors_dir"))
    if directory is None or not directory.is_dir():
        return []
    files = [str(p) for p in directory.iterdir() if p.suffix.lower() in (".pem", ".crt", ".cer", ".der")]
    return cms.load_anchors(files)


def _expected_signers(document_type: str) -> list[str]:
    signers = (_settings().get("expected_signers") or {}).get(str(document_type).upper()) or []
    return [str(s).strip().upper() for s in signers if str(s).strip()]


class PdfSignatureProvider(IssuerVerificationProvider):
    name = "pdf_signature"
    mode = ProviderMode.ISSUER

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        content = request.content or b""
        if not content.lstrip()[:5] == b"%PDF-":
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [NOT_APPLICABLE])
        signatures = pdf_signatures(content)
        if not signatures:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [NOT_SIGNED])
        # the signature that covers the most of the file decides
        der, covered, to_end = max(signatures, key=lambda s: len(s[1]))
        verdict = cms.verify(der, covered, anchors=_anchors())
        reference = f"sig:{(verdict.signer_fingerprint or hashlib.sha256(der).hexdigest())[:24]}"
        if not verdict.intact:
            if {"CONTENT_MODIFIED", "SIGNATURE_INVALID"} & set(verdict.problems):
                # the document's own signature says these bytes were altered
                return _answer(IssuerStatus.ISSUER_MISMATCH, self.name, [SIGNATURE_BROKEN] + verdict.problems,
                               reference_id=reference, verified_at=_now(),
                               mismatched_fields=["document_signature"])
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, verdict.problems or [SIGNATURE_BROKEN])
        if not to_end:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [MODIFIED_AFTER_SIGNING])
        if not verdict.trusted:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, verdict.problems)
        expected = _expected_signers(request.document_type)
        if not expected:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [SIGNER_NOT_CONFIGURED])
        organization = str(verdict.signer_organization or verdict.signer_subject or "").upper()
        if not any(e in organization for e in expected):
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [UNEXPECTED_SIGNER])
        return _answer(IssuerStatus.ISSUER_CONFIRMED, self.name, [REVOCATION_NOT_CHECKED],
                       reference_id=reference, verified_at=_now(), verified_fields=["document_signature"])


# =========================================================================================
# AADHAAR SECURE QR
# =========================================================================================
def _qr_text(content: bytes, filename: str | None) -> str | None:
    """The QR's text from an image (or a PDF's first page), or None."""
    try:
        import cv2
        import numpy as np

        images = []
        if content.lstrip()[:5] == b"%PDF-":
            from pdf2image import convert_from_bytes

            for page in convert_from_bytes(content, dpi=300, first_page=1, last_page=1):
                images.append(cv2.cvtColor(np.array(page.convert("RGB")), cv2.COLOR_RGB2BGR))
        else:
            image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                images.append(image)
        detector = cv2.QRCodeDetector()
        for image in images:
            text, _points, _ = detector.detectAndDecode(image)
            if text:
                return text
    except Exception as exc:  # noqa: BLE001 - unreadable is NOT_ESTABLISHED, never a crash
        logger.info("secure QR decode failed: %s", type(exc).__name__)
    return None


def secure_qr_payload(text: str) -> bytes | None:
    """UIDAI Secure QR: a big decimal integer -> bytes -> decompressed payload."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) < 100:
        return None
    number = int(digits)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    for wbits in (16 + zlib.MAX_WBITS, zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(raw, wbits)
        except zlib.error:
            continue
    return None


def secure_qr_fields(payload: bytes) -> dict[str, str]:
    """name / date_of_birth / gender from the signed part (never the reference id)."""
    parts = payload[:-256].split(b"\xff")
    offset = 1 if parts and parts[0][:1] == b"V" else 0     # V2+ carries a version first
    try:
        name, dob, gender = parts[offset + 2], parts[offset + 3], parts[offset + 4]
    except IndexError:
        return {}
    decode = lambda b: b.decode("iso-8859-1").strip()        # noqa: E731
    return {"name": decode(name), "date_of_birth": decode(dob), "gender": decode(gender)}


def _norm_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Za-z ]", " ", text).upper().split())


def _norm_date(value: Any) -> str:
    digits = re.findall(r"\d+", str(value or ""))
    if len(digits) == 3:
        d, m, y = (digits if len(digits[0]) <= 2 else [digits[2], digits[1], digits[0]])
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return ""


class AadhaarSecureQrProvider(IssuerVerificationProvider):
    name = "aadhaar_secure_qr"
    mode = ProviderMode.ISSUER

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if not request.content:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [NOT_APPLICABLE])
        text = _qr_text(request.content, request.filename)
        if not text:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [QR_NOT_FOUND])
        payload = secure_qr_payload(text)
        if payload is None or len(payload) <= 256:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [QR_UNREADABLE])
        path = _resolve(_settings().get("aadhaar_qr_certificate"))
        if path is None or not path.is_file():
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [UIDAI_CERT_MISSING])
        certificates = cms.load_anchors([str(path)])
        if not certificates:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [UIDAI_CERT_MISSING])
        signed, signature = payload[:-256], payload[-256:]
        reference = f"qr:{hashlib.sha256(signature).hexdigest()[:24]}"
        try:
            certificates[0].public_key().verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError, TypeError):
            return _answer(IssuerStatus.ISSUER_MISMATCH, self.name, [QR_SIGNATURE_INVALID],
                           reference_id=reference, verified_at=_now(), mismatched_fields=["secure_qr"])
        on_card = secure_qr_fields(payload)
        extracted = request.fields or {}
        checks = {
            "name": (_norm_name(on_card.get("name")), _norm_name(extracted.get("name"))),
            "date_of_birth": (_norm_date(on_card.get("date_of_birth")),
                              _norm_date(extracted.get("date_of_birth") or extracted.get("dob"))),
            "gender": (str(on_card.get("gender") or "")[:1].upper(), str(extracted.get("gender") or "")[:1].upper()),
        }
        verified = ["secure_qr"] + [k for k, (a, b) in checks.items() if a and b and a == b]
        mismatched = [k for k, (a, b) in checks.items() if a and b and a != b]
        if mismatched:
            return _answer(IssuerStatus.ISSUER_MISMATCH, self.name, [], reference_id=reference,
                           verified_at=_now(), verified_fields=verified, mismatched_fields=mismatched)
        return _answer(IssuerStatus.ISSUER_CONFIRMED, self.name, [], reference_id=reference,
                       verified_at=_now(), verified_fields=verified)


# =========================================================================================
# CONTRACTED ISSUER / REGISTRY APIs (configured, never hard-coded)
# =========================================================================================
def _dig(data: Any, path: str | None) -> Any:
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


class HttpIssuerProvider(IssuerVerificationProvider):
    """
    One configured API: the extracted fields it is sent (by name), where the
    answer's status / reference / per-field matches sit, and which status
    values mean confirmed or mismatch. URL and credentials: environment only.
    """

    mode = ProviderMode.ISSUER

    def __init__(self, key: str, spec: dict[str, Any], transport: Any = None) -> None:
        self.name = f"http:{key}"
        self.spec = spec
        self.transport = transport

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        import httpx

        url = os.getenv(str(self.spec.get("url_env") or ""))
        if not url:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [HTTP_NOT_CONFIGURED])
        headers = {}
        token = os.getenv(str(self.spec.get("auth_env") or ""))
        if token:
            headers[str(self.spec.get("auth_header") or "Authorization")] = token
        mapping = self.spec.get("request") or {}
        body = {api: (request.fields or {}).get(field) for api, field in mapping.items()}
        if request.consent_id:
            body[str(self.spec.get("consent_field") or "consent_id")] = request.consent_id
        response = self.spec.get("response") or {}
        with httpx.Client(timeout=float(self.spec.get("timeout_seconds") or 8), transport=self.transport) as c:
            reply = c.post(url, json=body, headers=headers)
        if reply.status_code >= 400:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [f"ISSUER_API_HTTP_{reply.status_code}"])
        try:
            data = reply.json()
        except ValueError:
            return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [HTTP_UNEXPECTED])
        status = str(_dig(data, response.get("status")) or "").upper()
        reference = _dig(data, response.get("reference"))
        per_field = _dig(data, response.get("fields")) or {}
        verified = [k for k, v in per_field.items() if v is True] if isinstance(per_field, dict) else []
        mismatched = [k for k, v in per_field.items() if v is False] if isinstance(per_field, dict) else []
        confirmed = {str(v).upper() for v in response.get("confirmed") or []}
        mismatch = {str(v).upper() for v in response.get("mismatch") or []}
        if status in confirmed and not mismatched:
            return _answer(IssuerStatus.ISSUER_CONFIRMED, self.name, [], reference_id=str(reference or "") or None,
                           verified_at=_now(), verified_fields=verified or ["issuer_record"])
        if status in mismatch or mismatched:
            return _answer(IssuerStatus.ISSUER_MISMATCH, self.name, [], reference_id=str(reference or "") or None,
                           verified_at=_now(), verified_fields=verified, mismatched_fields=mismatched or ["issuer_record"])
        return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, [HTTP_UNEXPECTED])


# =========================================================================================
# SEVERAL PROVIDERS FOR ONE TYPE
# =========================================================================================
class ChainProvider(IssuerVerificationProvider):
    """Ask each in order; the first CONFIRMED or MISMATCH answers."""

    mode = ProviderMode.ISSUER

    def __init__(self, providers: list[IssuerVerificationProvider]) -> None:
        self.providers = providers
        self.name = "chain:" + "+".join(p.name for p in providers)

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        codes: list[str] = []
        for provider in self.providers:
            try:
                answer = provider.verify(request)
            except Exception as exc:  # noqa: BLE001 - one provider never costs the others
                codes.append(f"{provider.name}:ISSUER_PROVIDER_ERROR")
                logger.warning("provider %s failed: %s", provider.name, type(exc).__name__)
                continue
            if answer.status is not IssuerStatus.NOT_ESTABLISHED:
                return answer
            codes += [f"{provider.name}:{c}" for c in answer.reason_codes]
        return _answer(IssuerStatus.NOT_ESTABLISHED, self.name, codes)


_REGISTERED = False


def register_all() -> None:
    """Register every provider under the name configuration uses. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    register_provider(PdfSignatureProvider())
    register_provider(AadhaarSecureQrProvider())
    for key, spec in ((_settings().get("http") or {}).items()):
        if isinstance(spec, dict):
            register_provider(HttpIssuerProvider(str(key), spec))
    _REGISTERED = True


__all__ = ["AadhaarSecureQrProvider", "ChainProvider", "HttpIssuerProvider", "PdfSignatureProvider",
           "pdf_signatures", "register_all", "secure_qr_fields", "secure_qr_payload"]
