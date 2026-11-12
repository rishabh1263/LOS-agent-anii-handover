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
from functools import lru_cache
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
    # a sign printed as a marker in front ("(-) 12,345" is a refund): config itr.negative_markers / positive_markers
    for marker, sign in [(m, "-") for m in _itr_config().get("negative_markers") or []] + \
                        [(m, "") for m in _itr_config().get("positive_markers") or []]:
        marker = str(marker).replace(" ", "")
        if marker and value.startswith(marker) and len(value) > len(marker):
            value = sign + value[len(marker):]
            break
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

    # A PHOTOGRAPHED / SCANNED ITR UPLOADED AS AN IMAGE is read as that image:
    # rasterising it as a PDF produced nothing, and every image ITR read as
    # "unreadable" (real samples ITR.jpeg, ITR_2.jpeg).
    if Path(path).suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"):
        try:
            from PIL import Image

            images = [Image.open(path)]
        except Exception as exc:
            logger.warning("Could not open ITR image: %s", exc)
            return []
    else:
        try:
            # ONLY THE PAGES THAT WILL BE READ (2026-11-02, audit G7): every page was rendered at 200 DPI and all but
            # max_ocr_pages() thrown away -- time and memory spent on a long scan for nothing
            images = convert_from_path(path, dpi=200, first_page=1, last_page=max(1, max_ocr_pages()))
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


#: field -> the caption it is read under (the evidence: where on the acknowledgement a value came from)
_CAPTIONS = {
    "acknowledgement_number": "Acknowledgement Number", "assessment_year": "Assessment Year",
    "pan": "PAN", "name": "Name", "total_income": "Total Income", "net_tax_payable": "Net tax payable",
    "total_tax_and_interest_payable": "Total tax, interest and Fee payable", "taxes_paid": "Taxes Paid",
    "tax_payable_or_refundable": "Tax Payable /(-) Refundable",
}


def _evidence(result: ITRResult, texts: list[str], source: str) -> None:
    """For each field read: the page its caption is on and how the page was read. Never a value of a PAN."""
    method = "OCR" if source == "SCANNED" else "TEXT_LAYER"
    for field, caption in _CAPTIONS.items():
        if getattr(result, field, None) is None:
            continue
        page = next((i for i, t in enumerate(texts, start=1) if caption.lower() in t.lower()), None)
        result.evidence[field] = {"page": page, "method": method, "caption": caption}


@lru_cache(maxsize=1)
def _itr_config() -> dict:
    import yaml

    path = Path(__file__).resolve().parents[2] / "config" / "financial_documents.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("itr") or {}


def _tolerance() -> Decimal:
    return Decimal(str(_itr_config().get("reconciliation_tolerance_rupees")))


def _reconcile(result: ITRResult) -> None:
    """(total tax, interest and fee payable) - (taxes paid) = (tax payable / (-) refundable), within the configured
    tolerance. A mismatch is a misread or an inconsistent document: PARTIAL (a person reviews), the values kept."""
    payable, paid, net = (result.total_tax_and_interest_payable, result.taxes_paid,
                          result.tax_payable_or_refundable)
    if payable is None or paid is None or net is None:
        result.reconciliation = "INCOMPLETE" if any(v is not None for v in (payable, paid, net)) else "NOT_ATTEMPTED"
        return
    diff = abs((payable - paid) - net)
    rounding = Decimal(str(_itr_config().get("payable_rounding_rupees") or 0))
    rounded = rounding > 0 and net % rounding == 0 and diff <= rounding / 2      # s.288B rounding of the printed amount
    if diff <= _tolerance() or rounded:
        result.reconciliation = "RECONCILED"
        return
    result.reconciliation = "MISMATCH"
    result.status = ITRStatus.PARTIAL
    result.warnings.append("The acknowledgement's figures do not add up (tax payable - taxes paid differs from the "
                           "payable / refundable amount): review the document; nothing was corrected.")


