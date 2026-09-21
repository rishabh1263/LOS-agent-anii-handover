"""
LOS Agentic AI service entrypoint.

Serves the Document Agent (PAN / Driving Licence / Voter ID / Passport), the
Financial Agent (bank statements, ITR, salary slips) and the Fraud & Risk
Agent.

Two dispatch paths exist, deliberately:

    /api/v1/agents/execute and /api/v1/extract-document
        Go through the LangGraph orchestrator, so agent configuration from
        agents.yaml, retry classification, circuit breaking, bulkheads and
        decision audit apply.

    /api/v1/document-agent
        The unified Document Agent API. Calls the document workflow directly
        -- extraction here is deterministic (OCR plus regex and spatial
        reasoning, no model call), so there is no model failure to retry or
        circuit-break around. It runs its own executors to keep the event
        loop free. An earlier version of this docstring claimed everything
        went through the orchestrator; it never did.

Run: 
    uvicorn main:app --host 0.0.0.0 --port 8010

Swagger:
    http://127.0.0.1:8010/docs
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# THREAD LIMITS
#
# These MUST be set before numpy, OpenCV or ONNX Runtime are imported: each
# library reads them once, at initialisation. Setting them further down the
# file -- after the route modules have already pulled in the OCR chain -- has
# no effect at all.
#
# The value is deliberately NOT forced to 1. Measured on a 16-core host,
# DOCUMENT_OCR_THREADS=4 made OCR roughly 25x slower than the ONNX default
# (13053ms against 510ms), and DOCUMENT_OCR_LIB_THREADS=1 was worse than
# leaving it alone. Set either only after measuring on the target machine.
# ---------------------------------------------------------------------------

import os as _os

_lib_threads = (_os.environ.get("DOCUMENT_OCR_LIB_THREADS") or "").strip()
if _lib_threads:
    for _var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        _os.environ.setdefault(_var, _lib_threads)


# ---------------------------------------------------------------------------
# Environment, before any app.* import so module-scope configuration reads
# the real values rather than defaults.
# ---------------------------------------------------------------------------

from dotenv import load_dotenv

load_dotenv()

_os.environ.setdefault("DOCUMENT_OCR_ENGINE", "rapidocr")
_os.environ.setdefault("DOCUMENT_NAME_SPACING", "true")


import logging
import time
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi

from app.observability.logging import configure_logging

configure_logging()

logger = logging.getLogger(__name__)

from app.agents.fraud_risk.config import (
    enabled as risk_enabled,
    llm_summary_enabled,
    policy_path,
    version as risk_version,
)
from app.api.routes.agent_service import router as agent_service_router
from app.api.routes.applicant_agent_api import router as applicant_agent_router
from app.api.routes.document_extraction_api import router as document_extraction_router
from app.api.routes.document_agent_api import router as document_agent_router
from app.api.routes.financial_api import router as financial_router
from app.api.routes.fos_api import router as fos_router
from app.api.routes.los_api import router as los_router
from app.api.routes.ops import router as ops_router
from app.api.routes.verification_api import router as verification_router
from app.llm.config import ollama_host, ollama_model
from app.security.auth import auth_health, require_jwt, validate_auth_configuration

# Login and JWKS. Deliberately NOT behind require_jwt -- this is how a
# caller obtains a token in the first place, so protecting it with the
# thing it issues would be circular.
from app.api.routes.auth_api import router as auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and load the OCR models before serving."""

    from app.agents.fraud_risk.config import get_policy

    print("\n" + "=" * 58)
    print("LOS AGENTIC AI")
    print("=" * 58)

    print(f"fraud & risk agent : {risk_enabled()}  (v{risk_version()})")
    print(f"policy file        : {policy_path()}")

    try:
        policy = get_policy()
    except Exception as exc:
        print(f"POLICY LOAD FAILED : {exc}")
        print("=" * 58 + "\n")
        raise

    signed = bool(policy.get("signed_off", False))
    rules = policy.get("rules", {})

    print(f"policy version     : {policy.get('policy_version')}")
    print(f"policy signed off  : {signed}")
    print(
        "rules enabled      : "
        f"{sum(1 for c in rules.values() if c.get('enabled'))}"
    )
    print(f"LLM summary        : {llm_summary_enabled()}")
    print(f"LLM host / model   : {ollama_host()} / {ollama_model()}")
    print(f"OCR engine         : {_os.getenv('DOCUMENT_OCR_ENGINE')}")
    # The EFFECTIVE value, not the environment variable.
    #
    # This line used to print the raw variable and fall back to the words
    # "ONNX default (recommended)" when it was unset. That was misleading in
    # both directions once the code grew a computed default: it reported
    # "ONNX default" while the process was in fact running a bounded pool,
    # and there was no way to see what the pool was actually sized at without
    # reading the source.
    from app.agents.document_agent.ocr import (
        _default_ocr_threads,
        get_ocr_executor,
    )

    _workers = get_ocr_executor()._max_workers
    _threads = _os.getenv("DOCUMENT_OCR_THREADS")
    _effective = int(_threads) if _threads else _default_ocr_threads()
    print(
        "OCR threads        : "
        f"{_effective} intra-op x {_workers} worker(s) = {_effective * _workers}"
        f"{' (DOCUMENT_OCR_THREADS override)' if _threads else ' (computed default)'}"
        f"{'  UNBOUNDED' if _effective == 0 else ''}"
    )

    # Load the OCR models now. Without this the first extraction request pays
    # engine construction plus lazy ONNX session init inside the request --
    # measured at roughly 2.3 seconds on top of inference.
    if (_os.getenv("DOCUMENT_OCR_WARMUP", "true") or "true").lower() == "true":
        started = time.perf_counter()
        try:
            from app.agents.document_agent.ocr import (
                get_ocr_executor,
                warmup_all,
            )

            # Every worker, not just one. Each builds its own engine, so a
            # cold worker would otherwise load a model inside the first
            # request that reached it.
            workers = get_ocr_executor()._max_workers
            elapsed = warmup_all()
            print(f"OCR warmup         : {elapsed:.0f} ms ({workers} worker(s))")
        except Exception as exc:
            # A warmup failure must not take the whole service down: an
            # extraction request will still report the OCR error itself.
            logger.warning("OCR warmup failed: %r", exc)
            print(f"OCR warmup FAILED  : {exc!r}  (continuing)")
        finally:
            del started
    else:
        print("OCR warmup         : disabled")

    # The summary model, for the same reason as the OCR engines: Ollama
    # unloads an idle model, so without this the FIRST request pays the load,
    # overruns the 1.5s budget, and marks the provider unavailable for the
    # whole cooldown -- every request in that window falls back too.
    #
    # Off the request path and never fatal: a model that cannot be reached
    # here simply means summaries are deterministic, which is the designed
    # behaviour rather than a failure.
    from app.agents.los import config as _los_config

    if _los_config.llm_summary_enabled():
        from app.agents.los.summary import warmup as _llm_warmup

        print(f"LLM warmup         : {await _llm_warmup():.0f} ms")
    else:
        print("LLM warmup         : disabled")

    # The case store, opened before traffic so a broken path is a startup
    # failure rather than a surprise inside the first FOS question.
    from app.agents.applicant import config as _applicant_config

    if _applicant_config.enabled():
        try:
            from app.store import get_repository, store_backend

            health = get_repository().health()
            print(f"case store         : {store_backend()}  {health}")
        except Exception as exc:
            print(f"CASE STORE FAILED  : {exc}")
            raise
        print(f"applicant agent    : enabled  (LLM {_applicant_config.llm_enabled()})")
    else:
        print("applicant agent    : disabled")

    if not signed:
        print("-" * 58)
        print("WARNING: risk policy is NOT signed off.")
        print("Thresholds marked [PLACEHOLDER] are not authoritative.")

    print("=" * 58)
    print("READY")
    print("  POST /api/v1/document-agent       Unified document API")
    print("  POST /api/v1/verify               Verify a document")
    print("  POST /api/v1/extract-document     PAN / DL / Voter ID / Passport")
    print("  POST /api/v1/financial/verify     Bank statement / ITR / payslip")
    print("  POST /api/v1/financial/extract    Bank statement / ITR / payslip")
    print("  POST /api/v1/los/process          Whole application end to end")
    print("  POST /api/v1/fos/applicants       FOS: open a case")
    print("  POST /api/v1/fos/copilot          FOS: copilot (all actions)")
    print("  POST /api/v1/kyc                  Cross-document consistency")
    print("  POST /api/v1/agents/execute       Any agent by id or stage")
    print("  GET  /docs                        Swagger")
    print("=" * 58 + "\n")

    yield

    print("\nLOS Agentic AI shutting down.\n")


