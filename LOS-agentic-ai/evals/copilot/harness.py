"""
COPILOT EVAL HARNESS -- the real public API, instrumented from outside.

    * a REAL uvicorn HTTP server on 127.0.0.1 (one persistent event loop, as
      in production), serving the current `main.app`;
    * REAL RS256 JWTs in the Authorization header, verified by production
      code -- only the JWKS key lookup is pointed at a throwaway keypair;
    * a seeded two-customer store: the caller's OWN case (full FOS data,
      document extraction, KYC finding, co-applicant) and ANOTHER customer's
      case full of canary values;
    * a TRAJECTORY recorder: every repository read, every retrieval call,
      every Qwen call (count, time, model, and the exact payload sent), next
      to the tools / route / timings the response publishes.

Nothing in the application is modified; every probe is a wrapper installed
here. Requests are sent one at a time, so the per-request trajectory is the
exact set of events between request and response.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

MINE, ME, MY_CO = "case_eval0000000000000000000000mine", "APP-EVALMINE01", "COAPP-EVALMINE01"
THEIRS, THEM = "case_eval000000000000000000000other", "APP-EVALOTHER01"
MISSING_CASE = "case_eval0000000000000000000doesnot"
#: Values that belong to the OTHER customer: none may ever appear.
CANARIES = ("Zara Qureshi", "ZZZPQ9999Z", "9123456780", "zara@example.com", "987654",
            "Juhu Tara Road", THEIRS, THEM, "EVALOTHER")
#: Full identifiers in the caller's OWN records: published only masked.
FULL_IDENTIFIERS = ("ABCDE1234F", "123456789012", "1234 5678 9012")
FOS_SCOPES = ["read_applicant", "read_application", "read_documents",
              "read_verification", "read_pending_items", "read_next_action"]
SERVICE_SCOPES = ["los.read", "los.write"]


@dataclass
class Trajectory:
    """What happened inside one request."""

    reads: list[str] = field(default_factory=list)
    read_ms: float = 0.0
    retrievals: int = 0
    retrieval_ms: float = 0.0
    qwen_calls: int = 0
    qwen_ms: float = 0.0
    qwen_models: set[str] = field(default_factory=set)
    qwen_payloads: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.__init__()


TRACE = Trajectory()


#: the Phase 3 feature flags (README_CHATBOT "Demo setup"); also pinned off in tests/conftest.py
PHASE3_FLAGS = ("COPILOT_CASE_WORKSPACE", "COPILOT_CASE_ACTIONS", "COPILOT_RESPONSE_STYLE", "COPILOT_VERIFY_DIAGNOSE",
                "COPILOT_DOCUMENT_ACTIONS", "COPILOT_TERMS_KNOWLEDGE", "COPILOT_LOCALIZED_KYC_REASONS",
                "COPILOT_SINGLE_CASE_RESOLVE", "COPILOT_EMPHASIS", "COPILOT_STREAMING", "COPILOT_SESSION_MEMORY",
                "LOS_COAPP_IDENTITY", "COPILOT_PARTY_RECOGNITION", "COPILOT_GUARDRAIL_HARDENING",
                "LOS_COAPP_MANDATORY_DOCS", "LOS_SIGNATURE_MANDATORY")


def _environment(live: bool, workdir: str) -> None:
    os.environ.update({
        "ENVIRONMENT": "test", "AUTH_ENABLED": "true",
        "LOS_DOCUMENT_STORE_PATH": os.path.join(workdir, "docs"),
        "LOS_CASE_MEMORY_ENABLED": "true", "APPLICANT_AGENT_LLM_ENABLED": "true",
        "QDRANT_PATH": "", "EMBEDDING_PROVIDER": "hashing", "LOS_DEMO_INDEX_ENABLED": "true",
        "LOS_MCP_MODE": "in_process", "LOS_OCR_WORKER_ENABLED": "false",
        "LOS_LLM_SUMMARY_ENABLED": "false", "LOS_DEV_IDP_ENABLED": "false",
        "APPLICANT_AGENT_AUDIT_PATH": os.path.join(workdir, "audit.jsonl"),
        "COPILOT_WARMUP": "false",
    })
    # PRODUCTION ACCESS DEFAULT: customer-facing (never enabled here).
    os.environ.pop("COPILOT_SERVICE_SCOPE_ACCESS", None)
    # PHASE 3 FEATURE FLAGS OFF unless an eval arm turns one on (EVAL_OVERRIDE_<FLAG> below): a developer's
    # .env may switch them on, and main.py loads .env -- "false" here is not overwritten by load_dotenv.
    for flag in PHASE3_FLAGS:
        os.environ[flag] = "false"
    if not live:
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"     # no model: fallback paths
    # A/B ARMS: EVAL_OVERRIDE_<NAME>=<value> sets <NAME> after the defaults
    # ("EVAL_OVERRIDE_CHATBOT_NATURAL_COMPOSITION=true").
    for key, value in list(os.environ.items()):
        if key.startswith("EVAL_OVERRIDE_"):
            os.environ[key[len("EVAL_OVERRIDE_"):]] = value


class Harness:
    """Start once; `ask()` many times; `stop()` at the end."""

    def __init__(self, *, live: bool = False, port: int | None = None) -> None:
        self.live = live
        self.workdir = tempfile.mkdtemp(prefix="copilot-eval-")
        _environment(live, self.workdir)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        # A THROWAWAY DATABASE, NEVER THE DEV ONE (step 6b). Without a DSN the
        # app opens the embedded dev PostgreSQL at runtime/pgdata, and the eval
        # case was seeded into it. Like the test fixtures: a clone on the test
        # server (app/store/testing.py), dropped when this process exits.
        from app.store.testing import session_dsn

        os.environ["LOS_STORE_DSN"] = session_dsn()
        self.port = port or _free_port()
        self._keys()
        self._instrument_model()
        import main  # noqa: F401  (after the environment is set)

        self._seed()
        self._instrument_store()
        self._start_server(main.app)
        import httpx

        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=120)
        # READINESS: one request before measuring, as a deployment's health
        # check / startup warm-up would -- the first request after a cold start
        # pays lazy module and configuration loading (~1.5 s), measured apart.
        started = time.perf_counter()
        self.ask(message="hello")
        self.first_request_ms = (time.perf_counter() - started) * 1000

    # -- auth ----------------------------------------------------------------
    def _keys(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())
        public = key.public_key()
        import app.security.auth as auth

        class _Key:
            def __init__(self, k):
                self.key = k

        class _JWKS:
            def get_signing_key(self, token):
                return _Key(public)

        auth.get_jwks_provider.cache_clear()
        auth.get_jwks_provider = lambda: _JWKS()
        auth.JWT_ISSUER = auth.JWT_ISSUER or "copilot-eval-issuer"
        auth.JWT_AUDIENCE = auth.JWT_AUDIENCE or "copilot-eval-audience"
        self._auth = auth

    def token(self, caller: str) -> str | None:
        import jwt

        if caller == "anonymous":
            return None
        subject, scopes = {
            "owner": ("eval-customer", FOS_SCOPES),
            "service": ("eval-service-desk", SERVICE_SCOPES),
            "stranger": ("eval-stranger", FOS_SCOPES),
        }[caller]
        now = datetime.now(timezone.utc)
        return jwt.encode({"sub": subject, "iss": self._auth.JWT_ISSUER,
                           "aud": self._auth.JWT_AUDIENCE, "iat": now, "nbf": now,
                           "exp": now + timedelta(minutes=30), "scope": " ".join(scopes)},
                          self._private_pem, algorithm="RS256",
                          headers={"kid": "eval-signing-key"})

    # -- data ----------------------------------------------------------------
    def _seed(self) -> None:
        from app.security.access import record_ownership
        from app.store import get_repository
        from app.store.ingest import persist_los_result
        from app.store.models import Applicant, Application, CaseFinding, FindingKind

        repo = get_repository()
        for case, app, co in ((MINE, ME, MY_CO), (THEIRS, THEM, None)):
            persist_los_result({
                "request_id": f"r-{case[-5:]}", "applicant_id": app,
                **({"co_applicant_id": co} if co else {}),
                "case_id": case, "status": "PARTIAL", "decision": "REVIEW",
                "next_action": "MANUAL_REVIEW", "documents": [
                    {"source_id": "pan.jpg", "type": "PAN", "party_id": app,
                     "party_role": "PRIMARY_APPLICANT", "verification": "PASS",
                     "reason_codes": []},
                    *([{"source_id": "pan2.jpg", "type": "PAN", "party_id": co,
                        "party_role": "CO_APPLICANT", "verification": "FAIL",
                        "reason_codes": ["DOCUMENT_TYPE_MISMATCH"]}] if co else [])]})
        repo.save_applicant(Applicant(applicant_id=ME, full_name="Rahul Sharma",
                                      mobile="9876501234", email="rahul@example.com",
                                      date_of_birth="1990-05-14",
                                      address="12 MG Road, Pune (A/c no. 123456789012)"))
        mine = repo.get_application(MINE)
        repo.save_application(Application(**{**mine.__dict__, "product": "PERSONAL_LOAN",
                                             "loan_amount": "500000",
                                             "employment_type": "SALARIED",
                                             "tenure_months": "36",
                                             "interest_rate_pct": "11.5"}))
        repo.save_applicant(Applicant(applicant_id=THEM, full_name="Zara Qureshi",
                                      mobile="9123456780", email="zara@example.com",
                                      date_of_birth="1985-01-01",
                                      address="7 Juhu Tara Road, Mumbai"))
        theirs = repo.get_application(THEIRS)
        repo.save_application(Application(**{**theirs.__dict__, "product": "PERSONAL_LOAN",
                                             "loan_amount": "987654"}))
        # DOCUMENT DATA: what the PAN itself says (a released extraction).
        repo.save_finding(CaseFinding(
            finding_id="F-EXT", case_id=MINE, party_id=ME, source_id="pan.jpg",
            finding_kind=FindingKind.EXTRACTION, status="PASS", reason_codes=[],
            content_hash="ext1", payload={"type": "PAN", "name": "RAHUL SHARMA",
                                          "pan_number": "ABCDE1234F"}))
        repo.save_finding(CaseFinding(
            finding_id="F-KYC", case_id=MINE, party_id=ME, finding_kind=FindingKind.KYC,
            status="REVIEW", reason_codes=["NAME_MISMATCH"], content_hash="kyc1",
            payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
                {"document_type": "PAN", "value": "RAHUL SHARMA"},
                {"document_type": "BANK_STATEMENT", "value": "R SHARMA"}]}]}))
        repo.save_finding(CaseFinding(
            finding_id="F-OTH", case_id=THEIRS, party_id=THEM, finding_kind=FindingKind.KYC,
            status="REVIEW", reason_codes=["NAME_MISMATCH"], content_hash="oth1",
            payload={"fields": [{"field": "PAN", "status": "FAIL", "sources": [
                {"document_type": "PAN", "value": "ZZZPQ9999Z"}]}]}))
        record_ownership("eval-customer", applicant_id=ME, case_id=MINE)
        record_ownership("someone-else", applicant_id=THEM, case_id=THEIRS)
        try:
            from app.knowledge import indexing
            from app.knowledge.vector_store import get_vector_store

            for case in (MINE, THEIRS):
                indexing.rebuild_case(repo, get_vector_store(), case)
        except Exception:
            pass
        self.repo = repo

    # -- instrumentation -------------------------------------------------------
    def _instrument_model(self) -> None:
        from agent_framework.ollama import OllamaChatClient

        original = OllamaChatClient.get_response

        async def recorded(client, messages, *a, **k):
            TRACE.qwen_calls += 1
            TRACE.qwen_models.add(str(getattr(client, "model", "") or ""))
            TRACE.qwen_payloads.append(json.dumps(
                [getattr(m, "contents", str(m)) for m in messages], default=str))
            started = time.perf_counter()
            try:
                return await original(client, messages, *a, **k)
            finally:
                TRACE.qwen_ms += (time.perf_counter() - started) * 1000
        OllamaChatClient.get_response = recorded

    def _instrument_store(self) -> None:
        from app.agents.applicant import knowledge_answer
        from app.knowledge import grounding

        # CASE DATA reads only: the conversation's own memory (get_conversation, moved into
        # the repository with the Postgres store) is turn plumbing, not a read of the case.
        plumbing = {"get_conversation", "list_conversations"}
        for name in dir(self.repo):
            if name.startswith(("get_", "list_", "has_access")) and name not in plumbing \
                    and callable(getattr(self.repo, name)):
                original = getattr(self.repo, name)

                def read(*args, _original=original, _name=name, **kwargs):
                    started = time.perf_counter()
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        TRACE.reads.append(_name)
                        TRACE.read_ms += (time.perf_counter() - started) * 1000
                setattr(self.repo, name, read)
        for module, name in ((grounding, "gather"), (knowledge_answer, "retrieve")):
            original = getattr(module, name)

            def retrieve(*args, _original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    TRACE.retrievals += 1
                    TRACE.retrieval_ms += (time.perf_counter() - started) * 1000
            setattr(module, name, retrieve)

    def _start_server(self, app) -> None:
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="off"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("eval server did not start")

    # -- requests --------------------------------------------------------------
    def ids(self, which: str) -> dict[str, str]:
        return {"mine": {"case_id": MINE, "applicant_id": ME},
                "theirs": {"case_id": THEIRS, "applicant_id": THEM},
                "theirs_case_only": {"case_id": THEIRS},
                "theirs_applicant_only": {"applicant_id": THEM},
                "missing": {"case_id": MISSING_CASE, "applicant_id": "APP-EVALNOBODY1"},
                "none": {}}[which]

    def ask(self, *, caller: str = "owner", ids: str = "mine", message: Any = None,
            context: dict | None = None, extra: dict | None = None,
            raw: Any = None) -> dict[str, Any]:
        TRACE.reset()
        headers = {}
        token = self.token(caller)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = raw if raw is not None else {**self.ids(ids), "message": message,
                                            **({"context": context} if context else {}),
                                            **(extra or {})}
        started = time.perf_counter()
        response = self.http.post("/api/v1/copilot/query", json=body, headers=headers)
        total = (time.perf_counter() - started) * 1000
        try:
            payload = response.json()
        except ValueError:
            payload = {"_raw": response.text}
        return {"status": response.status_code, "body": payload, "total_ms": total,
                "trace": Trajectory(**{k: (set(v) if isinstance(v, set) else list(v)
                                           if isinstance(v, list) else v)
                                       for k, v in TRACE.__dict__.items()}),
                "text": response.text}

    def openapi(self) -> dict[str, Any]:
        return self.http.get("/openapi.json").json()

    def unload_model(self) -> None:
        from app.llm.config import ollama_host, ollama_model

        import httpx

        httpx.post(ollama_host().rstrip("/") + "/api/generate",
                   json={"model": ollama_model(), "keep_alive": 0}, timeout=30)

    def stop(self) -> None:
        self.server.should_exit = True
        self.http.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


__all__ = ["CANARIES", "FULL_IDENTIFIERS", "Harness", "MINE", "ME", "MY_CO", "THEIRS",
           "THEM", "TRACE", "Trajectory"]
