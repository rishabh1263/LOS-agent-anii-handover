"""
How often the model was actually called. For tests, never for a caller.

WHY THIS IS COUNTED RATHER THAN ASSUMED. "The model is not called for simple
questions" is a claim about behaviour, and the only honest way to hold a
system to it is to count. A live run showed a mixed question taking 2773 ms
against a 3 ms baseline -- the model was being called, timing out, and
falling back, and every response field still looked correct. Nothing in the
public response says a model ran, so nothing in the public response could
have caught it.

NOT EXPOSED. These counters are process-wide and say nothing about one
request; publishing them would leak how the service is configured and invite
a caller to depend on numbers that mean nothing to them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

_LOCK = threading.Lock()


@dataclass
class Counts:
    called: int = 0
    not_called: int = 0

    @property
    def total(self) -> int:
        return self.called + self.not_called


_COUNTS = Counts()


def record(*, called: bool) -> None:
    """Note one decision about whether to call the model."""
    with _LOCK:
        if called:
            _COUNTS.called += 1
        else:
            _COUNTS.not_called += 1


def snapshot() -> Counts:
    with _LOCK:
        return Counts(called=_COUNTS.called, not_called=_COUNTS.not_called)


def reset() -> None:
    """Start counting again. Tests call this; nothing else should."""
    global _COUNTS
    with _LOCK:
        _COUNTS = Counts()


__all__ = ["Counts", "record", "reset", "snapshot"]
