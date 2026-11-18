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

import asyncio
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
from app.api.routes.copilot_api import router as copilot_router
from app.api.routes.credit_api import router as credit_router
from app.api.routes.document_extraction_api import router as document_extraction_router
from app.api.routes.document_agent_api import router as document_agent_router
from app.api.routes.financial_api import router as financial_router
from app.api.routes.fos_api import router as fos_router
from app.api.routes.eligibility_api import router as eligibility_router
from app.api.routes.los_api import router as los_router
from app.api.routes.ops import router as ops_router
from app.api.routes.verification_api import router as verification_router
from app.api.routes.passbook_api import router as passbook_router
from app.api.routes.marksheet_api import router as marksheet_router
from app.api.routes.pan_api import router as pan_router
from app.api.routes.hrms_api import router as hrms_router
from app.llm.config import ollama_host, ollama_model
from app.security.auth import auth_health, require_jwt, validate_auth_configuration

# Login and JWKS. Deliberately NOT behind require_jwt -- this is how a
# caller obtains a token in the first place, so protecting it with the
# thing it issues would be circular.
from app.api.routes.auth_api import router as auth_router
from app.api.routes.case_api import router as case_router


def _prepare_demo() -> None:
    """
    The demonstration corpus, put where the API will look for it.

    WHY THIS IS AT STARTUP AND NOT IN A SCRIPT. The demo runs Qdrant
    embedded, which keeps its storage in a directory and takes an
    exclusive lock on it. A second process cannot index into it while
    the API is running, and the previous arrangement -- index from a
    script, answer from the API -- was worse than locked: with no
    `QDRANT_PATH` at all, each process got its own `:memory:`
    database, so the API searched an empty one and every process
    question came back with no evidence.

    BOTH HALVES OR NEITHER. Seeded cases with no index answers
    nothing, and an index over an unseeded store cites cases the API
    cannot read. Each half has its own flag, both default off, and
    what actually happened is printed rather than assumed.

    NEVER IN PRODUCTION. Both flags off is the default and this
    prints one line saying so.
    """
    from app.knowledge import indexing
    from app.store import demo_seed

    if not (demo_seed.enabled() or indexing.demo_index_enabled()):
        print("demo corpus        : disabled")
        return

    try:
        from app.knowledge.vector_store import get_vector_store
        from app.store import get_repository

        repository = get_repository()

        if demo_seed.enabled():
            summary = demo_seed.seed(repository)
            print(f"demo seed          : {summary}")

        if indexing.demo_index_enabled():
            store = get_vector_store()
            mode = store.health().get("mode")
            indexed = indexing.ensure_demo_index(repository, store)
            print(f"demo index         : {mode}  "
                  f"{indexed if indexed else 'already indexed'}")
    except Exception as exc:
        # A DEMO FIXTURE MUST NOT TAKE THE SERVICE DOWN. Said loudly,
        # because a demo that silently did not load looks exactly like
        # a retrieval bug -- which is the failure this whole path
        # exists to stop.
        print(f"DEMO PREPARE FAILED: {type(exc).__name__}: {exc}")


