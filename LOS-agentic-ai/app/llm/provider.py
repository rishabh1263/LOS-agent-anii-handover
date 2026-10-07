"""
The one place an Ollama client is constructed.

Host and model come from app.llm.config, so a deployment changes them in one
place rather than in every agent that happens to talk to a model.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from agent_framework.ollama import OllamaChatClient

from app.llm.config import ollama_host, ollama_model


def create_ollama_client() -> OllamaChatClient:
    """
    Build a chat client pointed at the configured host and model.

    The keyword is `model`, not `model_id`. agent_framework renamed it, and
    this factory was still passing the old name, so every call raised
    TypeError: OllamaChatClient.__init__() got an unexpected keyword argument
    'model_id'. Nothing caught it because both callers wrap model use in a
    fallback -- the document verification agent degrades, and the summary
    layers fall back to their deterministic text -- so the model path had been
    silently dead rather than visibly broken.

    Verified against agent_framework 1.17.0 (agent_framework_ollama), where
    the constructed client exposes `.host` and `.model`.
    """
    return _Traced(_client_for(ollama_host(), ollama_model()))


def _text_size(messages: Any) -> int:
    total = 0
    for message in messages or []:
        for content in getattr(message, "contents", None) or []:
            total += len(str(getattr(content, "text", None) or ""))
    return total


class _Traced:
    """
    The client, with every `get_response` written to the request's model-call
    ledger (app/llm/trace.py). Transparent otherwise: attribute access goes
    to the wrapped client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def get_response(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        import sys
        import time

        from app.llm import trace

        # the first frame in application code: callers wrap this call in
        # asyncio.wait_for, so the immediate caller is asyncio itself
        frame, caller = sys._getframe(1), "?"
        while frame is not None:
            module = str(frame.f_globals.get("__name__", ""))
            if module.startswith("app.") and not module.startswith("app.llm"):
                caller = f"{module}.{frame.f_code.co_name}"
                break
            frame = frame.f_back
        host = str(getattr(self._client, "host", "") or "")
        # ONE CONTEXT SIZE FOR EVERY CALL (app/llm/config.py): a different one reloads the model
        from app.llm.config import with_num_ctx

        options = kwargs["options"] = with_num_ctx(kwargs.get("options"))
        entry: dict[str, Any] = {
            "caller": caller, "model": str(getattr(self._client, "model", "") or ""),
            "host": "local" if ("127.0.0.1" in host or "localhost" in host) else "remote",
            "prompt_chars": _text_size(messages),
            "max_tokens": options.get("max_tokens") or options.get("num_predict"),
        }
        started = time.perf_counter()
        try:
            response = await self._client.get_response(messages, *args, **kwargs)
        except asyncio.CancelledError:
            entry.update(outcome="TIMEOUT", ms=round((time.perf_counter() - started) * 1000, 1), output_chars=0)
            trace.record(entry)
            raise
        except Exception as exc:
            entry.update(outcome="ERROR", error=type(exc).__name__,
                         ms=round((time.perf_counter() - started) * 1000, 1), output_chars=0)
            trace.record(entry)
            raise
        text = getattr(response, "text", None)
        entry.update(outcome="OK" if text else "EMPTY", ms=round((time.perf_counter() - started) * 1000, 1),
                     output_chars=len(text or ""))
        # A PROMPT NEAR THE WINDOW IS CUT FROM THE FRONT by Ollama: said, never silent
        prompt_tokens = getattr(getattr(response, "usage_details", None), "input_token_count", None)
        window = options.get("num_ctx")
        if isinstance(prompt_tokens, int) and window and prompt_tokens >= 0.9 * window:
            import logging

            logging.getLogger(__name__).warning(
                "LLM prompt near the context window: %s of %s tokens (caller %s); raise OLLAMA_NUM_CTX",
                prompt_tokens, window, caller)
        trace.record(entry)
        return response


#: (host, model) -> (client, weak reference to the loop it was built on).
#: A plain dict rather than `lru_cache`, because the cache has to be able
#: to notice that its entry is stale, and `lru_cache` cannot.
_CLIENTS: dict[tuple[str, str], tuple[OllamaChatClient, object]] = {}


def _running_loop() -> object | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _client_for(host: str, model: str) -> OllamaChatClient:
    """
    One client per host/model pair, built once PER EVENT LOOP.

    Constructing it was measured at 302 ms median -- on EVERY request, for a
    thin HTTP wrapper -- which was a third of the summary's entire latency
    budget spent before a single token was generated. The client holds no
    per-request state, so there is nothing to keep separate between calls.

    IT DOES HOLD A CONNECTION POOL, AND A POOL BELONGS TO ONE LOOP. This
    was cached on host and model alone, and every caller after the first
    loop closed got `RuntimeError: Event loop is closed` -- reported by
    the client as a request failure, so each of them degraded to its
    fallback and nothing said why. A server runs one loop for its
    lifetime and never saw it; anything driving the app across loops,
    which includes `TestClient`, saw it on every second call.

    So the entry is kept only while it still belongs to the loop in
    hand. The reference to that loop is WEAK: a strong one would keep
    every closed loop of a long test session alive, and a loop that has
    been collected cannot be the one running now anyway.

    Keyed on host and model rather than cached as a single instance, so
    changing either through configuration still takes effect instead of
    silently serving the old one. `reset_clients()` clears it outright.
    """
    key = (host, model)
    loop = _running_loop()

    cached = _CLIENTS.get(key)
    if cached is not None:
        client, loop_ref = cached
        cached_loop = loop_ref() if isinstance(loop_ref, weakref.ref) else None
        if cached_loop is loop and not (loop is not None and loop.is_closed()):
            return client

    client = OllamaChatClient(host=host, model=model)
    _CLIENTS[key] = (client,
                     weakref.ref(loop) if loop is not None else None)
    return client


def reset_clients() -> None:
    """Drop every cached client. For tests and explicit reconfiguration."""
    _CLIENTS.clear()


__all__ = ["create_ollama_client", "reset_clients"]
