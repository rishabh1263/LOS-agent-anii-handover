"""
THE TONE OF A MESSAGE (2026-11-06) -- ABUSIVE / FRUSTRATED / NORMAL, LEARNED from examples (config copilot_tone.yaml),
never matched by a word list in code.

Same model shape as the answer act (semantics/answer_act.py, whose feature code it reuses): the sentence embedding
(nomic-embed-text) + hashed word / char n-grams -> a softmax classifier (models/tone.npz, trained by
scripts/train_answer_act.py --model tone). The abuse guard (capabilities/abuse_guard.py) asks `is_abusive()`: a
message the model is SURE is abusive gets the boundary reply even when no listed word is in it ("hatt saale?",
"chal be nikal"). Frustration is NOT abuse: it is answered as usual.

FALLBACKS, NEVER AN ERROR: no model file -> None (the lexicon alone decides, as before); the embedding model down ->
the n-gram half of the same file.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.applicant.copilot.semantics import answer_act as _aa

logger = logging.getLogger(__name__)

ROOT = _aa.ROOT
CONFIG = ROOT / "app" / "config" / "copilot_tone.yaml"
_LOCK = threading.RLock()
_MODEL: dict[str, Any] = {"mtime": None, "model": None}
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_CACHE: dict[str, "Tone | None"] = {}

ABUSIVE, FRUSTRATED, NORMAL = "ABUSIVE", "FRUSTRATED", "NORMAL"

# the trainer reads the same helpers as for the answer act
clean, embed, ngram_vector, features, _embedder = _aa.clean, _aa.embed, _aa.ngram_vector, _aa.features, _aa._embedder


@dataclass(frozen=True)
class Tone:
    name: str
    confidence: float
    by: str = "full"

    @property
    def sure(self) -> bool:
        spec = _cfg().get("model") or {}
        floor = spec.get("min_confidence_ngram", 0.9) if self.by == "ngram" else spec.get("min_confidence", 0.75)
        return self.confidence >= float(floor)


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
    return [(str(e), name) for name, spec in (_cfg().get("acts") or {}).items() for e in spec.get("examples") or []]


def _load():
    path = ROOT / str((_cfg().get("model") or {}).get("path") or "models/tone.npz")
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
                logger.warning("tone: model %s unreadable", path, exc_info=True)
                _MODEL.update(mtime=mtime, model=None)
        return _MODEL["model"]


def classify(message: str) -> Tone | None:
    key = clean(message)
    if not key:
        return None
    if key in _CACHE:
        return _CACHE[key]
    model = _load()
    if model is None:
        return None
    emb = embed(message)
    if emb is not None and len(emb) == model["emb_size"]:
        x = features(message, embedding=emb, dims=model["dims"], emb_w=model["emb_w"], ng_w=model["ng_w"])
        w, b, by = (*model["full"], "full")
    else:
        x = features(message, embedding=None, dims=model["dims"], emb_w=model["emb_w"], ng_w=model["ng_w"])
        w, b, by = (*model["ngram"], "ngram")
    p = _aa._softmax(x, w, b, model["temperature"])
    i = int(p.argmax())
    tone = Tone(model["classes"][i], round(float(p[i]), 3), by)
    with _LOCK:
        if len(_CACHE) >= 512:
            _CACHE.clear()
        _CACHE[key] = tone
    return tone


def is_abusive(message: str) -> bool:
    """The model is SURE this message is abusive (config min_confidence)."""
    tone = classify(message)
    return bool(tone is not None and tone.name == ABUSIVE and tone.sure)


__all__ = ["ABUSIVE", "FRUSTRATED", "NORMAL", "Tone", "classify", "examples", "is_abusive"]
