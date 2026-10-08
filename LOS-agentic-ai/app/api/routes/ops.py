"""
Operational endpoints: liveness, readiness, metrics.

One of each. Previously health was served from three places and metrics from
two, which meant a monitoring system could get three different answers about
the same process.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Response

from app.security.auth import require_jwt

from app.agents.fraud_risk import metrics
from app.agents.fraud_risk.config import (
    allow_unsigned_policy,
    environment,
    get_policy,
)
from app.core.exceptions import ConfigurationError
from app.orchestration import registry, resilience
from app.orchestration.agent_config import get_agent_configs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ops"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Process is up. Does not validate configuration -- see /ready."""
    return {
        "status": "healthy",
        "service": "los-agent-service",
        "environment": environment(),
    }


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict:
    """
    Ready to serve traffic.

    Returns 503 if the agent configuration or the risk policy cannot be
    loaded, so an orchestrator will not route traffic to a process that would
    fail every request.
    """
    try:
        agent_configs = get_agent_configs()
    except ConfigurationError as exc:
        logger.error("Readiness failed: agent config did not load: %s", exc)
        response.status_code = 503
        return {"status": "not_ready", "reason": "agent_config_load_failed"}

    try:
        policy = get_policy()
    except ConfigurationError as exc:
        logger.error("Readiness failed: risk policy did not load: %s", exc)
        response.status_code = 503
        return {"status": "not_ready", "reason": "risk_policy_load_failed"}

    routable = [
        agent_id
        for agent_id, cfg in agent_configs.items()
        if cfg.enabled and cfg.langgraph_enabled and registry.is_registered(agent_id)
    ]

    if not routable:
        response.status_code = 503
        return {"status": "not_ready", "reason": "no_routable_agents"}

    # THE COPILOT'S TOOL PATH. In protocol mode the case tools are reached
    # through the MCP server, so a broken one is a Copilot that cannot
    # answer: never reported ready. MISCONFIGURED is never ready either --
    # a typo'd mode falls back to a safe default, and saying "ready" would
    # hide that the intended one is not running. An explicitly configured
    # in-process fallback degrades instead of failing. In in_process mode
    # (the local default) no MCP server is needed: NOT_REQUIRED.
    import asyncio

    from app.mcp import runtime as mcp_runtime

    mcp = await asyncio.to_thread(mcp_runtime.health)
    if mcp["status"] == "MISCONFIGURED" or (
            mcp["status"] == "UNAVAILABLE" and mcp["fallback"] != "in_process"):
        logger.error("Readiness failed: MCP %s (%s)", mcp["status"],
                     "; ".join(mcp.get("problems") or []))
        response.status_code = 503
        return {"status": "not_ready", "reason": f"mcp_{mcp['status'].lower()}",
                "mcp": mcp}
    if mcp["status"] == "UNAVAILABLE":
        mcp = {**mcp, "degraded": True}

    # THE CASE STORE IS MANDATORY. Readiness never checked it: with PostgreSQL
    # down the probe still said "ready" and traffic kept arriving at a process
    # that failed every case request (found 2026-10-06).
    store = await asyncio.to_thread(_store_health)
    if not store.get("available"):
        logger.error("Readiness failed: case store unavailable (%s)", store.get("error"))
        response.status_code = 503
        return {"status": "not_ready", "reason": "case_store_unavailable", "case_store": store}

    # OPTIONAL dependencies are reported, never hidden, and never fail readiness:
    # JEV is advisory -- the LOS flow runs without it.
    jev = _jev_status()
    degraded = bool(mcp.get("degraded")) or jev not in ("READY", "DISABLED")
    dependencies = {"case_store": "READY", "mcp": mcp["status"], "jev": jev}

    # A RULE THAT BLOCKS EVERY CASE (step 5c: signature flag on, no activation
    # date). The service still answers, so not a 503 -- but never plain READY.
    from app.agents.applicant import workflow
    from app.agents.los import co_applicants

    configuration_errors = [e for e in (workflow.signature_rule_config_error(),
                                        co_applicants.config_error()) if e]
    if configuration_errors:
        logger.error("Readiness: configuration error: %s", "; ".join(configuration_errors))
        degraded = True

    # THE LLM ROUTER'S MODEL (step 6b-tune): while the router is on and Qwen is not
    # loaded, a router turn would time out into a clarification -- never plain READY.
    from app.agents.applicant.copilot.semantics import llm_router
    from app.llm import keep_warm

    if llm_router.enabled():
        dependencies["llm_router"] = "READY" if keep_warm.is_warm() else "MODEL_NOT_WARM"
        if not keep_warm.is_warm():
            degraded = True
    else:
        dependencies["llm_router"] = "DISABLED"

    return {
        "status": "ready",
        "overall": "DEGRADED" if degraded else "READY",
        "configuration_errors": configuration_errors,
        "dependencies": dependencies,
        "case_store": store,
        "mcp": mcp,
        "routable_agents": sorted(routable),
        "risk_policy_version": str(policy.get("policy_version", "")),
        "risk_policy_signed_off": bool(policy.get("signed_off", False)),
        "unsigned_policy_override": allow_unsigned_policy(),
        "circuits": resilience.breaker.snapshot(),
        "in_flight": resilience.bulkhead.snapshot(),
        "build": build_marker(),
    }


