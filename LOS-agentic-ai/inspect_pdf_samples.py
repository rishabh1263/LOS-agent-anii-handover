from pathlib import Path
from pypdf import PdfReader

root = Path("samples/real_batch")

for path in sorted(root.glob("*.pdf")):
    try:
        reader = PdfReader(str(path))

        pages = reader.pages[:2]
        text = " ".join((p.extract_text() or "") for p in pages).strip()

        has_images = any(
            "/XObject" in (p.get("/Resources") or {})
            for p in pages
        )

        print(
            f"{path.name} | "
            f"pages={len(reader.pages)} | "
            f"text_chars={len(text)} | "
            f"images={has_images}"
        )

    except Exception as exc:
        print(f"{path.name} | ERROR: {exc}")
