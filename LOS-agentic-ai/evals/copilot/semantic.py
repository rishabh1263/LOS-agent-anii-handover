"""
SEMANTIC UNDERSTANDING EVAL -- through the REAL HTTP API (POST /api/v1/fos/copilot).

    python -m evals.copilot.semantic                  # offline: model unreachable
    python -m evals.copilot.semantic --live           # real qwen2.5:3b fallback
    python -m evals.copilot.semantic --report out.json

Every case is graded on the TRAJECTORY, not only the final text:

    A  semantic frame (task / object)        from `understanding.frame`
    B  referent resolution                   from `understanding.referents`
    C  routing (intent)                      from `intent`
    D  authoritative tool selection          from the store reads the harness saw
    E  RAG appropriateness                   retrieval count: 0 unless KNOWLEDGE
    F  Qwen discipline                       no understanding call when confident; <= 1 call
    G  final answer correctness              a real answer, never the stage sentence
    H  conversation continuity               the two-turn "this stage" follow-up
    I  multilingual understanding            language detected, same frame
    J  no hallucination                      no foreign case values; grounded flag
    K  security                              another customer's case still refused
    L  latency                               per-request wall clock, P50 / P95
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Any

from evals.copilot.harness import CANARIES, MINE, TRACE, Harness
from evals.copilot.semantic_cases import CASES, FOLLOW_UP, UNSEEN

#: Intent -> the AUTHORITATIVE store reads that must have happened (the
#: harness records repository calls; a document answer must have read the
#: case's documents, a stage answer its stage record, and so on).
TOOLS = {
    "DOCUMENTS_REQUIRED": {"list_documents", "get_application"},
    "DOCUMENTS_MISSING": {"list_documents", "get_application"},
    "DOCUMENTS_PENDING": {"list_documents"}, "DOCUMENTS_UPLOADED": {"list_documents"},
    "DOCUMENT_VERIFICATION": {"list_documents"}, "PENDING_ITEMS": {"list_documents"},
    "NEXT_ACTION": {"list_documents"}, "READINESS": {"list_documents"},
    "APPLICATION_STAGE": {"get_case_stage"}, "APPLICATION_STATUS": {"get_application"},
    "FOS_KNOWLEDGE": set(),
}
STAGE_SENTENCE = ("is at the", "stage is", "currently at", "चरण में है", "stage mein hai")


def ask_fos(h: Harness, message: str, context: dict | None = None) -> dict[str, Any]:
    TRACE.reset()
    headers = {"Authorization": f"Bearer {h.token('owner')}"}
    body = {"applicant_id": h.ids("mine")["applicant_id"], "case_id": MINE,
            "action": "CUSTOM_QUERY", "message": message}
    if context:
        body["context"] = context
    started = time.perf_counter()
    response = h.http.post("/api/v1/fos/copilot", json=body, headers=headers)
    total = (time.perf_counter() - started) * 1000
    try:
        payload = response.json()
    except ValueError:
        payload = {"_raw": response.text}
    return {"status": response.status_code, "body": payload, "total_ms": total,
            "reads": list(TRACE.reads), "retrievals": TRACE.retrievals,
            "qwen_calls": TRACE.qwen_calls, "text": response.text}


def grade(case: dict[str, Any], out: dict[str, Any]) -> list[tuple[str, bool, str]]:
    body = out["body"]
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    understanding = result.get("understanding") or {}
    frame = understanding.get("frame") or {}
    intent = result.get("intent")
    checks: list[tuple[str, bool, str]] = []
    checks.append(("A.frame", frame.get("task") == case["task"] and
                   frame.get("object") == case["object"],
                   f"{frame.get('task')}/{frame.get('object')} != {case['task']}/{case['object']}"))
    if case.get("stage_ref") is not None:
        got = (frame.get("referents") or {}).get("stage")
        checks.append(("B.referents", got == case["stage_ref"],
                       f"stage referent {got} != {case['stage_ref']}"))
    checks.append(("C.route", intent == case["intent"], f"intent {intent} != {case['intent']}"))
    checks.append(("C.never_stage_answer", not (case["object"] == "DOCUMENTS"
                                                and intent == "APPLICATION_STAGE"),
                   "a documents question routed to APPLICATION_STAGE"))
    wanted = TOOLS.get(case["intent"], set())
    reads = set(out["reads"])
    checks.append(("D.tools", wanted <= reads if wanted else True,
                   f"reads {sorted(reads)} lack {sorted(wanted)}"))
    knowledge = case["intent"] == "FOS_KNOWLEDGE"
    checks.append(("E.rag", (out["retrievals"] > 0) if knowledge else out["retrievals"] == 0,
                   f"retrievals={out['retrievals']} for a {'knowledge' if knowledge else 'case'} question"))
    llm = understanding.get("llm") or {}
    checks.append(("F.qwen_understanding", not llm.get("consulted")
                   or understanding.get("decided_by") == "LLM",
                   f"understanding model call {llm} on a deterministic parse"))
    checks.append(("F.qwen_max_one", out["qwen_calls"] <= 1, f"{out['qwen_calls']} model calls"))
    answer = str(result.get("answer") or "")
    clarified = bool(result.get("clarification_required"))
    if knowledge:
        checks.append(("G.answer", bool(answer.strip()), "empty answer"))
    else:
        checks.append(("G.answer", bool(answer.strip()) and not clarified and
                       not any(s in answer.lower() for s in STAGE_SENTENCE),
                       f"answer={answer[:80]!r} clarified={clarified}"))
    checks.append(("I.language", frame.get("language") == case.get("language", "en"),
                   f"language {frame.get('language')} != {case.get('language', 'en')}"))
    text = out["text"]
    checks.append(("J.no_foreign_values", not any(c in text for c in CANARIES),
                   "another customer's value appeared"))
    checks.append(("L.latency", out["total_ms"] <= case.get("max_ms", 3000),
                   f"{out['total_ms']:.0f} ms"))
    return checks


def run(*, live: bool, report: str | None) -> int:
    h = Harness(live=live)
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    llm_calls = 0
    try:
        for case in CASES + UNSEEN:
            out = ask_fos(h, case["q"])
            checks = grade(case, out)
            body = out["body"]
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            understanding = result.get("understanding") or {}
            llm_calls += int(bool((understanding.get("llm") or {}).get("consulted")))
            latencies.append(out["total_ms"])
            passed = all(ok for _, ok, _ in checks)
            results.append({"id": case["id"], "q": case["q"], "unseen": case in UNSEEN,
                            "passed": passed, "ms": round(out["total_ms"], 1),
                            "frame": understanding.get("frame"),
                            "decided_by": understanding.get("decided_by"),
                            "referents": understanding.get("referents"),
                            "intent": result.get("intent"), "reads": out["reads"],
                            "retrievals": out["retrievals"], "qwen_calls": out["qwen_calls"],
                            "answer": str(result.get("answer") or "")[:160],
                            "failures": [(d, why) for d, ok, why in checks if not ok]})
            print(f"{'PASS' if passed else 'FAIL'}  {case['id']:<28} {result.get('intent'):<22} "
                  f"{(understanding.get('frame') or {}).get('task', '-'):<18} "
                  f"by={understanding.get('decided_by')} {out['total_ms']:6.0f} ms")
            for d, ok, why in checks:
                if not ok:
                    print(f"      - {d}: {why}")

        # H. the two-turn follow-up: "what stage am I in?" then "this stage"
        first = ask_fos(h, FOLLOW_UP["first"])
        body = first["body"]
        r1 = body.get("result") if isinstance(body.get("result"), dict) else body
        context = r1.get("context") or {}
        # THE CASE RECORD'S STAGE, as the first answer reported it.
        stage_now = ((r1.get("understanding") or {}).get("case_stage")
                     or context.get("last_stage"))
        second = ask_fos(h, FOLLOW_UP["second"], context=context)
        b2 = second["body"]
        r2 = b2.get("result") if isinstance(b2.get("result"), dict) else b2
        u2 = r2.get("understanding") or {}
        f2 = u2.get("frame") or {}
        ok_h = (r1.get("intent") == "APPLICATION_STAGE" and stage_now
                and f2.get("task") == "LIST_REQUIREMENTS" and f2.get("object") == "DOCUMENTS"
                and f2.get("stage") == stage_now and r2.get("intent") == "DOCUMENTS_REQUIRED")
        results.append({"id": "H.follow_up_this_stage", "q": FOLLOW_UP["second"],
                        "passed": bool(ok_h), "ms": round(second["total_ms"], 1),
                        "frame": f2, "resolved_stage": f2.get("stage"), "stage_now": stage_now,
                        "intent": r2.get("intent"), "failures": [] if ok_h else
                        [("H.continuity", f"stage_now={stage_now} frame={f2} intent={r2.get('intent')}")]})
        print(f"{'PASS' if ok_h else 'FAIL'}  H.follow_up_this_stage      stage={stage_now} -> "
              f"frame.stage={f2.get('stage')} intent={r2.get('intent')}")

        # M. bare words: clarified, and NOTHING runs for them
        from evals.copilot.semantic_cases import AMBIGUOUS, SHORT_FOLLOW_UPS

        for case in AMBIGUOUS:
            out = ask_fos(h, case["q"])
            body = out["body"]
            r = body.get("result") if isinstance(body.get("result"), dict) else body
            clar = r.get("clarification_required") or {}
            reads = set(out["reads"]) - {"get_application", "get_applicant", "has_access",
                                         "get_case_stage", "get_stage_transitions",
                                         "get_case_timeline"}
            ok = (clar.get("reason") == "AMBIGUOUS_SHORT_QUERY" and clar.get("head") == case["head"]
                  and len(clar.get("options") or []) >= 2 and not reads
                  and out["retrievals"] == 0 and out["qwen_calls"] == 0
                  and "mean" in str(r.get("answer") or "").lower())
            latencies.append(out["total_ms"])
            results.append({"id": case["id"], "q": case["q"], "passed": ok,
                            "ms": round(out["total_ms"], 1), "answer": str(r.get("answer"))[:120],
                            "reads": sorted(reads), "failures": [] if ok else
                            [("M.ambiguity", f"clarification={clar} reads={sorted(reads)} "
                                             f"retrievals={out['retrievals']} qwen={out['qwen_calls']}")]})
            print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<28} -> {str(r.get('answer'))[:70]}")

        for first, word, expected in SHORT_FOLLOW_UPS:
            a = ask_fos(h, first)
            ba = a["body"]
            ra = ba.get("result") if isinstance(ba.get("result"), dict) else ba
            b = ask_fos(h, word, context=ra.get("context") or {})
            bb = b["body"]
            rb = bb.get("result") if isinstance(bb.get("result"), dict) else bb
            ub = rb.get("understanding") or {}
            ok = (rb.get("intent") == expected and not rb.get("clarification_required")
                  and (ub.get("short_query") or {}).get("head"))
            latencies.append(b["total_ms"])
            cid = f"short_follow_up_{word}_after_{expected.lower()}"
            results.append({"id": cid, "q": f"{first} -> {word}", "passed": bool(ok),
                            "ms": round(b["total_ms"], 1), "intent": rb.get("intent"),
                            "short_query": ub.get("short_query"),
                            "failures": [] if ok else [("M.context", f"intent={rb.get('intent')} "
                                                                    f"short={ub.get('short_query')}")]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid:<44} {rb.get('intent')}")

        # N. CHATGPT-LIKE PHRASINGS, graded on meaning
        from evals.copilot.semantic_cases import CHAT, COMPOUND, CONTEXT_REFERENCES, GREETINGS

        for case in CHAT:
            out = ask_fos(h, case["q"])
            body = out["body"]
            r = body.get("result") if isinstance(body.get("result"), dict) else body
            u = r.get("understanding") or {}
            clar = r.get("clarification_required") or {}
            answer = str(r.get("answer") or "")
            llm_calls += int(bool((u.get("llm") or {}).get("consulted")))
            if case["clarify"]:
                ok = (clar.get("reason") == case["clarify"] and len(clar.get("options") or []) >= 2
                      and not set(out["reads"]) - {"has_access", "get_application",
                                                  "get_applicant", "get_case_stage"}
                      and out["retrievals"] == 0)
                why = f"clarification={clar} reads={out['reads']}"
            else:
                knowledge = r.get("intent") == "FOS_KNOWLEDGE"
                ok = (r.get("intent") in case["intents"] and not clar and bool(answer.strip())
                      and (knowledge or out["retrievals"] == 0)
                      and not (r.get("intent") not in ("APPLICATION_STAGE", "APPLICATION_STATUS",
                                                       "STAGE_PROCESS")
                               and any(s in answer.lower() for s in STAGE_SENTENCE)))
                why = (f"intent={r.get('intent')} not in {sorted(case['intents'])} "
                       f"clarified={bool(clar)} retrievals={out['retrievals']} answer={answer[:70]!r}")
            lang = (u.get("frame") or {}).get("language")
            ok = (ok and out["qwen_calls"] <= 1 and not any(c in out["text"] for c in CANARIES)
                  and out["total_ms"] <= case["max_ms"] and lang == case["language"])
            latencies.append(out["total_ms"])
            results.append({"id": case["id"], "q": case["q"], "unseen": True, "passed": bool(ok),
                            "ms": round(out["total_ms"], 1), "frame": u.get("frame"),
                            "decided_by": u.get("decided_by"), "intent": r.get("intent"),
                            "reads": out["reads"], "retrievals": out["retrievals"],
                            "qwen_calls": out["qwen_calls"], "answer": answer[:160],
                            "failures": [] if ok else [("N.meaning", why + f" lang={lang}")]})
            print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<28} {str(r.get('intent')):<22} "
                  f"{(u.get('frame') or {}).get('task', '-'):<18} by={u.get('decided_by')} "
                  f"{out['total_ms']:6.0f} ms")
            if not ok:
                print(f"      - N.meaning: {why} lang={lang}")

        # O. small talk: a reply from nothing
        for text_ in GREETINGS:
            out = ask_fos(h, text_)
            body = out["body"]
            r = body.get("result") if isinstance(body.get("result"), dict) else body
            ok = (r.get("category") == "CONVERSATION" and bool(str(r.get("answer") or "").strip())
                  and not out["reads"] and out["retrievals"] == 0 and out["qwen_calls"] == 0)
            latencies.append(out["total_ms"])
            cid = "greeting_" + "_".join(text_.split())
            results.append({"id": cid, "q": text_, "passed": ok, "ms": round(out["total_ms"], 1),
                            "reads": out["reads"], "answer": str(r.get("answer"))[:100],
                            "failures": [] if ok else
                            [("O.conversation", f"category={r.get('category')} reads={out['reads']} "
                                                f"retrievals={out['retrievals']} qwen={out['qwen_calls']}")]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid:<28} reads={out['reads']} -> {str(r.get('answer'))[:50]!r}")

        # P. a compound question: both halves, both records
        out = ask_fos(h, COMPOUND["q"])
        body = out["body"]
        r = body.get("result") if isinstance(body.get("result"), dict) else body
        comp = (r.get("understanding") or {}).get("compound") or {}
        ok = (comp.get("intents") == COMPOUND["intents"]
              and COMPOUND["reads"] <= set(out["reads"]) and out["retrievals"] == 0
              and len(str(r.get("answer") or "")) > 20)
        latencies.append(out["total_ms"])
        results.append({"id": "P.compound", "q": COMPOUND["q"], "passed": bool(ok),
                        "ms": round(out["total_ms"], 1), "compound": comp, "reads": out["reads"],
                        "answer": str(r.get("answer"))[:200],
                        "failures": [] if ok else [("P.compound", f"compound={comp} reads={out['reads']}")]})
        print(f"{'PASS' if ok else 'FAIL'}  P.compound                   {comp.get('intents')} reads={out['reads']}")

        # Q. context references after a real question
        for first, follow, accepted in CONTEXT_REFERENCES:
            a = ask_fos(h, first)
            ba = a["body"]
            ra = ba.get("result") if isinstance(ba.get("result"), dict) else ba
            b = ask_fos(h, follow, context=ra.get("context") or {})
            bb = b["body"]
            rb = bb.get("result") if isinstance(bb.get("result"), dict) else bb
            ok = rb.get("intent") in accepted and not rb.get("clarification_required")
            latencies.append(b["total_ms"])
            cid = "context_" + "_".join(follow.split()[:4])
            results.append({"id": cid, "q": f"{first} -> {follow}", "passed": bool(ok),
                            "ms": round(b["total_ms"], 1), "intent": rb.get("intent"),
                            "answer": str(rb.get("answer"))[:120],
                            "failures": [] if ok else
                            [("Q.context", f"intent={rb.get('intent')} clar={rb.get('clarification_required')}")]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid:<28} {rb.get('intent')}")

        # K. security: the other customer's case is still refused
        headers = {"Authorization": f"Bearer {h.token('owner')}"}
        refused = h.http.post("/api/v1/fos/copilot", json={
            "applicant_id": h.ids("theirs")["applicant_id"], "case_id": h.ids("theirs")["case_id"],
            "action": "CUSTOM_QUERY", "message": "What documents are mandatory for this stage?"},
            headers=headers)
        ok_k = refused.status_code == 403 and not any(c in refused.text for c in CANARIES)
        results.append({"id": "K.other_case_refused", "passed": ok_k, "ms": 0.0,
                        "status": refused.status_code, "failures": [] if ok_k else
                        [("K.security", f"status {refused.status_code}")]})
        print(f"{'PASS' if ok_k else 'FAIL'}  K.other_case_refused        {refused.status_code}")
    finally:
        h.stop()

    latencies.sort()
    lat = {"p50": round(statistics.median(latencies), 1),
           "p95": round(latencies[int(0.95 * (len(latencies) - 1))], 1),
           "max": round(latencies[-1], 1), "min": round(latencies[0], 1)} if latencies else {}
    passed = sum(1 for r in results if r["passed"])
    unseen = [r for r in results if r.get("unseen")]
    summary = {"passed": passed, "total": len(results), "latency_ms": lat,
               "qwen_understanding_calls": llm_calls,
               "questions": len(CASES) + len(UNSEEN) + len(CHAT),
               "qwen_call_pct": round(100 * llm_calls / max(1, len(CASES) + len(UNSEEN) + len(CHAT)), 1),
               "unseen_passed": sum(1 for r in unseen if r["passed"]), "unseen_total": len(unseen),
               "live": live, "first_request_ms": round(h.first_request_ms, 1)}
    print("\nSEMANTIC", json.dumps(summary))
    if report:
        from pathlib import Path

        Path(report).write_text(json.dumps({"summary": summary, "cases": results}, indent=2,
                                           ensure_ascii=False), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    sys.exit(run(live=args.live, report=args.report))
