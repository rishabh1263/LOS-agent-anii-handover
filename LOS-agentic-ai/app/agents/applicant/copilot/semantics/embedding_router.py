"""
THE EMBEDDING ROUTER (Phase 3 step 6b-tune-2) -- nearest labelled example, no LLM.

A question the rules could not read is compared with the example bank
(app/config/router_bank.yaml + evals/router_misses.yaml, the learning loop). When its
nearest examples agree on a tool and are close enough (`threshold`), that tool is
the answer in milliseconds; otherwise the caller asks Qwen (llm_router).

TWO VECTORISERS, both without a new dependency:
  char    character 3-5-gram hashing of the normalised text (typo-tolerant, any
          script, no model, ~0 RAM). Built here.
  ollama  the existing semantic model behind app/knowledge/embeddings.OllamaEmbedding
          (nomic-embed-text by default, ~0.3 GB in Ollama).

The bank is small (a few hundred vectors), so it is searched in memory: a store
round trip (Qdrant) would cost more than the search. Examples with a `state` are
eligible only when the previous turn had that intent ("kyu?" after a status answer).
No case data: the bank is wording, the context is labels.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[5]
BANK = ROOT / "app" / "config" / "router_bank.yaml"
MISSES = ROOT / "evals" / "router_misses.yaml"


@dataclass
class Match:
    tool: str
    args: dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    #: the best example of a DIFFERENT tool, for the agreement check
    runner_up: str | None = None
    runner_up_score: float = 0.0
    example: str = ""


def _config() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return (config.chatbot("router") or {}).get("embedding") or {}
    except Exception:  # noqa: BLE001
        return {}


def enabled() -> bool:
    return bool(_config().get("enabled", False))


def threshold() -> float:
    try:
        return float(_config().get("threshold", 0.6))
    except (TypeError, ValueError):
        return 0.6


def margin() -> float:
    """How far the best tool must beat the best OTHER tool to be answered without the model."""
    try:
        return float(_config().get("margin", 0.05))
    except (TypeError, ValueError):
        return 0.05


def confident(match: "Match | None") -> bool:
    return match is not None and match.score >= threshold() and match.score - match.runner_up_score >= margin()


def normalise(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[^\w\sऀ-ॿ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# vectorisers
# --------------------------------------------------------------------------

_DIM = 2048


def char_vector(text: str) -> list[float]:
    """Character 3-5-grams (word-boundary padded), hashed, sublinear, L2-normalised."""
    vector = [0.0] * _DIM
    for word in normalise(text).split():
        padded = f" {word} "
        for n in (3, 4, 5):
            for i in range(max(1, len(padded) - n + 1)):
                gram = padded[i:i + n]
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                vector[int.from_bytes(digest, "big") % _DIM] += 1.0
    vector = [math.log1p(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def _embedder(kind: str):
    if kind == "ollama":
        from app.knowledge.embeddings import OllamaEmbedding
        from app.llm.config import ollama_host

        model = str(_config().get("ollama_model") or "nomic-embed-text:latest")
        provider = OllamaEmbedding(url=ollama_host(), model=model, timeout=10)
        return lambda texts: provider.embed_all(texts)
    return lambda texts: [char_vector(t) for t in texts]


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


# --------------------------------------------------------------------------
# the bank
# --------------------------------------------------------------------------

def load_examples(include_misses: bool = True) -> list[dict[str, Any]]:
    import yaml

    out: list[dict[str, Any]] = []
    data = yaml.safe_load(BANK.read_text(encoding="utf-8")) or {}
    for tool, items in (data.get("examples") or {}).items():
        for item in items or []:
            out.append({"tool": str(tool), "text": str(item["text"]), "args": dict(item.get("args") or {}),
                        "state": item.get("state")})
    if include_misses and MISSES.exists():
        for item in (yaml.safe_load(MISSES.read_text(encoding="utf-8")) or {}).get("misses") or []:
            out.append({"tool": str(item["tool"]), "text": str(item["text"]), "args": dict(item.get("args") or {}),
                        "state": item.get("state"), "miss": True})
    return out


class Bank:
    """The examples and their vectors, built once per (vectoriser, misses) choice."""

    def __init__(self, kind: str = "char", include_misses: bool = True, vectors: dict | None = None) -> None:
        self.kind = kind
        self.examples = load_examples(include_misses)
        self._embed = _embedder(kind)
        texts = [e["text"] for e in self.examples]
        cached = vectors or {}
        missing = [t for t in texts if t not in cached]
        if missing:
            for text, vec in zip(missing, self._embed(missing)):
                cached[text] = vec
        self.vectors = [_norm(cached[t]) for t in texts]
        self.cache = cached

    def embed(self, text: str) -> list[float]:
        if text in self.cache:
            return _norm(self.cache[text])
        vec = self._embed([text])[0]
        self.cache[text] = vec
        return _norm(vec)

    def match(self, message: str, last_intent: str | None = None, *, tools: set[str] | None = None,
              k: int = 3) -> Match | None:
        """The nearest eligible examples' tool (k-NN, similarity-weighted vote)."""
        query = self.embed(message)
        scored = []
        for example, vec in zip(self.examples, self.vectors):
            if example.get("state") and example["state"] != last_intent:
                continue
            if tools is not None and example["tool"] not in tools:
                continue
            scored.append((sum(a * b for a, b in zip(query, vec)), example))
        if not scored:
            return None
        scored.sort(key=lambda s: s[0], reverse=True)
        votes: dict[str, float] = {}
        for score, example in scored[:k]:
            votes[example["tool"]] = votes.get(example["tool"], 0.0) + score
        tool = max(votes, key=votes.get)
        best_score, best = next((s, e) for s, e in scored if e["tool"] == tool)
        other = next(((s, e) for s, e in scored if e["tool"] != tool), (0.0, None))
        return Match(tool=tool, args=dict(best["args"]), score=round(best_score, 4),
                     runner_up=other[1]["tool"] if other[1] else None, runner_up_score=round(other[0], 4),
                     example=best["text"])


_BANKS: dict[tuple, Bank] = {}
_LOCK = threading.Lock()


def bank() -> Bank:
    key = (str(_config().get("vectoriser", "char")), bool(_config().get("include_misses", True)))
    with _LOCK:
        if key not in _BANKS:
            _BANKS[key] = Bank(kind=key[0], include_misses=key[1])
        return _BANKS[key]


def reload() -> None:
    with _LOCK:
        _BANKS.clear()


__all__ = ["Bank", "Match", "bank", "char_vector", "confident", "enabled", "load_examples", "margin", "normalise",
           "reload", "threshold"]
