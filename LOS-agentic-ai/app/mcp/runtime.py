"""
THE COPILOT'S MCP CLIENT RUNTIME -- how a case tool call is carried.

    mode = in_process   the capability coroutine is awaited directly. No
                        MCP message exists; the trace says `transport:
                        in_process` and nothing calls that MCP.
    mode = protocol     a real MCP client session (mcp.ClientSession)
                        sends `tools/call` JSON-RPC to the case MCP server
                        (app/mcp/case_server.py) over the configured
                        transport -- memory, stdio or streamable HTTP --
                        and the server re-authorises the caller.

ONE SESSION, REUSED. A session is opened once per transport and kept on a
dedicated thread with its own event loop; request handlers hand calls to it
with `run_coroutine_threadsafe`. Opening a session per question would pay
the `initialize` handshake -- and, for stdio, a Python process start -- on
every request.

THE CALLER IS PROVED, NOT ASSERTED. Each call carries the caller's own
signed token in `_meta.authorization`, and the server re-validates it
(app/mcp/case_server.py). No identity the client could simply write -- a
`caller` dict, a `caller_id` -- is sent, and the server would ignore one.

EVERY CALL IS TRACED: request, case, caller subject, stage, intent, tool,
provider, transport, whether it was authorised, timed out or fell back,
start, total / server / transport milliseconds, status and error code --
and handed to any registered listener (`add_listener`), the hook the
observability slice attaches to. Never the token, never argument values
beyond ids, never the result.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.mcp.contracts import CONTRACTS
from app.mcp.errors import ToolStatus
from app.mcp.schemas import ToolEnvelope, ToolErrorInfo

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ==========================================================================
# THE SESSION THREAD
# ==========================================================================

class _Session:
    """One MCP client session on its own thread and event loop."""

    def __init__(self, transport: str, server_url: str) -> None:
        self.transport = transport
        self.server_url = server_url
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session = None
        self.error: BaseException | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"mcp-{transport}")
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # pragma: no cover - reported via error
            self.error = self.error or exc
            self._ready.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            async with self._connect() as session:
                self.session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self.error = exc
            self.session = None
        finally:
            self._ready.set()

    @asynccontextmanager
    async def _connect(self):
        from mcp import ClientSession

        if self.transport == "memory":
            from mcp.shared.memory import create_connected_server_and_client_session

            from app.mcp.case_server import server

            async with create_connected_server_and_client_session(server) as session:
                yield session
        elif self.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=sys.executable, args=["-m", "app.mcp.case_server"],
                env=dict(os.environ), cwd=str(_PROJECT_ROOT))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self.server_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout) and self.session is not None

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and self.session is not None

    async def call(self, name: str, arguments: dict[str, Any],
                   meta: dict[str, Any], timeout: float):
        future = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(
                name, arguments,
                read_timeout_seconds=timedelta(seconds=timeout), meta=meta),
            self.loop)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout + 1.0)

    def close(self) -> None:
        if self.loop is not None and self._stop is not None:
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        self._thread.join(timeout=5)


_sessions: dict[tuple[str, str], _Session] = {}
_lock = threading.Lock()


def _session() -> _Session:
    from app.agents.applicant import config

    key = (config.mcp_transport(), config.mcp_server_url())
    with _lock:
        existing = _sessions.get(key)
        if existing is not None and existing.alive:
            return existing
        if existing is not None:
            existing.close()
        created = _Session(*key)
        _sessions[key] = created
    if not created.wait_ready(config.mcp_connect_timeout_seconds()):
        raise ConnectionError(
            f"MCP {key[0]} session unavailable: "
            f"{type(created.error).__name__ if created.error else 'timeout'}")
    return created


def reset() -> None:
    """Close every session (tests, configuration changes, shutdown)."""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


atexit.register(reset)


# ==========================================================================
# ONE TOOL CALL
# ==========================================================================

def mode() -> str:
    from app.agents.applicant import config

    return config.mcp_mode()


def _arguments(name: str, applicant_id: str | None, case_id: str | None,
               document_type: str | None) -> dict[str, Any]:
    fields = set((CONTRACTS[name].input_schema.get("properties") or {}))
    args: dict[str, Any] = {}
    if "applicant_id" in fields and applicant_id is not None:
        args["applicant_id"] = applicant_id
    if "case_id" in fields and case_id is not None:
        args["case_id"] = case_id
    if "document_type" in fields:
        args["document_type"] = document_type or ""
    return args


def _timed_out(exc: BaseException) -> bool:
    """Our own wait, or the SDK's read timeout (an McpError carrying 408)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    error = getattr(exc, "error", None)
    return getattr(error, "code", None) == 408 or "timed out" in str(exc).lower()


def _malformed(name: str, detail: str) -> ToolEnvelope:
    return ToolEnvelope(
        ok=False, capability=name, status=ToolStatus.UNAVAILABLE,
        error=ToolErrorInfo(code="MCP_MALFORMED_RESULT",
                            message="The MCP tool returned an unusable result.",
                            context={"detail": detail[:120]}))


