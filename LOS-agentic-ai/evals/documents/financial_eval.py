"""
ACCURACY AND TIME OF THE ITR / SALARY-SLIP EXTRACTORS on the synthetic set with ground truth (2026-11-12).

    python -m evals.documents.financial_synthetic --split dev --per 4
    python -m evals.documents.financial_eval --split dev [--report runs/fin_dev.json] [--only ITR|SALARY]

Diagnose on DEV only; HELDOUT is measured, never tuned on. Field accuracy = exact match after normalising case and
spaces (amounts as numbers, dates ISO). A document is "fully correct" when every labelled field is. Time is wall time
per document in this process; compare two versions only inside one process (the laptop is noisy across runs).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from decimal import Decimal
from pathlib import Path

from evals.documents.financial_synthetic import OUT


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, Decimal, float)):
        return str(int(Decimal(str(value))))
    s = str(value)
    if hasattr(value, "isoformat"):
        s = value.isoformat()
    return re.sub(r"\s+", " ", s).strip().upper()


#: names are matched in the system by name_key (letters only, app/agents/document_agent/normalize.py): a space OCR
#: dropped ("PRIYAREDDY") changes no match, so it is not counted as wrong here either
NAME_FIELDS = {"name", "employee_name", "employer_name", "designation"}   # designation: free text, same rule


def _match(field: str, expected, actual) -> bool:
    if field in NAME_FIELDS:
        from app.agents.document_agent.normalize import name_key

        return bool(actual) and name_key(str(expected)) == name_key(str(actual))
    if isinstance(expected, int):
        try:
            return actual is not None and Decimal(str(actual)) == Decimal(expected)
        except Exception:  # noqa: BLE001
            return False
    return _norm(expected) == _norm(actual)


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))], 1) if v else 0.0


def run(split: str, only: str | None = None) -> list[dict]:
    from app.agents.itr import extract_itr
    from app.agents.salary_slip import extract_salary_slip

    folder = OUT / split
    truth = json.loads((folder / "ground_truth.json").read_text(encoding="utf-8"))
    rows = []
    for doc in truth:
        if only and doc["kind"] != only:
            continue
        started = time.perf_counter()
        result = (extract_itr if doc["kind"] == "ITR" else extract_salary_slip)(str(folder / doc["file"]))
        ms = (time.perf_counter() - started) * 1000
        got = {f: getattr(result, f, None) for f in doc["fields"]}
        ok = {f: _match(f, v, got[f]) for f, v in doc["fields"].items()}
        check = getattr(result, "reconciliation", None) if doc["kind"] == "ITR" else result.net_pay_reconciles
        rows.append({"file": doc["file"], "kind": doc["kind"], "layout": doc["layout"], "quality": doc["quality"],
                     "ms": round(ms, 1), "status": result.status.value, "check": str(check), "ok": ok,
                     "wrong": sorted(f for f, v in ok.items() if not v)})
    return rows


def summarise(rows: list[dict]) -> None:
    for kind in ("ITR", "SALARY"):
        rs = [r for r in rows if r["kind"] == kind]
        if not rs:
            continue
        fields = sorted({f for r in rs for f in r["ok"]})
        cells = [v for r in rs for v in r["ok"].values()]
        print(f"\n=== {kind}: {len(rs)} docs   field accuracy {100 * sum(cells) / len(cells):.1f}%   "
              f"fully correct {100 * sum(not r['wrong'] for r in rs) / len(rs):.1f}%   "
              f"time p50 {_pct([r['ms'] for r in rs], .5)} ms  p95 {_pct([r['ms'] for r in rs], .95)} ms")
        for key in ("layout", "quality"):
            for val in sorted({r[key] for r in rs}):
                sub = [r for r in rs if r[key] == val]
                c = [v for r in sub for v in r["ok"].values()]
                print(f"   {key:7} {val:13} n={len(sub):2}  fields {100 * sum(c) / len(c):5.1f}%  "
                      f"full {100 * sum(not r['wrong'] for r in sub) / len(sub):5.1f}%  "
                      f"p50 {_pct([r['ms'] for r in sub], .5):7} ms  p95 {_pct([r['ms'] for r in sub], .95):7} ms")
        print("   per field:", ", ".join(f"{f}={sum(r['ok'].get(f, True) for r in rs if f in r['ok'])}/"
                                       f"{sum(f in r['ok'] for r in rs)}" for f in fields))
        print("   self-check:", dict(sorted({c: sum(r['check'] == c for r in rs) for c in {r['check'] for r in rs}}.items())))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "heldout"])
    ap.add_argument("--only", choices=["ITR", "SALARY"])
    ap.add_argument("--report")
    args = ap.parse_args()
    rows = run(args.split, args.only)
    summarise(rows)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