# Ordered so the documentation reads in the sequence a document actually
# travels: verify, then extract, then the financial path, with orchestration
# and operations last.
_TAGS = [
    {
        "name": "Verification Agent",
        "description": (
            "Is this document acceptable? QUICK checks class, legibility and "
            "identifier format in one OCR pass. FULL extracts and validates "
            "every field. Neither establishes authenticity."
        ),
    },
    {
        "name": "Document Agent",
        "description": (
            "Identity documents: PAN, Driving Licence, Voter ID and Passport. "
            "OCR plus deterministic extraction -- no model decides a field."
        ),
    },
    {
        "name": "Financial Agent",
        "description": (
            "Bank statements, ITR acknowledgements and salary slips, "
            "normalised into common income signals for underwriting."
        ),
    },
    {
        "name": "Orchestration",
        "description": "Execute any agent by id or stage through LangGraph.",
    },
    {
        "name": "FOS",
        "description": (
            "The field-officer integration surface. TWO endpoints: "
            "POST /api/v1/fos/applicants opens a case, and "
            "POST /api/v1/fos/copilot serves every question, dropdown action "
            "and document upload. One response shape for all of them. "
            "Credit, risk, KYC and lending questions are routed downstream, "
            "never answered here."
        ),
    },
    {
        "name": "Applicant Agent",
        "description": (
            "The FOS copilot. Natural-language questions about an applicant, "
            "their application, documents, what is pending and whether the "
            "case is ready for CPA -- answered from stored records. Credit, "
            "risk, KYC and lending decisions are routed downstream, never "
            "answered here."
        ),
    },
    {"name": "Ops", "description": "Liveness, readiness and metrics."},
]

