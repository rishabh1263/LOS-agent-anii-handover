"""
THE FOS-PLAN GOLDEN SET (evals/golden/*.yaml) against the real HTTP API, every plan feature on (as in production),
no model (fallback paths). Scores (FOS plan 9.7 / STEP 10a):

    accuracy        cases whose every expectation held
    wrong_answer    turns whose expected intent was not the one answered
    wrong_data      turns that said a forbidden value, missed a required one, or answered for the wrong case
    clarify_rate    turns answered with a clarifying question
    latency         p50 / p95 per turn (ms)

    python -m evals.golden.run [--report runs/golden/report.json] [--show]

`covered_by` cases (asserted by a pytest file instead) are counted as covered, not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
PLAN_FLAGS = ("COPILOT_CASE_WORKSPACE", "COPILOT_CASE_ACTIONS", "COPILOT_RESPONSE_STYLE", "COPILOT_VERIFY_DIAGNOSE",
              "COPILOT_DOCUMENT_ACTIONS", "COPILOT_TERMS_KNOWLEDGE", "COPILOT_GUARDRAIL_HARDENING",
              "COPILOT_LANGUAGE_LOCK", "COPILOT_KYC_TABLE", "COPILOT_READINESS_REPORT", "COPILOT_COUNT_ANSWERS",
              "COPILOT_HANDOFF_NOTE", "COPILOT_CASE_TIMELINE", "COPILOT_SNAPSHOT_QA",
              "COPILOT_PROFESSIONAL_FORMAT")
_HINGLISH = re.compile(r"\b(hai|hain|kripya|baaki|aapka|aapke|aapki|kijiye|kholiye|boliye|abhi|nahi|mein|jaana|"
                       r"karein|hua|kya)\b", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")


def load_cases() -> list[dict[str, Any]]:
    cases = []
    for path in sorted(HERE.glob("*.yaml")):
        for case in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("cases") or []:
            case["_file"] = path.name
            cases.append(case)
    return cases


class Runner:
    def __init__(self) -> None:
        for flag in PLAN_FLAGS:
            os.environ[f"EVAL_OVERRIDE_{flag}"] = "true"
        os.environ["EVAL_OVERRIDE_COPILOT_LLM_ROUTER"] = "false"
        from evals.copilot.harness import Harness

        self.h = Harness(live=False)
        self.headers = {"Authorization": f"Bearer {self.h.token('officer')}"}

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
        started = time.perf_counter()
        r = self.h.http.post(path, json=body, headers=self.headers)
        ms = (time.perf_counter() - started) * 1000
        try:
            return r.status_code, r.json(), ms
        except ValueError:
            return r.status_code, {}, ms

    def make_case(self, n: int, extra: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        status, body, _ = self.post("/api/v1/fos/applicants", {
            "applicant": {"full_name": f"Golden Person {n}", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                          "address": "12 MG Road, Pune"},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, **(extra or {})}})
        return status, body

    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        from app.agents.applicant.copilot.capabilities import safety

        safety.reset()
        setup = case.get("setup") or {}
        ids = []
        if "create_with" in setup:
            status, body = self.make_case(0, setup["create_with"])
            expected = (case.get("expect_http") or {})
            ok = status == expected.get("status") and (body.get("detail") or {}).get("error") == expected.get("error")
            return {"id": case["id"], "passed": ok, "turns": [], "problems": [] if ok else [f"http {status}"]}
        for n in range(int(setup.get("cases", 1))):
            _, body = self.make_case(n + 1, {"loan_amount": setup["loan_amount"]} if "loan_amount" in setup else None)
            ids.append(body.get("case_id"))
        self.post("/api/v1/fos/copilot", {"action": "EXIT_CASE"})
        # a case is opened unless the case says `open: 0` (an officer works inside a case)
        opened = int(setup.get("open", 1))
        if opened:
            self.post("/api/v1/fos/copilot", {"action": "OPEN_CASE", "case_id": ids[opened - 1]})
        universal = setup.get("endpoint") == "universal"
        ctx, turns, problems = None, [], []
        for turn in case.get("turns") or []:
            body: dict[str, Any] = {"message": turn["q"]}
            if not universal:
                body["action"] = "CUSTOM_QUERY"
            if ctx and not universal:
                body["context"] = ctx
            if setup.get("reply_language"):
                body["reply_language"] = setup["reply_language"]
            if setup.get("send_case_id"):
                body["case_id"] = ids[int(setup["send_case_id"]) - 1]
            status, reply, ms = self.post("/api/v1/copilot/query" if universal else "/api/v1/fos/copilot", body)
            ctx = reply.get("context") or ctx
            found = self.check(turn.get("expect") or {}, status, reply, ids)
            turns.append({"q": turn["q"], "intent": reply.get("intent"), "ms": round(ms, 1), "problems": found,
                          "clarify": bool(reply.get("clarification_required")),
                          "answer": str(reply.get("answer") or "")[:200]})
            problems += found
        return {"id": case["id"], "passed": not problems, "turns": turns, "problems": problems}

    @staticmethod
    def check(expect: dict[str, Any], status: int, reply: dict[str, Any], ids: list[str]) -> list[str]:
        out = []
        answer = str(reply.get("answer") or "")
        if status != 200:
            return [f"http:{status}"]
        if "intent" in expect and reply.get("intent") != expect["intent"]:
            out.append(f"WRONG_ANSWER:intent {reply.get('intent')} != {expect['intent']}")
        if "intent_not" in expect and reply.get("intent") == expect["intent_not"]:
            out.append(f"WRONG_ANSWER:intent is {expect['intent_not']}")
        if "contains" in expect and expect["contains"] not in answer:
            out.append(f"WRONG_DATA:missing {expect['contains']!r}")
        if "not_contains" in expect and expect["not_contains"] in answer:
            out.append(f"WRONG_DATA:said {expect['not_contains']!r}")
        if "case" in expect and ids[int(expect["case"]) - 1] not in answer:
            out.append(f"WRONG_DATA:not case {expect['case']}")
        if "clarify" in expect and (reply.get("clarification_required") or {}).get("reason") != expect["clarify"]:
            out.append("WRONG_ANSWER:no clarification " + str(expect["clarify"]))
        if expect.get("no_hinglish") and _HINGLISH.search(answer):
            out.append("WRONG_ANSWER:language lock")
        if expect.get("no_emoji") and _EMOJI.search(answer):
            out.append("WRONG_ANSWER:emoji")
        if "chips_not_contain" in expect and any(expect["chips_not_contain"] in str(s).lower()
                                                 for s in reply.get("suggested_questions") or []):
            out.append("WRONG_ANSWER:chip")
        return out


def score(results: list[dict[str, Any]], covered: int) -> dict[str, Any]:
    turns = [t for r in results for t in r["turns"]]
    ms = sorted(t["ms"] for t in turns) or [0.0]
    p95 = ms[min(len(ms) - 1, int(round(0.95 * (len(ms) - 1))))]
    return {"cases": len(results), "covered_by_tests": covered, "passed": sum(r["passed"] for r in results),
            "accuracy": round(100 * sum(r["passed"] for r in results) / max(1, len(results)), 1),
            "turns": len(turns),
            "wrong_answer_rate": round(100 * sum(any(p.startswith("WRONG_ANSWER") for p in t["problems"])
                                                 for t in turns) / max(1, len(turns)), 1),
            "wrong_data_rate": round(100 * sum(any(p.startswith("WRONG_DATA") for p in t["problems"])
                                               for t in turns) / max(1, len(turns)), 1),
            "clarify_rate": round(100 * sum(t["clarify"] for t in turns) / max(1, len(turns)), 1),
            "latency_ms": {"p50": round(statistics.median(ms), 1), "p95": round(p95, 1)}}


def run(report: str | None = None, show: bool = False, only: str | None = None) -> dict[str, Any]:
    cases = [c for c in load_cases() if not only or c["id"] == only]
    runnable = [c for c in cases if "covered_by" not in c or c.get("turns") or c.get("setup")]
    runnable = [c for c in runnable if not c.get("unit") and not c.get("expect_fields")]
    covered = len(cases) - len(runnable)
    runner = Runner()
    results = [runner.run_case(c) for c in runnable]
    summary = score(results, covered)
    for r in results:
        if show or not r["passed"]:
            print(("PASS " if r["passed"] else "FAIL ") + r["id"], "; ".join(r["problems"])[:200])
    print(json.dumps(summary, indent=2))
    if report:
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str),
                                encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()
    out = run(args.report, args.show, args.only)
    sys.exit(0 if out["passed"] == out["cases"] else 1)
