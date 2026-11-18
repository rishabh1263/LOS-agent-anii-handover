"""
THE SUMMARY OF A CASE'S RESULTS IN PLAIN WORDS -- on screen once the results are in, and in the PDF report.

    rules   written from the results by config wording (ai_summary.rules) -- always there, at once
    ai      the local model (Qwen via Ollama, app/llm/config.py) explains the results in a few sentences

IT EXPLAINS, IT NEVER DECIDES. The model sees only the results, one entry per person (role, status, risk, counts, the
sources with findings) -- never a name, a PAN or an id. Its text is CHECKED before it is shown: every figure must be a
figure of the facts, no banned word ("approved", "criminal record", "clean record" ...), at most `max_chars`. Refused,
slow, or the model down -> the rules-written summary stands (`source` says which).

When: a case whose subjects are all done (initiate / refresh) is queued at once; ONE background worker writes the
summaries one by one, so the screen never waits (it shows the rules-written text with `pending: true` and asks again).
The same facts already explained (another case with the same results) reuse the stored text, no model call.
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
from typing import Any

from app.agents.credit.crime_verification.config import say, section, words
from app.agents.credit.crime_verification.db import database
from app.agents.credit.crime_verification.services import queries
from app.agents.credit.crime_verification.services.access import Caller, require

logger = logging.getLogger(__name__)

_QUEUE: "queue.Queue[tuple[str, dict[str, Any], str]]" = queue.Queue()
_QUEUED: set[str] = set()
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_NUMBERS = ["two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
IN_FLIGHT = {"INITIATED", "PROCESSING"}


def enabled() -> bool:
    override = (os.getenv("CRIME_VERIFICATION_AI_SUMMARY") or "").strip().lower()
    if override:
        return override in ("1", "on", "true", "yes")
    return bool(section("ai_summary").get("enabled", False))


# ---- what is said ---------------------------------------------------------------------------------------------------
def facts(case: dict[str, Any]) -> list[dict[str, Any]]:
    """What the model sees: each person's result -- labels and counts only."""
    cap = int(section("ai_summary").get("max_items", 3))
    people = []
    for s in case["subjects"]:
        person: dict[str, Any] = {"role": s["role_label"], "status": s["status_label"]}
        r = s.get("result")
        if r:
            items = [i for g in r["groups"] for i in g["items"]]
            found = sorted((i for i in items if i["cases_found"]), key=lambda i: -i["cases_found"])
            person |= {"risk": r["risk_label"], "total_cases_found": r["total_cases"],
                       "sources_with_findings": len(found), "sources_covered": len(items),
                       "largest_sources": [{"source": i["name"], "cases": i["cases_found"]} for i in found[:cap]],
                       "matter_types": [{"type": c["label"], "count": c["count"]} for c in r["clusters"]]}
        else:
            person["result"] = s.get("outcome_label") or ""
        people.append(person)
    return people


