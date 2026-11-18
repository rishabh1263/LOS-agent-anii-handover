"""
THE CREDIT MCP SERVER -- the credit stage's read tools (app/mcp/credit.py) behind the real MCP protocol, apart from the
FOS case server (app/mcp/case_server.py) whose tools must never answer credit questions.

    stdio            `python -m app.mcp.credit_server`
    streamable HTTP  `python -m app.mcp.credit_server --http --port 8031`

AUTHENTICATED AND AUTHORISED HERE: every call carries the caller's signed token in `_meta.authorization`; it is
validated with the service's `validate_token`, and the caller must hold one of the credit scopes
(config/credit_underwriting/settings.yaml `scopes`) -- the same rule as POST /api/v1/credit/assess. Reads only.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.mcp import credit as capabilities
from app.mcp.errors import ToolStatus
from app.mcp.schemas import ToolEnvelope, ToolErrorInfo

logger = logging.getLogger(__name__)

server = FastMCP(
    "los-credit",
    instructions=("Read-only LOS credit-stage tools (the repayment report). Every call must carry the caller's "
                  "signed token in _meta.authorization."),
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MAX_BYTES = 512 * 1024             # the report with the vendor's sheets is ~25 KB; anything far larger is refused


def _refused(name: str, status: ToolStatus, code: str, message: str) -> dict[str, Any]:
    return ToolEnvelope(ok=False, capability=name, status=status,
                        error=ToolErrorInfo(code=code, message=message)).as_tool_payload()


def _credential(ctx: Context) -> str | None:
    meta = getattr(ctx.request_context, "meta", None)
    extra = getattr(meta, "model_extra", None) if meta is not None else None
    raw = (extra or {}).get("authorization") if isinstance(extra, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    token = raw.strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def _authorise(ctx: Context, name: str) -> dict[str, Any] | None:
    """None when the signed caller holds a credit scope (or the service read-all scope); else the refusal."""
    from app.agents.credit.repayment import config
    from app.security import access, auth

    if auth.auth_enabled():
        token = _credential(ctx)
        if token is None:
            return _refused(name, ToolStatus.FORBIDDEN, "UNAUTHENTICATED",
                            "An MCP call must carry the caller's signed credential.")
        try:
            claims = auth.validate_token(token)
        except Exception:  # noqa: BLE001 - any failure is the same refusal
            return _refused(name, ToolStatus.FORBIDDEN, "UNAUTHENTICATED",
                            "The caller's credential could not be verified.")
    else:
        claims = auth.local_claims()            # development only: the API's local identity
    held = set(auth.get_scopes(claims))
    if not (held & set(config.settings()["scopes"]) or access.is_service(held, write=False)):
        return _refused(name, ToolStatus.FORBIDDEN, "INSUFFICIENT_SCOPE",
                        "This tool needs one of: " + ", ".join(config.settings()["scopes"]) + ".")
    capabilities.CALLER_CLAIMS.set(dict(claims))       # a tool scoped by the caller's branches reads them here
    return None


async def _run(name: str, ctx: Context, case_id: str) -> dict[str, Any]:
    if name not in capabilities.READ_TOOLS:
        return _refused(name, ToolStatus.FORBIDDEN, "TOOL_NOT_ALLOWED", "This tool is not available over MCP.")
    if not _SAFE_ID.match(case_id or ""):
        return _refused(name, ToolStatus.INVALID_INPUT, "INVALID_INPUT", "case_id is not a valid identifier.")
    refused = _authorise(ctx, name)
    if refused is not None:
        logger.info("credit_mcp tool=%s authorized=false code=%s", name, refused["error"]["code"])
        return refused
    payload = (await capabilities.READ_TOOLS[name](case_id)).as_tool_payload()
    if len(json.dumps(payload, default=str)) > _MAX_BYTES:
        return _refused(name, ToolStatus.UNAVAILABLE, "RESULT_TOO_LARGE", "The tool result exceeded the permitted size.")
    return payload


def _by_case(name: str):
    async def tool(case_id: str, ctx: Context) -> dict[str, Any]:
        return await _run(name, ctx, case_id)
    return tool


for _name, _contract in capabilities.CONTRACTS.items():
    server.add_tool(_by_case(_name), name=_name, description=_contract.summary)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()                            # the API's JWKS / issuer / audience and store settings
    parser = argparse.ArgumentParser(description="LOS credit MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8031)
    args = parser.parse_args()
    if args.http:
        server.settings.host, server.settings.port = args.host, args.port
        server.run("streamable-http")
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
