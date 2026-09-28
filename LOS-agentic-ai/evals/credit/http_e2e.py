"""
Credit Underwriting -- REAL HTTP / Swagger E2E.

    python -m evals.credit.http_e2e                    # scenarios + latency
    python -m evals.credit.http_e2e --report out.json

A real uvicorn server on 127.0.0.1, real RS256 JWTs validated by the app's own
auth, the real SQLite store, the MCP runtime in process, the DEMO bureau.
Synthetic cases are written through the LOS ingest path. The model is pointed
at a dead port: the memo must fall back to its structured summary.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "/api/v1/credit/underwriting/run"
SCOPE = "los.credit.underwrite"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="credit-e2e-")
        os.environ.update({
            "ENVIRONMENT": "test", "AUTH_ENABLED": "true",
            "LOS_STORE_PATH": os.path.join(self.workdir, "store.sqlite3"),
            "LOS_DOCUMENT_STORE_PATH": os.path.join(self.workdir, "docs"),
            "LOS_CASE_MEMORY_ENABLED": "true", "LOS_MCP_MODE": "in_process",
            "LOS_OCR_WORKER_ENABLED": "false", "LOS_LLM_SUMMARY_ENABLED": "false",
            "LOS_DEV_IDP_ENABLED": "false", "QDRANT_PATH": "", "EMBEDDING_PROVIDER": "hashing",
            "COPILOT_WARMUP": "false", "OLLAMA_HOST": "http://127.0.0.1:9",
            "CREDIT_MEMO_LLM_ENABLED": "true",          # enabled, but the model is down
            "AGENT_RUN_AUDIT_PATH": os.path.join(self.workdir, "agent_runs.jsonl"),
            "APPLICANT_AGENT_AUDIT_PATH": os.path.join(self.workdir, "audit.jsonl"),
        })
        os.environ.pop("COPILOT_SERVICE_SCOPE_ACCESS", None)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        self._keys()
        import main

        self.port = _free_port()
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="off"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        import httpx

        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=60)

    def _keys(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = key.private_bytes(serialization.Encoding.PEM,
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
        auth.JWT_ISSUER = auth.JWT_ISSUER or "credit-e2e-issuer"
        auth.JWT_AUDIENCE = auth.JWT_AUDIENCE or "credit-e2e-audience"
        self._auth = auth

    def token(self, subject: str, scopes: list[str]) -> str:
        import jwt

        now = datetime.now(timezone.utc)
        return jwt.encode({"sub": subject, "iss": self._auth.JWT_ISSUER,
                           "aud": self._auth.JWT_AUDIENCE, "iat": now, "nbf": now,
                           "exp": now + timedelta(minutes=30), "scope": " ".join(scopes)},
                          self._pem, algorithm="RS256", headers={"kid": "credit-e2e"})

    def post(self, body: Any, *, subject: str | None = "e2e-officer",
             scopes: list[str] | None = None) -> tuple[int, dict, float]:
        headers = {}
        if subject is not None:
            headers["Authorization"] = f"Bearer {self.token(subject, scopes or [SCOPE])}"
        started = time.perf_counter()
        response = self.http.post(ENDPOINT, json=body, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            payload = response.json()
        except ValueError:
            payload = {"_raw": response.text}
        return response.status_code, payload, elapsed

    def stop(self) -> None:
        self.server.should_exit = True
        self.http.close()


def _seed() -> dict[str, str]:
    """Synthetic DEMO cases through the ingest path, one per scenario."""
    from app.store import get_repository
    from evals.credit import suite

    repo = get_repository()
    officer = "e2e-officer"
    full = suite.FULL
    specs = {
        "clean": (suite.Setup(result=full, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), {}),
        "wrong_stage": (suite.Setup(result=full, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}),
                        {"stage": "CPA"}),
        "provider_failure": (suite.Setup(result=full,
                                         bureau={suite.APP: "DEMO-BUREAU-ERROR"}), {}),
        "partial": (suite.Setup(result={k: v for k, v in full.items() if k != "risk"},
                                bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), {}),
        "insufficient": (suite.Setup(result=None, bureau={}), {}),
        # NO "missing applicant" case: the store's foreign key refuses an
        # application whose applicant does not exist, so the API can never be
        # handed one. A request naming an unknown case is the reachable form of
        # it, covered by the identical-403 check.
        "replay": (suite.Setup(result=full, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), {}),
        "latency": (suite.Setup(result=full, bureau={suite.APP: "DEMO-BUREAU-CLEAN"}), {}),
    }
    ids = {}
    for name, (setup, extra) in specs.items():
        case_id, app_id = f"CASE-E2E-{name.upper()}", f"APP-E2E-{name.upper()}"
        suite.build(repo, setup, case_id=case_id, app_id=app_id, co_id=f"{app_id}-CO",
                    officer=officer, **extra)
        ids[name] = case_id
    return ids


def run(report: str | None = None) -> int:
    server = Server()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), "" if ok else detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -- {detail}"))

    try:
        ids = _seed()

        code, body, first_ms = server.post({"case_id": ids["clean"], "correlation_id": "e2e-1"})
        result = body.get("result") or {}
        check("success: 200 READY_FOR_DECISION", code == 200 and
              result.get("status") == "READY_FOR_DECISION", f"{code} {body}")
        check("success: demo labels", (result.get("demo") or {}).get("labels") ==
              ["DEMO", "NON_PRODUCTION", "UNCONFIRMED"], str(result.get("demo")))
        check("success: correlation id propagated", body.get("correlation_id") == "e2e-1")
        check("success: memo fell back (model down)",
              (result.get("memo") or {}).get("validation") == "LLM_UNAVAILABLE",
              str((result.get("memo") or {}).get("validation")))
        check("success: no approval / rejection",
              not any(w in json.dumps(body).upper() for w in ("APPROVED", "REJECTED")))

        code, body, _ = server.post({"case_id": ids["clean"]}, subject=None)
        check("unauthorized: 401", code == 401, f"{code}")
        code, body, _ = server.post({"case_id": ids["clean"]}, scopes=["los.read"])
        check("wrong scope: 403 INSUFFICIENT_SCOPE",
              code == 403 and body["detail"]["code"] == "INSUFFICIENT_SCOPE", f"{code} {body}")
        code, body, _ = server.post({"case_id": ids["wrong_stage"]})
        check("wrong stage: 409 STAGE_NOT_ALLOWED",
              code == 409 and body["detail"]["code"] == "STAGE_NOT_ALLOWED", f"{code} {body}")
        code_missing, missing, _ = server.post({"case_id": "CASE-E2E-DOES-NOT-EXIST"})
        code_other, other, _ = server.post({"case_id": ids["clean"]}, subject="e2e-stranger")
        check("missing case and another's case: identical 403",
              code_missing == code_other == 403 and missing["detail"]["code"] ==
              other["detail"]["code"] == "CASE_ACCESS_DENIED",
              f"{code_missing} {missing} / {code_other} {other}")
        code, body, _ = server.post({"case_id": ids["provider_failure"]})
        check("provider failure: 200 DATA_INSUFFICIENT + TOOL_UNAVAILABLE",
              code == 200 and body["result"]["status"] == "DATA_INSUFFICIENT" and any(
                  e.startswith("TOOL_UNAVAILABLE:bureau.get")
                  for e in body["result"]["assessment"]["exceptions"]), f"{code}")
        code, body, _ = server.post({"case_id": ids["partial"]})
        check("partial evidence: 200 with the gap named",
              code == 200 and any("risk" in g for g in body["result"]["assessment"]
                                  ["data_gaps"]), f"{code}")
        code, body, _ = server.post({"case_id": ids["insufficient"]})
        check("insufficient evidence: 200 DATA_INSUFFICIENT",
              code == 200 and body["result"]["status"] == "DATA_INSUFFICIENT", f"{code}")
        _, a, _ = server.post({"case_id": ids["replay"]})
        _, b, _ = server.post({"case_id": ids["replay"]})
        check("replay: idempotent (same assessment, replayed)",
              b["result"]["replayed"] is True and
              a["result"]["assessment_id"] == b["result"]["assessment_id"] and
              a["run_id"] != b["run_id"], "")
        code, body, _ = server.post({"case_id": ids["clean"],
                                     "caller": {"subject": "e2e-officer"}},
                                    subject="e2e-stranger")
        check("a body cannot carry a caller: 422", code == 422, f"{code}")
        spec = server.http.get("/openapi.json").json()
        check("openapi: endpoint documented",
              ENDPOINT in spec["paths"] and "post" in spec["paths"][ENDPOINT])

        # -- latency: 1 cold (first write) + 20 warm (replays) ------------------
        latencies = []
        code, _, cold = server.post({"case_id": ids["latency"]})
        for _ in range(20):
            _, _, ms = server.post({"case_id": ids["latency"]})
            latencies.append(ms)
        latencies.sort()
        lat = {"first_request_ms": round(first_ms, 1), "cold_case_ms": round(cold, 1),
               "warm_min": round(latencies[0], 1),
               "warm_p50": round(statistics.median(latencies), 1),
               "warm_p95": round(latencies[int(0.95 * (len(latencies) - 1))], 1),
               "warm_max": round(latencies[-1], 1)}
        print("LATENCY", json.dumps(lat))
        audit = Path(server.workdir, "agent_runs.jsonl")
        check("audit: runs recorded, no token material",
              audit.exists() and "eyJ" not in audit.read_text(encoding="utf-8"))
    finally:
        server.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nHTTP E2E {passed}/{len(results)} passed")
    if report:
        Path(report).write_text(json.dumps({"results": results, "latency_ms": lat},
                                           indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    sys.exit(run(parser.parse_args().report))
