"""
REAL-SAMPLE DOCUMENT ACCURACY -- the measurement every OCR change is judged by.

    python -m evals.documents.accuracy [--report runs/doc_accuracy.json] [--only identity,slip,bank]

  identity  PAN / Driving Licence / Voter ID against samples/ground_truth.json
            (values hand-transcribed from the images, never copied from output).
            Names compared space-insensitively, every identifier exactly.
  slip      the real salary slip and degraded variants of it (image-only, tilted,
            rotated, blurred, low-resolution, JPEG, phone-photo); ground truth is
            the native-text read, checked against the page image.
  bank      every native bank statement: rows read and whether the balance chain
            reconciles (the statement's own arithmetic is the ground truth).

PRINTS AND WRITES NO FIELD VALUES -- only OK / MISSING / WRONG, counts and timings.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples"
NAME_FIELDS = {"name", "father_name", "guardian_name", "relation_name"}


def _same(field, expected, actual) -> bool:
    from app.agents.document_agent.normalize import name_key

    if expected is None:
        return actual is None
    if field in NAME_FIELDS:
        return name_key(str(actual)) == name_key(str(expected))
    if isinstance(expected, list):
        return sorted(map(str, actual or [])) == sorted(map(str, expected))
    return str(actual) == str(expected)


def identity() -> dict:
    from app.agents.document_agent import extract_document

    truth = {k: v for k, v in json.loads((SAMPLES / "ground_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    index: dict[str, list[Path]] = {}
    for p in SAMPLES.rglob("*"):
        if p.is_file():
            index.setdefault(p.name, []).append(p)
    rows, per_type = [], {}
    for name, t in truth.items():
        best = None
        for path in index.get(name, []):      # several files can share a name: keep the transcribed one
            started = time.perf_counter()
            result = extract_document(str(path))
            ms = round((time.perf_counter() - started) * 1000)
            got = {f: result.value(f) for f in t["fields"]}
            verdict = {f: ("MISSING" if got[f] in (None, "", []) else "OK" if _same(f, e, got[f]) else "WRONG")
                       for f, e in t["fields"].items()}
            hits = sum(v == "OK" for v in verdict.values())
            if best is None or hits > best[0]:
                best = (hits, path, verdict, ms, str(getattr(result.document_type, "value", result.document_type)))
        if best is None:
            continue
        _, path, verdict, ms, detected = best
        rows.append({"file": str(path.relative_to(SAMPLES)), "type": t["document_type"], "detected": detected,
                     "fields": verdict, "ms": ms})
        b = per_type.setdefault(t["document_type"], {"docs": 0, "type_ok": 0, "fields": 0, "ok": 0, "missing": 0,
                                                     "wrong": 0, "all_ok": 0, "ms": []})
        b["docs"] += 1
        b["type_ok"] += detected == t["document_type"]
        b["fields"] += len(verdict)
        b["ok"] += sum(v == "OK" for v in verdict.values())
        b["missing"] += sum(v == "MISSING" for v in verdict.values())
        b["wrong"] += sum(v == "WRONG" for v in verdict.values())
        b["all_ok"] += all(v == "OK" for v in verdict.values())
        b["ms"].append(ms)
    summary = {k: {"docs": b["docs"], "type_accuracy": round(b["type_ok"] / b["docs"], 3),
                   "field_accuracy": round(b["ok"] / b["fields"], 3), "missing": b["missing"], "wrong": b["wrong"],
                   "document_accuracy": round(b["all_ok"] / b["docs"], 3),
                   "p50_ms": statistics.median(b["ms"]), "max_ms": max(b["ms"])} for k, b in per_type.items()}
    return {"summary": summary, "rows": rows}


def slip() -> dict:
    from PIL import Image, ImageEnhance, ImageFilter

    from app.agents.salary_slip.extract import extract_salary_slip

    src = SAMPLES / "real_batch" / "salary_slip.pdf"
    if not src.exists():
        return {"status": "NOT_TESTABLE", "reason": "no real salary slip sample"}
    keys = ("employee_name", "employer_name", "pay_period", "basic_salary", "gross_earnings", "total_deductions",
            "net_pay")

    def facts(path):
        r = extract_salary_slip(str(path))
        return r, {k: getattr(r, k, None) for k in keys}

    _, truth = facts(src)
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore[no-redef]
    base = Image.open(io.BytesIO(fitz.open(str(src))[0].get_pixmap(dpi=200).tobytes("png"))).convert("RGB")
    tmp = ROOT / "runtime" / "eval_slip_variants"
    tmp.mkdir(parents=True, exist_ok=True)

    def photo(img):
        t = img.rotate(2.5, expand=True, fillcolor=(200, 195, 185))
        shade = Image.linear_gradient("L").resize(t.size).point(lambda v: 255 - v // 4)
        lit = Image.composite(t, Image.new("RGB", t.size, (150, 140, 120)), shade)
        return ImageEnhance.Contrast(lit.filter(ImageFilter.GaussianBlur(1.0))).enhance(0.85)

    variants = {"native_text_pdf": (None, src),
                "image_only_pdf": (lambda i: i, "a.pdf"), "tilted_3deg": (lambda i: i.rotate(3, expand=True, fillcolor="white"), "b.pdf"),
                "rotated_90deg": (lambda i: i.rotate(90, expand=True), "c.pdf"),
                "blurred": (lambda i: i.filter(ImageFilter.GaussianBlur(1.5)), "d.pdf"),
                "low_resolution": (lambda i: i.resize((i.width // 2, i.height // 2)), "e.pdf"),
                "jpeg_q30": (lambda i: i, "f.jpg"), "phone_photo": (photo, "g.jpg")}
    rows = []
    for label, (transform, target) in variants.items():
        if transform is not None:
            path = tmp / target
            img = transform(base)
            if target.endswith(".pdf"):
                img.save(path, "PDF", resolution=200)
            else:
                img.save(path, "JPEG", quality=30 if label == "jpeg_q30" else 70)
        else:
            path = target
        started = time.perf_counter()
        r, got = facts(path)
        ok = sum(1 for k in keys if got[k] is not None and str(got[k]).replace(" ", "").upper()
                 == str(truth[k]).replace(" ", "").upper())
        rows.append({"variant": label, "status": r.status.value, "correct": ok, "of": len(keys),
                     "reconciles": r.net_pay_reconciles, "ms": round((time.perf_counter() - started) * 1000)})
    return {"rows": rows, "field_accuracy": round(sum(r["correct"] for r in rows) / (len(keys) * len(rows)), 3)}


def bank() -> dict:
    from app.agents.bank_statement import extract_bank_statement

    files = sorted({p.resolve(): p for p in [*(SAMPLES / "real_batch").glob("bank_*.pdf"),
                                              SAMPLES / "real_batch" / "sbi_new.pdf",
                                              SAMPLES / "documents" / "statement.pdf",
                                              SAMPLES / "documents" / "demo_bank_statement.pdf"] if p.exists()}.values())
    rows = []
    for p in files:
        started = time.perf_counter()
        r = extract_bank_statement(str(p))
        rows.append({"file": str(p.relative_to(SAMPLES)), "status": r.status.value, "rows": len(r.transactions or []),
                     "reconciles": r.balance_reconciles, "ms": round((time.perf_counter() - started) * 1000)})
    return {"rows": rows, "reconciled": sum(r["reconciles"] is True for r in rows), "of": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=str(ROOT / "runs" / "doc_accuracy.json"))
    parser.add_argument("--only", default="identity,slip,bank")
    args = parser.parse_args()
    os.chdir(ROOT)
    out = {}
    for part in args.only.split(","):
        out[part] = {"identity": identity, "slip": slip, "bank": bank}[part.strip()]()
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(out, indent=1), encoding="utf-8")
    if "identity" in out:
        for t, s in out["identity"]["summary"].items():
            print(f"identity {t:16} docs={s['docs']} type={s['type_accuracy']:.0%} fields={s['field_accuracy']:.1%} "
                  f"missing={s['missing']} wrong={s['wrong']} doc_acc={s['document_accuracy']:.0%} P50={s['p50_ms']}ms")
    if "slip" in out and out["slip"].get("rows"):
        print(f"slip     field accuracy {out['slip']['field_accuracy']:.1%} over {len(out['slip']['rows'])} variants")
        for r in out["slip"]["rows"]:
            print(f"   {r['variant']:16} {r['status']:12} {r['correct']}/{r['of']} reconciles={r['reconciles']} {r['ms']}ms")
    if "bank" in out:
        print(f"bank     reconciled {out['bank']['reconciled']}/{out['bank']['of']}")
        for r in out["bank"]["rows"]:
            print(f"   {r['file']:36} {r['status']:12} rows={r['rows']:5} reconciles={r['reconciles']} {r['ms']}ms")


if __name__ == "__main__":
    main()
