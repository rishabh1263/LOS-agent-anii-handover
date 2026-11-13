"""
Salary slip extraction.

Reads a form-style layout common to Indian payroll software (eHCMS, greytHR,
Keka and similar): a header of label/value pairs (Employee Code, Name, PAN,
UAN...), followed by an Earnings table and a Deductions table, each ending in
a "Total" row, and a Net Pay figure.

pypdf extracts this kind of PDF one CELL per line -- a caption on one line,
its value on the next -- not "Label : Value" on a single line the way the
text visually reads. This is the same pattern the crowded-header Driving
Licence layout produced, and is handled the same way: locate the caption,
then read the value from the line(s) immediately following it, rather than
searching the caption's own line.

Every caption is matched by MEANING through a synonym list, not one fixed
wording -- "Net Pay" on this payslip, "Net Salary" or "Take Home" on
another, mirroring the Sale Deed's First Party/Vendor/Seller captions.
Nothing here is tied to a specific employer, employee, or amount: the
extractor was built by reading one real payslip's structure, but every value
it looks for is found by its caption, never by its position on the page or
by matching this payslip's own numbers.

VERIFICATION STATUS: proven on one real slip (one payroll software, one
employer). The net-pay arithmetic check is the load-bearing signal -- it is
what a caller should trust, not the presence of fields alone.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.agents.salary_slip.schemas import SalarySlipResult, SalarySlipStatus

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(
    r"^-?(?:"
    r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?"   # 1,23,456.00 -- grouped
    r"|\d+(?:\.\d{1,2})?"                   # 31986.00 -- plain, any length
    r")$"
)

# Caption synonyms. Each tuple is tried in order; the first that matches a
# line wins. Extending coverage to another payroll system means adding a
# variant here, not writing a new extractor.
# "Name of Workman" is the statutory FORM XIX wording used on contractor wage
# slips, which carry none of the corporate captions but are a real and common
# income document for contract-labour applicants.
_EMPLOYEE_NAME_CAPTIONS = (
    "Employee Name", "Name of Employee", "Emp Name",
    "Name of Workman", "Workman Name",
)
_EMPLOYEE_CODE_CAPTIONS = ("Employee Code", "Employee ID", "Emp Code", "Emp ID")
_DESIGNATION_CAPTIONS = ("Designation", "Job Title")
_DOJ_CAPTIONS = ("Date of Joining", "DOJ", "Joining Date")
_PAN_CAPTIONS = ("PAN No.", "PAN No", "PAN Number", "PAN")
_UAN_CAPTIONS = ("UAN NO.", "UAN No", "UAN Number", "UAN")

# The trailing FORM XIX variants are singular ("Gross Earning") or worded
# differently ("Net paid amount"); they are listed after the corporate
# captions so an ordinary payslip still matches on its own wording first.
_GROSS_CAPTIONS = (
    "Total Earnings", "Gross Earnings", "Total Salary", "Gross Salary",
    "Gross Earning", "Gross Wages",
)
_BASIC_CAPTIONS = (
    "Basic Salary", "Basic Pay", "BASIC", "Basic Wages",
)
_DEDUCTIONS_CAPTIONS = (
    "Total Deductions", "Total Deduction", "Deductions, if any",
)
_NET_PAY_CAPTIONS = (
    "Net Pay", "Net Salary", "Take Home Pay", "Net Amount",
    "Net paid amount", "Net Paid Amount",
)

# A company letterhead virtually always carries a legal-entity suffix. Used
# instead of "the first line on the page", because a form PDF's internal text
# order does not reliably put the letterhead first -- on a real slip it was
# roughly line 50 of over 100, with several unrelated tables ahead of it in
# extraction order despite appearing at the top of the printed page.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(LIMITED|LTD\.?|PVT\.?\s*LTD\.?|PRIVATE\s+LIMITED|LLP|INC\.?|CORP(?:ORATION)?\.?)\b",
    re.IGNORECASE,
)

_MONTH_RE = re.compile(
    r"\b(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|"
    r"AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
    r"\s*(\d{4})\b",                       # "JUNE 2025", and "JUNE2025" as OCR glues it
    re.IGNORECASE,
)


_GLUED_MONTH_RE = re.compile(
    r"(?:MONTHOF|PERIOD|FORTHEMONTH|SALARYFOR|PAYSLIPFOR)(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEPT|SEP|OCT|NOV|DEC)[-/,]?(\d{4})")

#: A company suffix OCR glued to the name ("ACMEINDUSTRIESLIMITED").
_GLUED_SUFFIX_RE = re.compile(r"(?<=[A-Z])(PRIVATELIMITED|PVTLTD|LIMITED|LTD)$")


def _parse_amount(text: str) -> Decimal | None:
    text = (text or "").strip()
    if not _AMOUNT_RE.match(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _find_caption_line(lines: list[str], captions: tuple[str, ...]) -> int | None:
    """
    Index of the first line that IS one of the caption variants.

    An exact (case/punctuation-insensitive) match on the WHOLE line, not a
    substring search -- captions on this form are short, standalone cells,
    and a substring match risks matching a longer sentence that happens to
    contain the caption text.
    """
    normalised = {c.strip().lower().rstrip(".:") for c in captions}
    for i, line in enumerate(lines):
        key = line.strip().lower().rstrip(".:")
        if key in normalised:
            return i
    return None


def _value_inline(lines: list[str], captions: tuple[str, ...]) -> str | None:
    """
    The value sitting on the SAME line as its caption.

    Corporate payslips are tables, so the value is in the next cell and
    _value_after finds it. Statutory FORM XIX wage slips are printed forms
    instead, and read "Name of Workman:- GOPAL ...", with the caption and the
    value on one line separated by a colon, a dash, or both.

    Anchored at the start of the line rather than searched anywhere in it,
    which keeps the guarantee _find_caption_line is careful about: a sentence
    that merely mentions the caption partway through is not a value cell.
    """
    for caption in captions:
        pattern = re.compile(
            r"^\s*" + re.escape(caption) + r"\s*[:\-]+\s*(\S.*)$",
            re.IGNORECASE,
        )
        for line in lines:
            match = pattern.match(line)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
    return None


#: Captions that start a NEW cell on an OCR'd two-column line, where a value ends.
_STOP_CAPTIONS = (
    "Bank Name", "BankName", "Bank A/c No", "Bank A/C No", "Account No", "UAN NO", "UAN No", "UAN",
    "PF No", "ESI No", "PAN No", "PAN", "Aadhar No", "Aadhaar No", "PaySlip No", "Payslip No",
    "Department", "Region", "Branch", "Location", "Designation", "Grade", "Date of Joining",
    "Paid Days", "Days Paid", "Employee Code", "Employee Name", "Regime Type", "RegimeType",
)


def _value_spaced(lines: list[str], captions: tuple[str, ...]) -> str | None:
    """
    An OCR'd line: "Employee Name  A PERSON  BankName AXIS BANK". The value is
    what follows the caption, up to the next known caption -- a value never runs
    into the next cell. Only consulted when the table and colon forms found nothing.
    """
    stop = re.compile(r"\s+(?:" + "|".join(re.escape(c) for c in _STOP_CAPTIONS) + r")\b\.?", re.IGNORECASE)
    for caption in captions:
        # the caption at the start of the line, or after a space mid-line (an OCR'd two-column header row puts
        # "Designation X  Date of Joining Y" on one line; 2026-11-12: joining date / UAN / code were read 0 times)
        pattern = re.compile(r"(?:^|\s)" + re.escape(caption) + r"\s*[:\-]*\s+(\S.*)$", re.IGNORECASE)
        for line in lines:
            match = pattern.search(line)
            if not match:
                continue
            value = stop.split(match.group(1), maxsplit=1)[0].strip(" :-")
            if value:
                return value
    return None


def _unglue(text: str) -> str:
    """
    OCR drops the space inside a caption: "EmployeeName", "TotalEarnings", "NetPay", "Payslipforthe month of".
    Every known caption is put back in its spaced form (case and spacing free), so the parser reads an OCR'd slip as
    it reads a text layer. Only OCR'd text is touched (2026-11-12: name 7/36, net pay 12/36 on synthetic scans).
    """
    captions = set(_STOP_CAPTIONS)
    for group in (_EMPLOYEE_NAME_CAPTIONS, _EMPLOYEE_CODE_CAPTIONS, _DESIGNATION_CAPTIONS, _DOJ_CAPTIONS,
                  _PAN_CAPTIONS, _UAN_CAPTIONS, _GROSS_CAPTIONS, _BASIC_CAPTIONS, _DEDUCTIONS_CAPTIONS,
                  _NET_PAY_CAPTIONS):
        captions.update(group)
    for caption in sorted(captions, key=len, reverse=True):
        words = [re.escape(w) for w in caption.split()]
        if len(words) < 2:
            continue
        pattern = re.compile(r"(?<![A-Za-z])" + r"\s*".join(words) + r"(?![a-z])", re.IGNORECASE)
        text = pattern.sub(caption, text)
    return text


def _value_after(lines: list[str], captions: tuple[str, ...]) -> str | None:
    """
    The value for a caption: the next cell, or the rest of the same line.

    Whole-line captions are tried first so existing table-style payslips keep
    resolving exactly as before; the inline form is only consulted when that
    finds nothing.
    """
    index = _find_caption_line(lines, captions)
    if index is None:
        return _value_inline(lines, captions) or _value_spaced(lines, captions)

    for offset in range(1, 3):
        if index + offset >= len(lines):
            break
        candidate = lines[index + offset].strip()
        if candidate:
            return candidate

    return _value_inline(lines, captions)


def _amount_after(lines: list[str], captions: tuple[str, ...]) -> Decimal | None:
    """
    The first genuine amount after a caption line.

    A caption is sometimes followed immediately by a label suffix like "(A)"
    on the same extracted line before the figure appears on a later one, so
    the caption line's own trailing text is tried first, then subsequent
    lines.
    """
    for caption in captions:
        for i, line in enumerate(lines):
            if caption.lower() not in line.lower():
                continue
            rest = line.split(caption, 1)[-1]
            trailing = re.sub(r"[^\d.,]", "", rest)
            amount = _parse_amount(trailing) if trailing else None
            if amount is not None:
                return amount
            # AN OCR'D TABLE ROW keeps its columns on one line ("Total Earnings (A)
            # 31986.00 0.00 63972.00"): gluing the digits made it unparseable and
            # the search fell through to another caption's figure. The FIRST
            # amount after the caption is the current-month cell.
            for piece in re.findall(r"-?\d[\d,]*(?:\.\d{1,2})?", rest):
                amount = _parse_amount(piece)
                if amount is not None:
                    return amount
            for offset in range(1, 4):
                if i + offset >= len(lines):
                    break
                amount = _parse_amount(lines[i + offset])
                if amount is not None:
                    return amount
    return None


def _find_employer_name(lines: list[str]) -> str | None:
    """
    The company-suffixed line most likely to be the genuine employer.

    A payslip processed through a third-party payroll vendor can print that
    vendor's own legally-suffixed name too ("Processed using XYZ Payroll
    Private Limited"), and this has not been tested against a real sample
    that does that -- the one real payslip available had only one
    company-suffixed name on the page at all, so this preference could not
    be exercised either way.

    Where more than one candidate exists, the genuine employer's letterhead
    is followed by a postal address (seen on the real sample: "SHRIRAM
    FINANCE LIMITED" then "6th Floor..., Navi Mumbai - 400710"), while a
    vendor credit line typically is not. A candidate followed within 3 lines
    by something PIN-code-shaped is preferred; when none qualifies, the
    first candidate in reading order is kept as before, so this is
    additive and cannot make an already-correct single-candidate case wrong.
    """
    candidates = [
        i for i, line in enumerate(lines[:200])
        if _COMPANY_SUFFIX_RE.search(line)
        or _GLUED_SUFFIX_RE.search(line.strip().upper().replace(" ", ""))
    ]
    # the glued suffix gets its space back ("ACMELIMITED" -> "ACME LIMITED")
    lines = [_GLUED_SUFFIX_RE.sub(lambda m: " " + m.group(1), l.strip())
             if _GLUED_SUFFIX_RE.search(l.strip()) else l for l in lines]
    if not candidates:
        return None

    pin_re = re.compile(r"\b\d{6}\b")
    for i in candidates:
        for offset in range(1, 4):
            if i + offset < len(lines) and pin_re.search(lines[i + offset]):
                return lines[i].strip()[:120]

    return lines[candidates[0]].strip()[:120]


def ocr_enabled() -> bool:
    """Whether a slip with no text layer is OCR'd (SALARY_SLIP_OCR_ENABLED, default on)."""
    import os

    return (os.getenv("SALARY_SLIP_OCR_ENABLED", "true") or "true").lower() == "true"


def max_ocr_pages() -> int:
    import os

    try:
        return max(1, int(os.getenv("SALARY_SLIP_OCR_PAGES", "2")))
    except ValueError:
        return 2


def _slip_complete(text: str) -> bool:
    """Enough read to stop OCR'ing pages: the employee, the net pay, and net = gross - deductions."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(text.strip()) < 100 or not _value_after(lines, _EMPLOYEE_NAME_CAPTIONS):
        return False
    gross, ded, net = (_amount_after(lines, c) for c in (_GROSS_CAPTIONS, _DEDUCTIONS_CAPTIONS, _NET_PAY_CAPTIONS))
    return None not in (gross, ded, net) and abs((gross - ded) - net) < Decimal("1.00")


def extract_salary_slip(path: str) -> SalarySlipResult:
    """Read a salary slip's identity, period and pay figures."""
    started = time.perf_counter()

    if not Path(path).exists():
        return SalarySlipResult(status=SalarySlipStatus.FAILED, errors=[f"File not found: {path}"])

    from app.agents.document_agent import scan_text

    text, pages, ocr_note = "", 0, None
    if not scan_text.is_image(path):
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            pages = len(reader.pages)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            # not readable as a PDF text layer -- the scan path below may still read it
            logger.warning("Could not read salary slip text layer: %s", exc)

    # A SCANNED OR PHOTOGRAPHED SLIP IS OCR'D INTO THE SAME PARSER (scan_text.py).
    # Measured: 0/7 fields from a real slip as an image before this path existed.
    # Whatever OCR reads still has to pass the parser's own checks -- net pay must
    # equal gross minus deductions -- or the slip is PARTIAL, never a quiet PASS.
    if len(text.strip()) < 100 and ocr_enabled():
        # PAGE BY PAGE, STOPPING EARLY (2026-11-12): a slip is one page; a scan of three months' slips was OCR'd in
        # full for the first month's figures. The next page is read only while the name / net pay are missing or the
        # net pay does not reconcile (config financial_documents.yaml scan_ocr.SALARY_SLIP.early_stop).
        early = bool(scan_text._scan_config("SALARY_SLIP").get("early_stop", False))
        page_texts, engine, rotated = [], "rapidocr", 0
        for page_text, info in scan_text.iter_scanned_pages(path, document_type="SALARY_SLIP",
                                                            max_pages=max_ocr_pages()):
            page_texts.append(page_text)
            engine = info.get("engine", engine)
            rotated += bool(info.get("rotated"))
            if early and _slip_complete(_unglue("\n".join(page_texts))):
                break
        if len("".join(page_texts).strip()) >= 100:
            text = _unglue("\n".join(page_texts))
            pages = pages or len(page_texts)
            ocr_note = (f"Read by OCR ({engine}"
                        + (f", {rotated} page(s) auto-rotated" if rotated else "")
                        + "): check the figures against the slip.")

    if len(text.strip()) < 100:
        return SalarySlipResult(
            status=SalarySlipStatus.UNSUPPORTED,
            pages=pages,
            warnings=[
                "No usable text could be read from this slip, from its text layer or "
                "by OCR. Upload a clearer scan or the original PDF."
            ],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    lines = [l for l in text.split("\n") if l.strip()]
    result = SalarySlipResult(status=SalarySlipStatus.SUCCESS, pages=pages)
    if ocr_note:
        result.warnings.append(ocr_note)

    result.employer_name = _find_employer_name(lines)

    month_match = _MONTH_RE.search(text)
    if month_match:
        result.pay_period = f"{month_match.group(1).upper()} {month_match.group(2)}"
    else:
        # OCR glues a caption line's words ("PAYSLIPFORTHEMONTHOFMAY2026")
        glued = _GLUED_MONTH_RE.search(re.sub(r"\s+", "", text).upper())
        if glued:
            result.pay_period = f"{glued.group(1)} {glued.group(2)}"

    result.employee_name = _value_after(lines, _EMPLOYEE_NAME_CAPTIONS)
    result.employee_code = _value_after(lines, _EMPLOYEE_CODE_CAPTIONS)
    result.designation = _value_after(lines, _DESIGNATION_CAPTIONS)
    result.date_of_joining = _value_after(lines, _DOJ_CAPTIONS)
    result.pan_masked = _value_after(lines, _PAN_CAPTIONS)
    result.uan_number = _value_after(lines, _UAN_CAPTIONS)

    result.gross_earnings = _amount_after(lines, _GROSS_CAPTIONS)
    result.total_deductions = _amount_after(lines, _DEDUCTIONS_CAPTIONS)
    result.net_pay = _amount_after(lines, _NET_PAY_CAPTIONS)
    result.basic_salary = _amount_after(lines, _BASIC_CAPTIONS)

    # The rest of the earnings, where both figures were read and the
    # subtraction makes sense. A basic line larger than the printed total
    # means one of the two was misread, and reporting a negative
    # allowance would publish that error as a figure.
    if (result.gross_earnings is not None
            and result.basic_salary is not None
            and result.gross_earnings >= result.basic_salary):
        result.allowances = result.gross_earnings - result.basic_salary

    # The one check a fabricated or misread slip cannot survive: net pay must
    # equal gross earnings minus deductions. Mirrors balance reconciliation
    # on a bank statement -- the arithmetic is the evidence, not the
    # presence of the fields.
    if (
        result.gross_earnings is not None
        and result.total_deductions is not None
        and result.net_pay is not None
    ):
        expected = result.gross_earnings - result.total_deductions
        result.net_pay_reconciles = abs(expected - result.net_pay) < Decimal("1.00")

    present = sum(
        1 for v in (
            result.employer_name, result.employee_name, result.pay_period,
            result.gross_earnings, result.net_pay,
        ) if v is not None
    )
    result.confidence = round(present / 5, 4)

    required_missing = [
        n for n, v in (
            ("employee_name", result.employee_name),
            ("net_pay", result.net_pay),
        ) if v is None
    ]
    if required_missing:
        result.status = SalarySlipStatus.PARTIAL
        result.warnings.append(f"Required field(s) not found: {', '.join(required_missing)}")
    elif result.net_pay_reconciles is False:
        result.status = SalarySlipStatus.PARTIAL
        result.warnings.append(
            "Net pay does not equal gross earnings minus deductions; "
            "treat the figures as unverified"
        )

    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


__all__ = ["extract_salary_slip"]
