"""
FOS LIVE 120 -- the chatbot as a field officer uses it, on a RUNNING server with every feature on (embeddings, Qwen,
guardrails, routing). Read-only: only questions are sent (evals/copilot/fos_live_120.yaml).

    python -m evals.copilot.fos_live_eval --base-url http://127.0.0.1:8021 [--subject local-developer]
                                          [--only A1,C2] [--report runs/fos_live.json]

Paced at 25 messages a minute (safety.py allows 30 per officer). Prints every failing turn with what came back, and
the pass rate by category and overall, with latency p50 / p95.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
SET = ROOT / "evals" / "copilot" / "fos_live_120.yaml"
MENU = "which of these did you mean"
BLOCK_INTENTS = {"ABUSIVE", "COOLDOWN", "ABUSIVE_INPUT"}


def _boundary_heads() -> tuple[list[str], list[str]]:
    """The guardrail's own reply openings (abuse_lexicon.yaml reply), read from config -- the published FOS reply
    carries only markdown / tts, so a boundary is recognised by the words the guardrail itself says."""
    import re

    cfg = yaml.safe_load((ROOT / "app" / "config" / "abuse_lexicon.yaml").read_text(encoding="utf-8")) or {}
    reply = cfg.get("reply") or {}

    def heads(keys):
        out = []
        for k in keys:
            for v in (reply.get(k) or {}).values():
                for text in (v if isinstance(v, list) else [v]):
                    head = re.split(r"[{(:]", str(text))[0].strip()[:24].lower()
                    if head:
                        out.append(head)
        return out
    return heads(("markdown", "markdown_unnamed")), heads(("cooldown",))


BLOCK_HEADS, COOLDOWN_HEADS = _boundary_heads()


def is_boundary(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(low.startswith(h) for h in BLOCK_HEADS + COOLDOWN_HEADS)


def is_cooldown(text: str) -> int:
    """Seconds left when the reply is the cooldown, else 0."""
    import re

    low = (text or "").lower()
    if any(h in low for h in COOLDOWN_HEADS):
        found = re.search(r"(\d+)", low.split(COOLDOWN_HEADS[0])[-1] if COOLDOWN_HEADS else low)
        return int(found.group(1)) if found else 60
    return 0


def grade(turn: dict, body: dict, markers: list[str]) -> list[str]:
    text = str(body.get("markdown") or body.get("answer") or "")
    low = text.lower()
    problems = []
    if turn.get("any") and not any(str(w).lower() in low for w in turn["any"]):
        problems.append(f"lacks any of {turn['any']}")
    for w in turn.get("all") or []:
        if str(w).lower() not in low:
            problems.append(f"lacks {w!r}")
    for w in turn.get("none") or []:
        if str(w).lower() in low:
            problems.append(f"contains {w!r}")
    intent = str(body.get("intent") or "")
    guard = (body.get("guardrail") or {}) if isinstance(body.get("guardrail"), dict) else {}
    blocked = intent in BLOCK_INTENTS or guard.get("action") == "BLOCKED" or is_boundary(text)
    if turn.get("blocked") is True and not blocked:
        problems.append("expected a guardrail boundary")
    if turn.get("blocked") is not True and blocked:
        problems.append(f"blocked ({intent})")
    if turn.get("no_case") and any(m.lower() in low for m in markers):
        problems.append("shows case data")
    if turn.get("not_menu") and MENU in low:
        problems.append("generic menu")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8021")
    ap.add_argument("--subject", default="local-developer")
    ap.add_argument("--only")
    ap.add_argument("--report")
    ap.add_argument("--pace", type=float, default=2.4, help="seconds between messages (25 a minute)")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from evals.copilot.golden_aw import token

    spec = yaml.safe_load(SET.read_text(encoding="utf-8"))
    case, markers = spec["case"], spec.get("case_data_markers") or []
    only = set(a.only.split(",")) if a.only else None
    client = httpx.Client(base_url=a.base_url, timeout=180,
                          headers={"Authorization": "Bearer " + token(a.subject)})
    results, latencies, last, cooldowns = [], [], 0.0, 0
    for conv in spec["conversations"]:
        if only and conv["id"] not in only:
            continue
        context = None
        chat_id = f"fos-live-{conv['id']}-{int(time.time())}"      # one chat per conversation, as the real UI sends
        for n, turn in enumerate(conv["turns"], start=1):
            wait = a.pace - (time.perf_counter() - last)
            if wait > 0:
                time.sleep(wait)
            payload = {"action": "CUSTOM_QUERY", "message": turn["q"], "chat_id": chat_id, **case}
            if context:
                payload["context"] = context
            for _attempt in range(3):
                last = time.perf_counter()
                r = client.post("/api/v1/fos/copilot", json=payload)
                ms = (time.perf_counter() - last) * 1000
                try:
                    body = r.json()
                except ValueError:
                    body = {"markdown": r.text}
                wait_s = is_cooldown(str(body.get("markdown") or body.get("answer") or ""))
                if wait_s and not turn.get("blocked"):
                    # THE ABUSE COOLDOWN of an earlier test turn (3 flagged messages in 10 minutes): the product
                    # working as designed -- an officer would wait, so the runner waits and asks again
                    cooldowns += 1
                    time.sleep(wait_s + 2)
                    continue
                break
            latencies.append(ms)
            context = body.get("context") or context
            problems = [f"HTTP {r.status_code}"] if r.status_code != 200 else grade(turn, body, markers)
            results.append({"id": f"{conv['id']}.{n}", "cat": conv["id"].rstrip("0123456789"), "q": turn["q"],
                            "ok": not problems, "problems": problems, "ms": round(ms),
                            "intent": body.get("intent"),
                            "answer": str(body.get("markdown") or body.get("answer") or "")[:300]})
            flag = "PASS" if not problems else "FAIL"
            print(f"{flag} {conv['id']}.{n} {ms:6.0f} ms {turn['q']!r}" + ("" if not problems else
                  f"\n      {problems}\n      -> {results[-1]['answer'][:220]!r}"), flush=True)
    passed = sum(r["ok"] for r in results)
    cats: dict[str, list[bool]] = {}
    for r in results:
        cats.setdefault(r["cat"], []).append(r["ok"])
    latencies.sort()
    summary = {"passed": passed, "total": len(results), "cooldown_waits": cooldowns, "accuracy": round(100 * passed / max(len(results), 1), 1),
               "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(cats.items())},
               "latency_ms": {"p50": round(statistics.median(latencies)) if latencies else 0,
                              "p95": round(latencies[int(0.95 * (len(latencies) - 1))]) if latencies else 0}}
    print("\nFOS_LIVE", json.dumps(summary, ensure_ascii=False))
    if a.report:
        Path(a.report).write_text(json.dumps({"summary": summary, "turns": results}, indent=1, ensure_ascii=False),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
