"""
CREDIT UNDERWRITING -- REPAYMENT (2026-10-19): can the applicant repay the proposed EMI?

The vendor's VERIFIED bank analysis (no OCR) + the eligibility case data -> 5 policy rules -> a clean report with a
verdict and reason points. Config: app/config/credit_underwriting/ (vendor_fields, policy, settings).

    vendor.py       vendor workbook / JSON -> account, monthly, transactions, vendor_summary
    obligations.py  the obligation map (karz ka naksha): loans taken, EMIs paid, missed, declared or not
    metrics.py      the figures on the last N full months
    rules.py        the 5 rules -> PASS / REFER / DECLINE
    report.py       verdict, reasons, sections, suggestion
    store.py        each case's analysis (files / postgres)
    service.py      the two calls the API makes: receive an analysis, build a case's report
"""
