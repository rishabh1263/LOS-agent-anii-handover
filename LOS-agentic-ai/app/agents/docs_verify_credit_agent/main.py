"""
The Credit Document Policy Agent on its own (development / integration testing):

    uvicorn app.agents.docs_verify_credit_agent.main:app --port 8020

Same route, same engine, same case store and the same JWT authentication as the LOS server (main.py), which also
mounts this router; nothing here is a second implementation.
"""

from __future__ import annotations

from dotenv import load_dotenv
from fastapi import Depends, FastAPI

load_dotenv()

from app.agents.docs_verify_credit_agent.api.routes import router  # noqa: E402
from app.security.auth import require_jwt  # noqa: E402

app = FastAPI(title="Credit Document Policy Agent", version="1.0.0")
app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}
