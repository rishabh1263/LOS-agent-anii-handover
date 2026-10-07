"""
ROUTER TUNE 2 (Phase 3 step 6b-tune-2): the whole first-turn routing pipeline, per variant.

    python -m evals.perf.router_tune2 [--variants B0,B1,...] [--model qwen3:4b]

For every message of ONE set (the 27 A/B messages + the 17-question script's replies):
  guardrail -> reply guard -> FAST LANE (follow-up resolution + the rules + small talk)
  -> router variant (example bank / Qwen / agreement ...).
Outcome per message: CORRECT, WRONG (a different answer -- the costly case) or CLARIFY
(a clarifying question; for "haan" / "pehla wala" / "nahi rehne do" that IS correct).

Variants (applicant_agent.yaml: chatbot.router overrides):
  B0 the 6b-tune router (V4)                 B1 B0 + constrained output (JSON schema enums)
  B2 B0 + normalise (typo pass + spelling)   B3 B0 + example bank, char n-grams (held out)
  B4 B0 + example bank, nomic (held out)     B5 best bank + agreement (disagree -> clarify)
  B6 hierarchical (category, then the bank)  B7 B5 + case labels in the prompt
  B8 B5 + router_misses.yaml in the bank (the learning loop; in-sample)
  B9 the best combination on another model (--model), when free memory allows

"Held out": the bank never contains a measured sentence; router_misses.yaml holds measured
misses, so it is excluded except in B8.

The FAST LANE here is approximate and pessimistic: the short-query and work layers of the
real pipeline (which read "docs" and "verify karna hai" first) are not run, so those reach
the router in this measurement.

NEVER BESIDE THE TEST SUITE: real Ollama models; free memory checked before each call.
Writes runs/router_tune2.json.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import statistics
import sys
import time
from pathlib import Path

from evals.perf import memguard

ROOT = Path(__file__).resolve().parents[2]

#: tool -> the intents a fast-lane answer for it may carry
INTENT_TOOL = {
    "APPLICATION_STATUS": "case_status", "APPLICATION_STAGE": "case_status", "READINESS": "case_status",
    "NEXT_ACTION": "next_action", "DOCUMENTS_PENDING": "documents_needing_action",
    "DOCUMENTS_MISSING": "documents_needing_action", "PENDING_ITEMS": "documents_needing_action",
    "DOCUMENT_VERIFICATION": "documents_needing_action", "DOCUMENT_DETAILS": "document_details",
    "KYC_RESULT": "kyc_result", "ELIGIBILITY": "loan_terms", "FOS_KNOWLEDGE": "knowledge",
    "STAGE_PROCESS": "knowledge", "CASE_PORTFOLIO": "list_cases", "OUT_OF_SCOPE": "out_of_scope",
    "PENDING_ITEMS": "documents_needing_action",
    "GUARDRAIL_BLOCKED": "refuse", "OFF_TOPIC": "out_of_scope",
}
REPLIES = [("haan", {}), ("pehla wala", {}), ("nahi rehne do", {})]
CASE_LABELS = "pending=PAN,ADDRESS_PROOF blockers=KYC_NAME_MISMATCH party=CO_APPLICANT"
SPELLING = {"kyon": "kyu", "kyun": "kyu", "kiyu": "kyu", "doc": "docs", "dcuments": "documents",
            "documnets": "documents", "staus": "status", "aplication": "application", "aadhar": "aadhaar",
            "kab": "kab", "hoga": "hoga", "baki": "baaki", "bacha": "baaki", "dikha": "dikhao"}


def _cases():
    from evals.perf.router_ab import CASES

    rows = [(m, state, acc, party) for m, _r, state, acc, party in CASES]
    rows += [(m, s, {"CLARIFY"}, None) for m, s in REPLIES]
    return rows


def _judge(tool, args, accepted, party):
    if "CLARIFY" in accepted:
        return "CORRECT" if tool == "CLARIFY" else "WRONG"
    if tool == "CLARIFY":
        return "CLARIFY"
    if tool not in accepted:
        return "WRONG"
    said = " ".join(str(v).upper() for v in (args or {}).values())
    if party == "CO" and "CO_APPLICANT" not in said:
        return "WRONG"
    if party == "SELF" and "CO_APPLICANT" in said:
        return "WRONG"
    return "CORRECT"


def fast_lane(message, state):
    """(tool, args) when the deterministic layers answer, else None."""
    from app.agents.applicant import conversation
    from app.agents.applicant.copilot import agent
    from app.agents.applicant.copilot.conversation import followup
    from app.agents.applicant.copilot.routing import subjects
    from app.agents.applicant.copilot.semantics import llm_router
    from app.security import guardrails

    if not guardrails.check_input(message).allowed:
        return "refuse", {}
    if llm_router.reply_like(message):
        return "CLARIFY", {}
    turn = conversation.classify(message)
    if turn is not None and turn.kind == "OFF_TOPIC":
        return "out_of_scope", {}
    resolved = followup.resolve(message, followup.Context(**state)).message
    classification, subject, _ = agent._classify_typed(resolved, has_case=True)
    intent = classification.intent.value
    if intent == "UNKNOWN":
        return None
    subject = subject or subjects.carried(message, state.get("last_subject"))     # as the agent does
    party = {"party": "CO_APPLICANT"} if subject in (subjects.Kind.CO, subjects.Kind.BOTH) else {}
    return INTENT_TOOL.get(intent, intent.lower()), party


def variant_config(name, base, best_vec):
    cfg = copy.deepcopy(base)
    emb = {"enabled": False, "vectoriser": best_vec, "threshold": 0.45, "margin": 0.05, "include_misses": False}
    if name == "B1":
        cfg["constrained"] = True
    if name == "B2":
        cfg["normalise"], cfg["spelling"] = True, SPELLING
    if name == "B3":
        emb.update(enabled=True, vectoriser="char")
    if name == "B4":
        emb.update(enabled=True, vectoriser="ollama")
    if name in ("B5", "B6", "B7", "B8", "B9"):
        emb.update(enabled=True)
        cfg["agreement"] = True
        cfg["constrained"] = True
    if name == "B6":
        cfg["hierarchical"] = True
    if name == "B7":
        cfg["case_labels"] = True
    if name in ("B8", "B9"):
        emb["include_misses"] = True
    cfg["embedding"] = emb
    return cfg


def run(names, base, model, nomic_cache, best_vec):
    from app.agents.applicant.copilot.conversation.followup import Context
    from app.agents.applicant.copilot.semantics import embedding_router, llm_router

    rows = []
    original = llm_router._config
    for name in names:
        cfg = variant_config(name, base, best_vec)
        if name == "B9" and model:
            cfg["model"] = model
        llm_router._config = lambda cfg=cfg: cfg
        embedding_router._config = lambda cfg=cfg: cfg.get("embedding") or {}
        embedding_router.reload()
        if cfg["embedding"].get("enabled"):
            kind = cfg["embedding"]["vectoriser"]
            pre = embedding_router.Bank(kind, include_misses=cfg["embedding"]["include_misses"],
                                        vectors=nomic_cache if kind == "ollama" else None)
            embedding_router.bank = lambda pre=pre: pre
        for message, state, accepted, party in _cases():
            memguard.check(f"{name} {message!r}")
            llm_router.clear_cache()
            started = time.perf_counter()
            fast = fast_lane(message, state)
            trace, llm_used = {}, False
            if fast is not None:
                tool, args, how = fast[0], fast[1], "FAST"
            else:
                routed, trace = asyncio.run(llm_router.route(
                    message, Context(**state), case_labels=CASE_LABELS if cfg.get("case_labels") else None))
                llm_used = bool(trace.get("consulted"))
                if routed is None or routed.options:
                    tool, args = "CLARIFY", {}
                else:
                    tool, args = routed.tool, routed.args
                how = trace.get("status")
            ms = round((time.perf_counter() - started) * 1000, 1)
            outcome = _judge(tool, args, accepted, party)
            rows.append({"variant": name, "message": message, "how": how, "tool": tool, "args": args,
                         "outcome": outcome, "llm": llm_used, "ms": ms,
                         "invalid": trace.get("status") == "INVALID", "timeout": trace.get("status") == "TIMEOUT",
                         "bank_score": trace.get("bank_score"), "category": trace.get("category")})
            r = rows[-1]
            print(f"{name} {ms:>7} ms {outcome:8} {str(how):10} {str(tool):26} {json.dumps(args)[:34]:34} [{message}]")
    llm_router._config = original
    return rows


def summary(rows):
    n = len(rows)
    llm_rows = [r for r in rows if r["llm"]]
    ms = sorted(r["ms"] for r in rows)
    pick = lambda q: ms[min(n - 1, int(round(q * (n - 1))))]  # noqa: E731
    return {"messages": n, "accuracy": round(sum(r["outcome"] == "CORRECT" for r in rows) / n, 3),
            "wrong_rate": round(sum(r["outcome"] == "WRONG" for r in rows) / n, 3),
            "clarify_rate": round(sum(r["outcome"] == "CLARIFY" for r in rows) / n, 3),
            "invalid_of_llm_calls": f"{sum(r['invalid'] for r in llm_rows)}/{len(llm_rows)}",
            "timeouts": sum(r["timeout"] for r in rows),
            "answered_without_llm": round(sum(not r["llm"] for r in rows) / n, 3),
            "p50_ms": statistics.median(ms), "p95_ms": pick(0.95), "max_ms": ms[-1]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--variants", default="B0,B1,B2,B3,B4,B5,B6,B7,B8")
    p.add_argument("--model", default="")
    p.add_argument("--vectoriser", default="char", help="the bank vectoriser for B5-B9")
    args = p.parse_args()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["COPILOT_LLM_ROUTER"] = "true"
    os.environ["COPILOT_TERMS_KNOWLEDGE"] = "true"            # the recommended display flag ("CIBIL kya hai?")
    from app.agents.applicant.copilot.semantics import embedding_router, llm_router

    base = copy.deepcopy(llm_router._config())
    names = args.variants.split(",")
    nomic_cache = {}
    if "B4" in names or args.vectoriser == "ollama":
        # ALL nomic vectors first (bank + every question), then nomic is unloaded: never two models at once
        memguard.check("nomic precompute")
        texts = [e["text"] for e in embedding_router.load_examples(True)]
        for message, *_ in _cases():
            texts += [message, llm_router.prepare(message)]
        from app.knowledge.embeddings import OllamaEmbedding
        from app.llm.config import ollama_host

        started = time.perf_counter()
        vectors = OllamaEmbedding(url=ollama_host(), model="nomic-embed-text:latest", timeout=120).embed_all(texts)
        nomic_cache = dict(zip(texts, vectors))
        print(f"nomic: {len(texts)} texts embedded in {(time.perf_counter() - started) * 1000:.0f} ms")
        import httpx

        httpx.post(ollama_host().rstrip("/") + "/api/generate",
                   json={"model": "nomic-embed-text:latest", "keep_alive": 0}, timeout=60)
    from app.llm import keep_warm

    asyncio.run(keep_warm.warm_up())
    out, stopped = [], None
    try:
        out = run(names, base, args.model, nomic_cache, args.vectoriser)
    except memguard.LowMemory as stop:
        stopped = str(stop)
        print(stop)
    table = {v: summary([r for r in out if r["variant"] == v]) for v in names if any(r["variant"] == v for r in out)}
    print(json.dumps(table, indent=2))
    target = ROOT / "runs" / "router_tune2.json"
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    previous.setdefault("table", {}).update(table)
    previous.setdefault("rows", [])
    previous["rows"] = [r for r in previous["rows"] if r["variant"] not in table] + out
    previous["stopped"] = stopped
    target.write_text(json.dumps(previous, indent=2, ensure_ascii=False), encoding="utf-8")
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
