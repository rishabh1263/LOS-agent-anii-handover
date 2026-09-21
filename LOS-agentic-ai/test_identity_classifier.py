from pathlib import Path
from pypdf import PdfReader

from app.agents.verification.basic import classify


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

        text_parts = []
        for page in reader.pages[:2]:
            text_parts.append(page.extract_text() or "")

        text = " ".join(text_parts).upper()
        compact = "".join(c for c in text if c.isalnum())

        result = classify(compact)

        print(f"{path.name} -> {result}")

    except Exception as exc:
        print(
            f"{path.name} -> ERROR: "
            f"{type(exc).__name__}: {exc}"
        )