def facts_hash(shown: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()


def rules_text(shown: list[dict[str, Any]]) -> str:
    out = []
    for p in shown:
        if "risk" in p:
            sources = ", ".join(f"{x['source']} {x['cases']}" for x in p["largest_sources"])
            risk = (say("ai_summary.rules.risk", risk=p["risk"], cases=p["total_cases_found"],
                        sources=say("ai_summary.rules.sources", sources=sources) if sources else "")
                    if p["total_cases_found"] else f" — {p['risk']}" + say("ai_summary.rules.no_match"))
        else:
            risk = f" — {p['result']}" if p.get("result") else ""
        out.append(say("ai_summary.rules.person", role=p["role"], status=p["status"], risk=risk))
    return " ".join(out)


def _people_in(sentence: str, shown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The people a sentence talks about: the roles it names (longest first: "Co-Applicant" is not "Applicant"),
    else everyone (a sentence about "both" or about none in particular)."""
    low = sentence.lower()
    named = []
    for p in sorted(shown, key=lambda x: -len(x["role"])):
        role = p["role"].lower()
        if role in low:
            named.append(p)
            low = low.replace(role, " ")
    return named or shown


def _risks_in(sentence: str) -> set[str]:
    """Risk labels a sentence names ("Very High Risk" before "High Risk")."""
    low, found = sentence.lower(), set()
    for label in sorted((str(v) for v in words("risk.labels").values()), key=len, reverse=True):
        if label.lower() in low:
            found.add(label)
            low = low.replace(label.lower(), " ")
    return found


def accepted(shown: list[dict[str, Any]], text: str) -> bool:
    """May the model's text be shown? Checked SENTENCE BY SENTENCE against the people it names: every figure must
    be one of THEIR figures, a total must be their total, a risk must be their risk; no banned word, not too long."""
    from app.agents.eligibility.output.summary import _COUNT_WORDS, _digits

    text = (text or "").strip()
    if not text or len(text) > _limits(len(shown))["max_chars"]:
        return False
    if re.search(r"[a-z]_[a-z]", text):                     # a field or code name
        return False
    low = text.lower()
    if set(re.findall(r"[a-z]+", low)) & (_COUNT_WORDS - {"both"}):
        return False
    if any(re.search(rf"\b{re.escape(str(w).lower())}\b", low) for w in words("ai_summary.banned_words")):
        return False
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        people = _people_in(sentence, shown)
        if not _digits(sentence) <= _digits(json.dumps(people, ensure_ascii=False)):
            return False                                   # a number that is not this person's
        totals = {str(p.get("total_cases_found")) for p in people}
        claimed = re.findall(r"(\d+)\s+(?:synthetic\s+)?cases?\b[^.]*?\b(?:in total|total|overall|altogether)"
                             r"|\b(?:total|overall)\s+(?:of\s+)?(\d+)", sentence, flags=re.I)
        if any((a or b) not in totals for a, b in claimed):
            return False                                   # "2 cases in total" when the person has 8
        if not _risks_in(sentence) <= {str(p.get("risk")) for p in people}:
            return False                                   # a risk that is not this person's
        said_missing = any(str(w).lower() in sentence.lower() for w in words("ai_summary.pending_words"))
        if said_missing and all("risk" in p for p in people):
            return False                                   # "not available yet" about a result that is in
    return True


def _clean(text: str) -> str:
    from app.agents.eligibility.output.summary import _whole_sentences

    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"\b(two|three|four|five|six|seven|eight|nine|ten)\b",
                  lambda m: str(_NUMBERS.index(m.group(1).lower()) + 2), text, flags=re.I)
    return _whole_sentences(re.sub(r"\s+", " ", text))


# ---- the model ------------------------------------------------------------------------------------------------------
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


def _limits(people: int) -> dict[str, int]:
    """How long the summary may be: config ai_summary, grown with the number of people (every one is named)."""
    spec = section("ai_summary")
    sentences = min(max(int(spec.get("sentences", 4)), people * int(spec.get("sentences_per_person", 1))),
                    int(spec.get("max_sentences", 12)))
    return {"sentences": sentences,
            "num_predict": max(int(spec.get("num_predict", 180)), people * int(spec.get("num_predict_per_person", 45))),
            "max_chars": max(int(spec.get("max_chars", 900)), people * int(spec.get("max_chars_per_person", 220)))}


def _ask_person(person: dict[str, Any]) -> tuple[str, bool]:
    """One person's point: (text, written by the model). No findings / no result -> the rules-written line at once;
    a refused or failed model answer -> the rules-written line too."""
    spec = section("ai_summary")
    plain = rules_text([person])
    if "risk" not in person or (not person.get("total_cases_found") and not spec.get("model_for_no_findings", False)):
        return plain, False
    sentences = int(spec.get("person_sentences", 1))
    messages = [{"role": "system", "content": say("ai_summary.person_instructions").strip()},
                {"role": "user", "content": json.dumps(person, ensure_ascii=False, separators=(",", ":"))}]
    text = _clean(_post(messages, int(spec.get("person_num_predict", 70)), sentences))
    return (text, True) if accepted([person], text) else (plain, False)


def ask(shown: list[dict[str, Any]]) -> str:
    """The case's summary text. Per person (config ai_summary.per_person): one point each, `parallel` calls at a time;
    "" when no point came from the model (then the screen keeps the rules-written summary, honestly labelled)."""
    spec = section("ai_summary")
    if spec.get("per_person", False):
        from concurrent.futures import ThreadPoolExecutor

        failures: list[Exception] = []

        def one(person: dict[str, Any]) -> tuple[str, bool]:
            try:
                return _ask_person(person)
            except Exception as exc:  # noqa: BLE001 - this person's model call failed: their rules-written line
                failures.append(exc)
                return rules_text([person]), False

        with ThreadPoolExecutor(max_workers=max(1, int(spec.get("parallel", 4)))) as pool:
            points = list(pool.map(one, shown))
        if not any(by_model for _, by_model in points):
            if failures:
                raise failures[0]                     # the model is down: the caller records "unavailable"
            return ""
        return " ".join(text for text, _ in points)
    limits = _limits(len(shown))
    messages = [{"role": "system", "content": say("ai_summary.instructions", sentences=limits["sentences"]).strip()},
                {"role": "user", "content": json.dumps(shown, ensure_ascii=False, separators=(",", ":"))}]
    return _clean(_post(messages, limits["num_predict"], limits["sentences"]))


# ---- the store and the background writer ----------------------------------------------------------------------------
def _put(case_id: str, key: str, text: str | None, source: str) -> None:
    with database().tx() as conn:
        conn.execute("INSERT INTO crime_verification_summaries (case_id, facts_hash, text, source) VALUES "
                     "(%s, %s, %s, %s) ON CONFLICT (case_id) DO UPDATE SET facts_hash = EXCLUDED.facts_hash, "
                     "text = EXCLUDED.text, source = EXCLUDED.source, created_at = now()", (case_id, key, text, source))


def _work() -> None:
    while True:
        case_id, shown, key = _QUEUE.get()
        try:
            with database().tx() as conn:
                reuse = conn.execute("SELECT text FROM crime_verification_summaries WHERE facts_hash = %s AND "
                                     "source = 'ai' LIMIT 1", (key,)).fetchone()
            if reuse:
                _put(case_id, key, reuse["text"], "ai")
                continue
            text = ask(shown)
            if accepted(shown, text):
                _put(case_id, key, text, "ai")
                logger.info("crime verification summary ready for %s (%d chars)", case_id, len(text))
            else:
                _put(case_id, key, None, "refused")
                logger.warning("crime verification summary refused by the check for %s", case_id)
        except Exception as exc:  # noqa: BLE001 - the model down / slow: the rules-written summary stands
            logger.warning("crime verification summary: model unavailable (%s)", type(exc).__name__)
            try:
                _put(case_id, key, None, "unavailable")
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _LOCK:
                _QUEUED.discard(case_id)
            _QUEUE.task_done()


def _enqueue(case_id: str, shown: list[dict[str, Any]], key: str) -> None:
    global _WORKER
    with _LOCK:
        if case_id in _QUEUED:
            return
        _QUEUED.add(case_id)
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="crime-verification-summary", daemon=True)
            _WORKER.start()
    _QUEUE.put((case_id, shown, key))


def _ready(case: dict[str, Any]) -> bool:
    return bool(case["subjects"]) and case["status"] != "WAIVED" and not any(
        s["status"] in IN_FLIGHT for s in case["subjects"])


def _stored(case_id: str, key: str) -> dict[str, Any] | None:
    with database().tx() as conn:
        return conn.execute("SELECT text, source FROM crime_verification_summaries WHERE case_id = %s AND "
                            "facts_hash = %s", (case_id, key)).fetchone()


def precompute(case: dict[str, Any]) -> None:
    """A case whose results are all in: write its summary in the background now."""
    if enabled() and _ready(case):
        shown = facts(case)
        key = facts_hash(shown)
        if not _stored(case["case_id"], key):
            _enqueue(case["case_id"], shown, key)


def summary_of(case: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
    """{"available", "text", "source": ai|rules, "pending", "label"}. `start=False` (the report) never queues."""
    if not _ready(case):
        return {"available": False, "text": None, "source": None, "pending": False, "label": None}
    shown = facts(case)
    key = facts_hash(shown)
    stored = _stored(case["case_id"], key)
    if stored and stored["source"] == "ai" and stored["text"]:
        return {"available": True, "text": stored["text"], "source": "ai", "pending": False,
                "label": say("ai_summary.labels.ai")}
    pending = enabled() and not stored
    if pending and start:
        _enqueue(case["case_id"], shown, key)
    return {"available": True, "text": rules_text(shown), "source": "rules", "pending": pending,
            "label": say("ai_summary.labels.pending" if pending else "ai_summary.labels.rules")}


def summarise(who: Caller, case_id: str) -> dict[str, Any]:
    require(who, "view")
    return {"case_id": case_id, **summary_of(queries.case_detail(who, case_id))}


# EVERY SUMMARY RETURNED AS NUMBERED POINTS (2026-11-02; app/core/summary_points.py, config summary_format.yaml):
# how it is made and checked above is unchanged -- only what is returned
from app.core import summary_points as _points  # noqa: E402

summary_of = _points.text_field(summary_of)
