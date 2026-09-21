"""
ITR extraction.

Every ITR form -- ITR-1 through ITR-7 -- produces the SAME acknowledgement
(ITR-V) when filed. Rather than writing seven form-specific parsers, this
reads that common acknowledgement and records which form produced it. That is
both less code and more robust: a new form revision changes the return
schedules, not the acknowledgement layout.

OCR is used only when the PDF carries no text layer. An e-filed ITR-V is
always a digital PDF, so the fast path is the normal one.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.agents.itr.schemas import ITRResult, ITRStatus

logger = logging.getLogger(__name__)

MIN_CHARS_FOR_DIGITAL = 300

# The acknowledgement prints a caption and its value on separate lines, so a
# field is located by its caption and read from what follows.
_PAN_RE = re.compile(r"\b([A-Z]{3}[ABCFGHLJPTKE][A-Z]\d{4}[A-Z])\b")
_FORM_RE = re.compile(r"\bITR[-\s]?([1-7])\b", re.IGNORECASE)
_AY_RE = re.compile(r"\b(20\d{2}\s*-\s*\d{2})\b")
_ACK_RE = re.compile(r"Acknowledgement\s*Number\s*:?\s*(\d{9,20})", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2}[-/][A-Za-z]{3}[-/]\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\b")

_AMOUNT_RE = re.compile(r"^-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?$|^-?\d+(?:\.\d{1,2})?$")


def _parse_amount(text: str) -> Decimal | None:
    value = (text or "").strip().replace(" ", "")
    if not value or not _AMOUNT_RE.match(value):
        return None
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _parse_date(text: str) -> "datetime.date | None":
    raw = (text or "").strip()
    for fmt in ("%d-%b-%Y", "%d/%b/%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _value_after(lines: list[str], caption: str, within: int = 4) -> str | None:
    """
    The value printed beneath a caption.

    The acknowledgement is a two-column form flattened into text, so a
    caption's value is on one of the next few lines rather than the same one.
    Blank lines and the row numbers that label each figure are skipped.
    """
    target = caption.lower()
    for index, line in enumerate(lines):
        if target not in line.lower():
            continue
        for offset in range(1, within + 1):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset].strip()
            if not candidate:
                continue
            # Row numbers ("1", "2a") label the figure, they are not the value.
            if re.fullmatch(r"\d{1,2}[A-Za-z]?", candidate):
                continue
            return candidate
    return None


# Row labels on the acknowledgement run from 1 to about 17 and are printed
# immediately before each figure. They parse as perfectly good numbers, so
# taking the first amount after a caption returned the row label rather than
# the value -- "Net tax payable" came back as 4.
_MAX_ROW_LABEL = 30


def _amount_after(lines: list[str], caption: str, within: int = 6) -> Decimal | None:
    """
    The value beneath a caption, skipping the row label that precedes it.

    Both the label and the value are bare numbers, so they are told apart by
    shape: a label is a small plain integer with no grouping, and it is
    followed by another number. When only one number follows the caption, it
    is the value.
    """
    target = caption.lower()
    for index, line in enumerate(lines):
        if target not in line.lower():
            continue

        found: list[tuple[Decimal, str]] = []
        for offset in range(1, within + 1):
            if index + offset >= len(lines):
                break
            raw = lines[index + offset].strip()
            amount = _parse_amount(raw)
            if amount is not None:
                found.append((amount, raw))
            if len(found) >= 2:
                break

        if not found:
            continue
        if len(found) == 1:
            return found[0][0]

        first_value, first_raw = found[0]
        looks_like_row_label = (
            "," not in first_raw
            and "." not in first_raw
            and 0 < first_value <= _MAX_ROW_LABEL
        )
        return found[1][0] if looks_like_row_label else first_value
    return None


def _multiline_value(lines: list[str], caption: str, stop_at: tuple[str, ...]) -> str | None:
    """Collect the lines under a caption until the next known caption."""
    target = caption.lower()
    for index, line in enumerate(lines):
        if target != line.strip().lower():
            continue
        collected: list[str] = []
        for follow in lines[index + 1:]:
            stripped = follow.strip()
            if not stripped:
                continue
            if any(stripped.lower().startswith(s.lower()) for s in stop_at):
                break
            collected.append(stripped)
            if len(collected) >= 10:
                break
        joined = " ".join(collected)
        joined = re.sub(r"\s*,\s*", ", ", joined)
        return re.sub(r"\s{2,}", " ", joined).strip(" ,-") or None
    return None


def _page_texts(path: str) -> list[str]:
    from pypdf import PdfReader

    try:
        return [(page.extract_text() or "") for page in PdfReader(path).pages]
    except Exception as exc:
        logger.warning("Could not read ITR PDF: %s", exc)
        return []


def _ocr_texts(path: str) -> list[str]:
    """OCR fallback for a scanned acknowledgement."""
    try:
        from pdf2image import convert_from_path

        from app.agents.document_agent import preprocess as PP
        from app.agents.document_agent.ocr import get_engine
    except ImportError as exc:
        logger.debug("ITR OCR unavailable: %s", exc)
        return []

    try:
        images = convert_from_path(path, dpi=200)
    except Exception as exc:
        logger.warning("Could not rasterise ITR PDF: %s", exc)
        return []

    engine = get_engine()
    out: list[str] = []
    for image in images[: max_ocr_pages()]:
        try:
            tokens, _ = engine.read_array(PP.to_array(PP.standard(image.convert("RGB"))))
        except Exception as exc:
            logger.warning("ITR OCR failed on a page: %s", exc)
            out.append("")
            continue
        ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))
        lines: list[list] = [[ordered[0]]] if ordered else []
        for token in ordered[1:]:
            last = lines[-1][-1]
            if abs(token.cy - last.cy) <= max(6.0, last.height * 0.6):
                lines[-1].append(token)
            else:
                lines.append([token])
        out.append("\n".join(
            " ".join(t.text for t in sorted(line, key=lambda x: x.x0))
            for line in lines
        ))
    return out


def max_ocr_pages() -> int:
    raw = (os.getenv("ITR_MAX_OCR_PAGES") or "").strip()
    try:
        return int(raw) if raw else 3
    except ValueError:
        return 3


def ocr_enabled() -> bool:
    return (os.getenv("ITR_OCR_ENABLED", "true") or "true").lower() == "true"


def extract_itr(path: str) -> ITRResult:
    """Read an ITR acknowledgement. Digital PDFs need no OCR."""
    started = time.perf_counter()

    if not Path(path).exists():
        return ITRResult(status=ITRStatus.FAILED, errors=[f"File not found: {path}"])

    texts = _page_texts(path)
    pages = len(texts)
    joined = "\n".join(texts)
    source = "DIGITAL"

    if len(joined.strip()) < MIN_CHARS_FOR_DIGITAL:
        if not ocr_enabled():
            return ITRResult(
                status=ITRStatus.REQUIRES_OCR,
                source_kind="SCANNED",
                pages=pages,
                warnings=[
                    "No text layer. OCR is disabled for ITR; route this to the "
                    "asynchronous queue or set ITR_OCR_ENABLED=true."
                ],
                processing_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        texts = _ocr_texts(path)
        joined = "\n".join(texts)
        pages = pages or len(texts)
        source = "SCANNED"

    if not joined.strip():
        return ITRResult(
            status=ITRStatus.FAILED,
            source_kind=source,
            pages=pages,
            errors=["No readable text in the document."],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    lines = [l for l in joined.split("\n")]
    result = ITRResult(status=ITRStatus.SUCCESS, source_kind=source, pages=pages)

    ack = _ACK_RE.search(joined)
    result.acknowledgement_number = ack.group(1) if ack else None

    form = _FORM_RE.search(joined)
    result.form_number = f"ITR-{form.group(1)}" if form else None

    ay = _AY_RE.search(joined)
    result.assessment_year = re.sub(r"\s", "", ay.group(1)) if ay else None

    filing = re.search(r"Date\s+of\s+filing\s*:?\s*([0-9A-Za-z/-]+)", joined, re.IGNORECASE)
    result.filing_date = _parse_date(filing.group(1)) if filing else None

    pan = _PAN_RE.search(joined)
    result.pan = pan.group(1) if pan else None

    name = _multiline_value(lines, "Name", stop_at=("Address", "Status", "PAN"))
    if name:
        result.name = re.sub(r"\s+", " ", name).upper()
        from app.agents.document_agent.normalize import name_key

        result.name_match_key = name_key(result.name)

    result.address = _multiline_value(
        lines, "Address", stop_at=("Status", "Form Number", "Filed u/s")
    )
    result.status_of_taxpayer = _value_after(lines, "Status")
    result.filed_under_section = _value_after(lines, "Filed u/s")

    result.total_income = _amount_after(lines, "Total Income")
    result.current_year_business_loss = _amount_after(
        lines, "Current Year business loss"
    )
    result.book_profit_mat = _amount_after(lines, "Book Profit under MAT")
    result.net_tax_payable = _amount_after(lines, "Net tax payable")
    result.total_tax_and_interest_payable = _amount_after(
        lines, "Total tax, interest and Fee payable"
    )
    result.taxes_paid = _amount_after(lines, "Taxes Paid")
    result.tax_payable_or_refundable = _amount_after(lines, "Tax Payable /(-) Refundable")

    verified = re.search(r"and\s+veri[fﬁ]ed\s+by\s*\n?\s*([A-Z][A-Z\s]{2,60})", joined)
    result.verified_by = re.sub(r"\s+", " ", verified.group(1)).strip() if verified else None

    mode = re.search(r"generated\s+through\s*\n?\s*([A-Za-z\s]{3,40})mode", joined)
    result.verification_mode = mode.group(1).strip() if mode else None

    # Required for the document to be usable: without a PAN and an assessment
    # year the return cannot be tied to an applicant or a period.
    required = {
        "pan": result.pan,
        "assessment_year": result.assessment_year,
        "name": result.name,
    }
    missing = [k for k, v in required.items() if not v]
    present = sum(
        1 for v in (
            result.acknowledgement_number, result.form_number,
            result.assessment_year, result.pan, result.name,
            result.total_income, result.status_of_taxpayer,
        ) if v is not None
    )
    result.confidence = round(present / 7, 4)

    if missing:
        result.status = ITRStatus.PARTIAL
        result.warnings.append(f"Required field(s) not found: {', '.join(missing)}")

    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


__all__ = ["extract_itr", "ocr_enabled", "max_ocr_pages"]