def build_marker() -> dict:
    """
    WHICH BACKEND IS THIS (FOS plan 1.1): the build step (applicant_agent.yaml chatbot.build), the git
    commit when one can be read, and every chatbot flag as it is RIGHT NOW in this process -- so a
    frontend tester can confirm with GET /ready that they reach this server with the features on.
    """
    import os
    import subprocess
    from pathlib import Path

    from app.agents.applicant import config

    build = config.chatbot("build") or {}
    on = {"1", "true", "yes", "on"}
    from app.agents.applicant.copilot.answering import kyc_table, language_lock, professional, readiness_report
    from app.agents.applicant.copilot.semantics import llm_router

    # flags whose default is ON in config when the environment says nothing: their own enabled() decides
    by_module = {llm_router.FLAG: llm_router.enabled, language_lock.FLAG: language_lock.enabled,
                 professional.FLAG: professional.enabled, kyc_table.FLAG: kyc_table.enabled,
                 readiness_report.FLAG: readiness_report.enabled}
    from app.agents.applicant.copilot.capabilities import snapshot_qa

    by_module[snapshot_qa.FLAG] = snapshot_qa.enabled
    from app.agents.applicant.copilot.answering import counts

    by_module[counts.FLAG] = counts.enabled
    from app.agents.applicant.copilot.answering import handoff_note

    by_module[handoff_note.FLAG] = handoff_note.enabled
    from app.agents.applicant.copilot.capabilities import smart_upload

    by_module[smart_upload.FLAG] = smart_upload.enabled
    from app.agents.applicant.copilot.capabilities import timeline

    by_module[timeline.FLAG] = timeline.enabled
    from app.agents.applicant.copilot.answering import officer_tools

    for _feature, _flag in officer_tools.FLAGS.items():
        by_module[_flag] = (lambda f=_feature: officer_tools.enabled(f))
    # MASTER SPEC v2: config-default-ON flags (their own enabled() reads env, then config)
    from app.agents.applicant.copilot.answering import contract as _contract_flag
    from app.agents.applicant.copilot.capabilities import abuse_guard, case_form, case_list, faq, product_flow

    for _module in (_contract_flag, case_list, faq, abuse_guard, product_flow, case_form):
        by_module[_module.FLAG] = _module.enabled
    flags = {name: (by_module[name]() if name in by_module
                    else (os.getenv(name, "") or "").strip().lower() in on)
             for name in build.get("chatbot_flags") or []}
    commit = None
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2,
                                cwd=Path(__file__).resolve().parents[3]).stdout.strip() or None
    except Exception:  # noqa: BLE001 - no git on the host: the step still says which build
        commit = None
    return {"phase": build.get("phase"), "step": build.get("step"), "commit": commit,
            "uncommitted_changes_possible": True, "chatbot_flags": flags,
            "chatbot_flags_on": sum(flags.values()), "chatbot_flags_total": len(flags)}


@router.get("/metrics", summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    """Agent, orchestration and Copilot aggregate metrics in one scrape."""
    from app.observability import analytics

    body = (metrics.render_prometheus() + metrics.render_orchestration_prometheus()
            + analytics.render_prometheus())
    return Response(content=body, media_type="text/plain")


def _analytics_scope() -> str:
    return (os.getenv("ANALYTICS_REQUIRED_SCOPE") or "los.ops.read").strip()


def _require_analytics_scope(claims: dict = Depends(require_jwt)) -> dict:
    """Authenticated, and holding the ops scope (configurable)."""
    from app.security.auth import forbidden, get_scopes

    if _analytics_scope() not in get_scopes(claims):
        raise forbidden(f"Required scope missing: {_analytics_scope()}")
    return claims


@router.get("/ops/analytics", summary="Copilot analytics (aggregate only)")
async def copilot_analytics(_: dict = Depends(_require_analytics_scope)) -> dict:
    """
    Aggregate Copilot signals -- intent / stage / language / sentiment
    counts, bypass and fallback rates, latency distributions. No message,
    answer, name, value or id is ever recorded, so none can be returned.
    Also reports whether the CloudWatch export is enabled and working.
    """
    from app.observability import analytics, cloudwatch

    return {**analytics.snapshot(), "cloudwatch": cloudwatch.status()}

def _store_health() -> dict:
    """The case store's own health (PostgreSQL reachable, schema version)."""
    try:
        from app.store import get_repository

        repository = get_repository()
        check = getattr(repository, "health", None)
        return check() if callable(check) else {"available": True}
    except Exception as exc:  # noqa: BLE001 - reported, never raised from a probe
        return {"available": False, "error": type(exc).__name__}


def _jev_status() -> str:
    """READY / DISABLED / CONFIGURATION_GAP / EXTERNAL_DEPENDENCY_REQUIRED -- JEV is optional."""
    try:
        from app.jev import engine

        return str(engine.health().get("jev_status") or "UNKNOWN")
    except Exception:  # noqa: BLE001
        return "UNKNOWN"
