"""
THE SUMMARY IN EASY WORDS for the case screen and the report.

    checklist   all eight rules, each in one easy sentence by its status (wording `plain`) -- always complete
    text        the local model (Qwen via Ollama, app/llm/config.py) explains the run in a few plain sentences

IT EXPLAINS, IT NEVER DECIDES. The model sees only the run's own results, grouped (passed / problems / needs a person /
not checked) so it cannot call an open item fine -- the account number is never in them. Its text is CHECKED before
it is shown: every figure must be one of the facts' figures, no banned word (approved / rejected / fraud ...), no
field name or rule code, at most `max_chars`. Refused, slow, or the model down -> the rules-written summary, and
`source` says "rules".

LATENCY (config ai_summary; CPU Qwen 4B takes ~8-25 s per summary):
    precompute     a run that finishes (process / finalize / seed) is queued at once; ONE background worker writes
                   the summaries one by one, so the case usually opens with its summary ready
    compact facts  short easy lines, capped lists, a small context window: prompt time is per token
    reuse          the same facts (another run / case with the same results) -> the stored text, no model call
    warm           a tiny prime call at server start loads the model before the first real summary
    never waits    the screen gets the rules-written summary at once with `pending: true` and asks again
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any

from app.agents.credit.bank_verification.config import cfg, say, section, words
from app.agents.credit.bank_verification.repository import Repository

logger = logging.getLogger(__name__)

_QUEUE: "queue.Queue[tuple[Path, str, dict[str, Any], str]]" = queue.Queue()
_QUEUED: set[str] = set()                   # runs queued or being written now: one model call per run
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_NUMBERS = ["two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def enabled() -> bool:
    override = (os.getenv("BANK_VERIFICATION_AI_SUMMARY") or "").strip().lower()
    if override:
        return override in ("1", "on", "true", "yes")
    return bool(section("ai_summary").get("enabled", False))


# ---- what is said ----------------------------------------------------------------------------------------------------

def checklist(run: dict[str, Any]) -> list[dict[str, str]]:
    """All eight rules in easy words (wording plain.rules.<id>.<status>)."""
    labels = words("plain.status")
    return [{"rule_id": r["rule_id"], "name": say(f"plain.rules.{r['rule_id']}.name"), "status": r["status"],
             "status_label": str(labels[r["status"]]), "text": say(f"plain.rules.{r['rule_id']}.{r['status']}")}
            for r in run["rules"]]


def _in_sentence(label: str) -> str:
    """A label inside a sentence: "Statement" -> "statement", but "AA data" stays "AA data" (an acronym keeps its case)."""
    return label if len(label) > 1 and label[1].isupper() else label[:1].lower() + label[1:]


def differences(rule: dict[str, Any], cap: int) -> list[str]:
    """What exactly differs in one check, with the values: "Transaction 3: 05-09-2026 / ₹12,500.00 / Credit on the
    form, 05-09-2026 / ₹12,500.00 / Debit on the statement (the debit / credit differs)"."""
    out = []
    for c in rule.get("comparisons") or []:
        if c.get("match") is False:
            out.append(say("ai_summary.difference", label=c["left_label"], left=c.get("left") or "—",
                           right_label=_in_sentence(str(c["right_label"])), right=c.get("right") or "—",
                           note=f" ({c['note']})" if c.get("note") else ""))
    return out[:cap]


def open_items(rule: dict[str, Any], cap: int) -> list[str]:
    """What could not be confirmed in one check (its missing evidence, or the unconfirmed comparisons)."""
    items = list(rule.get("missing_evidence") or [])
    return items[:cap]


