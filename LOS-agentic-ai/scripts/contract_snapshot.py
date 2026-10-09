"""
Write frontend_handoff/contract_snapshot.json -- the frontend contract as the code defines it NOW.

    python scripts/contract_snapshot.py

Run it ONLY together with bumping `contract_version` (app/config/copilot_reply.yaml) and updating
frontend_handoff/API_CONTRACT.md + contract.ts. tests/integration/test_api_contract.py compares the live app with it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: the request schemas the frontend sends (OpenAPI components)
REQUEST_SCHEMAS = ("CopilotRequest", "ActionRequest", "LoginRequest", "RefreshRequest", "_ChatRef")
#: the chat endpoints the frontend calls
PATHS = ("/api/v1/fos/copilot", "/api/v1/fos/copilot/stream", "/api/v1/fos/action", "/api/v1/fos/copilot/stop",
         "/api/v1/auth/login", "/api/v1/auth/refresh")


def schema_hash(schema: dict) -> str:
    """Only what the client depends on: property names, types, enums, required."""
    props = {k: {kk: v.get(kk) for kk in ("type", "enum", "anyOf", "$ref", "items") if kk in v}
             for k, v in (schema.get("properties") or {}).items()}
    return hashlib.sha256(json.dumps({"p": props, "r": sorted(schema.get("required") or [])},
                                     sort_keys=True).encode()).hexdigest()[:16]


def current() -> dict:
    import main
    from app.agents.applicant.copilot.answering import contract

    spec = main.app.openapi()
    schemas = spec["components"]["schemas"]
    return {
        "contract_version": contract.version(),
        "reply_keys": ["markdown", "request_id", "tts"],
        "error_keys": ["code", "message", "request_id", "retryable"],
        "action_result_types": ["copy", "open_ui", "reply", "upload"],
        "sse_events": ["cancelled", "delta", "error", "final", "status", "typing"],
        "paths": {p: sorted(spec["paths"].get(p, {})) for p in PATHS},
        "request_schemas": {name: schema_hash(schemas[name]) for name in REQUEST_SCHEMAS if name in schemas},
    }


if __name__ == "__main__":
    out = ROOT / "frontend_handoff" / "contract_snapshot.json"
    out.write_text(json.dumps(current(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
