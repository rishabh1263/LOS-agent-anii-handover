"""
THE TURN LOG -- real use becomes training data (2026-10-14, owner: "nlp ka 1").

Every copilot chat turn is one JSON line in `runtime/turn_log.jsonl` (config copilot_reply.yaml `turn_log`):

    {"ts", "chat" (a hash, never the user), "q" (masked: no PAN / Aadhaar / account / phone), "fixed" (the typo
     correction, masked), "frame" (the entity frame: document codes, party, stage ...), "layer", "intent",
     "label" (the catalogue intent when the turn was understood), "outcome": answered | asked | fallback, "ms"}

LABELLED EXAMPLES FROM REAL USE: when the bot ASKED BACK and the officer's next turn in the same chat (within
`resolve_within_seconds`) was understood as catalogue intent X, the unclear message is written to
`runtime/labelled_turns.jsonl` as {"q", "intent": X, "source": "resolved_clarification", "reviewed": false}.
scripts/train_meaning_classifier.py trains on REVIEWED rows only (scripts/review_labelled_turns.py approves them):
a wrong pair never reaches a model without a person seeing it.

FEEDBACK (2026-10-18): a 👍 / 👎 on an answer goes to `runtime/feedback.jsonl` as {"ts", "by" (caller hash), "rid"
(the answer's request_id -- every turn row carries its "rid" and "by" too), "rating", "reason" (masked),
"reviewed": false}. A rating whose "by" differs from its turn's "by" is not the asker's and is dropped at review.

DATABASE (2026-10-28): with CHATBOT_DSN set every row is also stored in the chatbot database (chat_store.py: tables
chat_turns_log, chat_labelled_turns, chat_feedback), queued in the background; the files stay as they are.

Never case data (no answer text is written), never a raw identifier, never the user's name. A convenience: any
failure is swallowed and the turn is answered as before. The files rotate at `max_mb`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ASKED: dict[str, tuple[float, str]] = {}           # chat -> (when, the masked unclear message) awaiting resolution
_ROOT = Path(__file__).resolve().parents[2]

#: replies that ASK the officer something back
_ASKING = {"CLARIFICATION", "VAGUE_OPTIONS", "WHICH_ACTION", "DID_YOU_MEAN"}
#: replies that say the bot did not understand / could not answer
_FALLBACK = {"UNKNOWN", "UNKNOWN_TERM", "NOT_IN_SCOPE", "GUARDRAIL_BLOCKED"}


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg().get("turn_log") or {}


def enabled() -> bool:
    import os

    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("TURN_LOG_IN_TESTS"):
        return False                     # a test run never writes the real log (tests opt in with TURN_LOG_IN_TESTS)
    return bool(cfg().get("enabled", False))


def _path(key: str, default: str) -> Path:
    return _ROOT / str(cfg().get(key) or default)


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = float(cfg().get("max_mb", 20)) * 1024 * 1024
    with _LOCK:
        if path.exists() and path.stat().st_size > limit:
            path.replace(path.with_suffix(path.suffix + ".1"))       # one previous file kept
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _to_db(kind: str, row: dict[str, Any]) -> None:
    """The same row in the chatbot database (app/agents/applicant/chat_store.py; CHATBOT_DSN), queued -- the JSONL file
    above stays the copy the review / training scripts read."""
    try:
        from app.agents.applicant import chat_store

        getattr(chat_store, kind)(row)
    except Exception:  # noqa: BLE001 - the file already has the row
        logger.debug("turn log: database copy not queued", exc_info=True)


def _chat_id(chat_key: str | None) -> str:
    return hashlib.sha256(str(chat_key or "").encode()).hexdigest()[:16] if chat_key else ""


def label_of(reply: dict[str, Any], meaning_intent: str | None) -> str | None:
    """The CATALOGUE intent this turn was understood as: the Qwen agent's tool (tool names are catalogue intents),
    else the meaning layer's decision; None when the turn asked back / fell back / was small talk."""
    intent = str(reply.get("intent") or "").upper()
    if intent in _ASKING or intent in _FALLBACK:
        return None
    if str(reply.get("response_source") or "") == "AGENT":
        tools = [t for t in reply.get("tools_invoked") or [] if isinstance(t, str)]
        return tools[0] if len(tools) == 1 else None
    return meaning_intent or None


