from pathlib import Path
from pypdf import PdfReader

root = Path("samples/real_batch")

skip = {
    "bank_amit.pdf",
    "bank_canara.pdf",
    "bank_generic.pdf",
    "bank_hdfc_new.pdf",
    "bank_kotak.pdf",
    "bank_sbi_scanned.pdf",
    "bank_std_chartered.pdf",
    "sbi_new.pdf",
    "itr_v.pdf",
    "salary_slip.pdf",
    "sale_deed2.pdf",
    "sale_deed_clean.pdf",
    "sale_deed_small.pdf",
    "sale_deed_test.pdf",
    "unknown_scanned.pdf",
}

for path in sorted(root.glob("*.pdf")):
    if path.name in skip:
        continue

    try:
        reader = PdfReader(str(path))
        text = ""

        for page in reader.pages[:2]:
            text += " " + (page.extract_text() or "")

        text = text.upper()

        if "PAN" in text or "PERMANENT ACCOUNT" in text:
            kind = "PAN"
        elif "DRIVING LICENCE" in text or "DRIVING LICENSE" in text:
            kind = "DRIVING_LICENCE"
        elif "ELECTION COMMISSION" in text or "ELECTOR" in text or "EPIC" in text:
            kind = "VOTER_ID"
        elif "PASSPORT" in text or "REPUBLIC OF INDIA" in text:
            kind = "PASSPORT"
        else:
            kind = "UNKNOWN"

        print(f"{path.name} -> {kind}")

    except Exception as exc:
        print(f"{path.name} -> ERROR: {type(exc).__name__}: {exc}")
