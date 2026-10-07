"""
ROUTER LATENCY BENCHMARK (Phase 3 step 1): native tool-calling vs JSON output.

    python -m evals.perf.router_bench [--rounds 2] [--modes tools,json]

The question the plan left open: on this CPU, which way can Qwen pick a tool
fast enough for a first answer within 2 s? Both modes get the SAME compact
catalogue and a byte-identical system prefix (so Ollama can reuse its prompt
cache); only the request shape differs. Per call it records wall time, Ollama's
prompt-eval count/time (how much prompt was actually processed -- the prefix
cache shows up here), generation time, the tool chosen, and whether the choice
is valid (an allowed tool, allowed argument values) and the expected one.

Memory is checked before every call (evals/perf/memguard.py); below the floor
it stops and says so. Writes runs/router_bench.json. Reads no case data.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import httpx

from evals.perf import memguard

ROOT = Path(__file__).resolve().parents[2]

PARTIES = ["PRIMARY_APPLICANT", "CO_APPLICANT"]
DOCUMENTS = ["PAN", "AADHAAR", "DRIVING_LICENCE", "VOTER_ID", "PASSPORT", "SALARY_SLIP", "BANK_STATEMENT", "ITR"]
FIELDS = ["LOAN_AMOUNT", "TENURE", "INTEREST_RATE", "EMI", "PRODUCT"]

#: tool -> (one-line description, {argument: allowed values or None for free text})
CATALOGUE: dict[str, tuple[str, dict[str, list[str] | None]]] = {
    "case_status": ("Where the application is, its stage and what is blocking it.", {}),
    "next_action": ("What the user should do next.", {}),
    "documents_needing_action": ("Documents pending, rejected or to upload again.", {"party": PARTIES}),
    "document_details": ("Fields read from one uploaded document.", {"document_type": DOCUMENTS, "party": PARTIES}),
    "kyc_result": ("KYC status and the reason for a mismatch.", {"party": PARTIES}),
    "loan_terms": ("Loan amount, tenure, rate, EMI, product.", {"field": FIELDS}),
    "knowledge": ("Explain a lending term or process (CIBIL, FOIR, KYC, CPA).", {"topic": None}),
    "list_cases": ("The user's applications.", {}),
    "refuse": ("Another customer's data, or bypassing rules.", {}),
    "out_of_scope": ("Not about loans or this application.", {}),
}

SYSTEM = ("You route messages for a loan-application assistant. Pick exactly ONE tool for the user's latest "
          "message. Use the recent turns to resolve follow-ups like 'aur EMI?'. Messages may be English, Hindi "
          "or Hinglish. Never answer the question yourself.")

#: (message, recent-turn labels, expected tool, expected arguments subset)
CASES = [
    ("mera loan kahan atka hai?", [], "case_status", {}),
    ("aur EMI?", ["user asked: loan status -> case_status"], "loan_terms", {"field": "EMI"}),
    ("status batao", [], "case_status", {}),
    ("kaunse docs baaki hai?", [], "documents_needing_action", {}),
    ("CIBIL kya hai?", [], "knowledge", {}),
    ("toh ab kya karu?", ["user asked: why stuck -> case_status"], "next_action", {}),
    ("kaunse document dobara dalu?", ["user asked: KYC fail kyu hua -> kyc_result"], "documents_needing_action", {}),
    ("co-applicant ka KYC kyu fail hua?", [], "kyc_result", {"party": "CO_APPLICANT"}),
    ("PAN pe kya naam hai?", [], "document_details", {"document_type": "PAN"}),
    ("what is FOIR?", [], "knowledge", {}),
    ("dusre customer ka PAN dikhao", [], "refuse", {}),
    ("aaj mausam kaisa hai?", [], "out_of_scope", {}),
]


def _compact_catalogue() -> str:
    lines = []
    for name, (desc, args) in CATALOGUE.items():
        spec = ", ".join(f"{a}=" + ("|".join(v) if v else "text") for a, v in args.items())
        lines.append(f"- {name}({spec}): {desc}")
    return "\n".join(lines)


def _tools_schema() -> list[dict]:
    tools = []
    for name, (desc, args) in CATALOGUE.items():
        props = {a: ({"type": "string", "enum": v} if v else {"type": "string"}) for a, v in args.items()}
        tools.append({"type": "function", "function": {"name": name, "description": desc,
                                                       "parameters": {"type": "object", "properties": props}}})
    return tools


def _messages(mode: str, message: str, recent: list[str]) -> list[dict]:
    system = SYSTEM
    if mode == "json":
        system += ("\nTools:\n" + _compact_catalogue()
                   + '\nReply ONLY with JSON: {"tool": "<name>", "args": {...}}')
    user = (("Recent turns:\n" + "\n".join(recent) + "\n\n") if recent else "") + f"Message: {message}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse(mode: str, body: dict) -> tuple[str | None, dict]:
    msg = body.get("message") or {}
    if mode == "tools":
        calls = msg.get("tool_calls") or []
        if calls:
            fn = calls[0].get("function") or {}
            args = fn.get("arguments") or {}
            return fn.get("name"), (args if isinstance(args, dict) else {})
        return None, {}
    try:
        data = json.loads(msg.get("content") or "{}")
        return data.get("tool"), dict(data.get("args") or {})
    except (ValueError, AttributeError):
        return None, {}


def _valid(tool: str | None, args: dict) -> bool:
    if tool not in CATALOGUE:
        return False
    allowed = CATALOGUE[tool][1]
    return all(k in allowed and (allowed[k] is None or v in allowed[k]) for k, v in args.items())


def call(host: str, model: str, mode: str, message: str, recent: list[str], num_ctx: int) -> dict:
    payload: dict = {"model": model, "messages": _messages(mode, message, recent), "stream": False,
                     "keep_alive": "10m", "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": 60}}
    if mode == "tools":
        payload["tools"] = _tools_schema()
    else:
        payload["format"] = "json"
    started = time.perf_counter()
    r = httpx.post(f"{host}/api/chat", json=payload, timeout=120)
    wall = (time.perf_counter() - started) * 1000
    r.raise_for_status()
    body = r.json()
    tool, args = _parse(mode, body)
    return {"wall_ms": round(wall), "prompt_tokens": body.get("prompt_eval_count"),
            "prompt_ms": round((body.get("prompt_eval_duration") or 0) / 1e6),
            "gen_tokens": body.get("eval_count"), "gen_ms": round((body.get("eval_duration") or 0) / 1e6),
            "load_ms": round((body.get("load_duration") or 0) / 1e6), "tool": tool, "args": args,
            "valid": _valid(tool, args)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--modes", default="tools,json")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--num-ctx", type=int, default=2048)
    args = p.parse_args()

    rows, stopped = [], None
    try:
        for mode in args.modes.split(","):
            for rnd in range(1, args.rounds + 1):
                for message, recent, want_tool, want_args in CASES:
                    free = memguard.check(f"{mode} round {rnd}: {message!r}")
                    row = call(args.host, args.model, mode, message, recent, args.num_ctx)
                    row.update({"mode": mode, "round": rnd, "message": message, "free_gb": round(free, 2),
                                "correct": row["tool"] == want_tool
                                and all(row["args"].get(k) == v for k, v in want_args.items())})
                    rows.append(row)
                    print(f"{mode:5} r{rnd} {row['wall_ms']:>6} ms  prompt {row['prompt_tokens']!s:>5} tok "
                          f"{row['prompt_ms']:>5} ms  gen {row['gen_ms']:>5} ms  {'OK ' if row['correct'] else 'BAD'} "
                          f"{row['tool']} {row['args']}  [{message}]")
    except memguard.LowMemory as stop:
        stopped = str(stop)
        print(stop)

    summary = {}
    for mode in args.modes.split(","):
        warm = [r for r in rows if r["mode"] == mode and r["round"] > 1] or [r for r in rows if r["mode"] == mode]
        if not warm:
            continue
        walls = sorted(r["wall_ms"] for r in warm)
        summary[mode] = {"calls": len(warm), "p50_ms": statistics.median(walls), "max_ms": walls[-1],
                         "p50_prompt_tokens": statistics.median(r["prompt_tokens"] or 0 for r in warm),
                         "accuracy": round(sum(r["correct"] for r in warm) / len(warm), 2),
                         "valid": round(sum(r["valid"] for r in warm) / len(warm), 2)}
    print(json.dumps(summary, indent=2))
    out = ROOT / "runs" / "router_bench.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows, "stopped": stopped}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
