"""
EVERY MODEL CALL, COUNTED WHERE IT HAPPENS.

Usage used to be INFERRED from response fields (`response_source == "LLM"`,
`phrased_by_model`, ...). Measured over HTTP, that missed calls: a request
whose knowledge answer the model wrote was then handed to the composer for a
second call, and the turn record still said one. Every Ollama client is built
by app.llm.provider.create_ollama_client, which wraps it so each
`get_response` lands here: one entry per call, per request.

WHAT IS RECORDED: which code asked (module.function), the model, whether the
host is local, start offset, latency, prompt and output size in characters,
the bounded generation limit, and the outcome -- OK, EMPTY, ERROR, or
TIMEOUT when the caller's own time budget cancelled it. NEVER the prompt or
the output text: both can carry customer data.

WHETHER THE OUTPUT WAS USED is the caller's to say (`mark_used`): a call can
succeed and still be discarded by a fidelity check.
"""

from __future__ import annotations

import contextvars
import time
from typing import Any

_LEDGER: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "llm_call_ledger", default=None)
_STARTED: contextvars.ContextVar[float | None] = contextvars.ContextVar("llm_ledger_started", default=None)


def begin() -> tuple[contextvars.Token, contextvars.Token]:
    """Start a fresh ledger for this request."""
    return _LEDGER.set([]), _STARTED.set(time.perf_counter())


def end(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    _LEDGER.reset(tokens[0])
    _STARTED.reset(tokens[1])


def calls() -> list[dict[str, Any]]:
    """This request's model calls so far (a copy)."""
    return [dict(c) for c in (_LEDGER.get() or [])]


def record(entry: dict[str, Any]) -> dict[str, Any]:
    ledger = _LEDGER.get()
    started = _STARTED.get()
    if started is not None:
        entry["at_ms"] = round((time.perf_counter() - started) * 1000 - entry.get("ms", 0.0), 1)
    entry.setdefault("used", None)
    if ledger is not None:
        entry["n"] = len(ledger) + 1
        ledger.append(entry)
    return entry


def mark_used(caller: str, used: bool) -> None:
    """The latest call from `caller` (module.function suffix) was used / discarded."""
    for entry in reversed(_LEDGER.get() or []):
        if str(entry.get("caller", "")).endswith(caller):
            entry["used"] = bool(used)
            return


def summary() -> dict[str, Any]:
    """Counts for the turn record."""
    entries = _LEDGER.get() or []
    return {"calls": len(entries),
            "ok": sum(e.get("outcome") == "OK" for e in entries),
            "timeouts": sum(e.get("outcome") == "TIMEOUT" for e in entries),
            "errors": sum(e.get("outcome") == "ERROR" for e in entries),
            "used": sum(e.get("used") is True for e in entries),
            "ms": round(sum(float(e.get("ms") or 0) for e in entries), 1),
            "detail": [{k: e.get(k) for k in ("n", "caller", "model", "outcome", "ms", "at_ms",
                                               "prompt_chars", "output_chars", "max_tokens", "used")}
                       for e in entries]}


__all__ = ["begin", "calls", "end", "mark_used", "record", "summary"]
