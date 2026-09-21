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
