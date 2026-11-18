"""
IFSC: format, OCR repair and the authoritative local master (app/data/ifsc/ifsc.sqlite, built by
scripts/build_ifsc_master.py from the RBI-derived Razorpay dataset). Read-only, offline, one connection per thread.

An IFSC is 4 letters (the bank), a literal 0, then 6 letters/digits (the branch). OCR confuses O/0, I/1, S/5, B/8
inside it; the repair below fixes only positions where the format leaves no doubt (letters in 1-4, the zero in 5)
and never invents a character. Branch characters 6-11 are left as read: guessing there could turn one real branch
into another real branch.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MASTER = Path(__file__).resolve().parents[2] / "data" / "ifsc" / "ifsc.sqlite"
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# Somewhere in a line of OCR text: 4 letter-like, a zero-like, 6 alphanumerics.
IFSC_IN_TEXT = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4}[0O][A-Z0-9]{6})(?![A-Z0-9])")
_TO_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}
_local = threading.local()


@dataclass(frozen=True)
class Branch:
    ifsc: str
    bank_code: str
    bank: str | None
    branch: str | None
    city: str | None
    district: str | None
    state: str | None
    micr: str | None


def available() -> bool:
    return MASTER.is_file()


def _db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{MASTER.as_posix()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        _local.conn = conn
    return conn


def repair(raw: str | None) -> str | None:
    """OCR text -> an IFSC-shaped string, or None. Fixes letters in the bank code and the 5th-character zero only."""
    if not raw:
        return None
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(s) != 11:
        return None
    bank = "".join(_TO_LETTER.get(c, c) for c in s[:4])
    fifth = "0" if s[4] in "0O" else s[4]
    candidate = bank + fifth + s[5:]
    return candidate if IFSC_RE.match(candidate) else None


_IFSC_SHAPE = re.compile(r"[A-Z0-9]{4}[0O][A-Z0-9]{6}")
_IFSC_LABEL_PREFIX = ("IFSCCODE", "IFSC", "IFS", "CODE")


def find_in_text(text: str) -> list[str]:
    """Every IFSC-shaped string in a piece of OCR text, repaired, in reading order, de-duplicated.

    WORD BY WORD, never glued (2026-10-17, real passbooks): deleting spaces / colons / dots first glued
    "Y/Reg No. 2014-48" into "RAGNO201448" -> a made-up "RAGN0201448", and hid "Code:UB1N0919225" inside
    "CODEUB1N0919225". A code split in two by the OCR ("SBIN 0020408") is joined; a label stuck to the code
    ("IFSCSBIN0020408") is cut off."""
    parts = [p.strip(".-") for p in re.split(r"[\s:;,|()/\\]+", (text or "").upper()) if p.strip(".-")]
    candidates: list[str] = []
    for i, p in enumerate(parts):
        candidates.append(p)
        if i + 1 < len(parts) and len(p) == 4 and len(parts[i + 1]) == 7:
            candidates.append(p + parts[i + 1])
        for prefix in _IFSC_LABEL_PREFIX:
            if p.startswith(prefix) and len(p) == len(prefix) + 11:
                candidates.append(p[len(prefix):])
    out: list[str] = []
    for c in candidates:
        if len(c) == 11 and _IFSC_SHAPE.fullmatch(c):
            fixed = repair(c)
            if fixed and fixed not in out:
                out.append(fixed)
    return out


@lru_cache(maxsize=4096)
def lookup(ifsc: str) -> Branch | None:
    """The branch of a well-formed IFSC, None when the master does not list it (or the master is absent).

    A SUBLET IFSC (a co-operative bank clearing through a sponsor, e.g. an HDFC-prefixed code that belongs to a
    co-operative bank) reports the bank that actually holds the account, not the sponsor its prefix names."""
    if not ifsc or not IFSC_RE.match(ifsc) or not available():
        return None
    row = _db().execute("SELECT ifsc, bank_code, bank, branch, city, district, state, micr FROM branch "
                        "WHERE ifsc = ?", (ifsc,)).fetchone()
    if not row:
        return None
    sublet = _db().execute("SELECT bank_code FROM sublet WHERE ifsc = ?", (ifsc,)).fetchone()
    if sublet:
        code = sublet[0]
        row = (row[0], code, bank_name(code) or row[2], *row[3:])
    return Branch(*row)


@lru_cache(maxsize=2048)
def bank_name(code: str) -> str | None:
    if not code or not available():
        return None
    row = _db().execute("SELECT name FROM bank WHERE code = ?", (code.upper(),)).fetchone()
    return row[0] if row and row[0] else None


@lru_cache(maxsize=1)
def bank_names() -> dict[str, str]:
    """code -> bank name, every bank in the master."""
    if not available():
        return {}
    return {code: name for code, name in _db().execute("SELECT code, name FROM bank WHERE name IS NOT NULL")}


@lru_cache(maxsize=1)
def master_info() -> dict[str, str]:
    if not available():
        return {}
    return dict(_db().execute("SELECT key, value FROM meta"))


__all__ = ["Branch", "IFSC_RE", "available", "bank_name", "bank_names", "find_in_text", "lookup", "master_info",
           "repair"]
