"""
THE SUMMARY OF A VALIDATION, IN POINTS -- in a fixed order:

    1. red flags and critical mismatches     3. agency report vs the documents     5. the next action
    2. amber items and missing evidence      4. the checks that passed

    rules   written from the rule results (wording ai_summary.lines) -- always there, at once
    ai      the local model (Ollama, app/llm/config.py) rewrites THOSE points in plain words, in the background

THE MODEL NEVER DECIDES. It gets the points with every person's name masked; its answer is checked line by line: the
same number of points in the same order, no figure that its own point does not hold, no banned word ("approved",
"clear title" ...), and a point that is not a pass never says "passed". Refused, slow, or the model down -> the
rules-written points stand (`source` says which).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
from typing import Any

from app.agents.credit.legal_verification.config import say, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import engine

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()                 # one model call at a time (a CPU serves one well, two badly)
_RUNNING: set[str] = set()


def enabled() -> bool:
    override = (os.getenv("LEGAL_VERIFICATION_AI_SUMMARY") or "").strip().lower()
    if override:
        return override in ("1", "on", "true", "yes")
    return bool(section("ai_summary").get("enabled", False))


def points(rules: list[dict[str, Any]], overview: dict[str, Any]) -> list[str]:
    """The rules-written points, in the configured order."""
    body = [r for r in rules if r["id"] != "R10"]
    line = lambda key, **v: say(f"ai_summary.lines.{key}", **v)  # noqa: E731
    out = []
    fails = [r for r in engine.ordered(body) if r["status"] == engine.FAIL and r["id"] != "R9"]
    for r in fails:
        out.append(line("critical" if r.get("severity") == "CRITICAL" else "fail", name=r["name"], reason=r["reason"]))
    for r in (r for r in engine.ordered(body) if r["status"] == engine.REVIEW and r["id"] != "R9"):
        out.append(line("review", name=r["name"], reason=r["reason"]))
    r9 = next((r for r in body if r["id"] == "R9"), None)
    if r9 and r9["status"] != engine.PASS:
        out.append(line("conflict", reason=r9["reason"]))
    passed = [r["name"] for r in body if r["status"] == engine.PASS]
    if passed:
        out.append(line("passed", names=", ".join(passed)))
    if overview.get("next_action_text"):
        out.append(line("next", action=overview["next_action_text"]))
    return out


# ---------------------------------------------------------------------------------------------------- the model
def _names(rules: list[dict[str, Any]]) -> list[str]:
    """Every person's name a point may carry (owners, the chain's parties) -- masked before the model reads."""
    found = set()
    for r in rules:
        if r["id"] in ("R1", "R4", "R9"):
            for c in r["comparisons"]:
                for v in c["values"].values():
                    for part in re.split(r"[,:→]| - |\d{4}-\d{2}-\d{2}", str(v or "")):
                        if len(part.strip()) > 3 and not part.strip().isdigit():
                            found.add(part.strip())
    return sorted(found, key=len, reverse=True)


MASK = "[name]"


def _masked(text: str, names: list[str]) -> tuple[str, list[str]]:
    """The point with every name replaced by MASK, and the names in the order they stood."""
    if not names:
        return text, []
    pattern = re.compile("|".join(re.escape(n) for n in names), re.I)
    order = [m.group(0) for m in pattern.finditer(text)]
    return pattern.sub(MASK, text), order


def _unmasked(text: str, order: list[str]) -> str | None:
    """The model's point with the names put back, in order -- None when it moved, dropped or added a name."""
    if text.count(MASK) != len(order):
        return None
    for name in order:
        text = text.replace(MASK, name, 1)
    return text


def accepted(source: list[str], answer: list[str]) -> bool:
    if len(answer) != len(source):
        return False
    banned = [str(w).lower() for w in words("ai_summary.banned_words")]
    passed_word = say("ai_summary.lines.passed", names="").split(":")[0].lower()
    for src, out in zip(source, answer):
        low = out.lower()
        if not out.strip() or any(re.search(rf"\b{re.escape(b)}\b", low) for b in banned):
            return False
        if not set(re.findall(r"\d+", out)) <= set(re.findall(r"\d+", src)):
            return False                                     # a number its own point does not hold
        if not src.lower().startswith(passed_word) and re.search(r"\bpass(ed|es)?\b|\bno issues?\b", low):
            return False                                     # "passed" about something that did not pass
    return True


