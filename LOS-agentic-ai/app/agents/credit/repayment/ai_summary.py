"""
THE AI SUMMARY at the end of the answer: the local model (Qwen via Ollama) says the finished answer in easy words.

IT EXPLAINS, IT NEVER DECIDES. It is shown the answer's own facts (decision, the summary line, the problems, the
points to watch, the key figures) and writes at most `sentences` sentences. Nothing it writes is read back.

CHECKED BEFORE IT IS SHOWN, the same guard as the eligibility explanation: every figure in it must be one of the
facts' figures, no field name, no word against the answer (wording.yaml ai_summary.contradicts), at most `max_chars`.
Refused, slow (settings.yaml ai_summary.timeout_seconds), or the model down -> the summary is built from the facts
instead, and `source` says "rules". The answer never waits past the timeout and never fails because of it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
from collections import OrderedDict
from typing import Any

from app.agents.credit.repayment import config

logger = logging.getLogger(__name__)

_CACHE: OrderedDict[str, str] = OrderedDict()
_LOCK = threading.Lock()


def _spec() -> dict[str, Any]:
    return dict(config.settings().get("ai_summary") or {})


def facts(answer: dict[str, Any]) -> dict[str, Any]:
    """What the model sees: the answer's own words and figures, compact (CPU prompt time is per token)."""
    say = config.wording()
    said = say["ai_summary"]["answers"]
    code_of = {label: code for code, label in say["checks"].items()}
    order = {code: i for i, code in enumerate(say["ai_summary"].get("good_order") or [])}
    checks = answer.get("checks") or []
    passed = [c for c in checks if c.get("result") == "pass"]
    # the passed checks' findings, most telling first (the short answer keeps them out; build hands them in `_good`)
    notes = dict(zip([c["label"] for c in passed], answer.get("_good") or [c.get("note") for c in passed]))
    good = [notes[c["label"]] for c in sorted(passed, key=lambda c: order.get(code_of.get(c["label"]), 99))
            if notes.get(c["label"])]
    found = answer.get("insights") or {}
    emi = answer.get("emi") or {}
    out = {"answer": said.get(str(answer.get("can_repay")), answer.get("summary")),
           "new_emi": _rs(emi["new"]) if emi.get("new") else None,
           "checks": {c["label"]: f"{c['result']} ({c['value']})" if c.get("value") else c["result"] for c in checks},
           "problems": (answer.get("problems") or [])[:2],
           "good": good[:2],
           "risks": (answer.get("watch") or [])[:2],
           "insights": [i if isinstance(i, str) else " ".join(x for x in (i.get("answer"), i.get("explain"),
                                                                           i.get("how_long"), i.get("line")) if x)
                        for i in (found.get("emi_dry_run"), found.get("monthly_surplus")) if i],
           "next_step": (answer.get("next_steps") or [None])[0]}
    return {k: v for k, v in out.items() if v not in (None, [], "", {})}


def _rs(value: Any) -> str:
    from app.agents.eligibility.calc.finance import money

    return f"{config.wording().get('currency', '')}{money(value)}"


def _digits(text: str) -> set[str]:
    from app.agents.eligibility.output.summary import _digits as digits

    return digits(text)


def accepted(answer: dict[str, Any], text: str) -> bool:
    """Whether the model's text may be shown (see the module note)."""
    text = (text or "").strip()
    if not text or len(text) > int(_spec().get("max_chars") or 420) or re.search(r"[a-z]_[a-z]", text):
        return False
    shown = json.dumps(facts(answer), ensure_ascii=False, default=str)
    if not _digits(text) <= _digits(shown):
        return False
    low = text.lower()
    from app.agents.eligibility.output.summary import _COUNT_WORDS

    if set(re.findall(r"[a-z]+", low)) & _COUNT_WORDS:
        return False                                    # a count in words is a figure the digit check cannot see
    words = (config.wording()["ai_summary"].get("contradicts") or {}).get(str(answer.get("decision")), [])
    return not any(w in low for w in words)


def fallback(answer: dict[str, Any]) -> str:
    """The same note built from the facts: the answer, the main reason, one insight, the next step."""
    f = facts(answer)
    reason = (f.get("problems") or f.get("good") or [""])[0]
    insight = next((i for i in f.get("insights") or [] if i not in reason), "")
    text = config.wording()["ai_summary"]["fallback"].format(
        summary=answer.get("summary") or "", reason=reason, insight=insight, next=f.get("next_step") or "")
    return re.sub(r"\s+", " ", text).strip().removesuffix(" Next step:")


