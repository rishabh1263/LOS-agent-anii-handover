"""
JEV -- the semantic decision layer over HTTP.

    GET  /api/v1/jev/health                      readiness: provider, thresholds, last success/failure, p50/p95
    GET  /api/v1/jev/metrics                     counts and latency (never values)
    POST /api/v1/jev/cases/{case_id}/evaluate    evaluate now (idempotent per evidence version)
    GET  /api/v1/jev/cases/{case_id}/decisions   the latest typed decisions (frontend contract)

AUTHORIZATION FIRST: the caller must own the case before any state is
built or any provider is called -- a denied request reaches neither.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.security import access
from app.security.auth import require_jwt

router = APIRouter()


def _authorize(claims: dict[str, Any], case_id: str, request_id: str) -> None:
    try:
        access.authorize_claims(claims, case_id=case_id)
    except access.AccessDenied as denied:
        raise access.http_denied(denied, request_id) from None


@router.get("/health", summary="JEV readiness -- never reports ready without a configured provider")
async def health(claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.jev import engine

    return engine.health()


@router.get("/metrics", summary="JEV calls, latency, confidence bands, fallbacks (counts only)")
async def metrics_view(claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.jev import metrics

    return metrics.snapshot()


class DecideRequest(BaseModel):
    state: dict[str, Any] = Field(..., description=(
        "Recorded statuses to decide on, in the shape app/jev/state.py builds, e.g. "
        '{"documents":[{"document_type":"PAN","verification":"PASS"}],'
        '"kyc":{"status":"REVIEW","reason_codes":["NAME_MISMATCH"]}}'),
        examples=[{"documents": [{"document_type": "PAN", "verification": "PASS", "reason_codes": []}],
                   "kyc": {"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"]}}])
    scope: str = Field("CASE_TRIAGE", max_length=40)


@router.post("/decide", summary="Ask JEV about a state you write (try-out / accuracy checks; nothing stored)")
async def decide_state(body: DecideRequest, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    """
    FOR TRYING THE DECISION LAYER FROM SWAGGER. The state is yours, not a
    case's -- no case data is read, nothing is persisted, no action runs. The
    reply is the same typed decision a case evaluation records: answer,
    probabilities, confidence, band, and whether the shipped policy would
    AUTOMATE or keep it ADVISORY.
    """
    import json as _json

    from app.jev import client, config, engine

    request_id = f"jevd_{uuid.uuid4().hex}"
    if len(_json.dumps(body.state, default=str)) > config.state_limits()[1]:
        raise HTTPException(422, detail={"request_id": request_id, "error": "STATE_TOO_LARGE"})
    try:
        questions = config.question_set(body.scope)["questions"]
    except KeyError:
        raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_SCOPE"}) from None
    try:
        reply = await asyncio.to_thread(client.system_one, body.state, questions)
    except client.JevError as exc:
        raise HTTPException(503 if exc.code == "EXTERNAL_DEPENDENCY_REQUIRED" else 409,
                            detail={"request_id": request_id, "error": exc.code, "message": str(exc)}) from None
    decisions = [engine._decision(qid, q, reply["answers"][qid], "JEV") for qid, q in questions.items()]
    return {"request_id": request_id, "provider": config.provider_name(), "model": reply.get("model"),
            "latency_ms": reply["latency_ms"],
            "decisions": [{k: d[k] for k in ("question_id", "decision_type", "question_type", "answer",
                                              "probabilities", "confidence", "band")}
                          | {"decision_policy": "AUTOMATE" if d["automate"] else "ADVISORY"} for d in decisions],
            "persisted": False, "actions_executed": False}


@router.post("/benchmark", summary="Measure JEV accuracy on the labelled states (live model, ~35 s)")
async def benchmark(claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    """
    The labelled states of evals/jev/benchmark.py through the configured
    provider, ONE call per state: accuracy per decision type, AUTO-band
    precision, confidence bands, latency. Synthetic hand-labelled states --
    accuracy on these, not a production figure. Qwen is not called here
    (python -m evals.jev.benchmark compares the two).
    """
    from app.jev import client, config, engine
    from evals.jev.benchmark import CASES, _right, _stats

    request_id = f"jevb_{uuid.uuid4().hex}"
    questions = config.question_set("CASE_TRIAGE")["questions"]
    per: dict[str, list[int]] = {}
    bands: dict[str, int] = {}
    auto = [0, 0]
    latency: list[float] = []
    rows = []
    for label, state, truth in CASES:
        try:
            reply = await asyncio.to_thread(client.system_one, state, questions)
        except client.JevError as exc:
            raise HTTPException(503, detail={"request_id": request_id, "error": exc.code, "message": str(exc)}) from None
        latency.append(reply["latency_ms"])
        row = {"case": label}
        for qid, q in questions.items():
            d = engine._decision(qid, q, reply["answers"][qid], "JEV")
            answer = d["answer"] if q["type"] == "choice" else d["raw_answer"]
            bands[d["band"]] = bands.get(d["band"], 0) + 1
            if qid in truth:
                good = _right(truth[qid], answer)
                per.setdefault(qid, [0, 0])
                per[qid][0] += good
                per[qid][1] += 1
                if d["band"] == "AUTO":
                    auto[0] += good
                    auto[1] += 1
                row[qid] = {"answer": answer, "expected": sorted(truth[qid]) if isinstance(truth[qid], set)
                            else truth[qid], "right": good, "confidence": d["confidence"], "band": d["band"]}
        rows.append(row)
    total = sum(v[1] for v in per.values())
    return {"request_id": request_id, "provider": config.provider_name(), "model": config.model(),
            "question_set": config.question_set("CASE_TRIAGE").get("version"), "states": len(CASES),
            "accuracy_by_decision": {k: round(v[0] / v[1], 3) for k, v in per.items()},
            "accuracy_overall": round(sum(v[0] for v in per.values()) / total, 3) if total else None,
            "auto_band_precision": round(auto[0] / auto[1], 3) if auto[1] else None,
            "confidence_bands": bands, "latency_ms": _stats(latency),
            "automation_policy": {qid: ("AUTOMATE" if q.get("automate") else "ADVISORY") for qid, q in questions.items()},
            "rows": rows}


@router.post("/cases/{case_id}/evaluate", summary="Evaluate a case with JEV now")
async def evaluate_case(case_id: str, party_id: str | None = Query(None),
                        scope: str = Query("CASE_TRIAGE"),
                        claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.jev import engine

    request_id = f"jev_{uuid.uuid4().hex}"
    _authorize(claims, case_id, request_id)
    try:
        run = await asyncio.to_thread(engine.evaluate, case_id, party_id, scope=scope, trigger="API")
    except LookupError:
        raise HTTPException(404, detail={"request_id": request_id, "error": "CASE_NOT_FOUND"}) from None
    except KeyError as exc:
        raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_SCOPE",
                                         "message": str(exc)}) from None
    out = engine.public(run if run.get("status") != "DISABLED" else None)
    out.update(request_id=request_id, jev_status=run.get("status"), reused=bool(run.get("reused")),
               error_code=run.get("error_code"), latency_ms=run.get("latency_ms"))
    return out


@router.get("/cases/{case_id}/decisions", summary="The latest JEV decisions on a case")
async def case_decisions(case_id: str, party_id: str | None = Query(None),
                         claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    from app.jev import engine

    request_id = f"jev_{uuid.uuid4().hex}"
    _authorize(claims, case_id, request_id)
    return {**engine.latest_decisions(case_id, party_id), "request_id": request_id}
