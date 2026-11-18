"""
THE SUMMARY at the end of the answer, in plain words (config/wording.yaml):

    headline        one line: can underwriting start, and if not, how many items are open
    missing         documents nobody has given yet (reason per item)
    not_satisfied   documents that ARE on file but do not count yet: outdated, too short, expired, unreadable,
                    rejected, waiting for verification, waiting for an exception's approval
    next_step       the recommended action, in words
    ai_summary      the local model (Qwen) says the same in a short note. It EXPLAINS, never decides: it sees only
                    the facts above, its text is checked (no new figures, no field names, no word against the status)
                    and it never makes the answer wait -- it uses the credit AI-summary machinery
                    (app/agents/credit/repayment/ai_summary.py). Slow, refused or down -> the rules-built note,
                    `source: "rules"`.

The first four are deterministic and recorded with the evaluation; the AI note is added when the answer is sent.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORDING = Path(__file__).resolve().parents[1] / "config" / "wording.yaml"
_MISSING_RULES = {"RULE-02", "RULE-03", "RULE-04", "RULE-05"}


@lru_cache(maxsize=1)
def wording() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_WORDING.read_text(encoding="utf-8")) or {}


def _is_missing(item: dict) -> bool:
    return item["status"] == "ACTION_REQUIRED" and item["rule_id"] in _MISSING_RULES and not \
        any(e.startswith("document:") for e in item.get("evidence") or [])


def route(items: list[dict], *, stage: str | None = None, target_stage: str | None = None) -> dict:
    """Where the case goes from here, from what is open (see wording.yaml `route`)."""
    say = wording()
    blocking = [i for i in items if i["severity"] == "BLOCKING"]
    critical = [i for i in blocking if i["status"] != "EXCEPTION_PENDING"]
    minor = [i for i in items if i["severity"] != "BLOCKING"]
    if critical:
        decision = "REFER_TO_AUTHORITY"
    elif blocking:
        decision = "AWAIT_APPROVAL"
    else:
        decision = "MOVE_AHEAD_WITH_NOTES" if minor else "MOVE_AHEAD"
    forms = say.get("plural") or {}
    word = lambda key, n: forms[key][0 if n == 1 else 1]  # noqa: E731
    spec = say["route"]
    key = decision
    if decision in ("MOVE_AHEAD", "MOVE_AHEAD_WITH_NOTES") and target_stage and (stage or "") != target_stage:
        key = f"{decision}_NOT_AT_STAGE"                           # documents done; the stage is not there yet
    text = spec["text"].get(key, spec["text"][decision]).format(
        critical=len(critical), minor=len(minor), authority=spec["authority"],
        stage=target_stage or "", current=stage or "an earlier stage",
        critical_items=word("critical_items", len(critical)), critical_them=word("critical_them", len(critical)),
        minor_items=word("minor_items", len(minor)), minor_them=word("minor_them", len(minor)))
    return {"decision": decision, "label": spec["label"][decision], "text": text, "critical": len(critical),
            "non_critical": len(minor), "authority": spec["authority"]}


def build(*, overall: str, stage: str | None, target_stage: str, action: str, items: list[dict],
          people: dict[str, str] | None = None) -> dict:
    say = wording()
    blocking = [i for i in items if i["severity"] == "BLOCKING"]
    if overall == "COMPLETE":
        key = "COMPLETE_AT_STAGE" if (stage or "") == target_stage else "COMPLETE_NOT_AT_STAGE"
    else:
        key = overall
    words = {w: forms[0 if len(blocking) == 1 else 1] for w, forms in (say.get("plural") or {}).items()}
    headline = say["headline"][key].format(count=len(blocking), stage=stage or "an earlier stage", **words)
    missing = [i["reason"] for i in items if _is_missing(i)]
    not_satisfied = [i["reason"] + ("" if i["severity"] == "BLOCKING" else " (does not block)")
                     for i in items if not _is_missing(i)]
    people = people or {}
    by_person = {}
    for role, name in people.items():
        mine = [i for i in items if i.get("party_role") == role]
        by_person[role] = {"name": name, "open": len(mine),
                           "critical": sum(1 for i in mine if i["severity"] == "BLOCKING")}
    # a NON-critical document that is genuinely absent (a supporting document such as an ITR's computation):
    # the only case where "missing" may be said without a critical missing document
    minor_missing = [i["reason"] for i in items if i["rule_id"] == "RULE-07" and i["status"] == "ACTION_REQUIRED"]
    return {"headline": headline, "route": route(items, stage=stage, target_stage=target_stage),
            "missing": missing, "minor_missing": minor_missing, "not_satisfied": not_satisfied,
            "people": by_person, "next_step": say["next_step"].get(action, action)}


# ---- the AI note -----------------------------------------------------------------------------------------------

_CACHE: OrderedDict[str, str] = OrderedDict()
_WRITING: set[str] = set()
_LOCK = threading.Lock()
_ROOT = Path(__file__).resolve().parents[4]


def _file() -> Path | None:
    name = str(wording()["ai_summary"].get("cache_file") or "").strip()
    return (_ROOT / name) if name else None


def _from_file(key: str) -> str | None:
    path = _file()
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(key) if path and path.is_file() else None
    except Exception:  # noqa: BLE001 - an unreadable cache file is an empty cache
        return None


def _to_file(key: str, text: str) -> None:
    path = _file()
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data[key] = text
        keep = int(wording()["ai_summary"].get("cache_size") or 256)
        data = dict(list(data.items())[-keep:])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - the note is still in memory
        logger.warning("document readiness ai_summary: cache file not written (%s)", type(exc).__name__)


def _facts(summary: dict, overall: str) -> dict:
    who = {"PRIMARY_APPLICANT": "applicant", "CO_APPLICANT": "co-applicant"}
    route = summary.get("route") or {}
    return {"status": overall, "route": route.get("text", ""), "higher_authority": route.get("authority", ""),
            "people": [f"{p['name'] or who.get(r, r)} ({who.get(r, r)}): {p['open']} open"
                       for r, p in (summary.get("people") or {}).items()],
            "missing": summary["missing"][:5], "not_satisfied": summary["not_satisfied"][:5],
            "next_step": summary["next_step"]}


def fallback(summary: dict) -> str:
    first = (summary["missing"] or summary["not_satisfied"] or [""])[0]
    text = wording()["ai_summary"]["fallback"].format(headline=summary["headline"], first=first,
                                                      route=(summary.get("route") or {}).get("text", ""),
                                                      next=summary["next_step"])
    return re.sub(r"\s+", " ", text).strip()


def accepted(text: str, summary: dict, overall: str) -> bool:
    from app.agents.eligibility.output.summary import _digits

    spec = wording()["ai_summary"]
    text = (text or "").strip()
    if not text or len(text) > int(spec.get("max_chars") or 600) or re.search(r"[a-z]_[a-z]", text):
        return False
    if not _digits(text) <= _digits(json.dumps(_facts(summary, overall), ensure_ascii=False)):
        return False
    low = text.lower()
    # "missing" only when something IS missing: a document under review or rejected is on file, not missing
    decision = (summary.get("route") or {}).get("decision")
    if not summary["missing"] and not summary.get("minor_missing") \
            and any(w in low for w in spec.get("missing_words") or []):
        return False
    contradicts = spec.get("contradicts") or {}
    words = contradicts.get(decision) if decision in contradicts else contradicts.get(overall, [])
    return not any(w in low for w in words or [])


def ai_note(summary: dict, overall: str) -> dict:
    """{"text", "source": "ai" | "rules", "pending"?} -- never raises, never waits past timeout_seconds."""
    import os

    spec = dict(wording()["ai_summary"])
    if not spec.get("enabled") or (os.getenv("DOCS_CREDIT_AI_SUMMARY") or "").strip().lower() == "false":
        return {"text": fallback(summary), "source": "rules"}
    key = json.dumps(_facts(summary, overall), ensure_ascii=False, sort_keys=True)
    with _LOCK:
        if key not in _CACHE and (saved := _from_file(key)):
            _CACHE[key] = saved
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return {"text": _CACHE[key], "source": "ai"}
        if key in _WRITING:
            return {"text": fallback(summary), "source": "rules", "pending": True}
        _WRITING.add(key)
    messages = [{"role": "system", "content": spec["instructions"].strip()}, {"role": "user", "content": key}]

    def run() -> None:
        from app.agents.credit.repayment.ai_summary import _ask, _clean

        try:
            text = _clean(_ask(messages, spec))
        except Exception as exc:  # noqa: BLE001 - model down / slow: the rules note stands
            logger.warning("document readiness ai_summary: model unavailable (%s: %s)", type(exc).__name__, exc)
            text = ""
        with _LOCK:
            _WRITING.discard(key)
            if not text:
                return
            if not accepted(text, summary, overall):
                logger.warning("document readiness ai_summary: model text refused: %r", text[:300])
                return
            _CACHE[key] = text
            while len(_CACHE) > int(spec.get("cache_size") or 256):
                _CACHE.popitem(last=False)
            _to_file(key, text)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    wait = float(spec.get("timeout_seconds") or 0)
    if wait > 0:
        worker.join(wait)
    with _LOCK:
        text = _CACHE.get(key)
    if text:
        return {"text": text, "source": "ai"}
    return {"text": fallback(summary), "source": "rules", "pending": worker.is_alive()}


__all__ = ["accepted", "ai_note", "build", "fallback", "wording"]


# EVERY SUMMARY RETURNED AS NUMBERED POINTS (2026-11-02; app/core/summary_points.py, config summary_format.yaml):
# how it is made and checked above is unchanged -- only what is returned
from app.core import summary_points as _points  # noqa: E402

ai_note = _points.text_field(ai_note)