def _ask(messages: list[dict[str, str]], spec: dict[str, Any]) -> str:
    """The model's reply, streamed and stopped once `sentences` sentences are complete (CPU time per word)."""
    from app.llm.config import ollama_host, ollama_model

    body = {"model": str(spec.get("model") or ollama_model()), "stream": True, "messages": messages,
            "keep_alive": str(spec.get("keep_alive") or "30m"),
            "options": {"temperature": 0.2, "num_predict": int(spec.get("num_predict") or 140)}}
    request = urllib.request.Request(f"{ollama_host().rstrip('/')}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    text, ended, wanted = "", 0, int(spec.get("sentences") or 3)
    with urllib.request.urlopen(request, timeout=float(spec.get("model_timeout_seconds") or 60)) as response:
        for raw in response:
            if not raw.strip():
                continue
            chunk = json.loads(raw)
            delta = str((chunk.get("message") or {}).get("content") or "")
            if text.rstrip()[-1:] in (".", "!", "?") and (delta[:1].isspace() or not delta):
                ended += 1
                if ended >= wanted:
                    break
            text += delta
            if chunk.get("done"):
                break
    return text


def _clean(text: str) -> str:
    """No think block, no quotes, whole sentences only, no dashes (the officer's reading rules)."""
    from app.agents.eligibility.output.summary import _whole_sentences

    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"\s*(?:--|—|–)\s*", ", ", text)
    # a count the model wrote in words becomes digits, so the figure check sees it ("three EMIs" -> "3 EMIs":
    # kept when the facts hold 3, refused when they do not). "one" is left alone: "one thing in their favour".
    text = re.sub(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
                  lambda m: str(_NUMBERS[m.group(1).lower()]), text, flags=re.I)
    # a fact's key copied into a sentence ("Next_step is to ...") reads as words
    text = re.sub(r"\b(next|new)_(step|emi)\b", lambda m: f"{m.group(1)} {m.group(2)}", text, flags=re.I)
    return _whole_sentences(re.sub(r"\s+", " ", text))


_NUMBERS = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                                        "nine", "ten", "eleven", "twelve"))}


_WRITING: set[str] = set()                # notes being written now: one model call per case, however often it is asked


def summarise(answer: dict[str, Any]) -> dict[str, Any]:
    """{"text", "source": "ai" | "rules", "pending"} -- never raises, never waits past `timeout_seconds` (0: does
    not wait at all -- the answer goes out with the facts-built note and `pending: true`, and the AI note is ready
    the next time the case is asked)."""
    spec = _spec()
    if not spec.get("enabled"):
        return {"text": fallback(answer), "source": "rules"}
    key = json.dumps(facts(answer), ensure_ascii=False, sort_keys=True, default=str)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return {"text": _CACHE[key], "source": "ai"}
        if key in _WRITING:
            return {"text": fallback(answer), "source": "rules", "pending": True}
        _WRITING.add(key)
    messages = [{"role": "system", "content": config.wording()["ai_summary"]["instructions"].strip()},
                {"role": "user", "content": key}]

    def run() -> None:
        # FINISHES EVEN AFTER THE ANSWER HAS GONE: a note slower than the wait is kept for the next time this case
        # is opened (the same facts -> the same note at once)
        try:
            text = _clean(_ask(messages, spec))
        except Exception as exc:  # noqa: BLE001 - the model down / slow: the facts-built note stands
            logger.warning("credit ai_summary: model unavailable (%s: %s)", type(exc).__name__, exc)
            text = ""
        with _LOCK:
            _WRITING.discard(key)
            if not text:
                return
            if not accepted(answer, text):
                logger.warning("credit ai_summary: model text refused by the check: %r", text[:300])
                return
            _CACHE[key] = text
            while len(_CACHE) > int(spec.get("cache_size") or 256):
                _CACHE.popitem(last=False)
        logger.info("credit ai_summary: note ready (%d chars)", len(text))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    wait = float(spec.get("timeout_seconds") if spec.get("timeout_seconds") is not None else 0)
    if wait > 0:
        worker.join(wait)
    with _LOCK:
        text = _CACHE.get(key)
    if text:
        return {"text": text, "source": "ai"}
    return {"text": fallback(answer), "source": "rules", "pending": worker.is_alive()}


__all__ = ["accepted", "fallback", "facts", "summarise"]
