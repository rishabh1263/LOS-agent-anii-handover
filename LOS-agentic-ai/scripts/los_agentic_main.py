"""
THE CREDIT AGENTS AS ONE SERVICE -- the handover project's main.py (scripts/build_los_agentic.py writes this file as
the project's main.py; in this repository the full LOS server, main.py at the root, mounts the same routers).

    eligibility            /api/v1/eligibility/...
    dedupe                 /api/v1/credit/dedupe, /api/v1/credit/dedupe/all
    docs policy            /api/v1/credit/document-readiness/{case_id}
    bank verification      /api/v1/bank-verification/...
    crime verification     /api/v1/credit/crime-verification/...
    legal verification     /api/v1/credit/legal-verification/...
    /health                the process answers
    /ready                 every agent's rules are readable (from the database in production)

    uvicorn main:app --host 0.0.0.0 --port 8010

START-UP FAILS CLOSED: rules that cannot be read, an incomplete or (in production) unconfirmed eligibility policy, a
broken or unsigned dedupe policy, or a production start without the identity key stops the service by name -- never a
first request that fails. Every request needs the LOS JWT (app/security/auth.py).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.agent_db import responses
from app.api import errors
from app.security.auth import require_jwt

logger = logging.getLogger("los_agentic")


def _startup_checks() -> list[str]:
    """Every check that must pass before the service answers; the lines it prints."""
    from app.agents.dedupe import config as dedupe_config
    from app.agents.dedupe import identity as dedupe_identity
    from app.agents.eligibility import config as eligibility_config

    lines = [f"eligibility rules : {eligibility_config.validate_for_startup()}"]
    dedupe_identity.require_in_production()
    policy = dedupe_config.get_policy()
    lines.append(f"dedupe policy     : {policy['policy_version']} (signed off: {bool(policy.get('signed_off'))})")
    return lines


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for line in _startup_checks():
        print(line)
    yield
    from app.agent_db import writer

    writer.close_all()                                # the background audit / log rows written before the exit


app = FastAPI(title="LOS Agentic -- credit agents", version="1.0", lifespan=lifespan,
              description="Eligibility, credit document policy, dedupe, bank, crime and legal verification. Rules "
                          "and every decision are kept in PostgreSQL (los_agentic, one schema per agent).")
errors.install(app)
responses.install(app)                     # every API response in its agent's schema (api_responses)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready() -> dict[str, object]:
    """Every rule set readable where it lives (its version's hash) -- never the rules themselves."""
    from app.agent_db import rule_store

    sets: dict[str, str] = {}
    ok = True
    for name in rule_store.names():
        try:
            if rule_store.spec(name).get("folder"):
                sets[name] = f"{len(rule_store.load_family(name))} file(s) in force"
            else:
                sets[name] = rule_store.load_with_hash(name)[0][:12]
        except Exception as exc:  # noqa: BLE001 - its reason, plainly
            ok, sets[name] = False, f"UNAVAILABLE ({type(exc).__name__})"
    if not ok:
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "not ready", "rules": sets}, status_code=503)
    return {"status": "ready", "rules": sets}


from app.agents.credit.bank_verification.api import router as bank_verification_router  # noqa: E402
from app.agents.credit.crime_verification.api import router as crime_verification_router  # noqa: E402
from app.agents.credit.legal_verification.api import router as legal_verification_router  # noqa: E402
from app.agents.docs_verify_credit_agent.api.routes import router as docs_policy_router  # noqa: E402
from app.api.routes.dedupe_api import router as dedupe_router  # noqa: E402
from app.api.routes.eligibility_api import router as eligibility_router  # noqa: E402

for _router in (eligibility_router, dedupe_router, docs_policy_router, bank_verification_router,
                crime_verification_router, legal_verification_router):
    app.include_router(_router, prefix="/api/v1", dependencies=[Depends(require_jwt)])
