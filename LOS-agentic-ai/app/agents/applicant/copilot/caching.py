"""
IN-PROCESS TTL CACHES for the copilot (Phase 3 step 6b) -- no Redis, no new dependency.

Two users: the router's decisions (normalised question + last-turn labels ->
tool choice) and knowledge answers (normalised question + language + the
knowledge state -> the handbook answer). NEITHER HOLDS CASE DATA: a router
decision is a tool name, a knowledge answer is handbook text that is the same
for every caller. Bounded (LRU) and expiring; a hit returns a deep copy, so a
caller that edits its result cannot change the cached one.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class TTLCache:
    def __init__(self, settings: Callable[[], tuple[float, int]]) -> None:
        #: () -> (ttl_seconds, max_entries), read on every use so config changes apply
        self._settings = settings
        self._data: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Any | None:
        ttl, _ = self._settings()
        with self._lock:
            found = self._data.get(key)
            if found is None or ttl <= 0 or time.monotonic() - found[0] > ttl:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(found[1])

    def put(self, key: Any, value: Any) -> None:
        ttl, size = self._settings()
        if ttl <= 0 or size <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic(), copy.deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = 0

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._data)}


def settings_from(section: str, key: str, default_ttl: float = 600.0,
                  default_size: int = 512) -> Callable[[], tuple[float, int]]:
    """Read (ttl_seconds, max_entries) from applicant_agent.yaml: chatbot.<section>.<key>."""
    def read() -> tuple[float, int]:
        try:
            from app.agents.applicant import config

            block = (config.chatbot(section) or {}).get(key) or {}
            return float(block.get("ttl_seconds", default_ttl)), int(block.get("max_entries", default_size))
        except Exception:  # noqa: BLE001
            return default_ttl, default_size
    return read


__all__ = ["TTLCache", "settings_from"]
