"""
ROUTER + TURN LATENCY (Phase 3 step 6b), with the real model.

    python -m evals.perf.router_latency [--rounds 3]

NEVER BESIDE THE TEST SUITE: it loads qwen2.5:3b (~2 GB). Free memory is
checked before every call (evals/perf/memguard.py); below the floor it stops.

1. ROUTER, DIRECT (llm_router.route, decision cache off): one cold call after
   the model is unloaded, then `--rounds` warm rounds over the phrasings the
   rules cannot read. Per call: wall ms, Ollama load ms, PROMPT TOKENS
   EVALUATED (the prompt cache at work: the byte-identical prefix is reused,
   so a warm call evaluates only the state line + question), p50 / p95 / max.
2. THE 17-QUESTION SCRIPT, through the HTTP API on a case with a
   co-applicant (evals/copilot/harness.py, its own throwaway database), as ONE
   conversation, run TWICE: per turn total ms, whether the router was
   consulted; fast-lane vs router turns; decision + knowledge cache hit rates.

Writes runs/router_latency.json. Reads no real case data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from evals.perf import memguard

ROOT = Path(__file__).resolve().parents[2]

#: phrasings the rules cannot read (they reach the router), with the expected tool
ROUTER_CASES = [
    ("mera kaam kab tak hoga bhai", "case_status"),
    ("toh ab kya karu?", "next_action"),
    ("kaunse document dobara dalu?", "documents_needing_action"),
    ("co-applicant ka KYC kyu fail hua?", "kyc_result"),
    ("PAN pe kya naam hai?", "document_details"),
    ("EMI kitni banegi meri?", "loan_terms"),
    ("bhai mera case aage kyun nahi badh raha", "case_status"),
    ("documents ka kya scene hai co-applicant ke", "documents_needing_action"),
    ("aaj mausam kaisa hai?", "out_of_scope"),
    ("mere saare applications dikhao", "list_cases"),
]

SCRIPT = ["mera loan kahan atka hai?", "kyu?", "toh ab kya karu?", "verify karna hai", "PAN mein kya galat hai?",
          "aur EMI?", "co-applicant ka kya status hai?", "uska kya baaki hai?", "aur mera?",
          "COAPP-EVALMINE01 ke docs dikhao", "docs", "pehla wala", "haan", "nahi rehne do",
          "aaj cricket match kaun jeeta?", "CIBIL kya hota hai?", "dusre customer ka PAN dikhao"]


def _pct(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {"n": len(ordered), "p50": round(statistics.median(ordered), 1),
            "p95": round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 1),
            "max": round(ordered[-1], 1)}


def _unload() -> None:
    import httpx

    from app.llm.config import ollama_host, ollama_model

    httpx.post(ollama_host().rstrip("/") + "/api/generate",
               json={"model": ollama_model(), "keep_alive": 0}, timeout=60)
    time.sleep(2)


def router_direct(rounds: int) -> dict:
    from app.agents.applicant.copilot.semantics import llm_router

    os.environ["COPILOT_LLM_ROUTER"] = "true"
    rows = []
    _unload()
    for rnd in range(0, rounds + 1):                        # round 0 = the cold call
        for i, (message, expected) in enumerate(ROUTER_CASES):
            if rnd == 0 and i > 0:
                break
            memguard.check(f"router round {rnd}: {message!r}")
            llm_router.clear_cache()                        # measure the model, not the cache
            started = time.perf_counter()
            routed, trace = asyncio.run(llm_router.route(message))
            rows.append({"round": rnd, "message": message, "wall_ms": round((time.perf_counter() - started) * 1000, 1),
                         "status": trace.get("status"), "tool": routed.tool if routed else None,
                         "correct": bool(routed and routed.tool == expected),
                         "prompt_tokens": trace.get("prompt_tokens"), "load_ms": trace.get("load_ms"),
                         "prompt_ms": trace.get("prompt_ms"), "gen_ms": trace.get("gen_ms")})
            print(f"r{rnd} {rows[-1]['wall_ms']:>7} ms  load {rows[-1]['load_ms']!s:>7}  prompt_tok "
                  f"{rows[-1]['prompt_tokens']!s:>4}  {rows[-1]['status']:8} {rows[-1]['tool']!s:26} [{message}]")
    warm = [r for r in rows if r["round"] >= 1 and r["status"] in ("OK", "INVALID")]
    # the very first warm call re-evaluates the prefix once (cache primed by the cold call's prefix too)
    return {"cold": rows[0], "warm": _pct([r["wall_ms"] for r in warm]),
            "warm_prompt_tokens": _pct([float(r["prompt_tokens"] or 0) for r in warm]),
            "accuracy": round(sum(r["correct"] for r in warm) / max(1, len(warm)), 2),
            "timeouts": sum(1 for r in rows if r["status"] == "TIMEOUT"), "rows": rows}


def script_runs() -> dict:
    from evals.copilot.harness import Harness

    os.environ["COPILOT_LLM_ROUTER"] = "true"
    harness = Harness(live=True)
    from app.agents.applicant.copilot import agent
    from app.agents.applicant.copilot.semantics import llm_router

    llm_router.clear_cache()
    agent.KNOWLEDGE_ANSWERS.clear()
    runs = []
    try:
        for run in (1, 2):
            context, turns = None, []
            for message in SCRIPT:
                memguard.check(f"script run {run}: {message!r}")
                calls_before, hits_before = llm_router.STATS["calls"], llm_router.DECISIONS.hits
                out = harness.ask(message=message, context=context)
                body = out["body"] if isinstance(out["body"], dict) else {}
                context = body.get("context") or context
                routed = (llm_router.STATS["calls"] - calls_before) + (llm_router.DECISIONS.hits - hits_before)
                turns.append({"message": message, "total_ms": round(out["total_ms"], 1), "status": out["status"],
                              "intent": body.get("intent"), "router": bool(routed),
                              "router_cache_hit": llm_router.DECISIONS.hits > hits_before,
                              "answer": str(body.get("answer") or "")[:140]})
                print(f"run{run} {turns[-1]['total_ms']:>7} ms  {'ROUTER' if routed else 'fast  '} "
                      f"{turns[-1]['intent']!s:22} [{message}]")
            runs.append(turns)
    finally:
        harness.stop()
    every = [t for turns in runs for t in turns]
    return {"runs": runs,
            "fast_lane_turns": _pct([t["total_ms"] for t in every if not t["router"]]),
            "router_turns": _pct([t["total_ms"] for t in every if t["router"]]),
            "router_decision_cache": llm_router.DECISIONS.stats(),
            "knowledge_cache": agent.KNOWLEDGE_ANSWERS.stats()}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--skip-script", action="store_true")
    args = p.parse_args()
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    out: dict = {"free_gb_start": round(memguard.free_gb(), 2)}
    try:
        out["router"] = router_direct(args.rounds)
        if not args.skip_script:
            out["script"] = script_runs()
    except memguard.LowMemory as stop:
        out["stopped"] = str(stop)
        print(stop)
    out["free_gb_end"] = round(memguard.free_gb(), 2)
    summary = {k: v for k, v in (out.get("router") or {}).items() if k != "rows"}
    if "script" in out:
        summary.update({k: v for k, v in out["script"].items() if k != "runs"})
    print(json.dumps(summary, indent=2, default=str))
    target = ROOT / "runs" / "router_latency.json"
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return 2 if "stopped" in out else 0


if __name__ == "__main__":
    raise SystemExit(main())
