"""
ITR acknowledgement rows read the way an OCR'd page prints them (2026-11-12).

MEASURED NEED: on 15 real scanned acknowledgements the name and all four tax figures were read on 0 of 15. The
text-layer parser expects a caption ALONE on its line with the value below; OCR puts them on ONE line, with the row
number in between ("Total Income 2 9,99,999"), reads a "," as "." ("9.999"), and garbles caption words. Rows are
found here by meaning (config financial_documents.yaml itr_captions), the amount is read from the caption's own line
or the lines below, and when a row could be read two ways the reading under which the acknowledgement's own
arithmetic holds -- payable - paid = payable/(-)refundable -- is the one taken.

Nothing here is corrected or invented: a value is what the page shows, read one way or another.
"""

from __future__ import annotations

import itertools
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

FIGURES = ("total_income", "net_tax_payable", "total_tax_and_interest_payable", "taxes_paid",
           "tax_payable_or_refundable")

#: "(7-8)", "(8 - 9)", "(13-14)" -- arithmetic printed in a caption, never a value
_ARITH_RE = re.compile(r"\(\s*\d{1,2}\s*[a-z]?\s*-\s*\d{1,2}\s*[a-z]?\s*\)", re.IGNORECASE)
#: "u/s 140B", "section 115TD", "115TE" -- a section number, never a value
_SECTION_RE = re.compile(r"(?:u\s*/\s*s|section)\s*\d+[A-Z]*\b|\b\d{2,3}[A-Z]{1,2}\b", re.IGNORECASE)
#: an amount token, with an optional sign marker in front: "2,27,550", "(-) 12,345", "9.999", "1.16.660.00"
_TOKEN_RE = re.compile(r"(\(\s*[-+]\s*\)\s*|(?<![\w/])-\s?)?(?<![\w.,/])(\d[\d.,]*\d|\d)(?![\w])")
_NAME_STOP_RE = re.compile(r"\s+(?:address|status|pan|form\s+number|filed\s+u/s)\b.*$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _rules() -> dict:
    import yaml

    path = Path(__file__).resolve().parents[2] / "config" / "financial_documents.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("itr_captions") or {}


def _norm(text: str) -> str:
    """lower-case, fancy brackets plain, commas gone, no spaces around / ( ), single spaces."""
    s = (text or "").lower().replace("（", "(").replace("）", ")").replace("，", ",").replace("&", " & ")
    s = re.sub(r"(?<!\d),|,(?!\d)", " ", s)                  # a caption's comma goes; an amount's "2,27,550" stays
    s = re.sub(r"\s*([/()])\s*", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def amount(token: str, sign: str = "") -> Decimal | None:
    """
    An acknowledgement amount: whole rupees, Indian grouping. A "." that groups ("9.999", "2.27.550") is read as the
    "," OCR took it for; a final ".dd" is paise.
    """
    t = token.strip()
    if not t or not t[0].isdigit():
        return None
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{2,3})*[.,]\d{3}", t):          # every separator groups
        value = t.replace(",", "").replace(".", "")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{2,3})*[.,]\d{2,3}[.,]\d{2}", t) or re.fullmatch(r"\d+[.]\d{1,2}", t):
        value = t[:-3].replace(",", "").replace(".", "") + "." + t[-2:]
    elif re.fullmatch(r"\d+", t):
        value = t
    else:
        return None
    try:
        out = Decimal(value)
    except InvalidOperation:
        return None
    return -out if sign else out


def _numbers(text: str) -> list[tuple[Decimal, str]]:
    """(value, raw) for every amount on a stretch of text, captions' arithmetic and section numbers removed."""
    cleaned = _SECTION_RE.sub(" ", _ARITH_RE.sub(" ", text))
    out = []
    for marker, raw in _TOKEN_RE.findall(cleaned):
        sign = "-" if marker and "-" in marker else ""
        value = amount(raw, sign)
        if value is not None:
            out.append((value, raw))
    return out


def _is_label(value: Decimal, raw: str, max_label: int) -> bool:
    return "," not in raw and "." not in raw and 0 < value <= max_label


def _candidates(values: list[tuple[Decimal, str]], max_label: int) -> list[Decimal]:
    """The value(s) after a caption, best first: the row label in front is skipped; a lone small number may be
    either the label or the value, so both readings are offered (the arithmetic decides)."""
    if not values:
        return []
    if len(values) == 1:
        v, raw = values[0]
        # a lone small number is most likely the ROW LABEL with the value lost (OCR drops a lone "0"): "not read"
        # comes first, and the number is only taken as the value when the arithmetic then holds (choose)
        return [v] if not _is_label(v, raw, max_label) else [None, v]
    rest = values[1:] if _is_label(*values[0], max_label) else values
    grouped = [v for v, raw in rest if "," in raw or "." in raw or v > max_label]
    ordered = grouped + [v for v, _ in rest if v not in grouped]
    return list(dict.fromkeys(ordered))


