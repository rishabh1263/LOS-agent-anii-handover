"""
The Bank Verification Agent on its own (the credit agent's bank verification step, demo on synthetic data):

    python -m uvicorn app.agents.credit.bank_verification.main:app --port 8041

API docs at /docs. When the frontend has been built (frontend/dist), it is served at / as well. The first start seeds
the demo cases if the store is empty. The LOS server (main.py) is not changed by this module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

# THE SAME .env AS THE LOS SERVER (2026-11-06): started on its own (port 8041) this app never read .env, so the store
# had no LOS_AGENTIC_DSN and every lookup was a 503 STORE_NOT_CONFIGURED. A variable already set wins (never overridden).
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[4] / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agents.credit.bank_verification.api import repository, router
from app.agents.credit.bank_verification.config import demo_mode, section
from app.agents.credit.bank_verification.services.verification_service import ServiceError

DIST = Path(__file__).resolve().parent / "frontend" / "dist"


def install_errors(app: FastAPI) -> None:
    """Every error in one shape: {"error": {"code", "message", "details"}}."""

    @app.exception_handler(ServiceError)
    async def _service(_: Request, exc: ServiceError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                            status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
        return JSONResponse({"error": {"details": None, **detail}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "The request is not valid.",
                                       "details": errors}}, status_code=422)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    """The first start seeds the demo cases when the store is empty. Production fails fast: no store, no start."""
    if not demo_mode():
        from app.agents.credit.bank_verification.repository import store_backend

        store_backend()                                      # StoreConfigError: set BANK_VERIFICATION_DSN
    if demo_mode() and section("server").get("seed_when_empty", True) and not repository().cases():
        from app.agents.credit.bank_verification.demo_data import seed

        seed.reset(repository())
    from app.agents.credit.bank_verification.services import summary_service

    summary_service.warm()                                   # the model loaded before the first summary is asked
    yield


def create_app() -> FastAPI:
    server = section("server")
    app = FastAPI(title=str(server.get("title", "Bank Verification Agent")), version="1.0.0", lifespan=_lifespan,
                  description=str(server.get("description_demo" if demo_mode() else "description", "")))
    app.add_middleware(CORSMiddleware, allow_origins=list(server.get("cors_origins") or []),
                       allow_methods=["GET", "POST"], allow_headers=["*"])
    install_errors(app)
    app.include_router(router, prefix="/api/v1")

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise ServiceError(404, "NOT_FOUND", path=path)
            return FileResponse(DIST / "index.html")

    return app


app = create_app()
