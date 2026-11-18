"""
PASSBOOK ACCURACY + LATENCY on the labelled synthetic sets (2026-10-15). Aggregates only -- no field value printed.

    python -m evals.passbook.evaluate --split dev [--report runs/passbook_dev.json]

Per field: exact (as labelled) and normalised (case / spacing) accuracy, and wrong vs missing counts -- only for the
fields the label says are readable. Verdicts: the expected verdict (or an acceptable one for a degraded image) per
scenario; a FALSE VERIFIED (a negative case verified) is counted separately: it is the failure that matters most.
Latency: P50 / P95 of the full in-process analysis (validate -> decode -> OCR -> fields -> verdict), warm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ("bank_name", "account_holder_name", "account_number", "ifsc_code", "branch_name")


def _norm(v) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _pct(values, q):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))]) if s else 0


async def run(split: str) -> dict:
    from app.agents.passbook import service

    folder = ROOT / "samples" / "synthetic_passbook" / split
    truth = {k: v for k, v in json.loads((folder / "ground_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    service.clear_cache()
    first = next(iter(truth))
    await service.analyse((folder / first).read_bytes(), first, caller="warm", request_id="warm")
    rows = []
    for name, t in truth.items():
        service.clear_cache()
        data = (folder / name).read_bytes()
        s = time.perf_counter()
        a = await service.analyse(data, name, caller="eval", request_id=name)
        ms = (time.perf_counter() - s) * 1000
        s2 = time.perf_counter()
        await service.analyse(data, name, caller="eval", request_id=name + "-2")       # the second API call
        reuse_ms = (time.perf_counter() - s2) * 1000
        verdicts = {}
        for f in FIELDS:
            expected = t["fields"].get(f)
            got = a.fields[f]
            if f == "account_number" and t["fields"]["account_number_status"] != "PRESENT":
                want = t["fields"]["account_number_status"]
                verdicts[f] = "OK" if got.status == want else ("WRONG" if got.value else "MISSING")
                continue
            if expected in (None, ""):
                continue
            if got.value in (None, ""):
                verdicts[f] = "MISSING"
            elif str(got.value) == str(expected):
                verdicts[f] = "OK"
            elif _norm(got.value) == _norm(expected):
                verdicts[f] = "OK_NORMALIZED"
            else:
                verdicts[f] = "WRONG"
        rows.append({"file": name, "bank": t["bank"], "scenario": t["scenario"], "quality": t["quality"],
                     "expected": t["expected_verdict"], "acceptable": t["acceptable_verdicts"],
                     "verdict": a.verdict.status, "reasons": a.verdict.reasons, "fields": verdicts,
                     "ms": round(ms), "reuse_ms": round(reuse_ms, 1), "timings": a.timings_ms})
    return {"split": split, "rows": rows}


def summarise(rows: list[dict]) -> dict:
    per_field = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for f, v in r["fields"].items():
            per_field[f][v] += 1
    fields = {f: {"n": sum(c.values()), "exact": round(100 * c["OK"] / max(1, sum(c.values())), 1),
                  "normalized": round(100 * (c["OK"] + c["OK_NORMALIZED"]) / max(1, sum(c.values())), 1),
                  "wrong": c["WRONG"], "missing": c["MISSING"]} for f, c in sorted(per_field.items())}
    by_scenario = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_scenario[r["scenario"]][r["verdict"]] += 1
    verdict_ok = sum(r["verdict"] == r["expected"] or r["verdict"] in r["acceptable"] for r in rows)
    false_verified = [r["file"] for r in rows if r["verdict"] == "VERIFIED" and r["expected"] != "VERIFIED"]
    ms = [r["ms"] for r in rows]
    return {"docs": len(rows), "fields": fields,
            "verdict_accuracy": round(100 * verdict_ok / max(1, len(rows)), 1),
            "false_verified": false_verified, "by_scenario": {k: dict(v) for k, v in sorted(by_scenario.items())},
            "p50_ms": _pct(ms, .5), "p90_ms": _pct(ms, .9), "p95_ms": _pct(ms, .95), "max_ms": max(ms) if ms else 0,
            "reuse_p50_ms": _pct([r["reuse_ms"] for r in rows], .5)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--report")
    a = ap.parse_args()
    data = asyncio.run(run(a.split))
    s = summarise(data["rows"])
    if a.report:
        Path(a.report).write_text(json.dumps({**s, "rows": data["rows"]}, indent=1), encoding="utf-8")
    print(f"{a.split}: {s['docs']} passbooks  verdict accuracy {s['verdict_accuracy']}%  "
          f"FALSE VERIFIED {len(s['false_verified'])}  P50 {s['p50_ms']} / P90 {s['p90_ms']} / P95 {s['p95_ms']} ms  "
          f"(second API on same file: {s['reuse_p50_ms']} ms)")
    for f, v in s["fields"].items():
        print(f"  {f:22} n={v['n']:3} exact {v['exact']:5}%  normalized {v['normalized']:5}%  wrong {v['wrong']}  "
              f"missing {v['missing']}")
    for sc, v in s["by_scenario"].items():
        print(f"  {sc:16} {v}")


if __name__ == "__main__":
    main()
