"""
The one place an Ollama client is constructed.

Host and model come from app.llm.config, so a deployment changes them in one
place rather than in every agent that happens to talk to a model.
"""

from __future__ import annotations

from functools import lru_cache

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
    return _client_for(ollama_host(), ollama_model())


@lru_cache(maxsize=4)
def _client_for(host: str, model: str) -> OllamaChatClient:
    """
    One client per host/model pair, built once.

    Constructing it was measured at 302 ms median -- on EVERY request, for a
    thin HTTP wrapper -- which was a third of the summary's entire latency
    budget spent before a single token was generated. The client holds no
    per-request state, so there is nothing to keep separate between calls.

    Keyed on host and model rather than cached as a single instance, so
    changing either through configuration still takes effect instead of
    silently serving the old one. `reset_clients()` clears it outright.
    """
    return OllamaChatClient(host=host, model=model)


def reset_clients() -> None:
    """Drop every cached client. For tests and explicit reconfiguration."""
    _client_for.cache_clear()


__all__ = ["create_ollama_client", "reset_clients"]