def _compact(text: str) -> tuple[str, list[int]]:
    """The text without spaces, and where each kept character sits in the original (OCR glues and splits words:
    "income-taxliability", "asper", "Taxes  Paid")."""
    keep = [i for i, ch in enumerate(text) if ch != " "]
    return "".join(text[i] for i in keep), keep


def _matches(line_norm: str, rule: dict) -> int:
    """Where the caption ends in the line (-1 = no match). Compared without spaces, so a glued or split OCR word
    still matches; the end is mapped back onto the line, where the amount is read."""
    flat, where = _compact(line_norm)
    if not flat or any(_compact(_norm(n))[0] in flat for n in rule.get("not") or []):
        return -1
    for phrase in rule.get("any") or []:
        p = _compact(_norm(phrase))[0]
        at = flat.find(p)
        while at >= 0:
            before = flat[max(0, at - 10):at]
            after = flat[at + len(p):at + len(p) + 24]
            if not any(before.endswith(_compact(w)[0]) for w in rule.get("not_before") or []) and \
                    not any(_compact(_norm(w))[0] in after for w in rule.get("not_after") or []):
                return where[at + len(p) - 1] + 1
            at = flat.find(p, at + 1)
    return -1


def _row_values(lines: list[str], index: int, end: int, max_label: int) -> list[Decimal | None]:
    """Values for the caption on lines[index]: its own line after the caption, else the next lines that are only a
    number (a digital acknowledgement prints the value in the cell below)."""
    own = _norm(lines[index])[end:]
    found = _candidates(_numbers(own), max_label)
    if any(v is not None for v in found):
        return found
    for nxt in lines[index + 1:index + 4]:
        stripped = nxt.strip()
        if not stripped:
            continue
        if re.search(r"[a-z]{3,}", stripped.lower()):                 # the next caption: this row had no value here
            break
        found = _candidates(_numbers(stripped), max_label)
        if any(v is not None for v in found):
            return found
    return []


def read_figures(text: str) -> dict[str, list[Decimal | None]]:
    """field -> candidate values (best first) from the first row that matches each field."""
    rules = _rules()
    max_label = int(rules.get("max_row_label", 30))
    lines = text.split("\n")
    out: dict[str, list[Decimal | None]] = {}
    for field in FIGURES:
        rule = rules.get(field) or {}
        for i, line in enumerate(lines):
            end = _matches(_norm(line), rule)
            if end < 0:
                continue
            values = _row_values(lines, i, end, max_label)
            if any(v is not None for v in values):
                out[field] = values
                break
    return out


def choose(candidates: dict[str, list[Decimal | None]], tolerance: Decimal, rounding: Decimal) -> dict[str, Decimal | None]:
    """One value per field; among the readings of payable / paid / refundable, the one that reconciles wins."""
    chosen = {f: (vals[0] if vals else None) for f, vals in candidates.items()}      # best first; may be "not read"
    keys = ("total_tax_and_interest_payable", "taxes_paid", "tax_payable_or_refundable")
    if all(k in candidates for k in keys):
        options = [candidates[k][:3] or [None] for k in keys]
        for payable, paid, net in itertools.product(*options):
            if payable is None or paid is None or net is None:
                continue
            diff = abs((payable - paid) - net)
            rounded = rounding > 0 and net % rounding == 0 and diff <= rounding / 2
            if diff <= tolerance or rounded:
                chosen.update(dict(zip(keys, (payable, paid, net))))
                break
    return chosen


def name_inline(lines: list[str]) -> str | None:
    """'Name AAAA BBBB' on one OCR'd line (the text layer prints the caption alone, the value below)."""
    for line in lines:
        m = re.match(r"^\s*name\s*[:\-]?\s+(.+)$", line, re.IGNORECASE)
        if not m:
            continue
        value = _NAME_STOP_RE.sub("", m.group(1)).strip(" :-.,")
        value = re.sub(r"[^A-Za-z .']", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        if len(re.sub(r"[^A-Za-z]", "", value)) >= 3:
            return value.upper()
    return None


def form_number(text: str) -> str | None:
    """The form under 'Form Number' -- not the first 'ITR-n' of the header's list of forms."""
    m = re.search(r"Form\s*Number\s*[:\-]?\s*(?:\n\s*)?ITR[-\s]?([1-7]|U)\b", text, re.IGNORECASE)
    return f"ITR-{m.group(1).upper()}" if m else None


__all__ = ["FIGURES", "amount", "choose", "form_number", "name_inline", "read_figures"]