def _envelope_from(name: str, result) -> ToolEnvelope:
    """
    The server's envelope, back out of the MCP CallToolResult -- or a
    refusal of it. A result that is too large, is not an envelope, or is an
    envelope for a DIFFERENT tool is rejected rather than trusted.
    """
    from app.agents.applicant import config

    limit = config.mcp_max_result_bytes()
    for block in getattr(result, "content", None) or ():
        if len(getattr(block, "text", "") or "") > limit:
            return _malformed(name, "result exceeded the permitted size")
    payload = getattr(result, "structuredContent", None)
    if not payload:
        for block in getattr(result, "content", None) or ():
            text = getattr(block, "text", None)
            if text:
                try:
                    payload = json.loads(text)
                    break
                except ValueError:
                    payload = None
    if getattr(result, "isError", False) or not isinstance(payload, dict) \
            or "ok" not in payload:
        # Schema validation and unknown tools are reported by the SDK as an
        # error result with a text message.
        message = ""
        for block in getattr(result, "content", None) or ():
            message = getattr(block, "text", "") or message
        return ToolEnvelope(
            ok=False, capability=name, status=ToolStatus.INVALID_INPUT,
            error=ToolErrorInfo(code="MCP_TOOL_ERROR",
                                message="The MCP server rejected the call.",
                                context={"detail": message[:200]}))
    try:
        envelope = ToolEnvelope.model_validate(payload)
    except Exception:
        return _malformed(name, "result is not a tool envelope")
    if envelope.capability != name:
        return _malformed(name, "result names a different tool")
    return envelope


#: Telemetry listeners: each receives the trace dict of every call (no
#: credentials, no results). The observability slice registers here.
_listeners: list = []


def add_listener(listener) -> None:
    """Receive every MCP call trace. A listener that raises is ignored."""
    _listeners.append(listener)