def facts(case: dict[str, Any], run: dict[str, Any], decision: dict[str, Any] | None) -> dict[str, Any]:
    """What the model sees: the case in one line, then every check grouped, each problem WITH ITS VALUES (CPU prompt
    time is per token, so compact). No account number beyond what the checks already show masked."""
    cap = int(section("ai_summary").get("max_items", 4))
    items = checklist(run)[:-1]
    by_id = {r["rule_id"]: r for r in run["rules"]}
    statement = (run.get("sources") or {}).get("statement") or {}
    file = (run.get("sources") or {}).get("statement_file") or {}
    out = {"applicant": case["applicant_name"],
           "bank": ((statement.get("details") or {}).get("bank_name")
                    or ((run.get("form") or {}).get("bank_name"))),
           "source": say(f"ai_summary.source.{run['mode']}"),
           "statement": say(f"ai_summary.statement.{file.get('extraction_status') or 'NONE'}"),
           # the exact counts, so the model says "4 checks found a problem", never "one" for four
           "counts": {"checks_run": sum(i["status"] != "NOT_APPLICABLE" for i in items),
                      "passed": sum(i["status"] == "PASS" for i in items),
                      "problems_found": sum(i["status"] == "FAIL" for i in items),
                      "needs_a_person": sum(i["status"] == "REVIEW_REQUIRED" for i in items)},
           "passed": [i["name"] for i in items if i["status"] == "PASS"],      # anchors the model: never say
           "problems_found": [{"check": i["name"], "what_differs": differences(by_id[i["rule_id"]], cap) or [i["text"]]}
                              for i in items if i["status"] == "FAIL"],
           "needs_a_person": [{"check": i["name"], "why": i["text"], "missing": open_items(by_id[i["rule_id"]], 2)}
                              for i in items if i["status"] == "REVIEW_REQUIRED"]}
    # (no next step: the screen shows it, and every token is CPU time)
    if decision:
        out["agency_decision"] = decision["final_status"]
        out["agency_blocked_reasons"] = (decision["outcome"].get("blocked_reasons") or [])[:cap]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def facts_hash(shown: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(shown, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fallback(case: dict[str, Any], run: dict[str, Any], decision: dict[str, Any] | None) -> str:
    """The same summary from the rules alone: headline, what is fine, what needs attention, next step."""
    from app.agents.credit.bank_verification.services.verification_service import FINAL

    shown = facts(case, run, decision)
    counts = shown["counts"]
    parts = [say(f"ai_summary.fallback.{run['overall_status']}", applicant=case["applicant_name"]),
             say("ai_summary.fallback.counted", passed=counts["passed"],
                 total=counts["checks_run"])]
    for problem in shown.get("problems_found") or []:          # every problem WITH its values
        parts.append(say("ai_summary.fallback.problem", check=problem["check"], what="; ".join(problem["what_differs"])))
    open_ = [f"{p['check'].lower()} ({p['why'].rstrip('.').lower()})" for p in shown.get("needs_a_person") or []]
    if open_:
        parts.append(say("ai_summary.fallback.open", items="; ".join(open_)))
    if decision and decision["final_status"] in FINAL:
        parts.append(say("ai_summary.fallback.final", status=decision["final_status"].lower()))
    else:
        parts.append(say("ai_summary.fallback.next",
                         action=(cfg().get("actions") or {}).get(run["recommended_action"], run["recommended_action"])))
    return " ".join(parts)


# ---- the check -------------------------------------------------------------------------------------------------------

def accepted(shown: dict[str, Any], text: str) -> bool:
    """May the model's text be shown? Figures only from the facts, no banned word, no field name, not too long."""
    from app.agents.eligibility.output.summary import _COUNT_WORDS, _digits

    text = (text or "").strip()
    if not text or len(text) > int(section("ai_summary").get("max_chars", 1000)):
        return False
    if re.search(r"[a-z]_[a-z]", text) or re.search(r"\bR[1-8]\b", text):
        return False
    if not _digits(text) <= _digits(json.dumps(shown, ensure_ascii=False, default=str)):
        return False
    low = text.lower()
    if set(re.findall(r"[a-z]+", low)) & (_COUNT_WORDS - {"both"}):
        return False                                   # a count in words is a figure the digit check cannot see
    return not any(re.search(rf"\b{re.escape(str(w).lower())}\b", low) for w in words("ai_summary.banned_words"))


def _clean(text: str) -> str:
    from app.agents.eligibility.output.summary import _whole_sentences

    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"\s*(?:--|—|–)\s*", ", ", text)
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)        # a list the model wrote anyway
    # a count in words becomes digits, so the figure check sees it ("last three transactions" -> "last 3 ...":
    # kept when the facts hold 3, refused when they do not); "one" is left alone ("one item is still open")
    text = re.sub(r"\b(two|three|four|five|six|seven|eight|nine|ten)\b",
                  lambda m: str(_NUMBERS.index(m.group(1).lower()) + 2), text, flags=re.I)
    return _whole_sentences(re.sub(r"\s+", " ", text))


# ---- the model -------------------------------------------------------------------------------------------------------

