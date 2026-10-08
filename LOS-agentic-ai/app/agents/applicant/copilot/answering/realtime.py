"""
REAL-TIME CONVERSATION (MASTER SPEC section 11; config app/config/copilot_reply.yaml `realtime`, `changes`,
`greeting`). Rides on the reply contract (COPILOT_MD_TTS_CONTRACT).

    STREAM      `typing` at once -> ONE generic `status` only when slow -> `delta` chunks of the markdown (answer
                line, points, tables / links) -> `final` {request_id, markdown, tts}. `cancelled` when a newer
                message (or stop) for the same chat supersedes it.
    REPLAY      the final reply of a request, by request_id, for its own subject (reconnect).
    IDEMPOTENT  the same Idempotency-Key from the same subject within the window returns the stored reply.
    CHANGES     what changed on a case since this chat last looked (document statuses, from the store) -- said on
                open ("Since your last check: PAN verified.") and pushed by GET /copilot/updates for watched cases.
    GREETING    "hi" on a new chat: what needs attention across the caller's cases (the case-list counts).

In-process state (one worker; the open item says so). Nothing here changes data.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable

_LOCK = threading.Lock()
_ACTIVE: dict[tuple[str, str], str] = {}                          # (subject, chat) -> the newest request_id
_REPLAY: dict[str, tuple[float, str, dict[str, Any]]] = {}        # request_id -> (expiry, subject, reply)
_IDEMPOTENT: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_SEEN: dict[tuple[str, str, str], dict[str, str]] = {}            # (subject, chat, case) -> doc type -> status


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg()


def _rt() -> dict[str, Any]:
    return _cfg().get("realtime") or {}


def _say(value: Any, lang: str, seed: int = 0, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    picked = language_lock.pick(value, lang) if isinstance(value, dict) else value
    if isinstance(picked, list):
        picked = picked[seed % len(picked)] if picked else ""
    return str(picked or "").format(**values)


# --------------------------------------------------------------------------
# cancel / replay / idempotency
# --------------------------------------------------------------------------

def start(subject: str, chat_id: str | None, request_id: str) -> None:
    """This request is now the chat's current one: an older reply still streaming is cancelled."""
    with _LOCK:
        _ACTIVE[(subject, chat_id or "default")] = request_id


def stop(subject: str, chat_id: str | None) -> None:
    with _LOCK:
        _ACTIVE[(subject, chat_id or "default")] = "stopped"


def current(subject: str, chat_id: str | None, request_id: str) -> bool:
    with _LOCK:
        return _ACTIVE.get((subject, chat_id or "default"), request_id) == request_id


def keep(subject: str, reply: dict[str, Any], idempotency_key: str | None = None) -> None:
    now = time.time()
    with _LOCK:
        for rid in [r for r, v in _REPLAY.items() if v[0] < now]:
            _REPLAY.pop(rid, None)
        if reply.get("request_id"):
            _REPLAY[str(reply["request_id"])] = (now + float(_rt().get("replay_ttl_seconds", 600)), subject, reply)
        if idempotency_key:
            _IDEMPOTENT[(subject, idempotency_key)] = (now + float(_rt().get("idempotency_ttl_seconds", 600)), reply)


def replay(subject: str, request_id: str) -> dict[str, Any] | None:
    with _LOCK:
        found = _REPLAY.get(request_id)
    if not found or found[0] < time.time() or found[1] != subject:
        return None                                    # unknown, expired, or another subject's: the same None
    return found[2]


def idempotent(subject: str, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    with _LOCK:
        found = _IDEMPOTENT.get((subject, key))
    return found[1] if found and found[0] >= time.time() else None


# --------------------------------------------------------------------------
# the stream
# --------------------------------------------------------------------------

def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def chunks(markdown: str) -> list[str]:
    """The markdown in reading order: the answer line, then each point / paragraph, then a table whole."""
    out: list[str] = []
    table: list[str] = []
    for line in str(markdown or "").split("\n"):
        if line.startswith("|"):
            table.append(line)
            continue
        if table:
            out.append("\n".join(table) + "\n")
            table = []
        out.append(line + "\n")
    if table:
        out.append("\n".join(table) + "\n")
    if out:
        out[-1] = out[-1].rstrip("\n")
    return out


async def events(answer: Callable[[], Awaitable[Any]], *, subject: str, chat_id: str | None, request_id: str,
                 lang: str, atomic: bool = False) -> AsyncIterator[str]:
    """`atomic`: a blocked message (abuse_guard) -- no status event, the warning as ONE delta, never word by word."""
    from app.agents.applicant.copilot.answering import streaming

    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
    start(subject, chat_id, request_id)
    if _rt().get("typing_event", True):
        yield _event("typing", {"request_id": request_id, "ms": ms()})
    task = asyncio.ensure_future(answer())
    status_after = float(_rt().get("status_after_ms", 700)) / 1000.0
    said_status = False
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.1)
        if done:
            break
        if not current(subject, chat_id, request_id):
            task.cancel()
            yield _event("cancelled", {"request_id": request_id, "ms": ms()})
            return
        if not atomic and not said_status and (time.perf_counter() - started) >= status_after:
            said_status = True                          # one GENERIC line: it names nothing that is being read
            yield _event("status", {"request_id": request_id, "text": _say(_rt().get("status_text"), lang),
                                    "ms": ms()})
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001 - an HTTP refusal becomes an error event, never a dropped stream
        yield _event("error", {"request_id": request_id, "status": getattr(exc, "status_code", 500),
                               "detail": getattr(exc, "detail", None) or {"error": type(exc).__name__}})
        return
    if not isinstance(result, dict) or "markdown" not in result:
        # the old envelope (contract off): the earlier `answer` event
        async for line in streaming.events(lambda: _done(result), ""):
            if not line.startswith("event: status"):
                yield line
        return
    pause = float(_rt().get("delta_pause_ms", 15)) / 1000.0
    first_text_ms = None
    for piece in ([result["markdown"]] if atomic else chunks(result["markdown"])):
        if not current(subject, chat_id, request_id):
            yield _event("cancelled", {"request_id": request_id, "ms": ms()})
            return
        first_text_ms = first_text_ms if first_text_ms is not None else ms()
        yield _event("delta", {"request_id": request_id, "markdown": piece})
        if pause:
            await asyncio.sleep(pause)
    streaming.logger.info("copilot_stream request_id=%s first_text_ms=%s total_ms=%s", request_id, first_text_ms, ms())
    yield _event("final", {**result, "latency": {"first_text_ms": first_text_ms, "total_ms": ms()}})


