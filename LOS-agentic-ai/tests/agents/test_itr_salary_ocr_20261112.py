"""
ITR and salary slips read the way OCR prints them (2026-11-12). Text only -- no OCR runs here; the lines are the
shapes measured on real scans (values synthetic). Synthetic held-out set: evals/documents/financial_eval.py.
"""

from decimal import Decimal

from app.agents.itr import figures as FG
from app.agents.itr.extract import _parse
from app.agents.salary_slip import extract as SX

ITR_V = """INDIANINCOMETAXRETURNACKNOWLEDGEMENTAssessmentYear
[Where the data of the Return of Income in Form ITR-1(SAHAJ), ITR-2, ITR-3, ITR-4(SUGAM) 2023-24
Acknowledgement Number : 962341684983340 Date of filing : 04-Oct-2023
PAN ABCPX1234Y
Name ASHA KULKARNI
Address FLAT 9, SHANTI NAGAR,PUNE, 411038
Status Individual
Form Number ITR-3
Current Year business loss, if any 1 0
Total Income 2 30,68,110
Book Profit under MAT where applicable 3 0
Adjusted Total Income under AMT, where applicable 4 0
Net tax payable 5 2,27,230
Interest and FeePayable 6 320
Total tax, interest and Fee payable 7 2,27,550
Taxes Paid 8 2,27,550
(+) Tax Payable /(-) Refundable (7-8) 9 0
Tax and interest paid 14 0
(+) Tax Payable /(-) Refundable (13-14) 15 0
address 10.12.34.56 and verified by ASHA KULKARNI having PAN ABCPX1234Y on
"""

ITR_U = """Acknowledgement Number:812375966558265 Date of fling : 20-Sep-2022 2022-23
PAN ABCPX1234Y
Name ASHAKULKARNI
Form Number ITR-4
Total Income asper Updatedreturn 2 4,87,310
Total income as per earlier retum 3 4,37,150
Tax payable (+) / Refundable (-) as per earlier retum 6 21,850
Additional income-taxliability on updated income 7 11,580
Net amount payable 8 11,580
Tax paid w/s 140B 9 11,580
Tax due(8-9) 10 0
address 10.12.34.56 and verified by ASHA KULKARNI having PAN ABCPX1234Y on
"""


def test_itr_v_ocr_lines_read_with_row_labels_skipped():
    r = _parse([ITR_V], "SCANNED", 1)
    assert (r.pan, r.assessment_year, r.form_number) == ("ABCPX1234Y", "2023-24", "ITR-3")
    assert r.name == "ASHA KULKARNI"
    assert r.total_income == Decimal("3068110")
    assert (r.net_tax_payable, r.total_tax_and_interest_payable, r.taxes_paid, r.tax_payable_or_refundable) == (
        Decimal("227230"), Decimal("227550"), Decimal("227550"), Decimal("0"))
    assert r.reconciliation == "RECONCILED"


def test_itr_u_layout_and_ocr_misreads():
    r = _parse([ITR_U], "SCANNED", 1)
    assert r.total_income == Decimal("487310")                     # not "as per earlier retum"
    assert r.taxes_paid == Decimal("11580")                         # "Tax paid w/s 140B"
    assert r.tax_payable_or_refundable == Decimal("0")              # "Tax due", not row 6 "as per earlier"
    assert r.name == "ASHA KULKARNI"                                # space restored from the verification line
    assert r.filing_date.isoformat() == "2022-09-20"                # "Date of fling"
    assert r.reconciliation == "RECONCILED"


def test_itr_u_newer_layout_amount_payable_as_per_updated_return_is_not_tax_due():
    text = ("Total Income as per Updated return 2 5,00,000\n"
            "Amount payable (+)/ Refundable(-) as per Updated return (+) 4,200\n"
            "Additional income-tax liability on updated income 7 4,200\n"
            "Net amount payable 4,200\n"
            "Tax paid u/s 140B 9 4,200\n"
            "Tax due (8 - 9) 10 0\n")
    r = _parse([text], "SCANNED", 1)
    assert r.tax_payable_or_refundable == Decimal("0")             # the "Tax due" row, not "as per Updated return"
    assert r.reconciliation == "RECONCILED"


def test_a_lost_value_stays_not_read_never_the_row_label():
    text = ITR_V.replace("(+) Tax Payable /(-) Refundable (7-8) 9 0", "(+) Tax Payable /(-) Refundable (7-8) 9")
    r = _parse([text], "SCANNED", 1)
    assert r.tax_payable_or_refundable is None                      # not 9
    assert r.reconciliation == "INCOMPLETE"


def test_amounts_as_ocr_reads_them():
    assert FG.amount("9.999") == Decimal("9999")                    # a "," read as "."
    assert FG.amount("2.27.550") == Decimal("227550")
    assert FG.amount("1,16,660.00") == Decimal("116660.00")
    assert FG.amount("12,345", "-") == Decimal("-12345")
    assert FG.read_figures("Taxes Paid 8 (-) 1,200")["taxes_paid"][0] == Decimal("-1200")


def test_the_arithmetic_picks_between_readings():
    chosen = FG.choose({"total_tax_and_interest_payable": [Decimal(5000)], "taxes_paid": [Decimal(4000)],
                        "tax_payable_or_refundable": [None, Decimal(1000)]}, Decimal(1), Decimal(10))
    assert chosen["tax_payable_or_refundable"] == Decimal(1000)     # the reading that reconciles


def test_form_number_is_the_returns_own_not_the_headers_list():
    assert FG.form_number("Form ITR-1(SAHAJ), ITR-2, ITR-3\nForm Number ITR-3") == "ITR-3"


SLIP = """SURYAFOODSPVTLTD
1th Floor, Trade Centre, NAGPUR - 440010
Payslipforthe month of JUNE2025
EmployeeName PRIYAREDDY Employee Code EMP58909
Designation SOFTWARE ENGINEER Date of Joining 19-02-2018
PAN No. ABCPX1234Y UAN No. 665125629798
Basic Salary 66,930.00
TotalEarnings 1,07,302.00
Total Deductions 2,000.00
NetPay 1,05,302.00"""


def test_salary_slip_ocr_lines():
    lines = [l for l in SX._unglue(SLIP).split("\n") if l.strip()]
    assert SX._value_after(lines, SX._EMPLOYEE_NAME_CAPTIONS) == "PRIYAREDDY"
    assert SX._value_after(lines, SX._EMPLOYEE_CODE_CAPTIONS) == "EMP58909"
    assert SX._value_after(lines, SX._DOJ_CAPTIONS) == "19-02-2018"           # mid-line caption
    assert SX._value_after(lines, SX._UAN_CAPTIONS) == "665125629798"
    assert SX._amount_after(lines, SX._GROSS_CAPTIONS) == Decimal("107302.00")
    assert SX._amount_after(lines, SX._NET_PAY_CAPTIONS) == Decimal("105302.00")
    assert SX._MONTH_RE.search(SX._unglue(SLIP)).group(0).upper() == "JUNE2025"
    assert SX._slip_complete(SX._unglue(SLIP))                                # stops OCR'ing after this page
