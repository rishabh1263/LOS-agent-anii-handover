"""
ROUTER A/B (Phase 3 step 6b-tune): which prompt format routes best, then fastest.

    python -m evals.perf.router_ab [--rounds 2] [--variants V1,V2,V3,V4]

  V1  the step 1 benchmark format exactly (evals/perf/router_bench.py: its system text,
      catalogue, {"tool","args"} output and "Recent turns" lines) -- the 92% baseline
  V2  the 6b router, readable keys: {"tool","args"} and argument names party / document / field / topic
  V3  V2 + 8 few-shot examples in the cached system prefix (Hinglish included)
  V4  V3 + shorter tool descriptions

Every variant gets THE SAME labelled set: the step 1 phrasings, the 17-question script
(minus the yes / no / option replies, which never reach the router since 6b-tune) and
the 6b latency phrasings. Follow-ups carry their previous turn: as "Recent turns" for
V1 (its own format), as the labels-only state line for V2-V4.

NOTE: V3/V4's few-shot examples were chosen by the user and five of them are in the
set ("toh ab kya karu", "kyu", "PAN mein kya galat", "co-applicant ka status",
"kaunse docs dobara dalu"), so accuracy is also reported on the HELD-OUT rest.

NEVER BESIDE THE TEST SUITE: real qwen2.5:3b; free memory checked before every call.
Writes runs/router_ab.json.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import statistics
import time
from pathlib import Path

from evals.perf import memguard

ROOT = Path(__file__).resolve().parents[2]

DOCS = ["PAN", "AADHAAR", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT", "SALARY_SLIP", "BANK_STATEMENT", "ITR"]

#: (message, V1 recent-turn lines, V2-V4 state labels, accepted tools, required party or None)
#: party: "CO" -> must say co-applicant; "SELF" -> must not say co-applicant
CASES = [
    # step 1 phrasings
    ("mera loan kahan atka hai?", [], {}, {"case_status"}, None),
    ("aur EMI?", ["user asked: loan status -> case_status"], {"last_intent": "APPLICATION_STATUS"}, {"loan_terms"}, None),
    ("status batao", [], {}, {"case_status"}, None),
    ("kaunse docs baaki hai?", [], {}, {"documents_needing_action"}, None),
    ("CIBIL kya hai?", [], {}, {"knowledge"}, None),
    ("toh ab kya karu?", ["user asked: why stuck -> case_status"], {"last_intent": "APPLICATION_STATUS"}, {"next_action"}, None),
    ("kaunse document dobara dalu?", ["user asked: KYC fail kyu hua -> kyc_result"], {"last_intent": "KYC_RESULT"},
     {"documents_needing_action"}, None),
    ("co-applicant ka KYC kyu fail hua?", [], {}, {"kyc_result"}, "CO"),
    ("PAN pe kya naam hai?", [], {}, {"document_details"}, None),
    ("what is FOIR?", [], {}, {"knowledge"}, None),
    ("dusre customer ka PAN dikhao", [], {}, {"refuse"}, None),
    ("aaj mausam kaisa hai?", [], {}, {"out_of_scope"}, None),
    # the 17-question script (router-relevant ones)
    ("kyu?", ["user asked: loan status -> case_status"], {"last_intent": "APPLICATION_STATUS"}, {"case_status"}, None),
    ("verify karna hai", [], {}, {"documents_needing_action"}, None),
    ("PAN mein kya galat hai?", [], {}, {"kyc_result", "document_details"}, None),
    ("co-applicant ka kya status hai?", [], {}, {"kyc_result", "documents_needing_action", "case_status"}, "CO"),
    ("uska kya baaki hai?", ["user asked: co-applicant status -> kyc_result(party=CO_APPLICANT)"],
     {"last_intent": "KYC_RESULT", "last_subject": "CO_APPLICANT"}, {"documents_needing_action"}, "CO"),
    ("aur mera?", ["user asked: co-applicant pending docs -> documents_needing_action(party=CO_APPLICANT)"],
     {"last_intent": "DOCUMENTS_PENDING", "last_subject": "CO_APPLICANT"}, {"documents_needing_action"}, "SELF"),
    ("COAPP-3351F2C1A068 ke docs dikhao", [], {}, {"documents_needing_action"}, "CO"),
    ("docs", [], {}, {"documents_needing_action"}, None),
    ("aaj cricket match kaun jeeta?", [], {}, {"out_of_scope"}, None),
    ("CIBIL kya hota hai?", [], {}, {"knowledge"}, None),
    # 6b latency phrasings
    ("mera kaam kab tak hoga bhai", [], {}, {"case_status"}, None),
    ("bhai mera case aage kyun nahi badh raha", [], {}, {"case_status"}, None),
    ("documents ka kya scene hai co-applicant ke", [], {}, {"documents_needing_action"}, "CO"),
    ("EMI kitni banegi meri?", [], {}, {"loan_terms"}, None),
    ("mere saare applications dikhao", [], {}, {"list_cases"}, None),
]

#: few-shot examples (user-chosen, 2026-10-07) -- messages that also appear in CASES are not "held out"
EXAMPLES = [
    {"message": "toh ab kya karu?", "state": "last=APPLICATION_STATUS", "tool": "next_action"},
    {"message": "kyu atka hai mera loan?", "tool": "case_status"},
    {"message": "PAN mein kya galat hai?", "tool": "kyc_result"},
    {"message": "co-applicant ka status kya hai?", "tool": "kyc_result", "args": {"party": "CO_APPLICANT"}},
    {"message": "kaunse docs dobara dalu?", "tool": "documents_needing_action"},
    {"message": "salary slip pe kya amount hai?", "tool": "document_details", "args": {"document": "SALARY_SLIP"}},
    {"message": "EMI kitni hogi?", "tool": "loan_terms", "args": {"field": "EMI"}},
    {"message": "kal IPL kaun jeeta?", "tool": "out_of_scope"},
]
SEEN = {"toh ab kya karu?", "kyu?", "PAN mein kya galat hai?", "co-applicant ka kya status hai?",
        "kaunse document dobara dalu?"}


def _readable_tools(base: dict, short_descriptions: bool) -> dict:
    """The 6b catalogue with readable argument names (and optionally shorter descriptions)."""
    rename = {"p": "party", "d": "document", "f": "field", "q": "topic"}
    short = {"case_status": "Stage and what is blocking it.", "next_action": "What to do next.",
             "documents_needing_action": "Pending, rejected or re-upload documents.",
             "document_details": "Fields read from one document.", "kyc_result": "KYC result and mismatch reason.",
             "loan_terms": "Amount, tenure, rate, EMI, product.", "knowledge": "Explain a lending term (CIBIL, FOIR, KYC).",
             "list_cases": "The user's applications.", "refuse": "Another customer's data or bypassing rules.",
             "out_of_scope": "Not about this loan application."}
    tools = {}
    for name, spec in copy.deepcopy(base).items():
        spec["args"] = {rename.get(k, k): v for k, v in (spec.get("args") or {}).items()}
        for field in ("question", "co_applicant_question"):
            if spec.get(field):
                for old, new in rename.items():
                    spec[field] = spec[field].replace("{" + old + "}", "{" + new + "}")
        if spec.get("by_value"):
            spec["by_value"] = {rename.get(k, k): v for k, v in spec["by_value"].items()}
        if short_descriptions:
            spec["description"] = short.get(name, spec.get("description"))
        tools[name] = spec
    return tools


def _variant_config(name: str, base: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["output_keys"] = "readable"
    cfg["tools"] = _readable_tools(base["tools"], short_descriptions=(name == "V4"))
    cfg["examples"] = EXAMPLES if name in ("V3", "V4") else []
    return cfg


def _judge(tool: str | None, args: dict, accepted: set, party: str | None) -> bool:
    if tool not in accepted:
        return False
    said = " ".join(str(v).upper() for v in (args or {}).values())
    if party == "CO":
        return "CO_APPLICANT" in said
    if party == "SELF":
        return "CO_APPLICANT" not in said
    return True


def run_v1(rounds: int) -> list[dict]:
    from evals.perf import router_bench as bench

    rows = []
    for rnd in range(1, rounds + 1):
        for message, recent, _state, accepted, party in CASES:
            memguard.check(f"V1 r{rnd} {message!r}")
            try:
                r = bench.call("http://127.0.0.1:11434", "qwen2.5:3b", "json", message, recent, 2048)
                valid = r["valid"]
                tool, args, wall, tok = r["tool"], r["args"], r["wall_ms"], r["prompt_tokens"]
            except Exception as exc:  # noqa: BLE001
                valid, tool, args, wall, tok = False, None, {}, None, None
                print("V1 error", type(exc).__name__)
            rows.append({"variant": "V1", "round": rnd, "message": message, "tool": tool, "args": args,
                         "valid": valid, "correct": valid and _judge(tool, args, accepted, party),
                         "wall_ms": wall, "prompt_tokens": tok})
            _print(rows[-1])
    return rows


def run_router_variant(name: str, rounds: int, base: dict) -> list[dict]:
    from app.agents.applicant.copilot.conversation.followup import Context
    from app.agents.applicant.copilot.semantics import llm_router

    cfg = _variant_config(name, base)
    original = llm_router._config
    llm_router._config = lambda: cfg
    rows = []
    try:
        for rnd in range(1, rounds + 1):
            for message, _recent, state, accepted, party in CASES:
                memguard.check(f"{name} r{rnd} {message!r}")
                llm_router.clear_cache()
                started = time.perf_counter()
                routed, trace = asyncio.run(llm_router.route(message, Context(**state)))
                wall = round((time.perf_counter() - started) * 1000, 1)
                tool, args = (routed.tool, routed.args) if routed else (None, {})
                rows.append({"variant": name, "round": rnd, "message": message, "tool": tool, "args": args,
                             "status": trace.get("status"), "valid": routed is not None,
                             "correct": routed is not None and _judge(tool, args, accepted, party),
                             "wall_ms": wall if trace.get("status") in ("OK", "INVALID") else None,
                             "timeout": trace.get("status") == "TIMEOUT",
                             "prompt_tokens": trace.get("prompt_tokens"), "prompt_ms": trace.get("prompt_ms")})
                _print(rows[-1])
    finally:
        llm_router._config = original
        llm_router.clear_cache()
    return rows


def _print(row: dict) -> None:
    print(f"{row['variant']} r{row['round']} {row['wall_ms']!s:>7} ms  {'OK ' if row['correct'] else 'BAD'} "
          f"{'valid' if row['valid'] else 'INVALID'} {row['tool']!s:26} {json.dumps(row['args'])[:40]:40} [{row['message']}]")


def _summary(rows: list[dict]) -> dict:
    walls = sorted(r["wall_ms"] for r in rows if r["wall_ms"] is not None)
    held = [r for r in rows if r["message"] not in SEEN]
    pick = lambda q: walls[min(len(walls) - 1, int(round(q * (len(walls) - 1))))] if walls else None  # noqa: E731
    return {"calls": len(rows), "accuracy": round(sum(r["correct"] for r in rows) / max(1, len(rows)), 3),
            "held_out_accuracy": round(sum(r["correct"] for r in held) / max(1, len(held)), 3),
            "invalid_rate": round(sum(not r["valid"] for r in rows) / max(1, len(rows)), 3),
            "timeouts": sum(bool(r.get("timeout")) for r in rows),
            "p50_ms": statistics.median(walls) if walls else None, "p95_ms": pick(0.95),
            "max_ms": walls[-1] if walls else None,
            "prompt_tokens_p50": statistics.median([r["prompt_tokens"] for r in rows if r.get("prompt_tokens")] or [0])}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--variants", default="V1,V2,V3,V4")
    args = p.parse_args()
    import os
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["COPILOT_LLM_ROUTER"] = "true"
    from app.agents.applicant.copilot.semantics import llm_router
    from app.llm import keep_warm

    asyncio.run(keep_warm.warm_up())                         # never measure a cold load here
    base = copy.deepcopy(llm_router._config())
    rows, stopped = [], None
    try:
        for name in args.variants.split(","):
            rows += run_v1(args.rounds) if name == "V1" else run_router_variant(name, args.rounds, base)
    except memguard.LowMemory as stop:
        stopped = str(stop)
        print(stop)
    summary = {name: _summary([r for r in rows if r["variant"] == name]) for name in args.variants.split(",")
               if any(r["variant"] == name for r in rows)}
    print(json.dumps(summary, indent=2))
    (ROOT / "runs" / "router_ab.json").write_text(
        json.dumps({"summary": summary, "rows": rows, "stopped": stopped, "cases": len(CASES)}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