async def _done(value: Any) -> Any:
    return value


# --------------------------------------------------------------------------
# changes since the last look
# --------------------------------------------------------------------------

def snapshot(case_id: str) -> dict[str, str]:
    """Document type -> status, now (superseded rows left out)."""
    from app.store import get_repository

    out: dict[str, str] = {}
    for d in get_repository().list_documents(case_id) or []:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper()
        if status != "SUPERSEDED":
            out[str(d.document_type).upper()] = status
    return out


def _diff(before: dict[str, str], after: dict[str, str], lang: str) -> list[str]:
    from app.agents.applicant.copilot.answering.answer import _readable

    words = (_cfg().get("changes") or {}).get("status_words") or {}
    out = []
    for doc, status in after.items():
        if before.get(doc) != status and status in words:
            out.append(f"{_readable(doc)} {_say(words[status], lang)}")
    return out


def since_last(subject: str, chat_id: str | None, case_id: str, lang: str) -> str | None:
    """The line said on open when something changed since this chat last looked at the case (then remembered)."""
    key = (subject, chat_id or "default", case_id)
    now = snapshot(case_id)
    with _LOCK:
        before = _SEEN.get(key)
        _SEEN[key] = now
    if before is None:
        return None
    changed = _diff(before, now, lang)
    if not changed:
        return None
    return _say((_cfg().get("changes") or {}).get("since_last"), lang, changes=", ".join(changed))


def watch(subject: str, chat_id: str | None, case_id: str) -> None:
    """Remember what this chat has seen on the case (an answer about it was just given)."""
    with _LOCK:
        _SEEN[(subject, chat_id or "default", case_id)] = snapshot(case_id)


def updates(subject: str, chat_id: str | None, lang: str) -> list[dict[str, Any]]:
    """A pushed message per watched case of this chat whose documents changed since it was last seen."""
    import uuid

    messages = []
    with _LOCK:
        watched = [(k, v) for k, v in _SEEN.items() if k[0] == subject and k[1] == (chat_id or "default")]
    for (s, c, case_id), before in watched:
        now = snapshot(case_id)
        changed = _diff(before, now, lang)
        if not changed:
            continue
        with _LOCK:
            _SEEN[(s, c, case_id)] = now
        from app.agents.applicant.copilot.answering import contract

        md = _say((_cfg().get("changes") or {}).get("update"), lang, case_id=case_id, changes=", ".join(changed))
        reply = {"request_id": f"upd_{uuid.uuid4().hex}", "markdown": md, "tts": contract.tts(md, lang)}
        keep(subject, reply)
        messages.append(reply)
    return messages


# --------------------------------------------------------------------------
# greeting
# --------------------------------------------------------------------------

def is_greeting(text: str) -> bool:
    import re

    words = re.sub(r"[^\w\sऀ-ॿ]", " ", str(text or "").lower()).split()
    phrases = {" ".join(str(p).lower().split()) for p in (_cfg().get("greeting") or {}).get("phrases") or []}
    joined = " ".join(words)
    return bool(words) and len(words) <= 3 and (joined in phrases or all(w in phrases or w in {"ji", "sir", "bhai"}
                                                                          for w in words))


def greeting(claims: dict[str, Any], lang: str, seed: int = 0) -> str:
    """Short: hello + what needs attention across the caller's cases (the case list's own counts)."""
    from app.agents.applicant.copilot.capabilities import case_list

    g = _cfg().get("greeting") or {}
    page = case_list.run(claims, case_list.ListQuery(size=1, with_counts=True))
    if page.all_total == 0:
        attention = _say(g.get("no_cases"), lang)
    else:
        need = {k: v for k, v in page.counts.items() if k in ("KYC", "DOCS")}
        if sum(need.values()):
            parts = ", ".join(case_list._name_of("part", k, lang, n=v) for k, v in need.items() if v)
            attention = _say(g.get("attention"), lang, n=sum(need.values()), total=page.all_total, parts=parts)
        else:
            attention = _say(g.get("all_clear"), lang, total=page.all_total)
    text = _say(g.get("text"), lang, seed, attention=attention)
    # MASTER SPEC 15.4: the notifications (cases pending past the configured days) -- the same list as the home
    from app.agents.applicant.copilot.capabilities import product_flow

    try:
        notes = product_flow.notifications(claims, lang) if page.all_total else []
    except Exception:  # noqa: BLE001 - the greeting never fails on a convenience line
        notes = []
    if notes:
        heading = product_flow.say((product_flow.cfg().get("notifications") or {}).get("heading"), lang)
        text += "\n\n" + heading + "\n" + "\n".join(f"- {n['text']}" for n in notes[:3])
    return text


__all__ = ["chunks", "events", "greeting", "idempotent", "is_greeting", "keep", "replay", "since_last", "snapshot",
           "start", "stop", "updates", "watch"]