def _post(messages: list[dict[str, str]], num_predict: int, sentences: int) -> str:
    """The model's reply, streamed and stopped once `sentences` sentences are complete."""
    from app.llm.config import ollama_host, ollama_model

    spec = section("ai_summary")
    body = {"model": str(spec.get("model") or ollama_model()), "stream": True, "messages": messages,
            "keep_alive": str(spec.get("keep_alive") or "30m"),
            "options": {"temperature": float(spec.get("temperature", 0.2)), "num_predict": num_predict,
                        "num_ctx": int(spec.get("num_ctx", 2048))}}
    request = urllib.request.Request(f"{ollama_host().rstrip('/')}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    text, ended = "", 0
    with urllib.request.urlopen(request, timeout=float(spec.get("model_timeout_seconds", 90))) as response:
        for raw in response:
            if not raw.strip():
                continue
            chunk = json.loads(raw)
            delta = str((chunk.get("message") or {}).get("content") or "")
            if text.rstrip()[-1:] in (".", "!", "?") and (delta[:1].isspace() or not delta):
                ended += 1
                if ended >= sentences:
                    break
            text += delta
            if chunk.get("done"):
                break
    return text


def ask(shown: dict[str, Any]) -> str:
    spec = section("ai_summary")
    messages = [{"role": "system", "content": say("ai_summary.instructions").strip()},
                {"role": "user", "content": json.dumps(shown, ensure_ascii=False, separators=(",", ":"))}]
    return _clean(_post(messages, int(spec.get("num_predict", 200)), int(spec.get("sentences", 4))))


def warm() -> None:
    """Load the model now (a one-token call with the same system prompt, so its prefix is cached too)."""
    if not (enabled() and section("ai_summary").get("warm_on_start", True)):
        return

    def run() -> None:
        try:
            _post([{"role": "system", "content": say("ai_summary.instructions").strip()},
                   {"role": "user", "content": "{}"}], 1, 1)
            logger.info("bank verification summary: model warm")
        except Exception as exc:  # noqa: BLE001 - warming is a convenience
            logger.warning("bank verification summary: warm-up failed (%s)", type(exc).__name__)

    threading.Thread(target=run, daemon=True).start()


# ---- the background writer -------------------------------------------------------------------------------------------

def _work() -> None:
    while True:
        root, run_id, shown, key = _QUEUE.get()
        try:
            repo = Repository(root)
            reuse = repo.summary_by_hash(key)
            if reuse:
                repo.put_summary(run_id, reuse, "ai", key)
                continue
            text = ask(shown)
            if accepted(shown, text):
                repo.put_summary(run_id, text, "ai", key)
                logger.info("bank verification summary ready for %s (%d chars)", run_id, len(text))
            else:
                repo.put_summary(run_id, None, "refused", key)
                logger.warning("bank verification summary refused by the check for %s", run_id)
        except Exception as exc:  # noqa: BLE001 - the model down / slow: the rules-written summary stands
            logger.warning("bank verification summary: model unavailable (%s: %s)", type(exc).__name__, exc)
        finally:
            with _LOCK:
                _QUEUED.discard(run_id)
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="bank-verification-summary", daemon=True)
            _WORKER.start()


def _context(repo: Repository, case_id: str):
    from app.agents.credit.bank_verification.services import verification_service

    case = repo.case(case_id)
    if case is None or case.get("historical"):
        raise verification_service.ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    found = verification_service.latest(repo, case_id)
    if found is None:
        raise verification_service.ServiceError(409, "NOT_PROCESSED")
    run = found[0]
    decisions = repo.decisions(case_id)
    decision = decisions[-1] if decisions and decisions[-1]["run_id"] == run["run_id"] else None
    return case, run, decision


def enqueue(repo: Repository, case_id: str) -> bool:
    """Queue the latest run's summary (precompute). True when queued now; False when ready, queued or off."""
    if not enabled():
        return False
    case, run, decision = _context(repo, case_id)
    if repo.summary(run["run_id"]):
        return False
    shown = facts(case, run, decision)
    key = facts_hash(shown)
    reuse = repo.summary_by_hash(key)
    if reuse:
        repo.put_summary(run["run_id"], reuse, "ai", key)          # the same results already explained: no model
        return False
    with _LOCK:
        if run["run_id"] in _QUEUED:
            return False
        _QUEUED.add(run["run_id"])
    _ensure_worker()
    _QUEUE.put((repo.root, run["run_id"], shown, key))
    return True


def precompute(repo: Repository, case_id: str) -> None:
    """After a run: queue its summary when config ai_summary.precompute is on (never raises)."""
    if section("ai_summary").get("precompute", True):
        try:
            enqueue(repo, case_id)
        except Exception as exc:  # noqa: BLE001 - a summary never breaks a verification run
            logger.warning("bank verification summary: not queued (%s)", type(exc).__name__)


def summarise(repo: Repository, case_id: str, *, start: bool = True) -> dict[str, Any]:
    """{"text", "source": "ai" | "rules", "pending", "note", "label", "checklist", "run_id"} -- never waits for the
    model past ai_summary.wait_seconds. `start=False` (the report) never queues a model call."""
    case, run, decision = _context(repo, case_id)
    out = {"run_id": run["run_id"], "checklist": checklist(run)}
    stored = repo.summary(run["run_id"])
    if not (stored and stored["source"] == "ai" and stored["text"]) and start:
        enqueue(repo, case_id)
        wait = float(section("ai_summary").get("wait_seconds", 0))
        if wait > 0:
            import time

            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and run["run_id"] in _QUEUED:
                time.sleep(0.2)
        stored = repo.summary(run["run_id"])
    if stored and stored["source"] == "ai" and stored["text"]:
        return {**out, "text": stored["text"], "source": "ai", "pending": False, "note": say("ai_summary.note_ai"),
                "label": say("ai_summary.label_ai")}
    with _LOCK:
        pending = run["run_id"] in _QUEUED
    return {**out, "text": fallback(case, run, decision), "source": "rules", "pending": pending,
            "note": say("ai_summary.pending") if pending else say("ai_summary.note_rules"),
            "label": say("ai_summary.label_rules")}


# EVERY SUMMARY RETURNED AS NUMBERED POINTS (2026-11-02; app/core/summary_points.py, config summary_format.yaml):
# how it is made and checked above is unchanged -- only what is returned
from app.core import summary_points as _points  # noqa: E402

summarise = _points.text_field(summarise)
