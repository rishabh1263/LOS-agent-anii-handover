"""
THE CASE MCP SERVER -- the Copilot's read tools behind the real MCP protocol.

The Copilot's case capabilities (app/mcp/applicant.py, contracts in
app/mcp/contracts.py) were plain coroutines the agent awaited directly: typed
and governed, but no MCP message ever crossed a boundary. This server
registers the same READ tools with FastMCP, so a client reaches them through
`initialize` / `tools/list` / `tools/call` JSON-RPC, over any MCP transport:

    in-memory streams  -- same process, full protocol (app.mcp.runtime)
    stdio              -- `python -m app.mcp.case_server`
    streamable HTTP    -- `python -m app.mcp.case_server --http --port 8030`

READS ONLY. Writes keep their confirmation flow in the agent and are not
exposed here.

AUTHENTICATED AND AUTHORISED HERE, not trusted from the client. Every call
carries the caller's own SIGNED token in `_meta.authorization`; the server
validates it with the service's `validate_token` (RS256/JWKS, issuer,
audience, expiry) and takes the subject and scopes from the verified
claims -- never from an identity the request asserts. It then runs the
same `permissions.check_tool` and `access.authorize` the agent does, and
refuses -- fails closed -- when any of them says no.

NO NEW BUSINESS LOGIC. Each tool forwards to the existing capability, whose
envelope (ok / status / result / error) is returned unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.mcp import applicant as capabilities
from app.mcp.contracts import CONTRACTS
from app.mcp.errors import ToolStatus
from app.mcp.schemas import ToolEnvelope, ToolErrorInfo

logger = logging.getLogger(__name__)

server = FastMCP(
    "los-case",
    instructions=("Read-only LOS case tools for the Universal Copilot. Every "
                  "call must carry the caller's signed token in "
                  "_meta.authorization."),
)


def _refused(name: str, code: str, message: str) -> dict[str, Any]:
    return ToolEnvelope(ok=False, capability=name, status=ToolStatus.FORBIDDEN,
                        error=ToolErrorInfo(code=code, message=message)
                        ).as_tool_payload()


def _invalid(name: str, message: str) -> dict[str, Any]:
    return ToolEnvelope(ok=False, capability=name,
                        status=ToolStatus.INVALID_INPUT,
                        error=ToolErrorInfo(code="INVALID_INPUT",
                                            message=message)).as_tool_payload()


def _meta(ctx: Context) -> dict[str, Any]:
    meta = getattr(ctx.request_context, "meta", None)
    extra = getattr(meta, "model_extra", None) if meta is not None else None
    return extra if isinstance(extra, dict) else {}


def _credential(ctx: Context) -> str | None:
    """The caller's signed token, as the client forwarded it."""
    raw = _meta(ctx).get("authorization")
    if not isinstance(raw, str) or not raw.strip():
        return None
    token = raw.strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def allowed_tools() -> frozenset[str]:
    """
    THE SERVER'S OWN ALLOWLIST: the read contracts, narrowed by
    configuration (`mcp.allowed_tools`) and never widened by it. A tool not
    in it is not registered -- the SDK refuses the call -- and `_run`
    refuses it again, so a registration mistake cannot expose one.
    """
    from app.agents.applicant import config

    reads = frozenset(capabilities.READ_TOOLS)
    narrowed = config.mcp_allowed_tools()
    return reads if narrowed is None else reads & narrowed


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_TYPE = re.compile(r"^[A-Za-z0-9_ ]{1,64}$")