class _McpMount:
    """
    THE CASE MCP SERVER MOUNTED IN THIS SERVICE (2026-10-13). Run as its own process it needed its own .env, fought
    the API for the embedded vector store's lock and for the schema-migration lock, and the API kept a dead session
    after it restarted. Mounted here (`/mcp`, streamable HTTP) it shares this process's configuration, store and
    lifecycle; the tool calls are still MCP JSON-RPC and every call is still re-authorised from the caller's token.
    LOS_MCP_HTTP_MOUNT=false leaves it unmounted (404).
    """

    def __init__(self) -> None:
        self.inner = None

    @staticmethod
    def enabled() -> bool:
        return (_os.getenv("LOS_MCP_HTTP_MOUNT") or "true").strip().lower() not in {"false", "0", "no", "off"}

    def serving(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def run():
            if not self.enabled():
                yield
                return
            from mcp.server.fastmcp.server import StreamableHTTPASGIApp
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

            from app.mcp.case_server import server

            # A SESSION MANAGER OF ITS OWN for this lifespan: the shared FastMCP object (its settings and its own
            # lazily-built manager, used by `python -m app.mcp.case_server` and the protocol tests) is never touched
            # DNS-REBINDING PROTECTION STAYS ON: localhost only by default; a deployment behind a host name lists it
            # in LOS_MCP_ALLOWED_HOSTS (comma separated, e.g. "los.internal:443")
            security = server.settings.transport_security
            extra = [h.strip() for h in (_os.getenv("LOS_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
            if extra and security is not None:
                security = security.model_copy(update={
                    "allowed_hosts": list(dict.fromkeys([*security.allowed_hosts, *extra]))})
            manager = StreamableHTTPSessionManager(
                app=server._mcp_server, event_store=server._event_store, retry_interval=server._retry_interval,
                json_response=server.settings.json_response, stateless=server.settings.stateless_http,
                security_settings=security, max_request_body_size=server.settings.max_request_body_size)
            try:
                async with manager.run():
                    self.inner = StreamableHTTPASGIApp(manager)
                    print("case MCP server    : mounted at /mcp (streamable HTTP)")
                    yield
            finally:
                self.inner = None

        return run()

    async def __call__(self, scope, receive, send):
        if self.inner is None:
            from starlette.responses import JSONResponse as _NotMounted

            await _NotMounted({"detail": "MCP is not mounted."}, status_code=404)(scope, receive, send)
            return
        await self.inner(scope, receive, send)


_MCP_PROXY = _McpMount()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and load the OCR models before serving."""

    from app.agents.fraud_risk.config import get_policy
    from app.security import auth as _auth

    # NEVER SERVE UNAUTHENTICATED OUTSIDE DEVELOPMENT. AUTH_ENABLED=false in
    # any other environment stops startup here; `require_jwt` separately
    # ignores the flag outside development, so a missed startup cannot open
    # the API either.
    _auth.validate_auth_mode()

    # THE JWT SETTINGS ARE CHECKED WHEN AUTHENTICATION IS ON. Outside
    # development a missing JWKS URL / issuer / audience stops startup
    # (fail closed) instead of failing every request later; in development
    # it is reported and the service still starts.
    if _auth.auth_enabled():
        try:
            validate_auth_configuration()
        except RuntimeError as exc:
            if _auth.environment() not in _auth._DEV_ENVIRONMENTS:
                raise
            print(f"auth configuration : INCOMPLETE ({exc}) -- development only")

    # PRODUCTION NEVER RUNS DEMO ELIGIBILITY RULES (2026-10-13). Outside development a config that is not CONFIRMED
    # stops startup here (fail closed); in development the demo config is named once at startup, with its data source.
    from app.agents.eligibility import config as _eligibility_config

    print(f"eligibility rules  : {_eligibility_config.validate_for_startup()} "
          f"-- data: {_eligibility_config.provider_name()}")

    # MANDATORY SIGNATURE RULE MISCONFIGURED (step 5c): flag on with no valid
    # activation date blocks EVERY case (fail closed). Said loudly at startup,
    # and /ready reports it, so it is noticed at once rather than case by case.
    from app.agents.applicant import workflow as _workflow

    _signature_problem = _workflow.signature_rule_config_error()
    if _signature_problem:
        logging.getLogger("los.startup").error(
            "CONFIGURATION ERROR: %s -- every case is blocked with SIGNATURE_RULE_MISCONFIGURED "
            "until readiness.signature_mandatory.activation_date is set in applicant_agent.yaml.",
            _signature_problem)
        print(f"signature rule     : MISCONFIGURED ({_signature_problem}) -- every case blocked")

    # OPENTELEMETRY, when OTEL_ENABLED=true (OTLP or console export).
    from app.observability import tracing as _tracing

    _tracing.configure_tracing()

    # PASSBOOK OCR WARMUP (PP-OCRv6 small), in the background on the OCR workers that will run it, so the first
    # passbook request does not pay the model load. Config is checked here: a missing key stops startup by name.
    from app.agents.passbook import config as _passbook_config

    _passbook_config.get()
    from app.agents.marksheet import config as _marksheet_config

    _marksheet_config.get()            # the marksheet service shares the passbook OCR engine (warmed below)
    if (_os.getenv("PASSBOOK_WARMUP", "true") or "true").lower() == "true":
        def _warm_passbook() -> None:
            try:
                from app.agents.document_agent.ocr import get_ocr_executor
                from app.agents.passbook import engine as _passbook_engine

                pool = get_ocr_executor()
                for future in [pool.submit(_passbook_engine.warm) for _ in range(pool._max_workers)]:
                    future.result(timeout=120)
            except Exception as exc:  # noqa: BLE001 - a cold engine costs only its first request
                logger.info("Passbook OCR warmup skipped (%s)", type(exc).__name__)

        import threading as _pb_threading

        _pb_threading.Thread(target=_warm_passbook, name="passbook-warmup", daemon=True).start()

    # RETRIEVAL WARMUP, in the background. The first Copilot request
    # otherwise pays the vector store client's import and connection
    # (~0.8 s measured locally) and the first question embedding. A daemon
    # thread, so an unreachable embedding service can never delay startup.
    # COPILOT_WARMUP=false skips it.
    if (_os.getenv("COPILOT_WARMUP", "true") or "true").lower() == "true":
        import threading as _threading

        def _warm_retrieval() -> None:
            # EACH STEP ON ITS OWN. They were one try-block: with no Qdrant
            # configured the first step raised and the understanding stack and
            # the knowledge index were never warmed -- measured, the first
            # question that reached dense retrieval paid ~5 s building the
            # handbook's vectors inside a user request.
            def step(name: str, run) -> None:
                try:
                    run()
                except Exception as exc:  # noqa: BLE001 - a cold step costs only its first use
                    logger.info("Copilot warmup step %s skipped (%s)", name, type(exc).__name__)

            def vector_store() -> None:
                from app.knowledge.vector_store import get_vector_store

                get_vector_store()._connect()

            def embedder() -> None:
                from app.knowledge.embeddings import get_query_embedder

                get_query_embedder().embed("warmup")

            def knowledge_index() -> None:
                # builds the dense vectors of the handbook once (the 'vector'
                # backend); a lexical backend just builds its BM25 index
                from app import knowledge

                knowledge.get_retriever().retrieve("what is KYC", "FOS", limit=1)

            def understanding() -> None:
                # configuration, lexicons, classifier and guardrail patterns
                # (measured: ~1.5 s on the first request after a cold start)
                from app.agents.applicant import conversation, language
                from app.agents.applicant.copilot.semantics import intents
                from app.security import guardrails

                for sample in ("hi", "what is my stage?", "mera stage kya hai?"):
                    guardrails.check_input(sample)
                    conversation.classify(sample)
                    language.detect(sample)
                    intents.understand(sample, has_case=True)

            def meaning_bank() -> None:
                # the meaning layer's example bank (intent examples + paraphrases): embedded once here, never inside
                # the first officer's turn (measured: ~100 s on a cold cache)
                from app.agents.applicant.copilot.semantics import meaning

                if meaning.enabled():
                    meaning.rank("warmup")

            def chat_model() -> None:
                # the small model the chooser / rewrite / general answer use: loaded and held (keep_alive)
                from app.agents.applicant.copilot.semantics import meaning
                from app.llm import availability

                if meaning.enabled():               # off in the test suite: no model is loaded there
                    availability.warm_in_background()
                    # MEASURED 2026-10-13: the first REAL prompt after a load took 35 s (weights paged in, the
                    # chooser's ~1 000-token prompt read cold); warm, 1.2 s. One real call here pays that before the
                    # first officer does.
                    meaning.prime()
                    from app.agents.applicant.copilot.capabilities import qwen_agent

                    qwen_agent.prime()             # LAST: the agent's prefix is the one Ollama holds when serving
                    # THE NATURAL LINE'S PROMPT TOO (2026-10-15): cold it needs ~6-11 s and its 4 s budget drops it;
                    # with OLLAMA_NUM_PARALLEL=4 each prompt keeps its own slot, so once read it stays ~1.1 s
                    from app.agents.applicant.copilot.answering import polish as _polish

                    _polish.prime()

            for name, run in (("vector_store", vector_store), ("embedder", embedder),
                              ("knowledge_index", knowledge_index), ("understanding", understanding),
                              ("meaning_bank", meaning_bank), ("chat_model", chat_model)):
                step(name, run)

        _threading.Thread(target=_warm_retrieval, name="copilot-warmup",
                          daemon=True).start()
        print("copilot warmup     : started (background)")

    print("\n" + "=" * 58)
    print("LOS AGENTIC AI")
    print("=" * 58)
    print(f"authentication     : "
          f"{'ON' if _auth.auth_enabled() else 'OFF (development only)'}")

    print(f"fraud & risk agent : {risk_enabled()}  (v{risk_version()})")
    print(f"policy file        : {policy_path()}")

    try:
        policy = get_policy()
    except Exception as exc:
        print(f"POLICY LOAD FAILED : {exc}")
        print("=" * 58 + "\n")
        raise

    # THE DEDUPE POLICY AND ITS HMAC KEY, at start-up: a broken policy, an unsigned one in production, or a production
    # start without LOS_IDENTITY_HMAC_KEY stops the server by name -- never a first request that fails
    from app.agents.dedupe import config as _dedupe_config
    from app.agents.dedupe import identity as _dedupe_identity

    try:
        _dedupe_identity.require_in_production()
        _dedupe = _dedupe_config.get_policy()
    except Exception as exc:
        print(f"DEDUPE POLICY FAILED: {exc}")
        print("=" * 58 + "\n")
        raise
    print(f"dedupe policy      : {_dedupe['policy_version']} (signed off: {bool(_dedupe.get('signed_off'))})")

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

    # THE CARD WARM-UP (2026-11-05), in the background: the engine warm-up above reads a blank canvas, so only the text
    # DETECTOR is warmed -- the recogniser met its first real text lines inside the first request (measured after a
    # restart: first PAN 7.9 s of OCR, the next ones 0.6-0.9 s; first passport 4.3 s, then 1.3-1.6 s). One SYNTHETIC
    # card of each kind (samples/synthetic, generated -- no real person) is read end to end through the same pipeline
    # the requests use. DOCUMENT_CARD_WARMUP=false switches it off; a missing sample is skipped.
    if (_os.getenv("DOCUMENT_CARD_WARMUP", "true") or "true").lower() == "true":
        def _warm_cards() -> None:
            from pathlib import Path as _P

            from app.agents.document_agent import extract_document as _extract

            folder = _P(__file__).resolve().parent / "samples" / "synthetic" / "dev"
            for name in ("pan_en_clean_0.jpg", "voter_id_en_clean_0.jpg", "driving_licence_hi_clean_0.jpg",
                         "passport_en_phone_1.jpg"):
                try:
                    if (folder / name).is_file():
                        _extract(str(folder / name))
                except Exception as exc:  # noqa: BLE001 - a cold card costs only its first request
                    logger.info("Card warm-up %s skipped (%s)", name, type(exc).__name__)

        import threading as _card_threading

        _card_threading.Thread(target=_warm_cards, name="card-warmup", daemon=True).start()

    # The bank-statement table workers (separate processes): started in the background now, so the first long
    # native statement does not pay their start-up inside its upload (2026-10-14; warm 104 pages: 4.8 s).
    if (_os.getenv("BANK_STATEMENT_TABLE_WARMUP", "true") or "true").lower() == "true":
        try:
            import threading as _threading

            from app.agents.bank_statement.extract import warm_page_readers, warm_table_pool

            _threading.Thread(target=warm_table_pool, name="table-pool-warmup", daemon=True).start()
            # the scanned-page readers' OCR engines, likewise (one per reader thread)
            _threading.Thread(target=warm_page_readers, name="page-reader-warmup", daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - the first statement starts the pool instead
            logger.warning("Table pool warmup not started: %r", exc)

    # The summary model, for the same reason as the OCR engines: Ollama
    # unloads an idle model, so without this the FIRST request pays the load,
    # overruns the 1.5s budget, and marks the provider unavailable for the
    # whole cooldown -- every request in that window falls back too.
    #
    # Off the request path and never fatal: a model that cannot be reached
    # here simply means summaries are deterministic, which is the designed
    # behaviour rather than a failure.
    from app.agents.los import config as _los_config

    # THE COPILOT COMPOSER uses the same model: warmed too when it is on, so
    # the first composed answer is not the one that falls back (measured:
    # the only summary fallback in the Phase 3 benchmark was that cold start).
    from app.agents.applicant import config as _composer_config
    # THE ROUTER (step 6b) uses the same model and is ON by default: warmed and
    # kept warm so the ~4.5 s cold load never lands on a user's turn.
    from app.agents.applicant.copilot.semantics import llm_router as _llm_router

    if _llm_router.deprecated_flag_in_use():
        logger.warning("COPILOT_UNDERSTANDING_LLM is DEPRECATED: it now maps to %s (removed after go-live)",
                       _llm_router.FLAG)
        print(f"WARNING            : COPILOT_UNDERSTANDING_LLM is deprecated -> {_llm_router.FLAG}")
    print(f"copilot LLM router : {'ON' if _llm_router.enabled() else 'OFF (kill-switch)'}")

    if _llm_router.enabled():
        # THE COLD LOAD IS PAID HERE, with no 2.5 s router limit (keep_warm.warm_up,
        # default 180 s). Until it succeeds /ready reports DEGRADED (MODEL_NOT_WARM).
        from app.llm import keep_warm as _router_warm

        _ok = await _router_warm.warm_up()
        print(f"router model load  : {'OK' if _ok else 'FAILED (router turns ask a clarifying question)'}"
              f" ({_router_warm.state().get('load_ms')} ms)")
    if _los_config.llm_summary_enabled() or _composer_config.llm_enabled() or _llm_router.enabled():
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
    # ELIGIBILITY DEMO DATA IN DEVELOPMENT: DATABASE_URL names a port the embedded dev PostgreSQL may no longer use
    # after a reboot -> the app's own embedded server, `eligibility` database + schema.sql (dev only; never prod)
    try:
        from app.agents.eligibility.data import dev_db as _elig_dev_db

        _elig_dsn = _elig_dev_db.ensure()
        if _elig_dsn:
            print("eligibility data   : DATABASE_URL unreachable -> embedded dev PostgreSQL, database 'eligibility'")
    except Exception as exc:  # noqa: BLE001 - the eligibility screen reports it; the app still starts
        print(f"eligibility data   : not prepared ({type(exc).__name__})")
    # EVERY AGENT'S WARNING+ LOGS IN ITS OWN DATABASE (app/agent_db/logs.py; config agent_databases.yaml): one handler
    # per agent whose DSN is set; masked, queued, never blocks a request
    try:
        if "pytest" not in __import__("sys").modules:
            from app.agent_db import logs as _agent_logs

            _logged = _agent_logs.install()
            print(f"agent logs in DB   : {', '.join(sorted(_logged)) or 'none (no agent DSN set)'}")
    except Exception as exc:  # noqa: BLE001 - logs stay on the console; the app still starts
        print(f"agent logs in DB   : not installed ({type(exc).__name__})")
    # EVERY AGENT'S AUDIT IN ONE COMMON TABLE (common_audit.audit_events; app/agent_db/common_audit.py): the triggers
    # on each agent's audit table, idempotent; never fails start-up
    try:
        if "pytest" not in __import__("sys").modules:
            from app.agent_db import common_audit as _common_audit

            print(f"common audit       : {_common_audit.ensure_at_startup()}")
    except Exception as exc:  # noqa: BLE001
        print(f"common audit       : not installed ({type(exc).__name__})")
    # THE SUMMARY MODEL WARM BEFORE THE FIRST OFFICER ASKS (background; never delays startup, never fails it)
    try:
        import threading as _threading

        from app.agents.eligibility.output import summary as _elig_explain

        if _elig_explain.enabled() and "pytest" not in __import__("sys").modules:
            _threading.Thread(target=_elig_explain.warm, name="eligibility-explain-warm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

    _prepare_demo()

    # WORK THAT OUTLIVES A REQUEST NEEDS SOMETHING TO DO IT. A scanned
    # statement is queued by the upload path; without this nothing ever
    # picks the job up, which is the state the "asynchronous extraction
    # queue" message described for months. Off by default.
    try:
        from app.store import ocr_queue

        if ocr_queue.start_worker():
            print("OCR worker        : running")
        else:
            print("OCR worker        : disabled")
    except Exception as exc:
        print(f"OCR WORKER FAILED  : {type(exc).__name__}: {exc}")

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

    # KEEP THE COMPOSER MODEL RESIDENT between quiet stretches (app/llm/
    # keep_warm.py): an unloaded qwen2.5:3b cost 6.0 s on the next request.
    keep_warm_task = None
    # CHAT HISTORY RETENTION (migration 0006; app/store/chat_history.py): once at startup, then every
    # cleanup_interval_hours -- only with session memory on and the table present
    chat_cleanup_task = None
    from app.agents.applicant.copilot.conversation.state import memory_enabled as _memory_enabled

    if _memory_enabled():
        from app.store import chat_history as _chat_history

        _mem = _applicant_config.chatbot("memory") or {}

        async def _chat_cleanup_loop() -> None:
            if _mem.get("cleanup_at_startup", True):
                await asyncio.to_thread(_chat_history.cleanup)
            hours = float(_mem.get("cleanup_interval_hours", 24) or 0)
            while hours > 0:
                await asyncio.sleep(hours * 3600)
                try:
                    await asyncio.to_thread(_chat_history.cleanup)
                except Exception as exc:  # noqa: BLE001 - never fatal
                    logger.warning("chat history cleanup failed: %r", exc)

        chat_cleanup_task = asyncio.create_task(_chat_cleanup_loop())
        print(f"chat history clean : at startup + every {_mem.get('cleanup_interval_hours', 24)} h")

    if _composer_config.llm_enabled() or _los_config.llm_summary_enabled() or _llm_router.enabled():
        from app.llm import keep_warm as _keep_warm

        if _keep_warm.interval_seconds() > 0:
            keep_warm_task = asyncio.create_task(_keep_warm.run_forever())
            print(f"model keep-warm    : every {_keep_warm.interval_seconds():.0f} s")

    # THE CASE MCP SERVER, IN THIS SERVICE (/mcp): a fresh session manager per lifespan (FastMCP runs one only once)
    async with _MCP_PROXY.serving():
        yield

    if keep_warm_task is not None:
        keep_warm_task.cancel()
    if chat_cleanup_task is not None:
        chat_cleanup_task.cancel()

    # The worker holds a thread and a claimed job. Asked to stop, it
    # finishes the iteration it is in and leaves the job PROCESSING,
    # which the next start picks up again.
    try:
        from app.store import ocr_queue

        ocr_queue.stop_worker()
    except Exception:
        pass

    # the queued agent-database rows (logs, chatbot audit / turns) written before the process goes
    try:
        from app.agent_db import logs as _agent_logs
        from app.agent_db import writer as _agent_writer

        _agent_writer.close_all()
        _agent_logs.uninstall()
    except Exception:
        pass

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

# ONE ERROR BLOCK ON EVERY ERROR, additive to `detail` (app/api/errors.py)
from app.api import errors as _errors  # noqa: E402

_errors.install(app)
from app.agent_db import responses as _responses  # noqa: E402

_responses.install(app)                       # every agent API response in its schema (api_responses)


@app.middleware("http")
async def _request_span(request, call_next):
    """
    One server span per request: method, route template and status code.
    Never headers, query strings or bodies -- a JWT or a question has no
    attribute to travel under.
    """
    from urllib.parse import unquote

    from app.llm import trace as _llm_trace
    from app.observability.tracing import annotate, span

    # A NUL BYTE IN A PATH OR QUERY is never a valid identifier: refused here,
    # before routing, so it cannot reach a store that rejects it with a 500.
    if "\x00" in unquote(request.url.path) or "\x00" in unquote(request.url.query or ""):
        from fastapi.responses import JSONResponse

        detail = {"code": "INVALID_REQUEST", "message": "The request contains an invalid character."}
        return JSONResponse({"detail": detail, "error": _errors.error_block(400, detail)}, status_code=400)
    # A BODY LARGER THAN ANY ROUTE ACCEPTS is refused from its declared length, before it is read into memory
    # (2026-10-11, security review): the upload route's own 25 MB check ran only after the whole body was buffered.
    # LOS_MAX_REQUEST_BYTES overrides; a chunked body without a length is still bounded by the route's own check.
    from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES as _max_upload

    try:
        _limit = int(_os.getenv("LOS_MAX_REQUEST_BYTES") or (_max_upload + 1024 * 1024))
    except ValueError:
        _limit = _max_upload + 1024 * 1024               # a malformed setting keeps the default, never "no limit"
    try:
        _declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        _declared = -1
    if _declared < 0 or (_limit and _declared > _limit):
        from fastapi.responses import JSONResponse

        too_large = _declared >= 0
        detail = {"code": "PAYLOAD_TOO_LARGE" if too_large else "INVALID_REQUEST",
                  "message": "The request body is too large." if too_large else "Invalid Content-Length."}
        status = 413 if too_large else 400
        return JSONResponse({"detail": detail, "error": _errors.error_block(status, detail)}, status_code=status)
    # API-WIDE LIMIT per caller (app/security/api_rate_limit.py): 429 + Retry-After before the route runs
    from app.security import api_rate_limit as _api_limit

    _wait = _api_limit.check(request.url.path, request.headers.get("authorization"),
                             request.client.host if request.client else None)
    if _wait:
        from fastapi.responses import JSONResponse

        detail = {"code": "RATE_LIMITED", "message": "Too many requests. Please wait and try again."}
        return JSONResponse({"detail": detail, "error": _errors.error_block(429, detail)}, status_code=429,
                            headers={"Retry-After": str(_wait)})
    # one model-call ledger per request (app/llm/trace.py)
    _llm_trace.begin()
    with span("http.request", http_method=request.method) as current:
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/fos", "/api/v1/copilot")):
            # THE FRONTEND CONTRACT VERSION (frontend_handoff/API_CONTRACT.md): the UI checks it, the contract test pins it
            from app.agents.applicant.copilot.answering import contract as _ui_contract

            response.headers["X-Contract-Version"] = _ui_contract.version()
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        # A route with no path parameters is published as its full path; a
        # templated one as its template -- never a concrete id.
        annotate(current, http_route=(request.url.path if path and "{" not in path
                                      else path),
                 http_status_code=response.status_code)
        return response


# /health, /ready and /metrics all live in ops_router. Defining another
# /health here would shadow the readiness contract the platform relies on.
from app.api.contract import COMMON_ERRORS  # noqa: E402 - the documented error union

app.include_router(ops_router)

# Every business API requires JWT authentication.
# Health/readiness remain on the ops router for infrastructure probes.
app.include_router(
    agent_service_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    document_extraction_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    document_agent_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    verification_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    financial_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    passbook_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    marksheet_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    pan_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
app.include_router(
    hrms_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

app.include_router(
    los_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# The FOS copilot. Reaches applicant, application and document records only
# through app/mcp/applicant.py, and enforces scope and case ownership before
# any of them is read.
app.include_router(
    applicant_agent_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# The Universal LOS Copilot. Same agent as the FOS copilot below, a
# separate door: the FOS surface is stage-bounded on purpose, and
# widening this one must never widen that one.
app.include_router(
    copilot_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# Case-data fetch: POST /api/v1/case/fetch
# Auth required; Case ID + APP ID are NOT user-scoped (any authenticated
# user can look up any valid combination -- separate flow from login).
app.include_router(
    case_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# The consolidated FOS surface: two endpoints a field-officer frontend
# integrates against. Thin adapters over the Applicant Agent above, which
# keeps its own routes for existing callers.
app.include_router(
    fos_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# The recorded eligibility verdict, read-only. Evaluation happens in the LOS
# pipeline; this and the Universal Copilot read what it recorded.
app.include_router(
    eligibility_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# Credit underwriting: an evidence-linked ASSESSMENT for the Decision Agent,
# run on the common agent harness. Scope, ownership and stage are enforced by
# the agent before any tool runs.
app.include_router(
    credit_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# Dedupe, UNDER THE CREDIT UNDERWRITING AGENT: ONE API, POST /api/v1/credit/dedupe -- the LOS's own dedupe, the MIFIN
# match, the loan history and the policy outcome in one answer; scoped (los.dedupe.read or the credit underwriting
# scope) and case-owned; other customers' matched cases are redacted.
from app.api.routes.dedupe_api import router as dedupe_router  # noqa: E402

app.include_router(
    dedupe_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# Credit document readiness: the Credit Document Policy Agent (a sub-agent of
# credit underwriting) -- open document requirements for the CREDIT stage.
from app.agents.docs_verify_credit_agent.api.routes import router as docs_credit_router  # noqa: E402

app.include_router(
    docs_credit_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# Bank verification (owner 2026-10-26: "backend 8010 chalega"), a sub-agent of credit underwriting: API 1 process,
# API 2 finalize, read-only views (app/agents/credit/bank_verification). DEMO MODE in its config: synthetic data,
# simulated AA / branch / roles. Its screen is served at /bank-verification/ (the built frontend, when present).
from app.agents.credit.bank_verification.api import router as bank_verification_router  # noqa: E402

app.include_router(
    bank_verification_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
from pathlib import Path as _BvPath  # noqa: E402

_BANK_VERIFICATION_UI = _BvPath(__file__).resolve().parent / "app/agents/credit/bank_verification/frontend/dist"
if _BANK_VERIFICATION_UI.is_dir():
    from fastapi.staticfiles import StaticFiles as _BvStatic  # noqa: E402

    app.mount("/bank-verification", _BvStatic(directory=_BANK_VERIFICATION_UI, html=True), name="bank-verification-ui")

# Crime record verification, a sub-agent of credit underwriting: API 1 initiate (after the confirmation popup), API 2
# finalize, read-only views (app/agents/credit/crime_verification) under /api/v1/credit/crime-verification. Its own
# PostgreSQL database; DEMO MODE in its config: synthetic applications, mock CrimeCheck provider. Screen at
# /crime-verification/ (the built frontend, when present).
from app.agents.credit.crime_verification.api import router as crime_verification_router  # noqa: E402

app.include_router(
    crime_verification_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
_CRIME_VERIFICATION_UI = _BvPath(__file__).resolve().parent / "app/agents/credit/crime_verification/frontend/dist"
if _CRIME_VERIFICATION_UI.is_dir():
    from fastapi.staticfiles import StaticFiles as _CvStatic  # noqa: E402

    app.mount("/crime-verification", _CvStatic(directory=_CRIME_VERIFICATION_UI, html=True),
              name="crime-verification-ui")

# Legal collateral verification, a sub-agent of credit underwriting (app/agents/credit/legal_verification): the CPA
# uploads the Sale Deed and enters its details (no OCR), sends the case to a legal agency, the agency submits its
# Verification Report (CERSAI search included), ten rules compare the three sources, the CPA decides. Its own
# PostgreSQL database (LEGAL_VERIFICATION_DSN); screen at /legal-verification/ (the built frontend, when present).
from app.agents.credit.legal_verification.api import router as legal_verification_router  # noqa: E402

app.include_router(
    legal_verification_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)
_LEGAL_VERIFICATION_UI = _BvPath(__file__).resolve().parent / "app/agents/credit/legal_verification/frontend/dist"
if _LEGAL_VERIFICATION_UI.is_dir():
    from fastapi.staticfiles import StaticFiles as _LvStatic  # noqa: E402

    app.mount("/legal-verification", _LvStatic(directory=_LEGAL_VERIFICATION_UI, html=True),
              name="legal-verification-ui")

# JEV: the semantic decision layer -- typed decisions on authoritative state,
# gated actions, never a domain status. Case ownership checked per route.
from app.api.routes.jev_api import router as jev_router  # noqa: E402

app.include_router(
    jev_router,
    prefix="/api/v1/jev",
    tags=["JEV semantic decisions"],
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# MAKER / CHECKER: four-eyes requests for configured high-risk actions.
from app.api.routes.approvals_api import router as approvals_router  # noqa: E402

app.include_router(
    approvals_router,
    prefix="/api/v1/approvals",
    tags=["Maker / Checker"],
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# FRONTEND STATUS CONTRACT: progress, summary, risk signals, reviews (read-only).
from app.api.routes.status_api import router as status_router  # noqa: E402

app.include_router(
    status_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
)

# BACKEND TEXT-TO-SPEECH: one voice per response language (app/tts).
from app.api.routes.tts_api import router as tts_router  # noqa: E402

app.include_router(
    tts_router,
    prefix="/api/v1",
    dependencies=[Depends(require_jwt)],
    responses=COMMON_ERRORS,
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
        responses=COMMON_ERRORS,
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


def _collapse_upload_unions(node: Any) -> None:
    """
    Document a file field as a file, not as "a file or some text".

    WHY THE UNION IS THERE AT ALL. Swagger UI submits an untouched file
    input as an EMPTY STRING part instead of omitting it, so an optional
    `list[UploadFile]` field is handed `""` and FastAPI rejects the whole
    request with 422. The endpoint therefore accepts `UploadFile | str`
    at runtime and drops the empty part (see `los_api._uploads`).

    THAT TOLERANCE MUST NOT REACH THE CONTRACT. Left alone, the generated
    schema says each item is `anyOf: [binary, string]`, and Swagger UI
    renders a TEXT BOX for it -- so the fix for the upload widget would
    have removed the upload widget. The string half is a workaround for a
    quirk of one client, not something a caller may send, so it is
    collapsed away here: the published contract says binary, which is
    what a client should upload and what every generated client will.
    """
    if isinstance(node, dict):
        options = node.get("anyOf")
        if isinstance(options, list) and len(options) == 2:
            binary = [o for o in options
                      if isinstance(o, dict)
                      and o.get("contentMediaType") == "application/octet-stream"]
            plain = [o for o in options
                     if isinstance(o, dict) and o == {"type": "string"}]
            if len(binary) == 1 and len(plain) == 1:
                node.pop("anyOf")
                node.update(binary[0])
                return

        for value in node.values():
            _collapse_upload_unions(value)

    elif isinstance(node, list):
        for item in node:
            _collapse_upload_unions(item)


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

    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    # THE CHAT REQUEST BODY. /fos/copilot reads its JSON by hand (it also takes
    # multipart uploads), so FastAPI never registered CopilotRequest -- and the
    # body's $ref pointed at nothing: the main chat endpoint had no request
    # schema in Swagger (found 2026-10-06). Registered here from the model.
    from app.api.routes.fos_api import CopilotRequest as _CopilotRequest

    _request_schema = _CopilotRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    for _name, _definition in (_request_schema.pop("$defs", None) or {}).items():
        schemas.setdefault(_name, _definition)
    schemas.setdefault("CopilotRequest", _request_schema)
    # Order matters: collapse the tolerance union first, then annotate
    # the binary that is left.
    _collapse_upload_unions(schemas)
    _mark_binary_uploads(schemas)

    app.openapi_schema = schema
    return schema


# Login + JWKS: unauthenticated by design -- this is how a token is obtained.
#
# A DEVELOPMENT IDENTITY PROVIDER, and only mounted as one. It issues signed
# tokens from inside the resource server; a production deployment must get
# its tokens from the real IdP (JWT_JWKS_URL) and must not expose these
# routes at all. `dev_idp_enabled` needs an explicit LOS_DEV_IDP_ENABLED=true
# AND a development ENVIRONMENT -- anything else leaves them unmounted (404).
def mount_dev_identity_provider(target: FastAPI) -> bool:
    from app.security.dev_idp import dev_idp_enabled

    if not dev_idp_enabled():
        logger.info("Development identity provider not mounted.")
        return False
    target.include_router(auth_router)
    logger.warning("Development identity provider MOUNTED (/api/v1/auth/*, "
                   "JWKS). Never enable this in production.")
    return True


mount_dev_identity_provider(app)
app.mount("/mcp", _MCP_PROXY)                 # the case MCP server in this service (see _McpMount)


app.include_router(auth_router)

app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8010, reload=False)