app = FastAPI(
    openapi_tags=_TAGS,
    title="LOS Agentic AI",
    version=risk_version(),
    description=(
        "Deterministic document extraction and risk assessment, orchestrated "
        "through LangGraph. Language models write summaries; they never decide."
    ),
    lifespan=lifespan,
)

# /health, /ready and /metrics all live in ops_router. Defining another
# /health here would shadow the readiness contract the platform relies on.
app.include_router(ops_router)

# Every business API requires JWT authentication.
# Health/readiness remain on the ops router for infrastructure probes.
app.include_router(
    agent_service_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)
app.include_router(
    document_extraction_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)
app.include_router(
    document_agent_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)
app.include_router(
    verification_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)
app.include_router(
    financial_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)

app.include_router(
    los_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)

# The FOS copilot. Reaches applicant, application and document records only
# through app/mcp/applicant.py, and enforces scope and case ownership before
# any of them is read.
app.include_router(
    applicant_agent_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)

# The consolidated FOS surface: two endpoints a field-officer frontend
# integrates against. Thin adapters over the Applicant Agent above, which
# keeps its own routes for existing callers.
app.include_router(
    fos_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
)

# KYC may already exist in this V21 checkout. Protect it automatically.
try:
    from app.api.routes.kyc_api import router as kyc_router
except ImportError:
    kyc_router = None

if kyc_router is not None:
    app.include_router(
        kyc_router,
        prefix="/api/v1",
        dependencies=[Depends(require_jwt)],
    )


# ============================================================================
# OPENAPI: FILE UPLOADS THAT SWAGGER UI CAN RENDER
#
# FastAPI 0.138 emits OpenAPI 3.1, where a binary upload is described as
#
#     {"type": "string", "contentMediaType": "application/octet-stream"}
#
# Swagger UI looks for `format: binary` to decide it should draw a file
# picker. Not finding one, it falls back to treating the field as a plain
# string array and renders "Add string item" -- so the multi-document upload
# this endpoint has always accepted could not be exercised from the docs page.
#
# The endpoint declaration is already correct (list[UploadFile] = File(...));
# only the emitted schema needed fixing. `format: binary` is ADDED rather than
# substituted, so 3.1 consumers keep the annotation they expect and Swagger UI
# gets the one it needs -- and the app stays on OpenAPI 3.1 instead of being
# downgraded wholesale for the sake of one widget.
# ============================================================================

def _mark_binary_uploads(node: Any) -> None:
    """Recursively add `format: binary` wherever a binary media type is set."""
    if isinstance(node, dict):
        if (
            node.get("type") == "string"
            and node.get("contentMediaType") == "application/octet-stream"
            and "format" not in node
        ):
            node["format"] = "binary"

        for value in node.values():
            _mark_binary_uploads(value)

    elif isinstance(node, list):
        for item in node:
            _mark_binary_uploads(item)


def custom_openapi() -> dict[str, Any]:
    """The service's OpenAPI document, with uploads Swagger UI can render."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )

    _mark_binary_uploads(schema.get("components", {}).get("schemas", {}))

    app.openapi_schema = schema
    return schema


# Login + JWKS: unauthenticated by design -- this is how a token is obtained.
app.include_router(auth_router)


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8010, reload=False)
