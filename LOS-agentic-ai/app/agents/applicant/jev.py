"""
JEV: an optional semantic annotation layer. ADDITIVE ONLY.

WHAT IT MAY DO. Given the evidence packet the Copilot has already built
from authoritative records -- the question, the stage, the established
answer, the case evidence -- a JEV provider may return short NOTES:
a document inconsistency it noticed, an exception worth a reviewer's eye,
an ambiguity in the question. The notes are handed to the answer composer
beside the evidence, labelled as annotations.

WHAT IT MAY NOT DO. It cannot change the established answer, a status, a
decision or a verdict; it cannot add a tool call; it cannot see anything
the packet does not already contain. The composed answer is still checked
against the deterministic answer by `validate.check_composed`, so a note
that tempted the model into a new claim is rejected there.

OFF, OR ON WITH NOTHING REGISTERED, IT IS A NO-OP. `chatbot.jev.enabled`
(or JEV_ENABLED) switches it; `register()` installs a provider. A provider
that raises, times out or returns something malformed costs its notes and
nothing else -- the Copilot answers exactly as it would without JEV.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: At most this many notes, each at most this long, reach the composer.
MAX_NOTES = 3
MAX_NOTE_CHARS = 200


class Provider(Protocol):
    """Anything that can annotate a packet. Returns plain-text notes."""

    def annotate(self, packet: dict[str, Any]) -> list[str]: ...


_PROVIDER: Provider | None = None
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2,
                                              thread_name_prefix="jev")


def register(provider: Provider | None) -> None:
    """Install (or, with None, remove) the JEV provider."""
    global _PROVIDER
    _PROVIDER = provider


def active() -> bool:
    from app.agents.applicant import config

    return config.jev_enabled() and _PROVIDER is not None


def annotate(packet: dict[str, Any]) -> list[str]:
    """
    Notes for this packet, or [] -- never an exception, never a change to
    the packet itself (the provider is given a copy).
    """
    from app.agents.applicant import config

    if not active():
        return []
    provider = _PROVIDER
    try:
        future = _POOL.submit(provider.annotate, dict(packet))
        raw = future.result(timeout=config.jev_timeout_seconds())
    except concurrent.futures.TimeoutError:
        logger.info("JEV annotation timed out; answering without it")
        return []
    except Exception as exc:
        logger.info("JEV annotation failed (%s); answering without it",
                    type(exc).__name__)
        return []

    if not isinstance(raw, (list, tuple)):
        return []
    notes = [" ".join(str(n).split())[:MAX_NOTE_CHARS]
             for n in raw if isinstance(n, str) and n.strip()]
    return notes[:MAX_NOTES]


__all__ = ["MAX_NOTES", "Provider", "active", "annotate", "register"]
