"""
SALARY SLIP + VOTER ID REGRESSIONS FROM THE REAL-SAMPLE SWEEP (2026-10-03).

Salary slip: the parser read 7/7 fields from the real native-text slip and 0/7
from the same slip scanned, photographed, rotated, blurred or compressed -- it had
no OCR path. It now OCRs scans (scan_text.py: orient, deskew, RapidOCR) into the
same parser, which learned the shapes OCR produces: a table row's columns on one
line, two columns merged into one line, glued words. The net-pay arithmetic check
still decides whether the figures can be trusted.

Voter ID: a bilingual gender marker read as Hindi noise glued to the English word
("geuMale") was lost.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.agents.salary_slip import extract as slip

SLIP = Path("samples/real_batch/salary_slip.pdf")


# ---- the parser on OCR-shaped text (no OCR, no sample needed) -------------------------
OCR_TEXT = [
    "ACMEINDUSTRIESLIMITED",
    "Plot 7, Industrial Area",
    "Pune - 411001",
    "PAYSLIPFORTHEMONTHOFAPRIL2026",
    "Employee Code 1001 PaySlip No. 55",
    "Employee Name ASHA RAO BankName ANY BANK",
    "Designation OFFICER PF No. XX/123",
    "BASIC SALARY 20000.00 0.00 40000.00",
    "Total Earnings (A) 30000.00 0.00 60000.00",
    "Total Deductions(B) 2000.00 0.00 4000.00",
    "Net Pay (A -B) 28000.00",
    "Total Salary 360000.00",
]


def test_the_first_amount_of_an_ocrd_table_row_is_the_current_month():
    assert str(slip._amount_after(OCR_TEXT, slip._GROSS_CAPTIONS)) == "30000.00"   # not the annual 360000
    assert str(slip._amount_after(OCR_TEXT, slip._DEDUCTIONS_CAPTIONS)) == "2000.00"


def test_a_value_ends_where_the_next_caption_begins():
    assert slip._value_after(OCR_TEXT, slip._EMPLOYEE_NAME_CAPTIONS) == "ASHA RAO"
    assert slip._value_after(OCR_TEXT, slip._DESIGNATION_CAPTIONS) == "OFFICER"


def test_glued_month_and_company_suffix_are_read():
    month = slip._GLUED_MONTH_RE.search("".join(OCR_TEXT).replace(" ", "").upper())
    assert month and (month.group(1), month.group(2)) == ("APRIL", "2026")
    assert slip._find_employer_name(OCR_TEXT).replace(" ", "") == "ACMEINDUSTRIESLIMITED"
    assert slip._find_employer_name(OCR_TEXT).endswith(" LIMITED")


# ---- the real slip, degraded the way uploads degrade ---------------------------------
def _truth():
    r = slip.extract_salary_slip(str(SLIP))
    return r


def _variant(tmp_path, transform, name):
    import pymupdf as fitz
    from PIL import Image

    page = fitz.open(str(SLIP))[0]
    img = transform(Image.open(io.BytesIO(page.get_pixmap(dpi=200).tobytes("png"))).convert("RGB"))
    out = tmp_path / name
    img.save(out, "PDF" if name.endswith(".pdf") else "JPEG", **({"resolution": 200} if name.endswith(".pdf") else {"quality": 40}))
    return out


@pytest.mark.skipif(not SLIP.exists(), reason="real salary slip sample not present")
@pytest.mark.parametrize("name, transform", [
    ("image_only.pdf", lambda im: im),
    ("tilted_3deg.pdf", lambda im: im.rotate(3, expand=True, fillcolor="white")),
    ("compressed.jpg", lambda im: im),
])
def test_a_scanned_or_photographed_slip_reads_like_the_original(tmp_path, name, transform):
    truth = _truth()
    got = slip.extract_salary_slip(str(_variant(tmp_path, transform, name)))
    assert got.status.value == "SUCCESS" and got.net_pay_reconciles is True
    for field in ("employee_name", "pay_period", "gross_earnings", "total_deductions", "net_pay", "basic_salary"):
        assert str(getattr(got, field)) == str(getattr(truth, field)), field     # values compared, never printed
    assert got.employer_name.replace(" ", "") == truth.employer_name.replace(" ", "")
    assert any("Read by OCR" in w for w in got.warnings)


def test_an_unreadable_slip_is_unsupported_never_a_guess(tmp_path):
    from PIL import Image

    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (800, 1000), "white").save(blank)
    assert slip.extract_salary_slip(str(blank)).status.value == "UNSUPPORTED"


# ---- voter ID: the bilingual gender marker --------------------------------------------
@pytest.mark.skipif(not Path("samples/real_batch/voter5.jpg").exists(), reason="real voter sample not present")
def test_a_bilingual_gender_marker_glued_to_hindi_noise_is_read():
    from app.agents.document_agent import extract_document

    assert extract_document("samples/real_batch/voter5.jpg").value("gender") == "MALE"
