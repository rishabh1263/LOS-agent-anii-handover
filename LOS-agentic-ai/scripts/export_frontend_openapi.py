"""
Write docs/frontend/openapi.yaml: the app's own OpenAPI schema, filtered to what the frontend calls (login, the FOS
routes, the universal copilot, tts, readiness). Run after changing a route:

    python -m scripts.export_frontend_openapi
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ("/ready", "/api/v1/auth/", "/api/v1/fos/", "/api/v1/copilot/", "/api/v1/tts")


def main() -> None:
    os.environ.setdefault("LOS_MCP_MODE", "in_process")
    os.environ.setdefault("APPLICANT_AGENT_LLM_ENABLED", "false")
    from fastapi.testclient import TestClient

    import main as app_main

    schema = TestClient(app_main.app).get("/openapi.json").json()
    schema["paths"] = {p: v for p, v in sorted(schema.get("paths", {}).items()) if p.startswith(PREFIXES)}
    out = ROOT / "docs/frontend/openapi.yaml"
    out.write_text(yaml.safe_dump(schema, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"{out.relative_to(ROOT)}: {len(schema['paths'])} paths")


if __name__ == "__main__":
    main()
