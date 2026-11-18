"""DEV ONLY: the OCR lines of one synthetic document and what the readers made of them (synthetic = no real person).
python -m evals.documents.financial_diag <file in samples/synthetic_financial/dev> [max_lines]"""
import json
import sys

from app.agents.document_agent import scan_text
from evals.documents.financial_synthetic import OUT

name = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 60
path = OUT / "dev" / name
truth = next(d for d in json.loads((OUT / "dev" / "ground_truth.json").read_text(encoding="utf-8")) if d["file"] == name)
kind = "ITR" if truth["kind"] == "ITR" else "SALARY_SLIP"
texts = [t for t, _ in scan_text.iter_scanned_pages(str(path), document_type=kind, max_pages=1)]
for line in texts[0].split("\n")[:limit]:
    print("  |", line)
if kind == "ITR":
    from app.agents.itr import figures as FG
    print("\ncandidates:", {k: [str(x) for x in v] for k, v in FG.read_figures(texts[0]).items()})
print("truth:", {k: v for k, v in truth["fields"].items()})
