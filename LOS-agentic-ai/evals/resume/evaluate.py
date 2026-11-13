"""
RESUME CHECK: accuracy + latency on the labelled synthetic set, plus OTHER real documents that must be refused.

    python -m evals.resume.evaluate --split dev [--report runs/resume_dev.json]

Aggregates only -- no field value printed. A FALSE VERIFIED (a negative verified) and a NOT-A-RESUME accepted are
counted separately: they are the failures that matter most.
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
FIELDS = ("name", "email", "phone", "highest_qualification", "year_10th", "year_12th", "graduation_year")
OTHER_DOCS = ["samples/documents/lPan.jpg", "samples/real_test/dl1.jpg", "samples/real_test/voter4.jpg",
              "samples/passports/passport_samples0_10.jpg"]


def _norm(v):
    return re.sub(r"[^A-Z0-9@.]", "", str(v or "").upper())


def _warm_all_ocr_workers() -> None:
    """The OCR model loaded on EVERY OCR worker before timing, exactly as the server does at startup (main.py
    passbook warm-up): each worker owns its engine, so a cold one would load the model inside a timed request."""
    from app.agents.document_agent.ocr import get_ocr_executor
    from app.agents.passbook import engine

    pool = get_ocr_executor()
    for future in [pool.submit(engine.warm) for _ in range(pool._max_workers)]:
        future.result(timeout=180)


async def run(split: str) -> list[dict]:
    from app.agents.resume import service

    folder = ROOT / "samples" / "synthetic_resume" / split
    truth = {k: v for k, v in json.loads((folder / "ground_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    others = [(ROOT / p) for p in OTHER_DOCS if (ROOT / p).is_file()]
    passbooks = sorted((ROOT / "samples" / "synthetic_passbook" / split).glob("*.jpg"))[:3]
    cases = [(folder / k, v) for k, v in truth.items()] + [
        (p, {"scenario": "NOT_RESUME", "expected_verdict": "UNSUPPORTED", "format": "jpg", "expected_input": {},
             "fields": {}, "kind": "real_" + p.stem}) for p in others + passbooks]
    first = cases[0][0]
    _warm_all_ocr_workers()
    await service.analyse(first.read_bytes(), first.name, caller="warm", request_id="w")
    rows = []
    for path, t in cases:
        service.clear_cache()
        s = time.perf_counter()
        try:
            a = await service.analyse(path.read_bytes(), path.name, caller="eval", request_id=path.name,
                                      expected=t.get("expected_input") or {})
        except service.Refused as refused:            # refused before reading: a rejection, with its own code
            rows.append({"file": path.name, "scenario": t["scenario"], "format": t.get("format"),
                         "expected": t["expected_verdict"], "verdict": "UNSUPPORTED", "reasons": [refused.code],
                         "looks_like": None, "fields": {}, "ms": round((time.perf_counter() - s) * 1000),
                         "source": "refused"})
            continue
        ms = (time.perf_counter() - s) * 1000
        fields = a.resume.public()
        years = {e.level: e.year for e in a.resume.education if e.year}
        got = {"name": fields["name"], "email": fields["email"], "phone": fields["phone"],
               "highest_qualification": fields["highest_qualification"], "year_10th": years.get("10th"),
               "year_12th": years.get("12th"), "graduation_year": years.get("graduation")}
        verdicts = {}
        for f in FIELDS:
            want = t["fields"].get(f)
            if f not in t["fields"] or (want is None and f in ("graduation_year",)):
                continue
            if want is None:
                verdicts[f] = "OK" if got[f] in (None, "") else "WRONG"
            elif got[f] in (None, ""):
                verdicts[f] = "MISSING"
            else:
                verdicts[f] = "OK" if _norm(got[f]) == _norm(want) else "WRONG"
        rows.append({"file": path.name, "scenario": t["scenario"], "format": t.get("format"),
                     "expected": t["expected_verdict"], "verdict": a.verdict.status, "reasons": a.verdict.reasons,
                     "looks_like": a.verdict.looks_like, "fields": verdicts, "ms": round(ms), "source": a.source})
    return rows


def summarise(rows):
    per_field = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for f, v in r["fields"].items():
            per_field[f][v] += 1
    by_scenario = defaultdict(lambda: defaultdict(int))
    by_format = defaultdict(list)
    for r in rows:
        by_scenario[r["scenario"]][r["verdict"]] += 1
        by_format[r["format"]].append(r["ms"])
    ok = sum(r["verdict"] == r["expected"] for r in rows)
    return {"docs": len(rows), "verdict_accuracy": round(100 * ok / len(rows), 1),
            "false_verified": [r["file"] for r in rows if r["verdict"] == "VERIFIED" and r["expected"] != "VERIFIED"],
            "not_resume_accepted": [r["file"] for r in rows if r["scenario"] == "NOT_RESUME"
                                    and r["verdict"] not in ("UNSUPPORTED", "UNREADABLE")],
            "wrong_verdicts": [(r["file"], r["expected"], r["verdict"], r["reasons"]) for r in rows
                               if r["verdict"] != r["expected"]],
            "fields": {f: {"n": sum(c.values()), "accuracy": round(100 * c["OK"] / max(1, sum(c.values())), 1),
                           "wrong": c["WRONG"], "missing": c["MISSING"]} for f, c in sorted(per_field.items())},
            "by_scenario": {k: dict(v) for k, v in sorted(by_scenario.items())},
            "latency_by_format": {k: {"p50": sorted(v)[len(v) // 2], "max": max(v), "n": len(v)}
                                  for k, v in sorted(by_format.items())}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--report")
    a = ap.parse_args()
    rows = asyncio.run(run(a.split))
    s = summarise(rows)
    if a.report:
        Path(a.report).write_text(json.dumps({**s, "rows": rows}, indent=1), encoding="utf-8")
    print(f"{a.split}: {s['docs']} files  verdict accuracy {s['verdict_accuracy']}%  FALSE VERIFIED "
          f"{len(s['false_verified'])}  NOT-A-RESUME ACCEPTED {len(s['not_resume_accepted'])}")
    for f, v in s["fields"].items():
        print(f"  {f:22} n={v['n']:3} accuracy {v['accuracy']:5}%  wrong {v['wrong']}  missing {v['missing']}")
    for k, v in s["by_scenario"].items():
        print(f"  {k:14} {v}")
    for k, v in s["latency_by_format"].items():
        print(f"  latency {k:5} p50 {v['p50']} ms  max {v['max']} ms  (n={v['n']})")
    for w in s["wrong_verdicts"][:12]:
        print("  WRONG", w)


if __name__ == "__main__":
    main()
