"""
THE COMMON UPLOAD GATE -- every uploaded document passes it before any OCR,
classification or verification runs.

    empty?  too large?  allowed extension?
      -> the BYTES are what the extension claims (file signature / magic bytes)
      -> no executable or script disguised as a document
      -> no PDF that asks the reader to launch a program

WHY THE SIGNATURE. The extension is a name the sender chose. A Windows
executable renamed "pan.pdf" passed every earlier check (extension, size,
non-empty) and reached the PDF renderer. The first bytes of a file are what
every parser actually reads, so they decide.

It decides nothing about the document itself -- class, legibility and the
verdict belong to the verification agent. A rejection here is reported per
document (INVALID_UPLOAD), never as a failure of the other documents in the
same upload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: extension -> the signatures its bytes may start with
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".bmp": (b"BM",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".webp": (b"RIFF",),
}

#: Content that is never a document, whatever it is called.
_EXECUTABLE = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
               b"#!", b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf")
_SCRIPT_MARKERS = (b"<script", b"<?php", b"<%@", b"<html", b"<svg")
#: PDF actions that start a program on the reader's machine.
_PDF_ACTIONS = (b"/Launch",)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    code: str | None = None
    message: str | None = None


ALLOWED = Verdict(True)


def max_bytes() -> int:
    try:
        return int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    except ValueError:
        return 25 * 1024 * 1024


def _any_document(content: bytes) -> bool:
    """The bytes are SOME supported document format, whatever the extension says."""
    head = content[:16]
    if content[:1024].find(b"%PDF-") != -1:
        return True
    if head.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return True
    return any(head.startswith(sig) for ext, sigs in _SIGNATURES.items()
               if ext not in (".pdf", ".webp") for sig in sigs)


def check(content: bytes, filename: str, *, limit: int | None = None) -> Verdict:
    """Whether these bytes may be processed as the document they claim to be.
    `limit` is the calling route's own size limit, when it has one."""
    suffix = Path(str(filename or "")).suffix.lower()
    if not content:
        return Verdict(False, "EMPTY_FILE", "The uploaded file is empty.")
    if len(content) > (limit or max_bytes()):
        return Verdict(False, "FILE_TOO_LARGE", "The uploaded file is too large.")
    if suffix not in _SIGNATURES:
        return Verdict(False, "UNSUPPORTED_FILE_TYPE", f"Unsupported file type: {suffix or 'unknown'}.")
    head = content[:16]
    if head.startswith(_EXECUTABLE):
        return Verdict(False, "MALICIOUS_CONTENT", "The file is not a document.")
    if any(marker in content[:1024].lower() for marker in _SCRIPT_MARKERS):
        return Verdict(False, "MALICIOUS_CONTENT", "The file is not a document.")
    signatures = _SIGNATURES[suffix]
    # a PDF may carry a few bytes of junk before the header (the PDF spec
    # tolerates it within the first 1 KB); images must start with theirs
    starts = content[:1024].find(b"%PDF-") != -1 if suffix == ".pdf" else head.startswith(signatures)
    if suffix == ".webp" and starts:
        starts = content[8:12] == b"WEBP"
    if not starts and not _any_document(content):
        # NOT A DOCUMENT FORMAT AT ALL. A real document under the wrong
        # extension (a JPEG saved as .pdf, as phones do) is still a document:
        # it goes on to be read and judged; bytes that are no supported format
        # never reach a parser.
        return Verdict(False, "FILE_SIGNATURE_MISMATCH",
                       f"The file's content is not a {suffix.lstrip('.').upper()} file.")
    if suffix == ".pdf" and any(action in content for action in _PDF_ACTIONS):
        return Verdict(False, "MALICIOUS_CONTENT", "The PDF asks to launch a program.")
    return ALLOWED


__all__ = ["ALLOWED", "Verdict", "check", "max_bytes"]
