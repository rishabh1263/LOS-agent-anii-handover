"""
ACCURACY + LATENCY ON THE SYNTHETIC SETS (2026-10-15) -- by document type, language and image quality.

    python -m evals.documents.synthetic_eval --split dev [--report runs/synth_dev.json] [--only PAN,VOTER_ID]

Per document: classified type, every asserted field OK / WRONG / MISSING, the time `extract_document` took.
Reports, per group: field accuracy (exact and normalized), document correctness (every field OK), required-field
completeness, classification accuracy, latency P50 / P95 / max. A fast wrong value is a failure, never a pass.
The labels come from evals/documents/synthetic.py (generated -- never extractor output).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMES = {"name", "father_name", "guardian_name", "relation_name", "surname", "given_names"}


def _norm(field: str, value) -> str:
    """Normalized comparison: case, spaces, Unicode form, date separators -- never letters or digits themselves."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(sorted(_norm(field, v) for v in value))
    text = unicodedata.normalize("NFC", str(value)).upper().strip()
    if field in NAMES:
        return re.sub(r"\s+", " ", text)
    return re.sub(r"[\s\-/.]", "", text)


def _exact(field: str, expected, actual) -> bool:
    if isinstance(expected, list):
        return sorted(map(str, actual or [])) == sorted(map(str, expected))
    if field in NAMES:
        return str(actual or "").upper() == str(expected).upper()        # names: case-insensitive, space-sensitive
    return str(actual) == str(expected)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run(split: str, only: set[str] | None) -> dict:
    from app.agents.document_agent import extract_document

    folder = ROOT / "samples" / "synthetic" / split
    truth = {k: v for k, v in json.loads((folder / "ground_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_") and (not only or v["document_type"] in only)}
    rows = []
    for name, t in truth.items():
        started = time.perf_counter()
        result = extract_document(str(folder / name))
        ms = (time.perf_counter() - started) * 1000
        detected = str(getattr(result.document_type, "value", result.document_type))
        fields = {}
        for f, expected in t["fields"].items():
            got = result.value(f)
            if got in (None, "", []):
                fields[f] = "MISSING"
            elif _exact(f, expected, got):
                fields[f] = "OK"
            elif _norm(f, got) == _norm(f, expected):
                fields[f] = "OK_NORMALIZED"
            else:
                fields[f] = "WRONG"
        rows.append({"file": name, "type": t["document_type"], "language": t["language"], "quality": t["quality"],
                     "detected": detected, "fields": fields, "ms": round(ms)})
    return {"split": split, "rows": rows}


def summarise(rows: list[dict], key) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = {}
    for g, rs in sorted(groups.items()):
        verdicts = [v for r in rs for v in r["fields"].values()]
        n = len(verdicts) or 1
        out[g] = {"docs": len(rs),
                  "field_exact": round(100 * sum(v == "OK" for v in verdicts) / n, 1),
                  "field_normalized": round(100 * sum(v in ("OK", "OK_NORMALIZED") for v in verdicts) / n, 1),
                  "doc_correct": round(100 * sum(all(v in ("OK", "OK_NORMALIZED") for v in r["fields"].values())
                                                 for r in rs) / len(rs), 1),
                  "completeness": round(100 * sum(v != "MISSING" for v in verdicts) / n, 1),
                  "wrong_values": sum(v == "WRONG" for v in verdicts),
                  "type_ok": round(100 * sum(r["detected"] == r["type"] for r in rs) / len(rs), 1),
                  "p50_ms": round(_pct([r["ms"] for r in rs], 0.5)), "p95_ms": round(_pct([r["ms"] for r in rs], 0.95)),
                  "max_ms": max(r["ms"] for r in rs)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "heldout"])
    ap.add_argument("--report")
    ap.add_argument("--only")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    t0 = time.perf_counter()
    data = run(a.split, only)
    rows = data["rows"]
    report = {"split": a.split, "docs": len(rows), "wall_s": round(time.perf_counter() - t0, 1),
              "by_type": summarise(rows, lambda r: r["type"]),
              "by_type_language": summarise(rows, lambda r: f"{r['type']}/{r['language']}"),
              "by_quality": summarise(rows, lambda r: r["quality"]),
              "overall": summarise(rows, lambda r: "ALL")["ALL"], "rows": rows}
    fields_by = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for f, v in r["fields"].items():
            fields_by[f"{r['type']}.{f}"][v] += 1
    report["by_field"] = {k: dict(v) for k, v in sorted(fields_by.items())}
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    hdr = f"{'group':28} {'docs':>4} {'exact%':>7} {'norm%':>6} {'doc%':>6} {'compl%':>7} {'wrong':>5} {'type%':>6} {'p50':>6} {'p95':>6}"
    for title in ("by_type", "by_type_language", "by_quality"):
        print(f"\n== {title}\n{hdr}")
        for g, s in report[title].items():
            print(f"{g:28} {s['docs']:>4} {s['field_exact']:>7} {s['field_normalized']:>6} {s['doc_correct']:>6} "
                  f"{s['completeness']:>7} {s['wrong_values']:>5} {s['type_ok']:>6} {s['p50_ms']:>6} {s['p95_ms']:>6}")
    s = report["overall"]
    print(f"\nOVERALL {s['docs']} docs: field exact {s['field_exact']}%, normalized {s['field_normalized']}%, "
          f"documents fully correct {s['doc_correct']}%, type {s['type_ok']}%, P50 {s['p50_ms']} ms, P95 {s['p95_ms']} ms")
    print("\nworst fields:")
    for k, v in sorted(report["by_field"].items(), key=lambda kv: -(kv[1].get("WRONG", 0) + kv[1].get("MISSING", 0)))[:12]:
        print(f"  {k:32} {v}")


if __name__ == "__main__":
    main()
