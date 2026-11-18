"""
EVERY AGENT'S SUMMARY AS NUMBERED POINTS (owner 2026-11-02; config app/config/summary_format.yaml).

    numbered(text) -> "1. ...\\n2. ..."       the sentences (or lines, or existing bullets) of a summary, one per line
    numbered_list(points) -> ["1. ...", ...] the same for a summary that is already a list of points
    text_field(fn) / points_field(fn) / as_text(fn) / final_event(fn)
                                             decorators: the summary an agent RETURNS is numbered; how it was made and
                                             checked is untouched
A summary already numbered is returned as it is (never "1. 1. ...").
"""

from __future__ import annotations

import functools
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config" / "summary_format.yaml"
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def _sentences(text: str) -> list[str]:
    """Split on . ! ? followed by a space and a capital / digit -- never after a configured abbreviation."""
    abbreviations = {str(a).lower().rstrip(".") for a in settings().get("abbreviations") or []}
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s+[A-Z0-9\"'(])", text):
        word = re.search(r"([A-Za-z/.]+)$", text[start:m.start()])
        if word and word.group(1).lower().rstrip(".") in abbreviations:
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def _points(text: str) -> list[str]:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if len(lines) > 1:                                   # already one point per line (or bullets)
        return [_BULLET.sub("", ln) for ln in lines]
    return [_BULLET.sub("", s) for s in _sentences(str(text).strip())]


def _already_numbered(text: str) -> bool:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    return bool(lines) and all(re.match(rf"^{i}[.)]\s", ln) for i, ln in enumerate(lines, start=1))


def numbered(text: str | None) -> str | None:
    if not text or not settings().get("enabled", True) or _already_numbered(text):
        return text
    fmt = str(settings().get("point_format") or "{n}. {text}")
    return "\n".join(fmt.format(n=i, text=p) for i, p in enumerate(_points(text), start=1))


def numbered_list(points: list[str] | None) -> list[str] | None:
    if not points or not settings().get("enabled", True) or _already_numbered("\n".join(points)):
        return points
    fmt = str(settings().get("point_format") or "{n}. {text}")
    return [fmt.format(n=i, text=_BULLET.sub("", str(p)).strip()) for i, p in enumerate(points, start=1)]


def text_field(fn: Callable) -> Callable:
    """A function returning {"text": ...}: the text numbered."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        if isinstance(out, dict) and isinstance(out.get("text"), str):
            out = {**out, "text": numbered(out["text"])}
        return out
    return wrapper


def points_field(fn: Callable) -> Callable:
    """A function returning {"points": [...]}: the points numbered."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        if isinstance(out, dict) and isinstance(out.get("points"), list):
            out = {**out, "points": numbered_list(out["points"])}
        return out
    return wrapper


def as_text(fn: Callable) -> Callable:
    """A function returning the summary as a string: numbered."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        return numbered(out) if isinstance(out, str) else out
    return wrapper


def final_event(fn: Callable) -> Callable:
    """A generator of (kind, text) events: the "final" text numbered (the deltas stream as written)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for kind, text in fn(*args, **kwargs):
            yield (kind, numbered(text) if kind == "final" and isinstance(text, str) else text)
    return wrapper


__all__ = ["as_text", "final_event", "numbered", "numbered_list", "points_field", "settings", "text_field"]