def _authorise(ctx: Context, name: str, *, applicant_id: str | None,
               case_id: str | None) -> dict[str, Any] | None:
    """
    None when the caller may make this call; else the refusal envelope.

    WHO THE CALLER IS COMES FROM THE SIGNED TOKEN, AND ONLY FROM IT. The
    token is validated here with the service's own `validate_token` --
    signature against the IdP's JWKS, algorithm, kid, issuer, audience,
    expiry -- and the subject and scopes are read from the verified claims.
    Anything else in `_meta` that claims an identity (a `caller` dict, a
    `caller_id`) is ignored: an assertion the client can write is not an
    authentication. Then the same scope check and the same ownership check
    the agent runs, so this server never relies on the client having
    authorised the request.
    """
    from app.agents.applicant import permissions
    from app.security import access, auth

    token = _credential(ctx)
    if not auth.auth_enabled():
        # AUTHENTICATION OFF (development only, auth.auth_enabled): the
        # same local identity the API uses. Scope and ownership below run
        # unchanged.
        claims = auth.local_claims()
    elif token is None:
        return _refused(name, "UNAUTHENTICATED",
                        "An MCP call must carry the caller's signed credential.")
    else:
        try:
            claims = auth.validate_token(token)
        except Exception:
            return _refused(name, "UNAUTHENTICATED",
                            "The caller's credential could not be verified.")
    subject = auth.get_subject(claims)
    if not subject:
        return _refused(name, "UNAUTHENTICATED",
                        "The caller's credential names no subject.")
    caller = permissions.Caller(subject=subject,
                                scopes=frozenset(auth.get_scopes(claims)),
                                roles=frozenset(auth.get_roles(claims)))
    try:
        permissions.check_tool(caller, name)
    except permissions.PermissionDenied as denied:
        return _refused(name, denied.code, denied.message)
    try:
        access.authorize(caller.subject, caller.scopes,
                         applicant_id=applicant_id or None,
                         case_id=case_id or None, write=False)
    except access.AccessDenied as denied:
        return _refused(name, "CASE_ACCESS_DENIED", denied.message)
    return None


def _bounded(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A result no larger than the boundary carries (`mcp.max_result_bytes`)."""
    from app.agents.applicant import config

    size = len(json.dumps(payload, default=str))
    if size > config.mcp_max_result_bytes():
        logger.warning("mcp_server tool=%s result_too_large bytes=%d", name, size)
        return ToolEnvelope(ok=False, capability=name,
                            status=ToolStatus.UNAVAILABLE,
                            error=ToolErrorInfo(
                                code="RESULT_TOO_LARGE",
                                message="The tool result exceeded the "
                                        "permitted size.")).as_tool_payload()
    return payload


async def _run(name: str, ctx: Context, *, applicant_id: str | None = None,
               case_id: str | None = None, document_type: str | None = None
               ) -> dict[str, Any]:
    if name not in allowed_tools():
        return _refused(name, "TOOL_NOT_ALLOWED",
                        "This tool is not available over MCP.")
    # TYPED, BOUNDED INPUT, checked before anything is read.
    for label, value in (("applicant_id", applicant_id), ("case_id", case_id)):
        if value is not None and not _SAFE_ID.match(value):
            return _invalid(name, f"{label} is not a valid identifier.")
    if document_type and not _SAFE_TYPE.match(document_type):
        return _invalid(name, "document_type is not a valid document type.")

    refused = _authorise(ctx, name, applicant_id=applicant_id, case_id=case_id)
    if refused is not None:
        logger.info("mcp_server tool=%s authorized=false code=%s", name,
                    refused["error"]["code"])
        return refused

    handler = capabilities.READ_TOOLS[name]
    if name in ("applicant.get", "applications.list"):
        envelope = await handler(applicant_id)
    elif name == "documents.verification":
        envelope = await handler(case_id, document_type or "")
    else:
        envelope = await handler(case_id)
    return _bounded(name, envelope.as_tool_payload())


# ==========================================================================
# REGISTRATION -- one MCP tool per READ contract, with the contract's own
# name and description. The argument shape follows the contract's schema.
# ==========================================================================

def _by_applicant(name: str):
    async def tool(applicant_id: str, ctx: Context) -> dict[str, Any]:
        return await _run(name, ctx, applicant_id=applicant_id)
    return tool


def _by_case(name: str):
    async def tool(case_id: str, ctx: Context) -> dict[str, Any]:
        return await _run(name, ctx, case_id=case_id)
    return tool


def _by_case_and_document(name: str):
    async def tool(case_id: str, document_type: str,
                   ctx: Context) -> dict[str, Any]:
        return await _run(name, ctx, case_id=case_id,
                          document_type=document_type)
    return tool


for _name in sorted(allowed_tools()):
    _contract = CONTRACTS[_name]
    _fields = set(_contract.input_schema.get("properties") or ())
    if "applicant_id" in _fields and "case_id" not in _fields:
        _fn = _by_applicant(_name)
    elif "document_type" in _fields:
        _fn = _by_case_and_document(_name)
    else:
        _fn = _by_case(_name)
    server.add_tool(_fn, name=_name, description=_contract.summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="LOS case MCP server")
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8030)
    args = parser.parse_args()
    if args.http:
        server.settings.host = args.host
        server.settings.port = args.port
        server.run("streamable-http")
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
