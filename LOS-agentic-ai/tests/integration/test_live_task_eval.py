"""
SEMANTIC TASK EVALUATION (Phase 2; live: real nomic embeddings + the meaning adapter + Qwen; opt-in LOS_LIVE_LLM_TESTS=1).

Every item of evals/copilot/task_heldout.yaml (and task_dev.yaml) is asked through the REAL API in its own chat, and
the interpretation the system acted on -- scope / operation / the list row referred to / the portfolio filter -- is
judged from WHAT IT DID (which layer answered, the reply's intent, the case it answered for, the count / list line),
the same way before and after a change. When the turn diagnostics carry the structured task (Phase 2), its fields are
checked too ("task" accuracy). Per-category and per-style accuracy and every miss go to runs/task_eval_<set>.json.

    LOS_LIVE_LLM_TESTS=1 TASK_EVAL_SET=heldout pytest -q tests/integration/test_live_task_eval.py -s
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_live_fos_scenarios import _ollama_up, _warm
from tests.integration.test_reupload_supersedes import client  # noqa: F401

pytestmark = [pytest.mark.live_llm,
              pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                                 reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")]

ROOT = Path(__file__).resolve().parents[2]
_ID = re.compile(r"CASE-[0-9A-F]{6,}")
# the engine's / general layer's APPROVED workflow and checklist answers (handbook-grounded, no case data)
KNOWLEDGE_INTENTS = ("FOS_KNOWLEDGE", "STAGE_PROCESS", "PROCESS_ANSWER", "PRODUCT_CHECKLIST")


def _items(name: str) -> list[dict]:
    return yaml.safe_load((ROOT / "evals" / "copilot" / f"task_{name}.yaml").read_text(encoding="utf-8"))["items"]


def test_heldout_sentences_are_not_training_examples():
    """No held-out sentence is a catalogue example / reviewed paraphrase / dev sentence (generalisation, not recall)."""
    norm = lambda s: " ".join(re.sub(r"[^\w\s]", " ", str(s).lower()).split())          # noqa: E731
    cat = yaml.safe_load((ROOT / "app/config/intent_catalogue.yaml").read_text(encoding="utf-8"))
    para = yaml.safe_load((ROOT / "app/config/intent_paraphrases.yaml").read_text(encoding="utf-8")) or {}
    bank = {norm(e) for s in cat["intents"].values() for e in s.get("examples") or []}
    bank |= {norm(e) for items in (para.get("paraphrases") or {}).values() for e in items or []}
    bank |= {norm(i["q"]) for i in _items("dev")}
    bank |= {norm(i["q"]) for i in _items("dev2")}
    # sets A, B and C are RETIRED (looked at; A's sentences were reused as dev2 by the 2026-10-12 directive): the
    # live held-out set D must not repeat any of them either
    for retired in ("heldout", "heldout_b", "heldout_c"):
        bank |= {norm(i["q"]) for i in _items(retired)}
    clash = [i["id"] for i in _items("heldout_d") if norm(i["q"]) in bank]
    assert not clash, clash


def observed(body: dict, listed: list[str], ctx_ids: dict) -> dict:
    """The interpretation the system ACTED on, from its reply (same rules before and after Phase 2)."""
    d = body.get("diagnostics") or {}
    md = body.get("markdown") or ""
    layer, intent = str(d.get("layer") or ""), str(d.get("intent") or "").upper()
    ids = list(dict.fromkeys(_ID.findall(md)))
    out: dict = {"layer": layer, "intent": intent}
    if ctx_ids["foreign"] in md:
        out.update(scope="LEAK", op="LEAK")
    elif intent in ("GREETING",) or (layer.startswith("greeting") and md[:5] in ("Hello", "Hi th", "Hi! ", "Hi, ")):
        out.update(scope="conversation", op="greet")
    elif intent in ("GOODBYE", "FAREWELL"):
        out.update(scope="conversation", op="farewell")
    elif intent == "NEGATION" or (intent == "ACKNOWLEDGEMENT" and d.get("outcome") == "NEGATION"):
        out.update(scope="conversation", op="negate")
    elif intent in ("THANKS", "ACKNOWLEDGEMENT"):
        out.update(scope="conversation", op="ack")
    elif "Which case is this about" in md or intent in ("WHICH_CASE", "CASE_SELECTION"):
        out.update(scope="one_case", op="which_case")
    elif intent in KNOWLEDGE_INTENTS:
        out.update(scope="knowledge", op="explain")          # the approved workflow / checklist answer
    elif layer.startswith("case_insights") or intent == "CASE_INSIGHT":
        op = ("count" if re.search(r"\b\d+ of your \d+ cases\b", md) else
              "compare" if "side by side" in md else "rank")
        out.update(scope="portfolio", op=op, ids=ids)
    elif intent.startswith("CASE_LIST") or (layer == "workspace" and len(ids) >= 2 and not md.lstrip().startswith("CASE-")):
        # a PAGE is the rows after a list already shown (or "that is the whole list" said ABOUT a shown list)
        # ...never a list NARROWED by a filter ("3 of your cases match (documents pending)")
        filtered = bool(re.search(r"(?i)cases match \(", md))
        paged = bool(listed) and not filtered and bool(re.search(r"(?i)whole list|no more|that's all|that is all", md)
                                                       or (ids and ids[0] not in listed[:1]))
        out.update(scope="portfolio", op="page" if paged else "list", ids=ids)
    elif (md.lstrip().startswith("CASE-") or (d.get("case_in_scope") and layer in ("engine", "workspace"))) \
            and intent not in ("WHICH_ACTION", "NOT_IN_SCOPE", "CREDIT_DECISION", "GUARDRAIL_BLOCKED"):
        # an answer FOR the case in scope (a later turn in an open case carries no CASE- header)
        out.update(scope="one_case", op="retrieve", case=ids[0] if ids else None)
    elif intent in ("WHICH_ACTION", "CLARIFY_ACTION", "AMBIGUOUS_ACTION"):
        out.update(scope="none", op="clarify")
    elif intent in ("NOT_IN_SCOPE", "OUT_OF_SCOPE", "CREDIT_DECISION", "DECISION_DECLINED", "ACCESS_DENIED",
                    "OTHER_PEOPLE", "OTHER_PEOPLES_CASES") or "BLOCK" in intent or "DECLIN" in intent:
        out.update(scope="none", op="refuse")
    elif layer == "general" or (d.get("retrieval") or {}).get("sources") or "not configured" in md:
        out.update(scope="knowledge", op="abstain" if "don't have an answer" in md else "explain")
    elif re.search(r"(?i)can only help with|only your own cases|can't (approve|show)|cannot (approve|show)", md):
        out.update(scope="none", op="refuse")
    else:
        out.update(scope="?", op="?")
    out["task"] = d.get("task")
    return out


def judge(item: dict, got: dict, listed: list[str], filter_names: dict) -> list[str]:
    exp, misses = item["expect"], []
    if got["scope"] != exp["scope"]:
        misses.append(f"scope {got['scope']} != {exp['scope']}")
    if got["op"] != exp["op"]:
        misses.append(f"op {got['op']} != {exp['op']}")
    rows = item.get("rows")
    if rows and listed:
        want = [listed[r - 1] for r in rows if r - 1 < len(listed)]
        have = got.get("ids") or ([got.get("case")] if got.get("case") else [])
        if not set(want) <= set(have):
            misses.append(f"rows {rows} not answered")
    if exp.get("filter") and got["scope"] == "portfolio":
        # the reply expands acronyms -- "KYC (Know Your Customer) issue": drop the expansions only
        expansion = r"\s*\([A-Z][a-z][^()]*\)"
        plain = re.sub(expansion, "", got.get("_md", "")).replace("*", "").lower()
        if re.sub(expansion, "", filter_names.get(exp["filter"], exp["filter"])).lower() not in plain:
            misses.append(f"filter {exp['filter']} not applied")
    return misses


def judge_task(item: dict, task: dict | None) -> list[str]:
    """The STRUCTURED task (diagnostics.task) against the expectation: scope, operation, reference, filter."""
    if not task:
        return ["no structured task"]
    exp, misses = item["expect"], []
    for key in ("scope", "op"):
        field = "operation" if key == "op" else key
        want = "explain" if exp[key] == "abstain" else exp[key]     # abstaining is the OUTCOME of an explain task
        if task.get(field) != want:
            misses.append(f"task.{field} {task.get(field)} != {exp[key]}")
    if exp.get("ref") and (task.get("references") or {}).get("kind") != exp["ref"]:
        misses.append(f"task.references {task.get('references')} != {exp['ref']}")
    if exp.get("filter") and exp["filter"] not in (task.get("filters") or []):
        misses.append(f"task.filters {task.get('filters')} lacks {exp['filter']}")
    return misses


@pytest.fixture
def live(client, other, prod, monkeypatch):
    from tests import conftest

    for flag in conftest._PHASE3_FLAGS:
        if flag != "LOS_LOGIN_SELF_GRANT_LEGACY":
            monkeypatch.setenv(flag, "true")
    for name in ("LOS_STAGE_GATE_IN_SERVICE", "LOS_FOS_CPA_KYC_RULE", "COPILOT_LLM_ROUTER", "COPILOT_MEANING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    from app.agents.applicant.copilot.capabilities import safety
    from app.agents.applicant.copilot.semantics import meaning

    real = safety._num
    monkeypatch.setattr(safety, "_num", lambda k, d: 10_000 if k == "rate_limit_per_minute" else real(k, d))
    _warm(meaning)
    mine = [make_case(client, n)[1] for n in ("Task Eval Arjun", "Task Eval Bela", "Task Eval Chetan")]
    foreign = make_case(other, "Task Eval Foreign")[1]
    return {"mine": mine, "foreign": foreign}


def test_task_eval(client, live):
    from app.agents.applicant.copilot.capabilities import case_list

    which = os.getenv("TASK_EVAL_SET", "heldout")
    items = _items(which)
    only = {s for s in os.getenv("TASK_EVAL_ONLY", "").split(",") if s}
    filter_names = {k: case_list._name_of("filter_names", k, "en") for k in (case_list.cfg().get("filters") or {})}
    stamp = int(time.time())

    def say(text, chat):
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                      "chat_id": chat})
        assert r.status_code == 200, (text, r.status_code, r.text[:300])
        return r.json()

    results, latencies = [], []
    for item in items:
        if only and item["id"] not in only:
            continue
        chat = f"te-{stamp}-{item['id']}"
        listed: list[str] = []
        if item["ctx"] == "open":
            say(f"open {live['mine'][0]}", chat)
        elif item["ctx"] == "listed":
            listed = list(dict.fromkeys(_ID.findall(say("show my cases", chat)["markdown"])))
        for turn in item.get("before") or []:
            shown = list(dict.fromkeys(_ID.findall(say(turn, chat)["markdown"])))
            if listed and len(shown) >= 2:
                listed = shown                     # a list shown by a before-turn is the list on screen now
        t = time.perf_counter()
        body = say(item["q"], chat)
        latencies.append(time.perf_counter() - t)
        got = observed(body, listed, live)
        got["_md"] = body.get("markdown") or ""
        misses = judge(item, got, listed, filter_names)
        task_misses = judge_task(item, got.get("task"))
        results.append({"id": item["id"], "cat": item["cat"], "style": item.get("style"), "q": item["q"],
                        "expect": item["expect"], "got": {k: got.get(k) for k in ("scope", "op", "layer", "intent")},
                        "task": got.get("task"), "ok": not misses, "misses": misses,
                        "task_ok": not task_misses, "task_misses": task_misses,
                        "reply": got["_md"][:220], "seconds": round(latencies[-1], 2),
                        "tools": list((body.get("diagnostics") or {}).get("tools") or [])})
        if os.getenv("TASK_EVAL_QUIET") != "1":       # quiet: a fresh held-out set's totals only
            print(f"{'OK ' if not misses else 'XX '}{'T ' if not task_misses else 't '}{item['id']:4} {item['q'][:50]!r:52} "
                  f"-> {got['scope']}/{got['op']} [{got['layer']}/{got['intent']}] {'; '.join(misses + task_misses)}")

    def table(key):
        agg = defaultdict(lambda: [0, 0, 0])
        for r in results:
            agg[r[key]][0] += 1
            agg[r[key]][1] += r["ok"]
            agg[r[key]][2] += r["task_ok"]
        return {k: {"n": n, "behaviour": ok, "task": tk} for k, (n, ok, tk) in sorted(agg.items())}

    lat = sorted(latencies) or [0.0]
    report = {"set": which, "n": len(results), "behaviour_ok": sum(r["ok"] for r in results),
              "task_ok": sum(r["task_ok"] for r in results), "by_category": table("cat"), "by_style": table("style"),
              "latency": {"p50": round(lat[len(lat) // 2], 2), "p95": round(lat[int(len(lat) * 0.95) - 1], 2),
                          "max": round(lat[-1], 2)},
              "misses": [r for r in results if not r["ok"] or not r["task_ok"]], "all": results,
              "leaks": [r["id"] for r in results if r["got"]["scope"] == "LEAK"],
              # tool calls per turn (diagnostics.tools): social / knowledge turns should read no case data
              "tools": {"total": sum(len(r["tools"]) for r in results),
                        "max_per_turn": max((len(r["tools"]) for r in results), default=0),
                        "social_turns_with_tools": [r["id"] for r in results if r["tools"]
                                                    and r["expect"]["scope"] == "conversation"]}}
    tag = os.getenv("TASK_EVAL_TAG", "run")
    out = ROOT / "runs" / f"task_eval_{which}_{tag}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{which}: behaviour {report['behaviour_ok']}/{report['n']}, task {report['task_ok']}/{report['n']}, "
          f"latency {report['latency']}, tools {report['tools']} -> {out}")
    for k, v in report["by_category"].items():
        print(f"  {k:16} n={v['n']:3} behaviour={v['behaviour']:3} task={v['task']:3}")
    assert not report["leaks"], report["leaks"]                     # applicant isolation: never another officer's case
