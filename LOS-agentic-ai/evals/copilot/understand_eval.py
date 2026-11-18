"""
UNDERSTANDING EVAL (2026-10-28) -- the PRODUCTION decision end to end (meaning.understand: embeddings, then the model
choosing among the candidates when the reading is not clear), on the held-out meaning sets, no HTTP.

    python -m evals.copilot.understand_eval                               # sets e, f, g, h with the configured model
    python -m evals.copilot.understand_eval --model qwen3.5:4b --sets g,h --misses

Per set:
    correct       the final intent is the labelled one
    not_understood  no decision at all: the officer would get "I did not understand" -- the owner's "fail"
    to_model      readings the model was asked to choose
    model_failed  the model did not answer in time / at all (the embedding reading or nothing stood in)
    clear_wrong   answered from the embeddings alone AND wrong (the model never saw it)
    p50 / p95     seconds per question (model calls included)
Reads .env (the embedding model, Ollama) exactly as the LOS server does. Writes runs/understand_eval_<model>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


async def _one(meaning, q: str) -> tuple[object, float, dict]:
    before = dict(meaning.STATS)
    started = time.perf_counter()
    d = await meaning.understand(q, case_open=True)
    took = time.perf_counter() - started
    delta = {k: meaning.STATS.get(k, 0) - before.get(k, 0) for k in meaning.STATS}
    return d, took, delta


def run(sets: list[str], model: str | None, misses: bool, overrides: dict | None = None) -> dict:
    """`overrides`: decide.* settings tried for this run only (candidates, accept, margin ...) -- the config file is
    never changed by an eval."""
    from app.agents.applicant.copilot.semantics import meaning

    if model:
        meaning.catalogue().setdefault("model", {})["name"] = model
    for key, value in (overrides or {}).items():
        if key.startswith("model."):                     # the chooser's own settings (catalogue `model`)
            meaning.catalogue().setdefault("model", {})[key[6:]] = value
        else:
            meaning._decide_cfg()[key] = value
    out: dict = {"model": (meaning.catalogue().get("model") or {}).get("name"), "decide": dict(overrides or {})}
    # warmed first, as the LOS server does at start (meaning.prime): the weights paged in, the system prompt cached
    out["primed_ms"] = meaning.prime()
    rows_all = []
    for name in sets:
        items = yaml.safe_load((ROOT / "evals" / "copilot" / f"meaning_heldout_{name}.yaml")
                               .read_text(encoding="utf-8"))["items"]
        correct = none = to_model = failed = clear_wrong = 0
        times, rows = [], []
        for item in items:
            meaning._RANKED.clear()
            d, took, delta = asyncio.run(_one(meaning, item["q"]))
            got = getattr(d, "intent", None)
            by = getattr(d, "decided_by", None)
            ok = got == item["intent"]
            correct += ok
            none += d is None
            asked = bool(delta.get("model") or delta.get("model_failed"))
            to_model += asked
            failed += bool(delta.get("model_failed"))
            clear_wrong += (by == "embedding" and not ok)
            times.append(took)
            if not ok:
                rows.append({"q": item["q"], "want": item["intent"], "got": got, "by": by,
                             "model_failed": bool(delta.get("model_failed")), "s": round(took, 2)})
        times.sort()
        out[name] = {"n": len(items), "correct": f"{correct}/{len(items)} ({100 * correct / len(items):.1f}%)",
                     "not_understood": none, "to_model": to_model, "model_failed": failed,
                     "clear_wrong": clear_wrong, "p50_s": round(statistics.median(times), 2),
                     "p95_s": round(times[int(0.95 * (len(times) - 1))], 2)}
        if misses:
            out[name]["misses"] = rows
        rows_all += rows
    return out


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="e,f,g,h")
    ap.add_argument("--model", default="")
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="decide.<key>=<number> for this run, e.g. "
                                                                  "--set model_candidates=8 --set accept=0.82")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    def _value(v: str):
        if v.lower() in ("true", "false"):
            return v.lower() == "true"
        return float(v) if "." in v else int(v)

    overrides = {k: _value(v) for k, v in (s.split("=", 1) for s in args.set)}
    result = run([s.strip() for s in args.sets.split(",") if s.strip()], args.model or None, args.misses, overrides)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    (ROOT / "runs").mkdir(exist_ok=True)
    tag = str(result["model"]).replace(":", "_").replace("/", "_") + (f"_{args.tag}" if args.tag else "")
    (ROOT / "runs" / f"understand_eval_{tag}.json").write_text(text, encoding="utf-8")     # first: never lost
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")       # Hindi questions on a Windows console
    print(text)
