import os


def ollama_host() -> str:
    return os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")


def ollama_model() -> str:
    """
    The model every agent talks to. `OLLAMA_MODEL` overrides it.

    THE DEFAULT MUST NAME A MODEL THAT IS ACTUALLY PULLED. It was
    `qwen3:8b`, which is not installed on the reference host:

        POST /api/generate {"model": "qwen3:8b"}
          -> {"error": "model 'qwen3:8b' not found"}   (404)

    Nothing broke, because every caller falls back to deterministic
    text -- which is exactly why it went unnoticed. The LOS summary
    quietly took the fallback on every request and paid ~0.9s per
    cooldown window for the failing probe first.
    """
    return os.getenv("OLLAMA_MODEL", "qwen2.5:3b")


def ollama_num_ctx() -> int:
    """
    The context window EVERY call asks for (`OLLAMA_NUM_CTX`, default 2048; 0 = Ollama's own).

    ONE VALUE FOR ALL CALLERS. Ollama reloads the model when a request asks for
    a different context size than the loaded one, and a reload is the ~4.5 s
    cold start. Loaded with no option, qwen2.5:3b came up at 4096 here
    (`ollama ps`, 2026-10-07); 2048 halves the KV cache. A prompt longer than the
    window is cut from the front by Ollama, so callers log when they get close
    (app/llm/provider.py).
    """
    try:
        return max(0, int(os.getenv("OLLAMA_NUM_CTX") or 2048))
    except ValueError:
        return 2048


def with_num_ctx(options: dict | None) -> dict:
    """`options` plus the shared context size, unless the caller set its own."""
    merged = dict(options or {})
    if ollama_num_ctx() and "num_ctx" not in merged:
        merged["num_ctx"] = ollama_num_ctx()
    return merged
