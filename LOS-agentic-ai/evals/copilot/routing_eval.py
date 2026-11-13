"""
ROUTING EVAL (2026-10-16) -- the production meaning reader (embeddings + adapter + classifier blend, meaning.rank /
meaning.clear_reading) on the held-out meaning sets, no HTTP, no Qwen.

    python -m evals.copilot.routing_eval                 # sets e, f, g
    python -m evals.copilot.routing_eval --sets g --misses

Per set:
    top1          the best intent is the labelled one
    clear         the fast path answers it without Qwen (clear_reading)
    clear_wrong   CLEAR AND WRONG -- the dangerous case: answered at once with the wrong intent (Qwen never sees it)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def run(sets: list[str], misses: bool) -> dict:
    from app.agents.applicant.copilot.semantics import meaning

    out = {}
    for name in sets:
        items = yaml.safe_load((ROOT / "evals" / "copilot" / f"meaning_heldout_{name}.yaml")
                               .read_text(encoding="utf-8"))["items"]
        top1 = clear = clear_wrong = 0
        rows = []
        started = time.perf_counter()
        for item in items:
            ranked = meaning.rank(item["q"])
            got = ranked[0][0] if ranked else None
            is_clear = meaning.clear_reading(item["q"], True)
            ok = got == item["intent"]
            top1 += ok
            clear += is_clear
            clear_wrong += is_clear and not ok
            if not ok:
                rows.append({"q": item["q"], "want": item["intent"], "got": got, "clear": is_clear,
                             "score": round(ranked[0][1], 3) if ranked else None})
        n = len(items)
        out[name] = {"n": n, "top1": f"{top1}/{n} ({100 * top1 / n:.1f}%)", "clear": f"{clear}/{n}",
                     "clear_wrong": clear_wrong, "ms_per_q": round(1000 * (time.perf_counter() - started) / n, 1)}
        if misses:
            out[name]["misses"] = rows
    return out


if __name__ == "__main__":
    # the PRODUCTION reader: the embedding model and its address come from .env, as the LOS server reads them
    # (without it the reader has no model, ranks nothing and every set reads 0% -- seen 2026-10-28)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="e,f,g")
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args()
    result = run([s.strip() for s in args.sets.split(",") if s.strip()], args.misses)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    sys.exit(0)
