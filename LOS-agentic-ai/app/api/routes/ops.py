"""
Operational endpoints: liveness, readiness, metrics.

One of each. Previously health was served from three places and metrics from
two, which meant a monitoring system could get three different answers about
the same process.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

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

    return {
        "status": "ready",
        "routable_agents": sorted(routable),
        "risk_policy_version": str(policy.get("policy_version", "")),
        "risk_policy_signed_off": bool(policy.get("signed_off", False)),
        "unsigned_policy_override": allow_unsigned_policy(),
        "circuits": resilience.breaker.snapshot(),
        "in_flight": resilience.bulkhead.snapshot(),
    }


@router.get("/metrics", summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    """Agent and orchestration metrics in one scrape."""
    body = metrics.render_prometheus() + metrics.render_orchestration_prometheus()
    return Response(content=body, media_type="text/plain")