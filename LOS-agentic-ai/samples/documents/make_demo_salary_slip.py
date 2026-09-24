"""
Build the demonstration salary slip: one page, real text layer.

WHY THIS EXISTS. The eligibility demonstration needs income to arrive the
way it does in production -- on a document that is classified, verified
and released by the extraction gate -- rather than typed into a request.
The repository's real salary slip belongs to a real person, and its net
pay does not match the demonstration case.

EVERY VALUE IS INVENTED. The employee, the employer, the employee code and
every figure are made up. The name matches the Swagger demonstration
applicant so the case reads coherently.

THE ARITHMETIC IS DELIBERATE. Net pay must equal gross earnings minus
deductions -- the one check the salary slip extractor runs to decide a slip
is internally consistent -- so the figures below reconcile exactly:

    earnings    28,000 + 14,000 + 14,200           = 56,200  (gross)
    deductions   1,800 +    200 +  4,200           =  6,200
    net pay     56,200 - 6,200                     = 50,000

SHAPED FOR THE EXISTING EXTRACTOR (app/agents/salary_slip/extract.py): each
caption sits on its own line with its value on the next, exactly as the
real sample's text layer reads, and the employer carries a legal-entity
suffix the employer finder anchors on.

Run from the repository root:

    python samples/documents/make_demo_salary_slip.py
"""

from __future__ import annotations

import pathlib

import fitz

OUT = pathlib.Path("samples/documents/demo_salary_slip_rahul_sharma.pdf")

LINES = [
    "DEMO TECHNOLOGIES PRIVATE LIMITED",
    "Plot 12, Example Business Park, Pune - 411001",
    "",
    "SALARY SLIP",
    "PAYSLIP FOR THE MONTH OF APRIL 2026",
    "",
    "Employee Name",
    "RAHUL SHARMA",
    "Employee Code",
    "DEMO-10421",
    "Designation",
    "SENIOR ANALYST",
    "Date of Joining",
    "01/06/2021",
    "",
    "Earnings",
    "BASIC SALARY",
    "28000.00",
    "HOUSE RENT ALLOWANCE",
    "14000.00",
    "SPECIAL ALLOWANCE",
    "14200.00",
    "Total Earnings (A)",
    "56200.00",
    "",
    "Deductions",
    "PROVIDENT FUND",
    "1800.00",
    "PROFESSIONAL TAX",
    "200.00",
    "INCOME TAX (TDS)",
    "4200.00",
    "Total Deductions (B)",
    "6200.00",
    "",
    "Net Pay (A - B)",
    "50000.00",
    "",
    "This is a SYNTHETIC DEMONSTRATION document. It is not a payroll record",
    "and every value in it is invented.",
]


def build() -> pathlib.Path:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 60), "\n".join(LINES), fontname="helv", fontsize=9)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(OUT))
    document.close()
    return OUT


if __name__ == "__main__":
    written = build()
    with fitz.open(str(written)) as check:
        extracted = check[0].get_text()
    print(f"wrote {written} ({written.stat().st_size} bytes, "
          f"{len(extracted)} characters of text layer)")