def ask(lines: list[str]) -> list[str]:
    from app.llm.config import ollama_host, ollama_model

    spec = section("ai_summary")
    content = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    body = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": spec.get("keep_alive", "30m"),
            "messages": [{"role": "system", "content": str(words("ai_summary.instructions")).strip()},
                         {"role": "user", "content": content}],
            "options": {"temperature": float(spec.get("temperature", 0.1)), "num_predict": int(spec.get("num_predict", 260)),
                        "num_ctx": int(spec.get("num_ctx", 2048))}}
    if str(body["model"]).startswith("qwen3"):
        body["think"] = False
    request = urllib.request.Request(f"{ollama_host().rstrip('/')}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=float(spec.get("timeout_seconds", 25))) as response:
        text = str((json.loads(response.read()).get("message") or {}).get("content") or "")
    return [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", l).strip() for l in text.splitlines() if l.strip()]


def _store(run_id: str, key: str, text: str | None, source: str) -> None:
    with database().tx() as conn:
        conn.execute("INSERT INTO legal_verification_summaries (run_id, facts_hash, text, source) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT (run_id) DO UPDATE SET text = EXCLUDED.text, source = EXCLUDED.source",
                     (run_id, key, text, source))


def _write(run_id: str) -> None:
    try:
        with database().tx() as conn:
            run = conn.execute("SELECT * FROM legal_verification_runs WHERE run_id = %s", (run_id,)).fetchone()
            reuse = conn.execute("SELECT text FROM legal_verification_summaries WHERE facts_hash = %s AND source = 'ai' "
                                 "LIMIT 1", (run["facts_hash"],)).fetchone() if run else None
        if run is None:
            return
        if reuse:
            _store(run_id, run["facts_hash"], reuse["text"], "ai")
            return
        rules = list(run["rules"])
        names = _names(rules)
        masked = [_masked(p, names) for p in points(rules, run["overview"])]
        source = [m[0] for m in masked]
        with _LOCK:
            answer = ask(source)
        restored = [_unmasked(a, m[1]) for a, m in zip(answer, masked)] if len(answer) == len(masked) else []
        if accepted(source, answer) and restored and all(r is not None for r in restored):
            _store(run_id, run["facts_hash"], "\n".join(r for r in restored if r), "ai")
        else:
            _store(run_id, run["facts_hash"], None, "refused")
            logger.info("legal verification summary refused by the check (%s)", run_id)
    except Exception as exc:  # noqa: BLE001 - the model down / slow: the rules-written points stand
        logger.warning("legal verification summary: model unavailable (%s)", type(exc).__name__)
        try:
            _store(run_id, "", None, "unavailable")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _RUNNING.discard(run_id)


def precompute(run_id: str) -> None:
    if not enabled() or run_id in _RUNNING:
        return
    _RUNNING.add(run_id)
    threading.Thread(target=_write, args=(run_id,), name="legal-verification-summary", daemon=True).start()


def of_run(run: dict[str, Any]) -> dict[str, Any]:
    """{"points", "source": ai|rules, "pending", "label", "note"} for a run."""
    with database().tx() as conn:
        stored = conn.execute("SELECT text, source FROM legal_verification_summaries WHERE run_id = %s",
                              (run["run_id"],)).fetchone()
    if stored and stored["source"] == "ai" and stored["text"]:
        return {"points": stored["text"].split("\n"), "source": "ai", "pending": False,
                "label": say("ai_summary.labels.ai"), "note": say("ai_summary.labels.note_ai")}
    pending = enabled() and stored is None
    if pending:
        precompute(run["run_id"])
    return {"points": points(list(run["rules"]), run["overview"]), "source": "rules", "pending": pending,
            "label": say("ai_summary.labels.rules"), "note": say("ai_summary.labels.note_rules")}


# EVERY SUMMARY RETURNED AS NUMBERED POINTS (2026-11-02; app/core/summary_points.py, config summary_format.yaml):
# how it is made and checked above is unchanged -- only what is returned
from app.core import summary_points as _points  # noqa: E402

of_run = _points.points_field(of_run)