def remove_listener(listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def _emit(trace: dict[str, Any]) -> None:
    for listener in list(_listeners):
        try:
            listener(dict(trace))
        except Exception:  # pragma: no cover - a listener never breaks a call
            logger.debug("MCP telemetry listener failed", exc_info=True)


async def call(
    name: str,
    *,
    applicant_id: str | None,
    case_id: str | None,
    document_type: str | None,
    caller,
    request_id: str | None = None,
    stage: str | None = None,
    intent: str | None = None,
) -> tuple[ToolEnvelope, dict[str, Any]]:
    """One tool call, inside an `mcp.tool` span (tool, transport, outcome)."""
    from app.observability.tracing import annotate, span

    with span("mcp.tool", tool=name, stage=stage, intent=intent,
              request_id=request_id) as current:
        envelope, trace = await _call(
            name, applicant_id=applicant_id, case_id=case_id,
            document_type=document_type, caller=caller, request_id=request_id,
            stage=stage, intent=intent)
        annotate(current, protocol=trace.get("protocol"),
                 transport=trace.get("transport"), status=trace.get("status"),
                 authorized=trace.get("authorized"), ok=trace.get("ok"),
                 error_code=trace.get("error"), timed_out=trace.get("timed_out"),
                 duration_ms=trace.get("duration_ms"),
                 transport_ms=trace.get("transport_ms"))
        return envelope, trace


async def _call(
    name: str,
    *,
    applicant_id: str | None,
    case_id: str | None,
    document_type: str | None,
    caller,
    request_id: str | None = None,
    stage: str | None = None,
    intent: str | None = None,
) -> tuple[ToolEnvelope, dict[str, Any]]:
    """
    Carry one read-tool call over the configured MCP mode.

    Returns the tool's envelope and the trace record for it. Never raises:
    an unreachable server, a timeout or a protocol error becomes a failed
    envelope, exactly as a failing capability already does.
    """
    from app.agents.applicant import config
    from app.mcp import applicant as capabilities

    contract = CONTRACTS.get(name)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    trace: dict[str, Any] = {
        "tool": name, "provider": contract.provider if contract else None,
        "mode": config.mcp_mode(), "transport": "in_process",
        "request_id": request_id, "case_id": case_id,
        "caller": getattr(caller, "subject", None), "stage": stage,
        "intent": intent, "started_at": started_at,
    }

    async def direct() -> ToolEnvelope:
        handler = capabilities.READ_TOOLS[name]
        if name in ("applicant.get", "applications.list"):
            return await handler(applicant_id)
        if name == "documents.verification":
            return await handler(case_id, document_type or "")
        return await handler(case_id)

    if config.mcp_mode() != "protocol" or name not in capabilities.READ_TOOLS:
        envelope = await direct()
    else:
        trace["transport"] = config.mcp_transport()
        # THE CALLER'S OWN SIGNED TOKEN, which the server validates. Not a
        # description of the caller: a description is something anyone can
        # write. Absent (a caller with no bearer credential), the server
        # refuses -- this client does not invent one.
        meta: dict[str, Any] = {"request_id": request_id, "stage": stage,
                                "intent": intent}
        credential = getattr(caller, "credential", None)
        if credential:
            meta["authorization"] = f"Bearer {credential}"
        try:
            session = _session()
            result = await session.call(
                name, _arguments(name, applicant_id, case_id, document_type),
                meta, config.mcp_call_timeout_seconds())
            envelope = _envelope_from(name, result)
            trace["protocol"] = "mcp"
        except Exception as exc:
            if _timed_out(exc):
                # A slow tool is not an unreachable server: the session is
                # kept, and the trace says it timed out.
                trace["timed_out"] = True
                envelope = ToolEnvelope(
                    ok=False, capability=name, status=ToolStatus.UNAVAILABLE,
                    error=ToolErrorInfo(code="MCP_TIMEOUT",
                                        message="The MCP tool did not answer "
                                                "in time."))
            elif config.mcp_fallback() == "in_process":
                logger.warning("MCP %s unavailable (%s); running %s in process.",
                               trace["transport"], type(exc).__name__, name)
                trace["transport"] = "in_process"
                trace["fallback"] = type(exc).__name__
                envelope = await direct()
            else:
                envelope = ToolEnvelope(
                    ok=False, capability=name, status=ToolStatus.UNAVAILABLE,
                    error=ToolErrorInfo(code="MCP_UNAVAILABLE",
                                        message="The MCP tool server could not "
                                                "be reached.",
                                        context={"error_type": type(exc).__name__}))

    code = envelope.error.code if envelope.error else None
    trace.update({
        # Refused by the server's own authorisation (FORBIDDEN), or by the
        # permission codes it passes through.
        "authorized": not (envelope.status is ToolStatus.FORBIDDEN
                           or code in {"INSUFFICIENT_SCOPE", "CASE_NOT_ACCESSIBLE",
                                       "CASE_ACCESS_DENIED", "CALLER_REQUIRED",
                                       "UNAUTHENTICATED", "TOOL_NOT_ALLOWED"}),
        "status": envelope.status.value if hasattr(envelope.status, "value")
        else str(envelope.status),
        "ok": envelope.ok,
        "error": code,
        "timed_out": bool(trace.get("timed_out")),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    })
    # WHERE THE TIME WENT, over the protocol: the tool's own time as the
    # server measured it, and everything else -- serialisation, transport,
    # the session hop -- as transport time.
    if trace.get("protocol") == "mcp":
        server_ms = round(float(envelope.processing_ms or 0.0), 2)
        trace["server_ms"] = server_ms
        trace["transport_ms"] = round(max(0.0, trace["duration_ms"] - server_ms), 2)
    logger.info("mcp_call %s", json.dumps(trace, default=str))
    _emit(trace)
    return envelope, trace


# ==========================================================================
# READINESS
# ==========================================================================

def health(*, probe: bool = True) -> dict[str, Any]:
    """
    Whether the configured MCP path can serve, for /ready and startup.

        NOT_REQUIRED   in_process mode: no MCP server is used (local default)
        MISCONFIGURED  the configuration, as written, is invalid
        AVAILABLE      protocol mode: a session is open and the server lists
                       every allowed tool
        UNAVAILABLE    protocol mode: the server cannot be reached, or does
                       not expose what it should

    `fallback` says what a request does when this is not AVAILABLE --
    `in_process` degrades, `fail` fails. Never raises.
    """
    from app.agents.applicant import config

    report: dict[str, Any] = {"mode": config.mcp_mode(),
                              "transport": config.mcp_transport(),
                              "fallback": config.mcp_fallback()}
    problems = config.mcp_config_errors()
    if problems:
        return {**report, "status": "MISCONFIGURED", "problems": problems}
    if config.mcp_mode() != "protocol":
        return {**report, "status": "NOT_REQUIRED"}
    if not probe:
        return {**report, "status": "UNKNOWN"}
    try:
        from app.mcp.case_server import allowed_tools

        session = _session()
        listed = asyncio.run_coroutine_threadsafe(
            session.session.list_tools(), session.loop).result(
                timeout=config.mcp_connect_timeout_seconds())
        names = {tool.name for tool in getattr(listed, "tools", []) or []}
        missing = sorted(allowed_tools() - names)
        if missing:
            return {**report, "status": "UNAVAILABLE",
                    "problems": [f"server does not expose: {', '.join(missing)}"]}
        return {**report, "status": "AVAILABLE", "tools": sorted(names)}
    except Exception as exc:
        return {**report, "status": "UNAVAILABLE",
                "problems": [f"session unavailable: {type(exc).__name__}"]}


__all__ = ["add_listener", "call", "health", "mode", "remove_listener", "reset"]
