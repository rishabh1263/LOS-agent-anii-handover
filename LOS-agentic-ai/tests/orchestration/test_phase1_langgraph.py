"""
Phase 1 tests: Agent Configuration, Registry, LangGraph, Agent Service.

The central guarantee verified here: routing Agent 2 through LangGraph does
NOT change its deterministic risk_score, risk_category or final_outcome.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.fraud_risk.agent import FraudRiskAgent
from app.agents.fraud_risk.schemas import FraudRiskRequest
from app.api.routes.agent_service import router as agent_service_router
from app.core.exceptions import ConfigurationError
from app.orchestration import registry
from app.orchestration.agent_config import (
    AgentConfig,
    agents_config_path,
    clear_agent_config_cache,
    get_agent_config,
    load_agent_configs,
)
from app.orchestration.graph import build_agent_graph, run_agent

SAMPLE = json.loads(Path("sample_request.json").read_text())


@pytest.fixture
def client(auth_headers) -> TestClient:
    """Authenticated: /agents/execute is a protected route."""
    app = FastAPI()
    app.include_router(agent_service_router, prefix="/api/v1")
    client = TestClient(app)
    client.headers.update(auth_headers)
    return client


@pytest.fixture
def unauthenticated(self=None) -> TestClient:
    """No credentials, for asserting the route is protected."""
    app = FastAPI()
    app.include_router(agent_service_router, prefix="/api/v1")
    return TestClient(app)


# =========================================================================
# 1. AGENT CONFIGURATION
# =========================================================================


def test_all_configured_agents_load_and_validate():
    configs = load_agent_configs(agents_config_path())
    assert "fraud_risk_agent" in configs
    assert all(isinstance(c, AgentConfig) for c in configs.values())


def test_agent2_config_has_every_required_field():
    cfg = get_agent_config("fraud_risk_agent")
    for field in (
        "agent_id",
        "agent_name",
        "stage",
        "enabled",
        "model",
        "temperature",
        "tools",
        "prompt_version",
        "input_schema",
        "output_schema",
        "approval_required",
        "max_iterations",
        "timeout",
        "fallback_agent",
    ):
        assert hasattr(cfg, field), f"missing config field: {field}"


def test_defaults_are_merged_into_sparse_entries():
    """Agents declaring only 'enabled' still receive defaults."""
    cfg = get_agent_config("income")
    assert cfg.enabled is False
    assert cfg.model  # from defaults
    assert cfg.timeout > 0


def test_model_is_not_hardcoded_in_business_logic():
    """Model selection must come from configuration."""
    cfg = get_agent_config("fraud_risk_agent")
    assert cfg.model.startswith("qwen")


def test_missing_config_file_raises():
    with pytest.raises(ConfigurationError):
        load_agent_configs("/nonexistent/agents.yaml")


def test_invalid_config_is_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text("agents:\n  x:\n    timeout: -5\n")
    with pytest.raises(ConfigurationError):
        load_agent_configs(bad)


def test_config_without_agents_key_is_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text("something_else: true\n")
    with pytest.raises(ConfigurationError):
        load_agent_configs(bad)


# =========================================================================
# 2. REGISTRY
# =========================================================================


def test_agent2_is_registered():
    assert registry.is_registered("fraud_risk_agent")


def test_resolve_returns_handler_and_config():
    handler, cfg = registry.resolve("fraud_risk_agent")
    assert callable(handler)
    assert cfg.agent_id == "fraud_risk_agent"


def test_unknown_agent_raises():
    with pytest.raises(registry.UnknownAgentError):
        registry.resolve("does_not_exist")


def test_future_agents_are_not_routable():
    """Phase 2 placeholders must not be executable yet."""
    for agent_id in ("credit_agent", "rcu_agent", "decision_agent"):
        assert get_agent_config(agent_id) is not None
        assert not registry.is_registered(agent_id)


def test_stage_resolution():
    assert registry.resolve_by_stage("risk_assessment") == "fraud_risk_agent"


def test_unknown_stage_raises():
    with pytest.raises(registry.UnknownAgentError):
        registry.resolve_by_stage("no_such_stage")


def test_document_agent_backwards_compatible():
    """The pre-existing helper must still exist."""
    assert hasattr(registry, "get_document_agent")


# =========================================================================
# 3. LANGGRAPH
# =========================================================================


def test_graph_compiles():
    assert build_agent_graph() is not None


@pytest.mark.asyncio
async def test_graph_executes_agent2():
    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="T1")
    assert state["status"] == "success"
    assert state["result"]["agent"] == "fraud_risk_agent"


@pytest.mark.asyncio
async def test_graph_visits_every_node():
    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="T2")
    trace = " ".join(state["trace"])
    assert "config loaded" in trace
    assert "resolved from registry" in trace
    assert "input validated" in trace
    assert "executed on attempt 1" in trace
    assert "output validated" in trace


@pytest.mark.asyncio
async def test_graph_routes_by_stage():
    state = await run_agent(stage="risk_assessment", payload=SAMPLE, request_id="T3")
    assert state["status"] == "success"
    assert state["agent_id"] == "fraud_risk_agent"


@pytest.mark.asyncio
async def test_graph_rejects_invalid_input():
    state = await run_agent(
        agent_id="fraud_risk_agent",
        payload={"verifications": {"itr": "MAYBE"}},
        request_id="T4",
    )
    assert state["status"] == "failed"
    assert state["error_type"] == "invalid_input"


@pytest.mark.asyncio
async def test_graph_rejects_unknown_agent():
    state = await run_agent(agent_id="ghost", payload={}, request_id="T5")
    assert state["status"] == "failed"
    assert state["error_type"] == "unknown_agent"


@pytest.mark.asyncio
async def test_graph_rejects_missing_agent_and_stage():
    state = await run_agent(payload={}, request_id="T6")
    assert state["status"] == "failed"
    assert state["error_type"] == "bad_request"


@pytest.mark.asyncio
async def test_disabled_agent_is_not_executed(monkeypatch, tmp_path):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text(
        "defaults:\n  timeout: 5\nagents:\n"
        "  fraud_risk_agent:\n"
        "    agent_id: fraud_risk_agent\n"
        "    agent_name: Fraud\n"
        "    stage: risk_assessment\n"
        "    enabled: false\n"
    )
    monkeypatch.setenv("AGENTS_CONFIG_PATH", str(cfg))
    clear_agent_config_cache()

    state = await run_agent(agent_id="fraud_risk_agent", payload={}, request_id="T7")
    assert state["status"] == "failed"
    assert state["error_type"] == "agent_disabled"

    clear_agent_config_cache()


# ---- timeout / retries / fallback -------------------------------------


@pytest.mark.asyncio
async def test_configured_timeout_is_enforced(monkeypatch):
    async def slow(payload, config, request_id):
        await asyncio.sleep(5)
        return {}

    cfg = AgentConfig(
        agent_id="slow_agent", agent_name="Slow", enabled=True, timeout=0.1
    )
    monkeypatch.setitem(registry._HANDLERS, "slow_agent", slow)
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda aid: cfg)
    monkeypatch.setattr(registry, "get_agent_config", lambda aid: cfg)
    state = await run_agent(agent_id="slow_agent", payload={}, request_id="T8")
    assert state["status"] == "failed"
    assert "timeout" in state["error"]


@pytest.mark.asyncio
async def test_max_iterations_retries(monkeypatch):
    calls = {"n": 0}

    async def flaky(payload, config, request_id):
        calls["n"] += 1
        raise RuntimeError("boom")

    cfg = AgentConfig(
        agent_id="flaky_agent", agent_name="Flaky", enabled=True, max_iterations=3
    )
    monkeypatch.setitem(registry._HANDLERS, "flaky_agent", flaky)
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda aid: cfg)
    monkeypatch.setattr(registry, "get_agent_config", lambda aid: cfg)

    state = await run_agent(agent_id="flaky_agent", payload={}, request_id="T9")
    assert calls["n"] == 3
    assert state["status"] == "failed"


@pytest.mark.asyncio
async def test_fallback_agent_used_when_configured(monkeypatch):
    async def broken(payload, config, request_id):
        raise RuntimeError("primary down")

    async def backup(payload, config, request_id):
        return {"agent": "backup_agent", "ok": True}

    configs = {
        "primary_agent": AgentConfig(
            agent_id="primary_agent",
            agent_name="Primary",
            enabled=True,
            fallback_agent="backup_agent",
        ),
        "backup_agent": AgentConfig(
            agent_id="backup_agent", agent_name="Backup", enabled=True
        ),
    }
    monkeypatch.setitem(registry._HANDLERS, "primary_agent", broken)
    monkeypatch.setitem(registry._HANDLERS, "backup_agent", backup)
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", configs.get)
    monkeypatch.setattr(registry, "get_agent_config", configs.get)

    state = await run_agent(agent_id="primary_agent", payload={}, request_id="T10")
    assert state["status"] == "success"
    assert state["used_fallback"] is True
    assert state["fallback_from"] == "primary_agent"


@pytest.mark.asyncio
async def test_no_fallback_when_not_configured(monkeypatch):
    async def broken(payload, config, request_id):
        raise RuntimeError("down")

    cfg = AgentConfig(
        agent_id="lonely_agent", agent_name="Lonely", enabled=True, fallback_agent=None
    )
    monkeypatch.setitem(registry._HANDLERS, "lonely_agent", broken)
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda aid: cfg)
    monkeypatch.setattr(registry, "get_agent_config", lambda aid: cfg)

    state = await run_agent(agent_id="lonely_agent", payload={}, request_id="T11")
    assert state["status"] == "failed"
    assert state["used_fallback"] is False


# =========================================================================
# 4. AGENT 2 DETERMINISM PRESERVED THROUGH LANGGRAPH
# =========================================================================


@pytest.mark.asyncio
async def test_langgraph_does_not_change_the_risk_decision():
    """The central Phase 1 guarantee."""
    direct = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(**SAMPLE))
    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="T12")
    routed = state["result"]

    assert routed["risk_score"] == direct.risk_score
    assert routed["risk_category"] == direct.risk_category.value
    assert routed["final_outcome"] == direct.final_outcome.value
    assert [f["rule"] for f in routed["flags"]] == [f.rule for f in direct.flags]


@pytest.mark.asyncio
async def test_routed_result_is_deterministic_across_runs():
    results = []
    for i in range(5):
        state = await run_agent(
            agent_id="fraud_risk_agent", payload=SAMPLE, request_id=f"T13-{i}"
        )
        r = state["result"]
        results.append((r["risk_score"], r["risk_category"], r["final_outcome"]))
    assert len(set(results)) == 1


@pytest.mark.asyncio
async def test_mandatory_document_result_preserved():
    payload = dict(SAMPLE)
    payload["documents"] = {"sections_present": ["Age Proof"]}
    state = await run_agent(agent_id="fraud_risk_agent", payload=payload, request_id="T14")
    rules = [f["rule"] for f in state["result"]["flags"]]
    assert "MANDATORY_DOCS_MISSING" in rules


# =========================================================================
# 5. PYTHON AGENT SERVICE (HTTP)
# =========================================================================


def test_list_agents(client):
    body = client.get("/api/v1/agents/").json()
    by_id = {a["agent_id"]: a for a in body}
    assert by_id["fraud_risk_agent"]["routable"] is True
    assert by_id["credit_agent"]["routable"] is False


def test_execute_returns_risk_assessment(client):
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"]["risk_score"] == 100
    assert body["result"]["final_outcome"] == "FAIL"


def test_execute_by_stage(client):
    response = client.post(
        "/api/v1/agents/execute",
        json={"stage": "risk_assessment", "payload": SAMPLE},
    )
    assert response.status_code == 200
    assert response.json()["agent_id"] == "fraud_risk_agent"


def test_execute_requires_agent_or_stage(client):
    assert client.post("/api/v1/agents/execute", json={"payload": {}}).status_code == 400


def test_execute_unknown_agent_returns_404(client):
    response = client.post(
        "/api/v1/agents/execute", json={"agent_id": "ghost", "payload": {}}
    )
    assert response.status_code == 404
    assert response.json()["error_type"] == "unknown_agent"


def test_execute_invalid_payload_returns_422(client):
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": {"verifications": {"itr": "X"}}},
    )
    assert response.status_code == 422
    assert response.json()["error_type"] == "invalid_input"


def test_execute_rejects_unknown_fields(client):
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": {}, "surprise": 1},
    )
    assert response.status_code == 422


def test_request_id_echoed(client):
    response = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
        headers={"X-Request-ID": "NET-LOS-9"},
    )
    assert response.headers["X-Request-ID"] == "NET-LOS-9"
    assert response.json()["request_id"] == "NET-LOS-9"


def test_orchestration_route_requires_authentication(unauthenticated):
    assert unauthenticated.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).status_code == 401


def test_orchestration_is_the_only_path_to_the_agent(auth_headers):
    """
    The direct /api/v1/risk/assess route was removed. It bypassed LangGraph,
    so it ignored agents.yaml configuration, the circuit breaker, the bulkhead
    and approval_required. One agent must have one entry point obeying one set
    of rules.
    """
    from app.api.routes.agent_service import router as svc

    app = FastAPI()
    app.include_router(svc, prefix="/api/v1")
    c = TestClient(app)
    c.headers.update(auth_headers)

    assert c.post("/api/v1/risk/assess", json={}).status_code == 404
    assert c.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).status_code == 200


# =========================================================================
# 6. RESILIENCE — retries, circuit breaker, bulkhead
# =========================================================================


from app.orchestration import resilience  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_resilience():
    resilience.breaker.reset()
    resilience.bulkhead.reset()
    yield
    resilience.breaker.reset()
    resilience.bulkhead.reset()


def _install(monkeypatch, agent_id: str, handler, **cfg_kwargs):
    cfg = AgentConfig(agent_id=agent_id, agent_name=agent_id, enabled=True, **cfg_kwargs)
    monkeypatch.setitem(registry._HANDLERS, agent_id, handler)
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda a: cfg)
    monkeypatch.setattr(registry, "get_agent_config", lambda a: cfg)
    return cfg


# ---- retry classification -------------------------------------------


def test_classify_transient_vs_permanent():
    assert resilience.classify(asyncio.TimeoutError()) is resilience.ErrorClass.TRANSIENT
    assert resilience.classify(ConnectionError()) is resilience.ErrorClass.TRANSIENT
    assert resilience.classify(ValueError("bad")) is resilience.ErrorClass.PERMANENT
    assert resilience.classify(TypeError("bad")) is resilience.ErrorClass.PERMANENT


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def permanent(payload, config, request_id):
        calls["n"] += 1
        raise ValueError("will never succeed")

    _install(monkeypatch, "perm_agent", permanent, max_iterations=5)
    state = await run_agent(agent_id="perm_agent", payload={}, request_id="R1")
    assert calls["n"] == 1
    assert state["status"] == "failed"


@pytest.mark.asyncio
async def test_transient_error_is_retried(monkeypatch):
    calls = {"n": 0}

    async def flaky(payload, config, request_id):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("temporarily down")
        return {"ok": True}

    _install(monkeypatch, "flaky2", flaky, max_iterations=5)
    state = await run_agent(agent_id="flaky2", payload={}, request_id="R2")
    assert calls["n"] == 3
    assert state["status"] == "success"


def test_backoff_grows_and_is_jittered():
    assert resilience.backoff_delay(1) <= 0.2
    assert resilience.backoff_delay(3) <= 0.8
    assert resilience.backoff_delay(50) <= 5.0
    samples = {round(resilience.backoff_delay(4), 6) for _ in range(20)}
    assert len(samples) > 1, "backoff must be jittered, not fixed"


# ---- circuit breaker -------------------------------------------------


def test_breaker_opens_after_threshold():
    breaker = resilience.CircuitBreaker(threshold=3, reset_timeout=60)
    for _ in range(3):
        breaker.record_failure("a")
    assert breaker.state("a") is resilience.BreakerState.OPEN
    with pytest.raises(resilience.CircuitOpenError):
        breaker.check("a")


def test_breaker_half_opens_then_closes():
    # Margin is deliberately wide: Windows clock granularity is ~15.6ms, so a
    # 0.05s timeout with a 0.06s sleep flakes. 0.1 vs 0.4 is safe everywhere.
    breaker = resilience.CircuitBreaker(threshold=2, reset_timeout=0.1)
    breaker.record_failure("a")
    breaker.record_failure("a")
    assert breaker.state("a") is resilience.BreakerState.OPEN

    import time as _time

    _time.sleep(0.4)
    breaker.check("a")  # transitions to half-open
    assert breaker.state("a") is resilience.BreakerState.HALF_OPEN

    breaker.record_success("a")
    assert breaker.state("a") is resilience.BreakerState.CLOSED


def test_breaker_reopens_if_probe_fails():
    breaker = resilience.CircuitBreaker(threshold=2, reset_timeout=0.1)
    breaker.record_failure("a")
    breaker.record_failure("a")

    import time as _time

    _time.sleep(0.4)
    breaker.check("a")
    breaker.record_failure("a")
    assert breaker.state("a") is resilience.BreakerState.OPEN


def test_breaker_is_per_agent():
    breaker = resilience.CircuitBreaker(threshold=2, reset_timeout=60)
    breaker.record_failure("a")
    breaker.record_failure("a")
    assert breaker.state("a") is resilience.BreakerState.OPEN
    assert breaker.state("b") is resilience.BreakerState.CLOSED


@pytest.mark.asyncio
async def test_breaker_short_circuits_a_dead_agent(monkeypatch):
    """A dead agent must stop being called, not hammered forever."""
    calls = {"n": 0}

    async def dead(payload, config, request_id):
        calls["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(
        resilience, "breaker", resilience.CircuitBreaker(threshold=3, reset_timeout=60)
    )
    _install(monkeypatch, "dead_agent", dead, max_iterations=1)

    for i in range(10):
        await run_agent(agent_id="dead_agent", payload={}, request_id=f"R3-{i}")

    assert calls["n"] == 3, "breaker should stop calls after 3 failures"
    assert resilience.breaker.state("dead_agent") is resilience.BreakerState.OPEN


# ---- bulkhead --------------------------------------------------------


@pytest.mark.asyncio
async def test_bulkhead_caps_concurrency(monkeypatch):
    peak = {"n": 0, "current": 0}

    async def slow(payload, config, request_id):
        peak["current"] += 1
        peak["n"] = max(peak["n"], peak["current"])
        await asyncio.sleep(0.05)
        peak["current"] -= 1
        return {"ok": True}

    monkeypatch.setattr(resilience, "bulkhead", resilience.Bulkhead(limit=3))
    _install(monkeypatch, "slow2", slow, timeout=10, max_iterations=1)

    await asyncio.gather(
        *[run_agent(agent_id="slow2", payload={}, request_id=f"R4-{i}") for i in range(12)]
    )
    assert peak["n"] <= 3, f"concurrency exceeded the cap: {peak['n']}"


# ---- switches --------------------------------------------------------


@pytest.mark.asyncio
async def test_resilience_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_RETRY_ENABLED", "false")
    calls = {"n": 0}

    async def flaky(payload, config, request_id):
        calls["n"] += 1
        raise ConnectionError("down")

    _install(monkeypatch, "noretry", flaky, max_iterations=5)
    await run_agent(agent_id="noretry", payload={}, request_id="R5")
    assert calls["n"] == 1


# ---- metrics ---------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestration_metrics_recorded():
    from app.agents.fraud_risk import metrics

    before = metrics.orchestration_snapshot()["total"]
    await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="R6")
    after = metrics.orchestration_snapshot()
    assert after["total"] == before + 1
    assert "fraud_risk_agent|success" in after["by_agent_status"]


def test_orchestration_prometheus_renders():
    from app.agents.fraud_risk import metrics

    text = metrics.render_orchestration_prometheus()
    assert "# TYPE orchestration_total counter" in text


# ---- the guarantee still holds --------------------------------------


@pytest.mark.asyncio
async def test_resilience_does_not_change_the_risk_decision():
    direct = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(**SAMPLE))
    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="R7")
    assert state["result"]["risk_score"] == direct.risk_score
    assert state["result"]["final_outcome"] == direct.final_outcome.value


# =========================================================================
# 7. COMPACT RESPONSE ENVELOPE
# =========================================================================


def test_execute_response_is_compact_by_default(client):
    body = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).json()

    # No null noise on a healthy call.
    for key in ("error", "error_type", "trace", "attempts", "used_fallback"):
        assert key not in body, f"'{key}' should be omitted on success"

    result = body["result"]
    for key in ("evidence", "data_gaps", "rules_not_implemented", "policy_version"):
        assert key not in result, f"audit field '{key}' leaked into compact result"

    assert isinstance(result["flags"][0], str)
    assert result["risk_score"] == 100


def test_detail_returns_full_record_and_diagnostics(client):
    body = client.post(
        "/api/v1/agents/execute?detail=true",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).json()

    assert "trace" in body and len(body["trace"]) == 5
    assert body["attempts"] == 1
    assert "evidence" in body["result"]
    assert isinstance(body["result"]["flags"][0], dict)


def test_compact_and_detail_agree_on_the_decision(client):
    compact = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).json()["result"]
    full = client.post(
        "/api/v1/agents/execute?detail=true",
        json={"agent_id": "fraud_risk_agent", "payload": SAMPLE},
    ).json()["result"]

    for key in ("risk_score", "risk_category", "final_outcome"):
        assert compact[key] == full[key]


def test_diagnostics_appear_on_failure(client):
    body = client.post(
        "/api/v1/agents/execute",
        json={"agent_id": "fraud_risk_agent", "payload": {"verifications": {"itr": "X"}}},
    ).json()
    assert body["error_type"] == "invalid_input"
    assert body["trace"] is not None, "trace must be present when something fails"


# =========================================================================
# 8. CONFIGURATION IS ENFORCED, NOT DECORATIVE
# =========================================================================


def test_model_temperature_prompt_version_reach_the_llm():
    """
    Regression: these three were declared in config, printed in the trace, and
    then ignored -- the agent used environment defaults instead. A config
    field that does not change behaviour is worse than no field, because the
    trace lies about what ran.
    """
    cfg = AgentConfig(
        agent_id="fraud_risk_agent",
        agent_name="Fraud",
        enabled=True,
        model="qwen3:235b",
        temperature=0.85,
        prompt_version="v2",
        timeout=44,
    )
    generator = FraudRiskAgent(agent_config=cfg)._make_generator()
    assert generator.model == "qwen3:235b"
    assert generator.temperature == 0.85
    assert generator.prompt_version == "v2"
    assert generator.timeout == 44


def test_prompt_versions_are_distinct():
    from app.agents.fraud_risk.prompts import get_system_prompt

    assert get_system_prompt("v1") != get_system_prompt("v2")


def test_unknown_prompt_version_is_rejected():
    from app.agents.fraud_risk.prompts import (
        UnknownPromptVersionError,
        get_system_prompt,
    )

    with pytest.raises(UnknownPromptVersionError):
        get_system_prompt("v99")


@pytest.mark.asyncio
async def test_bad_prompt_version_fails_before_execution(monkeypatch):
    cfg = AgentConfig(
        agent_id="fraud_risk_agent",
        agent_name="Fraud",
        enabled=True,
        prompt_version="does_not_exist",
    )
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda a: cfg)
    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="C1")
    assert state["status"] == "failed"
    assert state["error_type"] == "invalid_config"


@pytest.mark.asyncio
async def test_approval_required_marks_the_result(monkeypatch):
    cfg = AgentConfig(
        agent_id="fraud_risk_agent",
        agent_name="Fraud",
        enabled=True,
        approval_required=True,
        input_schema="app.agents.fraud_risk.schemas.FraudRiskRequest",
        output_schema="app.agents.fraud_risk.schemas.FraudRiskResponse",
    )
    monkeypatch.setattr("app.orchestration.graph.get_agent_config", lambda a: cfg)
    monkeypatch.setattr(registry, "get_agent_config", lambda a: cfg)

    state = await run_agent(agent_id="fraud_risk_agent", payload=SAMPLE, request_id="C2")
    assert state["status"] == "pending_approval"
    assert state["approval_required"] is True
    # The assessment still ran and is still correct.
    assert state["result"]["risk_score"] == 100


def test_agent_name_is_carried():
    cfg = get_agent_config("fraud_risk_agent")
    assert cfg.agent_name == "Fraud & Risk Agent"


# ---- load-time cross-validation ----------------------------------------


def test_unknown_fallback_agent_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "agents:\n  a:\n    agent_id: a\n    agent_name: A\n"
        "    enabled: true\n    fallback_agent: ghost\n"
    )
    with pytest.raises(ConfigurationError, match="not configured"):
        load_agent_configs(bad)


def test_disabled_fallback_agent_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "agents:\n"
        "  a:\n    agent_id: a\n    agent_name: A\n    enabled: true\n    fallback_agent: b\n"
        "  b:\n    agent_id: b\n    agent_name: B\n    enabled: false\n"
    )
    with pytest.raises(ConfigurationError, match="disabled"):
        load_agent_configs(bad)


def test_fallback_loop_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "agents:\n"
        "  a:\n    agent_id: a\n    agent_name: A\n    enabled: true\n    fallback_agent: b\n"
        "  b:\n    agent_id: b\n    agent_name: B\n    enabled: true\n    fallback_agent: a\n"
    )
    with pytest.raises(ConfigurationError, match="loop"):
        load_agent_configs(bad)


def test_unknown_tool_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "agents:\n  a:\n    agent_id: a\n    agent_name: A\n"
        "    enabled: true\n    tools: ['no_such_tool']\n"
    )
    with pytest.raises(ConfigurationError, match="unknown tools"):
        load_agent_configs(bad)


def test_out_of_range_temperature_rejected(tmp_path):
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "agents:\n  a:\n    agent_id: a\n    agent_name: A\n"
        "    enabled: true\n    temperature: 5.0\n"
    )
    with pytest.raises(ConfigurationError, match="temperature"):
        load_agent_configs(bad)


def test_shipped_config_passes_cross_validation():
    assert load_agent_configs(agents_config_path())