def by_id(subject: str | None) -> str:
    """Who, as a hash (never the user): lets a rating be matched to the turns of the same caller."""
    return hashlib.sha256(str(subject or "").encode()).hexdigest()[:16] if subject else ""


def ratings() -> tuple[str, ...]:
    return tuple(str(r).lower() for r in cfg().get("feedback_ratings") or ("up", "down"))


def feedback(*, subject: str | None, reply_to: str, rating: str, reason: str | None) -> bool:
    """One 👍 / 👎 on an answer, joined to its turn by `rid`; the reason masked and cut. False when not written
    (the log is off, or the write failed) -- the officer is never shown an error for it."""
    if not enabled():
        return False
    try:
        from app.agents.applicant.copilot.semantics import meaning

        limit = int(cfg().get("feedback_reason_max_chars", 300))
        said = meaning._masked(str(reason or "").strip()[:limit]) or None
        row = {"ts": round(time.time(), 1), "by": by_id(subject), "rid": reply_to, "rating": rating,
               "reason": said, "reviewed": False}
        _append(_path("feedback_path", "runtime/feedback.jsonl"), row)
        _to_db("feedback", row)
        return True
    except Exception:  # noqa: BLE001 - the log is a convenience, never the answer
        logger.debug("turn log: feedback not recorded", exc_info=True)
        return False


def record(*, chat_key: str | None, typed: str, fixed: str | None, reply: dict[str, Any],
           meaning_intent: str | None, layer: str | None, started: float | None,
           request_id: str | None = None, subject: str | None = None) -> None:
    """One turn; and, when it resolves the previous turn's question, one labelled example (unreviewed)."""
    if not enabled() or not isinstance(reply, dict) or not typed:
        return
    try:
        from app.agents.applicant.copilot.semantics import entities, meaning

        q = meaning._masked(typed)
        intent = str(reply.get("intent") or "").upper()
        label = label_of(reply, meaning_intent)
        outcome = "asked" if intent in _ASKING else "fallback" if intent in _FALLBACK else "answered"
        chat = _chat_id(chat_key)
        row = {"ts": round(time.time(), 1), "rid": request_id, "by": by_id(subject), "chat": chat, "q": q,
               "fixed": meaning._masked(fixed) if fixed and fixed != typed else None,
               "frame": entities.extract(typed).as_dict(), "layer": layer, "intent": intent, "label": label,
               "outcome": outcome, "ms": round((time.perf_counter() - started) * 1000) if started else None}
        try:
            from app.agents.applicant.copilot.capabilities import qwen_agent

            read = qwen_agent.HISTORY_READ.get()
            if read:
                row["hist"] = {"src": read[0], "n": int(read[1])}     # what this turn read as history: counts only
        except Exception:  # noqa: BLE001 - a diagnostic, never the log row
            pass
        _append(_path("path", "runtime/turn_log.jsonl"), row)
        _to_db("turn", row)
        if not chat:
            return
        now = time.time()
        with _LOCK:
            waiting = _ASKED.pop(chat, None)
            if outcome == "asked":
                _ASKED[chat] = (now, q)
                while len(_ASKED) > 5000:
                    _ASKED.pop(next(iter(_ASKED)))
        if waiting and label and now - waiting[0] <= float(cfg().get("resolve_within_seconds", 300)):
            pair = {"ts": round(now, 1), "q": waiting[1], "intent": label, "source": "resolved_clarification",
                    "reviewed": False}
            _append(_path("labelled_path", "runtime/labelled_turns.jsonl"), pair)
            _to_db("labelled", pair)
    except Exception:  # noqa: BLE001 - the log is a convenience, never the answer
        logger.debug("turn log: not recorded", exc_info=True)


__all__ = ["by_id", "cfg", "enabled", "feedback", "label_of", "ratings", "record"]
