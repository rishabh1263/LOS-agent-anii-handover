"""
A REQUEST-SCOPED READ MEMO -- one record read once per request.

Answering one question used to read the application record ten times: the
stage resolver, the party lookup, each workflow tool and the screen fill
each read it afresh, and nothing in between could have changed it. This
memo remembers a repository read for the duration of ONE request and hands
the same result back to every later caller in that request.

WHAT IT IS NOT. Not a cache across requests: the scope is opened by the
route and closed when the response is built, so the next request reads
live. Not a cache for writes: only the read methods named by the callers go
through it, and a route that writes does not open a scope. Not a change to
authorisation: the ownership checks read the repository directly and are
never served from here. Outside a scope every call is a plain live read.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

_CACHE: contextvars.ContextVar[dict[tuple[Any, ...], Any] | None] = contextvars.ContextVar(
    "repository_request_cache", default=None)


@contextmanager
def scoped() -> Iterator[None]:
    """A memo for the calling request; nested scopes share the outer one."""
    if _CACHE.get() is not None:
        yield
        return
    token = _CACHE.set({})
    try:
        yield
    finally:
        _CACHE.reset(token)


def active() -> bool:
    return _CACHE.get() is not None


def read(repository: Any, method: str, *args: Any) -> Any:
    """`repository.<method>(*args)`, read once per request inside a scope."""
    cache = _CACHE.get()
    if cache is None:
        return getattr(repository, method)(*args)
    key = (method, *args)
    if key not in cache:
        cache[key] = getattr(repository, method)(*args)
    return cache[key]


__all__ = ["active", "read", "scoped"]
