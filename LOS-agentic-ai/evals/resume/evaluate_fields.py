"""
PER-FIELD ACCURACY on the real-world-style set (evals/resume/realworld.py), through the real service.

    python -m evals.resume.evaluate_fields --split dev [--misses] [--report runs/resume_fields_dev.json]

For every HR field: OK / WRONG (a value that is not the truth -- the failure that matters) / MISSING. By field, by
file format and by template; latency by format. PRIVACY on every output: father's name, gender, a raw PAN or
Aadhaar, the referee's phone must never appear. Aggregates only unless --misses (synthetic people, no real data).
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


def _n(v) -> str:
    return re.sub(r"[^A-Z0-9@.]", "", str(v or "").upper())


def _cmp(got, want) -> str:
    if want in (None, "", [], {}):
        return "OK" if got in (None, "", [], {}) else "WRONG"
    if got in (None, "", [], {}):
        return "MISSING"
    return "OK" if got == want or _n(got) == _n(want) else "WRONG"


def score(t: dict, out: dict) -> dict[str, str]:
    f = {}
    f["name"] = _cmp(out.get("name"), t["name"])
    f["email"] = _cmp(out.get("email"), t["email"])
    f["phone"] = _cmp(out.get("phone"), t["phone"])
    f["date_of_birth"] = _cmp(out.get("date_of_birth"), t["date_of_birth"])
    loc = out.get("location") or {}
    f["city"] = _cmp(loc.get("city"), t["city"])
    f["state"] = _cmp(loc.get("state"), t.get("state"))
    years = {e["level"]: e["year"] for e in out.get("education") or [] if e.get("year")}
    f["highest_qualification"] = _cmp(out.get("highest_qualification"), t["highest_qualification"])
    f["year_10th"] = _cmp(years.get("10th"), t["year_10th"])
    f["year_12th"] = _cmp(years.get("12th"), t["year_12th"])
    if t.get("graduation_year"):
        f["graduation_year"] = _cmp(years.get("graduation"), t["graduation_year"])
    # jobs: matched by start month
    got_jobs = {j["start"]: j for j in out.get("experience") or []}
    for k, j in enumerate(t["jobs"]):
        g = got_jobs.get(j["start"])
        f[f"job{k}_dates"] = "MISSING" if g is None else ("OK" if g["end"] == j["end"] else "WRONG")
        f[f"job{k}_employer"] = "MISSING" if g is None else _cmp(g.get("employer"), j["employer"])
        f[f"job{k}_designation"] = "MISSING" if g is None else _cmp(g.get("designation"), j["designation"])
    extra = len(got_jobs) - len({j["start"] for j in t["jobs"]} & set(got_jobs))
    f["no_phantom_jobs"] = "OK" if extra == 0 else "WRONG"
    if t["current_employer"]:
        f["current_employer"] = _cmp(out.get("current_employer"), t["current_employer"])
        f["current_designation"] = _cmp(out.get("current_designation"), t["current_designation"])
    f["employment_gaps"] = "OK" if len(out.get("employment_gaps") or []) == t["gap_count"] else "WRONG"
    cur = out.get("current_ctc") or {}
    if t["current_ctc_annual"]:
        got = cur.get("annual_inr")
        f["current_ctc"] = "MISSING" if got is None else ("OK" if abs(got - t["current_ctc_annual"]) <= 1000
                                                          else "WRONG")
    else:
        f["current_ctc"] = "OK" if not cur.get("annual_inr") else "WRONG"
    exp = out.get("expected_ctc") or {}
    want = t["expected_ctc"]
    if "negotiable" in want:
        f["expected_ctc"] = "OK" if exp.get("negotiable") else ("MISSING" if not exp else "WRONG")
    elif "min" in want:
        f["expected_ctc"] = "MISSING" if not exp else ("OK" if exp.get("amount_min_inr") == want["min"]
                                                       and exp.get("amount_max_inr") == want["max"] else "WRONG")
    else:
        f["expected_ctc"] = "MISSING" if not exp else ("OK" if exp.get("annual_inr") == want["annual"] else "WRONG")
    notice = out.get("notice_period") or {}
    f["notice_period"] = "MISSING" if notice.get("days") is None else \
        ("OK" if notice["days"] == t["notice_days"] else "WRONG")
    got_l, want_l = sorted(out.get("languages") or []), t["languages"]
    f["languages"] = "MISSING" if not got_l else ("OK" if got_l == want_l else "WRONG")
    return f


def privacy(t: dict, out: dict) -> list[str]:
    blob = json.dumps(out, ensure_ascii=False)
    squashed = re.sub(r"\s", "", blob)
    leaks = []
    for item in t["_forbidden"]:
        if (item.isdigit() and item in squashed) or (not item.isdigit() and re.search(rf"\b{re.escape(item)}\b", blob)):
            leaks.append("forbidden value")
    if re.search(rf'"\s*{t["_gender"]}\s*"|\b(?:gender|sex)\b', blob, re.I):
        leaks.append("gender")
    return leaks


async def run(split: str) -> list[dict]:
    from app.agents.resume import service

    folder = ROOT / "samples" / "resume_realworld" / split
    truth = {k: v for k, v in json.loads((folder / "ground_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    # warm the OCR model on every worker, the way the server does at startup, so no photo pays a cold load
    from evals.resume.evaluate import _warm_all_ocr_workers

    _warm_all_ocr_workers()
    first = next((k for k in truth if k.endswith((".jpg", ".png"))), next(iter(truth)))
    await service.analyse((folder / first).read_bytes(), first, caller="warm", request_id="w")
    rows = []
    for name, t in truth.items():
        service.clear_cache()
        s = time.perf_counter()
        try:
            a = await service.analyse((folder / name).read_bytes(), name, caller="eval", request_id=name,
                                      expected={"name": t["name"]})
            out, verdict, reasons = a.resume.public(), a.verdict.status, a.verdict.reasons
        except service.Refused as exc:
            out, verdict, reasons = {}, "REFUSED", [exc.code]
        ms = round((time.perf_counter() - s) * 1000)
        resp = service.response(a) if verdict != "REFUSED" else {}
        rows.append({"file": name, "template": t["template"], "format": t["format"], "verdict": verdict,
                     "reasons": reasons, "ms": ms, "fields": score(t, out) if out else {},
                     "privacy": privacy(t, resp.get("fields") or {}) if resp else []})
    return rows


def summarise(rows: list[dict]) -> dict:
    by_field = defaultdict(lambda: defaultdict(int))
    by_fmt = defaultdict(lambda: [0, 0])
    by_tpl = defaultdict(lambda: [0, 0])
    lat = defaultdict(list)
    for r in rows:
        lat[r["format"]].append(r["ms"])
        for k, v in r["fields"].items():
            key = re.sub(r"job\d+_", "job_", k)
            by_field[key][v] += 1
            by_fmt[r["format"]][0] += v == "OK"
            by_fmt[r["format"]][1] += 1
            by_tpl[r["template"]][0] += v == "OK"
            by_tpl[r["template"]][1] += 1
    ok = sum(c["OK"] for c in by_field.values())
    total = sum(sum(c.values()) for c in by_field.values())
    return {"files": len(rows), "field_accuracy": round(100 * ok / max(1, total), 2),
            "wrong_values": sum(c["WRONG"] for c in by_field.values()),
            "missing_values": sum(c["MISSING"] for c in by_field.values()),
            "verdicts": dict(sorted({v: sum(r["verdict"] == v for r in rows) for v in {r["verdict"] for r in rows}}
                                    .items())),
            "privacy_leaks": [(r["file"], r["privacy"]) for r in rows if r["privacy"]],
            "fields": {k: {"accuracy": round(100 * c["OK"] / max(1, sum(c.values())), 1), "wrong": c["WRONG"],
                           "missing": c["MISSING"], "n": sum(c.values())} for k, c in sorted(by_field.items())},
            "by_format": {k: round(100 * a / max(1, b), 1) for k, (a, b) in sorted(by_fmt.items())},
            "by_template": {k: round(100 * a / max(1, b), 1) for k, (a, b) in sorted(by_tpl.items())},
            "latency_ms": {k: {"p50": sorted(v)[len(v) // 2], "max": max(v)} for k, v in sorted(lat.items())}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--report")
    a = ap.parse_args()
    rows = asyncio.run(run(a.split))
    s = summarise(rows)
    if a.report:
        Path(a.report).write_text(json.dumps({**s, "rows": rows}, indent=1), encoding="utf-8")
    print(f"{a.split}: {s['files']} files  FIELD ACCURACY {s['field_accuracy']}%  wrong {s['wrong_values']}  "
          f"missing {s['missing_values']}  verdicts {s['verdicts']}  PRIVACY LEAKS {len(s['privacy_leaks'])}")
    for k, v in s["fields"].items():
        print(f"  {k:22} {v['accuracy']:6}%  wrong {v['wrong']:3}  missing {v['missing']:3}  (n={v['n']})")
    print("  by format:  ", s["by_format"])
    print("  by template:", s["by_template"])
    print("  latency:    ", {k: v["p50"] for k, v in s["latency_ms"].items()},
          "max", max(v["max"] for v in s["latency_ms"].values()))
    if a.misses:
        for r in rows:
            bad = {k: v for k, v in r["fields"].items() if v != "OK"}
            if bad or r["privacy"] or r["verdict"] not in ("VERIFIED", "MANUAL_REVIEW"):
                print("  MISS", r["file"], r["verdict"], r["reasons"][:3], bad, r["privacy"])


if __name__ == "__main__":
    main()
