"""
THE ANSWER ACT (2026-11-04) -- what KIND of answer a question wants, LEARNED from examples (config
copilot_answer_acts.yaml), never matched by words in code.

    message -> features = [ sentence embedding (nomic-embed-text, the meaning layer's model)  -- the meaning
                            | hashed word 1-2 grams + char 2-4 grams                         -- typos, Hinglish, Devanagari ]
            -> softmax classifier (models/answer_act.npz, scripts/train_answer_act.py)
            -> Act(name, confidence)   e.g. CONDITIONAL 0.91 for "after doing all this can it move to CPA?"

The intent (meaning.py / intents.py) says what the question is ABOUT; the act says how to ANSWER it: a yes / no first,
a because, a when, a list, a count. The answer composer (answering/fact_line.py) reads both.

FALLBACKS, NEVER AN ERROR: no model file, the embedding model unreachable or a different size -> the n-gram part alone
(trained alongside, saved in the same file); nothing loadable -> None, and every answer keeps its default shape.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[5]
CONFIG = ROOT / "app" / "config" / "copilot_answer_acts.yaml"
_LOCK = threading.RLock()
_MODEL: dict[str, Any] = {"mtime": None, "model": None}
_CACHE: dict[str, "Act | None"] = {}
_CACHE_MAX = 512


@dataclass(frozen=True)
class Act:
    name: str
    confidence: float
    by: str = "full"            # full (embedding + n-grams) / ngram (embedding unavailable)

    @property
    def sure(self) -> bool:
        return self.confidence >= float(_cfg().get("model", {}).get("min_confidence", 0.55))


# --------------------------------------------------------------------------------------------------------- config
_CFG: dict[str, Any] = {"mtime": None, "data": {}}


def _cfg() -> dict[str, Any]:
    import yaml

    try:
        mtime = CONFIG.stat().st_mtime
    except OSError:
        return {}
    if _CFG["mtime"] != mtime:
        _CFG.update(mtime=mtime, data=yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {})
    return _CFG["data"]


def examples() -> list[tuple[str, str]]:
    """(text, act) training pairs from the config."""
    return [(str(e), name) for name, spec in (_cfg().get("acts") or {}).items() for e in spec.get("examples") or []]


# ------------------------------------------------------------------------------------------------------- features
_TOKEN = re.compile(r"\w+", re.UNICODE)


def clean(text: str) -> str:
    return " ".join((text or "").lower().split())


def ngram_vector(text: str, dims: int) -> list[float]:
    """Hashed word 1-2 grams and char 2-4 grams (inside word boundaries), L2-normalised. Language-agnostic: the
    classifier learns which grams carry the act; nothing here knows any word."""
    import numpy as np

    t = clean(text)
    words = _TOKEN.findall(t)
    grams = [f"w:{w}" for w in words] + [f"b:{a}_{b}" for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"<{w}>"
        for n in (2, 3, 4):
            grams += [f"c:{padded[i:i + n]}" for i in range(max(len(padded) - n + 1, 0))]
    v = np.zeros(dims)
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "little")
        v[h % dims] += 1.0 if (h >> 63) & 1 else -1.0       # signed hashing: collisions cancel instead of pile up
    norm = float(np.linalg.norm(v))
    return list(v / norm) if norm else list(v)


def _embedder():
    try:
        from app.agents.applicant.copilot.semantics import meaning

        return meaning._embedder()
    except Exception:  # noqa: BLE001
        return None


def embed(text: str, embedder=None) -> list[float] | None:
    """The sentence embedding (unit length), or None when the model is unreachable."""
    embedder = embedder if embedder is not None else _embedder()
    if embedder is None:
        return None
    try:
        v = list(embedder.embed(clean(text)))
    except Exception:  # noqa: BLE001 - no embedding is the n-gram model, never an error
        logger.info("answer_act: embedding unavailable", exc_info=True)
        return None
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def features(text: str, *, embedding: list[float] | None, dims: int, emb_w: float, ng_w: float) -> list[float]:
    ng = [x * ng_w for x in ngram_vector(text, dims)]
    if embedding is None:
        return ng
    return [x * emb_w for x in embedding] + ng


# ---------------------------------------------------------------------------------------------------------- model
def _load():
    spec = _cfg().get("model") or {}
    path = ROOT / str(spec.get("path") or "models/answer_act.npz")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        if _MODEL["mtime"] != mtime:
            try:
                import numpy as np

                d = np.load(path, allow_pickle=False)
                _MODEL.update(mtime=mtime, model={
                    "full": (d["w_full"], d["b_full"]), "ngram": (d["w_ngram"], d["b_ngram"]),
                    "classes": [str(c) for c in d["classes"]], "temperature": float(d["temperature"]),
                    "dims": int(d["dims"]), "emb_w": float(d["emb_w"]), "ng_w": float(d["ng_w"]),
                    "emb_size": int(d["emb_size"])})
                _CACHE.clear()
            except Exception:  # noqa: BLE001
                logger.warning("answer_act: model %s unreadable", path, exc_info=True)
                _MODEL.update(mtime=mtime, model=None)
        return _MODEL["model"]


def _softmax(x, w, b, temperature):
    import numpy as np

    logits = (np.asarray(x) @ w + b) / temperature
    logits = logits - logits.max()
    p = np.exp(logits)
    return p / p.sum()


def classify(message: str, embedder=None) -> Act | None:
    """The act of this message, or None (no model). Cached per text."""
    key = clean(message)
    if not key:
        return None
    if key in _CACHE:
        return _CACHE[key]
    model = _load()
    if model is None:
        return None
    emb = embed(message, embedder)
    if emb is not None and len(emb) == model["emb_size"]:
        x = features(message, embedding=emb, dims=model["dims"], emb_w=model["emb_w"], ng_w=model["ng_w"])
        w, b = model["full"]
        by = "full"
    else:
        x = features(message, embedding=None, dims=model["dims"], emb_w=model["emb_w"], ng_w=model["ng_w"])
        w, b = model["ngram"]
        by = "ngram"
    p = _softmax(x, w, b, model["temperature"])
    i = int(p.argmax())
    act = Act(model["classes"][i], round(float(p[i]), 3), by)
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = act
    return act


def sure_act(message: str) -> str | None:
    """The act's name when the model is confident, else None (the answer keeps its default shape)."""
    act = classify(message)
    return act.name if act is not None and act.sure else None


__all__ = ["Act", "classify", "clean", "embed", "examples", "features", "ngram_vector", "sure_act"]