def _parse(texts: list[str], source: str, pages: int) -> ITRResult:
    """Every field from the page texts. The text-layer readings come first; the OCR-shaped readings
    (app/agents/itr/figures.py) are added as candidates and the acknowledgement's arithmetic picks among them."""
    from app.agents.itr import figures as FG

    joined = "\n".join(texts)
    lines = [l for l in joined.split("\n")]
    result = ITRResult(status=ITRStatus.SUCCESS, source_kind=source, pages=pages)

    ack = _ACK_RE.search(joined)
    result.acknowledgement_number = ack.group(1) if ack else None

    # the form under "Form Number", not the first ITR-n of the header's list of forms (it always said ITR-1)
    form = FG.form_number(joined) or None
    if not form:
        found = _FORM_RE.search(joined)
        form = f"ITR-{found.group(1)}" if found else None
    result.form_number = form

    ay = _AY_RE.search(joined)
    result.assessment_year = re.sub(r"\s", "", ay.group(1)) if ay else None

    filing = re.search(r"Date\s+of\s+f\w{0,3}ing\s*:?\s*([0-9A-Za-z/-]+)", joined, re.IGNORECASE)   # "fling" (OCR)
    result.filing_date = _parse_date(filing.group(1)) if filing else None

    pan = _PAN_RE.search(joined)
    result.pan = pan.group(1) if pan else None

    name = _multiline_value(lines, "Name", stop_at=("Address", "Status", "PAN")) or FG.name_inline(lines)
    # "verified" as OCR / a plain text layer prints it, and "veriﬁed" with the PDF ligature (only the ligature
    # matched before 2026-11-12: a scanned acknowledgement's verifier was never read)
    verified = re.search(r"and\s+veri(?:fi|ﬁ)ed\s+by\s*\n?\s*([A-Z][A-Z\s]{2,60})", joined)
    result.verified_by = re.sub(r"\s+", " ", verified.group(1)).strip() if verified else None
    if not name and result.verified_by and result.pan:
        # the verifier is the assessee when the verification line carries the return's own PAN
        own = re.search(r"veri(?:fi|ﬁ)ed\s+by[\s\S]{0,80}?having\s+PAN\s+([A-Z0-9]{10})", joined)
        if own and own.group(1) == result.pan:
            name = re.sub(r"\s+(having|PAN)\b.*$", "", result.verified_by)
    if name and result.verified_by and " " in result.verified_by.strip():
        # OCR drops a space in the name ("KAVITANAIR"); the verification line, carrying the same name, keeps it
        spaced = re.sub(r"\s+(having|PAN)\b.*$", "", result.verified_by).strip()
        if (re.sub(r"\s", "", spaced).upper() == re.sub(r"\s", "", name).upper()
                and spaced.count(" ") > name.strip().count(" ")):
            name = spaced
    if name:
        result.name = re.sub(r"\s+", " ", name).upper()
        from app.agents.document_agent.normalize import name_key

        result.name_match_key = name_key(result.name)

    result.address = _multiline_value(
        lines, "Address", stop_at=("Status", "Form Number", "Filed u/s")
    )
    result.status_of_taxpayer = _value_after(lines, "Status")
    result.filed_under_section = _value_after(lines, "Filed u/s")

    old = {
        "total_income": _amount_after(lines, "Total Income"),
        "net_tax_payable": _amount_after(lines, "Net tax payable"),
        "total_tax_and_interest_payable": _amount_after(lines, "Total tax, interest and Fee payable"),
        "taxes_paid": _amount_after(lines, "Taxes Paid"),
        "tax_payable_or_refundable": _amount_after(lines, "Tax Payable /(-) Refundable"),
    }
    new = FG.read_figures(joined)
    candidates = {f: list(dict.fromkeys(([old[f]] if old[f] is not None else []) + new.get(f, [])))
                  for f in FG.FIGURES}
    candidates = {f: v for f, v in candidates.items() if v}
    rounding = Decimal(str(_itr_config().get("payable_rounding_rupees") or 0))
    chosen = FG.choose(candidates, _tolerance(), rounding)
    for field in FG.FIGURES:
        setattr(result, field, chosen.get(field))

    result.current_year_business_loss = _amount_after(lines, "Current Year business loss")
    result.book_profit_mat = _amount_after(lines, "Book Profit under MAT")

    mode = re.search(r"generated\s+through\s*\n?\s*([A-Za-z\s]{3,40})mode", joined)
    result.verification_mode = mode.group(1).strip() if mode else None

    _evidence(result, texts, source)
    _reconcile(result)
    return result


def _complete(result: ITRResult) -> bool:
    """Enough read to stop OCR'ing pages: who, which year, the income, and figures that add up."""
    return bool(result.pan and result.assessment_year and result.name and result.total_income is not None
                and result.reconciliation == "RECONCILED")


def _filled(result: ITRResult) -> int:
    return sum(1 for f in ("acknowledgement_number", "assessment_year", "pan", "name", "total_income",
                           "taxes_paid", "tax_payable_or_refundable") if getattr(result, f) is not None)


def _best_acknowledgement(texts: list[str], source: str, pages: int) -> ITRResult:
    """
    A BUNDLE (computation of income + returns of several years, 6-10 pages) holds more than one acknowledgement:
    each acknowledgement page is read on its own and the latest assessment year wins (then the most fields read).
    One acknowledgement, or none (a computation sheet only): the whole document is read as one.
    """
    ack_pages = [t for t in texts if re.search(r"acknowledgement\s*number", t, re.IGNORECASE)]
    if len(ack_pages) <= 1:
        return _parse(texts, source, pages)
    parsed = [_parse([t], source, pages) for t in ack_pages]
    return max(parsed, key=lambda r: ((r.assessment_year or ""), r.reconciliation == "RECONCILED", _filled(r)))


def _finish(result: ITRResult, started: float) -> ITRResult:
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


def extract_itr(path: str) -> ITRResult:
    """Read an ITR acknowledgement. Digital PDFs need no OCR; a scan is OCR'd page by page and stops early."""
    started = time.perf_counter()

    if not Path(path).exists():
        return ITRResult(status=ITRStatus.FAILED, errors=[f"File not found: {path}"])

    texts = _page_texts(path) if Path(path).suffix.lower() == ".pdf" else []
    pages = len(texts)
    joined = "\n".join(texts)

    if len(joined.strip()) >= MIN_CHARS_FOR_DIGITAL:
        return _finish(_best_acknowledgement(texts, "DIGITAL", pages), started)

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

    # SCANNED / PHOTOGRAPHED: page by page (app/agents/document_agent/scan_text.py), stopping at the first page set
    # whose read is complete -- an acknowledgement is one page; the pages after it were OCR'd for nothing.
    from app.agents.document_agent import scan_text

    early = bool(scan_text._scan_config("ITR").get("early_stop", False))
    ocr_texts: list[str] = []
    result: ITRResult | None = None
    for text, _info in scan_text.iter_scanned_pages(path, document_type="ITR", max_pages=max(1, max_ocr_pages())):
        ocr_texts.append(text)
        if not "\n".join(ocr_texts).strip():
            continue
        result = _parse(ocr_texts, "SCANNED", max(pages, len(ocr_texts)))
        if early and _complete(result):
            break

    if result is None:
        return ITRResult(
            status=ITRStatus.FAILED,
            source_kind="SCANNED",
            pages=pages,
            errors=["No readable text in the document."],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    return _finish(result, started)


__all__ = ["extract_itr", "ocr_enabled", "max_ocr_pages"]
