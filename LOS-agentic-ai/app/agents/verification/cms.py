"""
CMS / PKCS#7 SignedData, READ AND VERIFIED -- the cryptography behind a
digitally signed document (a bank e-statement, an e-Aadhaar PDF, an ITR
acknowledgement, a DigiLocker-issued document).

A small DER reader walks the structure (RFC 5652); every cryptographic
decision is `cryptography`'s: the digest, the signature over the signed
attributes, and each certificate in the chain being issued by the next.

    ContentInfo { contentType = signedData, [0] SignedData }
    SignedData  { version, digestAlgorithms, encapContentInfo,
                  [0] certificates, [1] crls, signerInfos }
    SignerInfo  { version, sid, digestAlgorithm, [0] signedAttrs,
                  signatureAlgorithm, signature, [1] unsignedAttrs }

WHAT A VALID RESULT MEANS. The signed bytes are exactly what the signer
signed (messageDigest matches), the signer's key made the signature, and --
only when a trust anchor is configured -- the signer's certificate chains to
it and was valid when it signed. Revocation (CRL / OCSP) is NOT checked here;
the result says so.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SIGNING_TIME = "1.2.840.113549.1.9.5"
_DIGESTS = {
    "1.3.14.3.2.26": (hashlib.sha1, hashes.SHA1),
    "2.16.840.1.101.3.4.2.1": (hashlib.sha256, hashes.SHA256),
    "2.16.840.1.101.3.4.2.2": (hashlib.sha384, hashes.SHA384),
    "2.16.840.1.101.3.4.2.3": (hashlib.sha512, hashes.SHA512),
}


class CmsError(ValueError):
    """The bytes are not a SignedData this reader understands."""


# ---- a minimal DER reader ---------------------------------------------------------------
@dataclass
class Node:
    tag: int
    raw: bytes            # the whole TLV
    value: bytes          # the contents

    @property
    def constructed(self) -> bool:
        return bool(self.tag & 0x20)

    def children(self) -> list["Node"]:
        return list(_read_all(self.value))


def _read(data: bytes, offset: int) -> tuple[Node, int]:
    if offset + 2 > len(data):
        raise CmsError("truncated DER")
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise CmsError("multi-byte tags are not used by CMS")
    length = data[offset + 1]
    header = 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or offset + 2 + count > len(data):
            raise CmsError("unsupported DER length")
        length = int.from_bytes(data[offset + 2:offset + 2 + count], "big")
        header += count
    end = offset + header + length
    if end > len(data):
        raise CmsError("DER length runs past the data")
    return Node(tag, data[offset:end], data[offset + header:end]), end


def _read_all(data: bytes):
    offset = 0
    while offset < len(data):
        node, offset = _read(data, offset)
        yield node


def parse(data: bytes) -> Node:
    node, end = _read(data, 0)
    return node


def oid(node: Node) -> str:
    if node.tag != 0x06:
        raise CmsError("expected an OID")
    body = node.value
    first = body[0]
    parts = [str(first // 40), str(first % 40)]
    value = 0
    for byte in body[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return ".".join(parts)


def _time(node: Node) -> datetime | None:
    text = node.value.decode("ascii", "replace")
    for fmt in ("%y%m%d%H%M%SZ", "%Y%m%d%H%M%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---- SignedData ------------------------------------------------------------------------
@dataclass
class Signature:
    digest_oid: str
    signed_attrs_der: bytes | None
    message_digest: bytes | None
    signing_time: datetime | None
    signature: bytes
    signer: x509.Certificate | None
    certificates: list[x509.Certificate] = field(default_factory=list)


def read_signed_data(der: bytes) -> Signature:
    """The first signer of a CMS ContentInfo(SignedData)."""
    content_info = parse(der.rstrip(b"\x00"))
    parts = content_info.children()
    if len(parts) < 2 or oid(parts[0]) != OID_SIGNED_DATA:
        raise CmsError("not a CMS SignedData")
    signed_data = parts[1].children()[0].children()        # [0] EXPLICIT SignedData
    certificates: list[x509.Certificate] = []
    signer_infos: list[Node] = []
    for node in signed_data[3:]:
        if node.tag == 0xA0:                                  # [0] IMPLICIT certificates
            for cert in node.children():
                try:
                    certificates.append(x509.load_der_x509_certificate(cert.raw))
                except ValueError:
                    continue
        elif node.tag == 0x31:                                # signerInfos SET
            signer_infos = node.children()
    if not signer_infos:
        raise CmsError("no signer")
    info = signer_infos[0].children()
    sid, digest_algorithm = info[1], info[2]
    index = 3
    signed_attrs_der = None
    message_digest = signing_time = None
    if index < len(info) and info[index].tag == 0xA0:        # [0] IMPLICIT signedAttrs
        attrs = info[index]
        # the signature covers the attributes re-tagged as a SET OF
        signed_attrs_der = bytes([0x31]) + attrs.raw[1:]
        for attr in attrs.children():
            kids = attr.children()
            name = oid(kids[0])
            values = kids[1].children()
            if name == OID_MESSAGE_DIGEST and values:
                message_digest = values[0].value
            elif name == OID_SIGNING_TIME and values:
                signing_time = _time(values[0])
        index += 1
    index += 1                                                # signatureAlgorithm
    signature = info[index].value
    signer = _find_signer(sid, certificates)
    return Signature(digest_oid=oid(digest_algorithm.children()[0]), signed_attrs_der=signed_attrs_der,
                     message_digest=message_digest, signing_time=signing_time, signature=signature,
                     signer=signer, certificates=certificates)


def _find_signer(sid: Node, certificates: list[x509.Certificate]) -> x509.Certificate | None:
    if sid.tag == 0x30:                                       # IssuerAndSerialNumber
        issuer, serial = sid.children()[:2]
        number = int.from_bytes(serial.value, "big", signed=True)
        for cert in certificates:
            if cert.serial_number == number and cert.issuer.public_bytes() == issuer.raw:
                return cert
    elif sid.tag == 0x80:                                     # [0] SubjectKeyIdentifier
        for cert in certificates:
            try:
                ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
            except x509.ExtensionNotFound:
                continue
            if ski == sid.value:
                return cert
    return None


# ---- verification ------------------------------------------------------------------------
@dataclass
class Verdict:
    intact: bool = False                 # the signed bytes are unchanged and the key signed them
    trusted: bool = False                # the signer chains to a configured anchor, valid then
    signer_subject: str | None = None
    signer_organization: str | None = None
    signer_fingerprint: str | None = None
    signing_time: datetime | None = None
    problems: list[str] = field(default_factory=list)


def verify(der: bytes, signed_content: bytes, *, anchors: list[x509.Certificate]) -> Verdict:
    """Verify one detached CMS signature over `signed_content`."""
    out = Verdict()
    try:
        sig = read_signed_data(der)
    except (CmsError, IndexError, ValueError) as exc:
        out.problems.append(f"CMS_UNREADABLE:{type(exc).__name__}")
        return out
    if sig.signer is None:
        out.problems.append("SIGNER_CERTIFICATE_MISSING")
        return out
    algorithms = _DIGESTS.get(sig.digest_oid)
    if algorithms is None:
        out.problems.append("DIGEST_ALGORITHM_UNSUPPORTED")
        return out
    hasher, hash_cls = algorithms
    digest = hasher(signed_content).digest()
    out.signer_subject = sig.signer.subject.rfc4514_string()
    orgs = sig.signer.subject.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)
    out.signer_organization = str(orgs[0].value) if orgs else None
    out.signer_fingerprint = sig.signer.fingerprint(hashes.SHA256()).hex()
    out.signing_time = sig.signing_time

    # 1. the content is what was signed
    if sig.signed_attrs_der is not None:
        if sig.message_digest != digest:
            out.problems.append("CONTENT_MODIFIED")               # messageDigest mismatch
            return out
        signed_bytes = sig.signed_attrs_der
    else:
        signed_bytes = signed_content
    # 2. the signer's key made the signature
    key = sig.signer.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(sig.signature, signed_bytes, padding.PKCS1v15(), hash_cls())
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(sig.signature, signed_bytes, ec.ECDSA(hash_cls()))
        else:
            out.problems.append("KEY_TYPE_UNSUPPORTED")
            return out
    except InvalidSignature:
        out.problems.append("SIGNATURE_INVALID")
        return out
    out.intact = True
    # 3. the signer chains to a configured anchor, and was valid when it signed
    if not anchors:
        out.problems.append("TRUST_ANCHOR_NOT_CONFIGURED")
        return out
    out.trusted = _chains(sig.signer, sig.certificates, anchors,
                          at=sig.signing_time or datetime.now(timezone.utc), problems=out.problems)
    return out


def _valid_at(cert: x509.Certificate, at: datetime) -> bool:
    return cert.not_valid_before_utc <= at <= cert.not_valid_after_utc


def _chains(cert: x509.Certificate, pool: list[x509.Certificate], anchors: list[x509.Certificate], *,
            at: datetime, problems: list[str]) -> bool:
    anchor_prints = {a.fingerprint(hashes.SHA256()) for a in anchors}
    candidates = list(pool) + list(anchors)
    current = cert
    for _depth in range(8):
        if not _valid_at(current, at):
            problems.append("CERTIFICATE_NOT_VALID_AT_SIGNING")
            return False
        if current.fingerprint(hashes.SHA256()) in anchor_prints:
            return True
        issuer = next((c for c in candidates if c.subject == current.issuer and c is not current), None)
        if issuer is None:
            problems.append("CHAIN_INCOMPLETE")
            return False
        try:
            current.verify_directly_issued_by(issuer)
        except (InvalidSignature, ValueError, TypeError):
            problems.append("CHAIN_SIGNATURE_INVALID")
            return False
        current = issuer
    problems.append("CHAIN_TOO_LONG")
    return False


def load_anchors(paths: list[str]) -> list[x509.Certificate]:
    """PEM or DER certificates from files (a bundle may hold several)."""
    out: list[x509.Certificate] = []
    for path in paths:
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if b"-----BEGIN CERTIFICATE-----" in data:
            try:
                out.extend(x509.load_pem_x509_certificates(data))
            except ValueError:
                continue
        else:
            try:
                out.append(x509.load_der_x509_certificate(data))
            except ValueError:
                continue
    return out


__all__ = ["CmsError", "Signature", "Verdict", "load_anchors", "read_signed_data", "verify"]
