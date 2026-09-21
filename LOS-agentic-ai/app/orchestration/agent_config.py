"""
Agent configuration.

Single source of truth for agent execution settings, loaded from
app/config/agents.yaml. Model names, timeouts, routing and limits are
resolved here so business logic and API routes never hardcode them.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import CONFIG_DIR
from app.core.exceptions import ConfigurationError

DEFAULT_AGENTS_FILENAME = "agents.yaml"

# Tool names an agent may declare. Empty for Agent 2: the deterministic engine
# uses no tools. Populate as MCP tools are introduced in a later phase.
KNOWN_TOOLS: set[str] = set()


class AgentConfig(BaseModel):
    """Execution configuration for one agent."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    agent_id: str
    agent_name: str = ""
    stage: str = ""
    enabled: bool = False
    version: str = "1.0.0"

    # LLM settings. Applies to summary generation only for Agent 2; the
    # deterministic engine never calls a model.
    model: str = "qwen3:30b"
    temperature: float = 0.1
    prompt_version: str = "v1"

    # Tool names this agent may use. Validated at load time against the
    # registry, so a typo fails at startup rather than at 3am.
    tools: list[str] = Field(default_factory=list)

    input_schema: str | None = None
    output_schema: str | None = None
    # Optional trimmed view returned to callers by default. The full record is
    # still produced, validated and audited; this only controls what goes over
    # the wire.
    compact_output_schema: str | None = None

    approval_required: bool = False
    max_iterations: int = Field(default=1, ge=1, le=10)
    timeout: float = Field(default=30.0, gt=0, le=600)
    fallback_agent: str | None = None

    # Whether this agent is routable through the LangGraph flow. The document
    # agent runs on the separate agent_framework workflow, so it is excluded.
    langgraph_enabled: bool = True


def agents_config_path() -> Path:
    override = os.getenv("AGENTS_CONFIG_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_AGENTS_FILENAME


def load_agent_configs(path: str | Path | None = None) -> dict[str, AgentConfig]:
    """
    Load and validate every agent entry.

    Raises ConfigurationError on a missing or malformed file rather than
    falling back to defaults: unknown routing configuration must not be
    guessed at runtime.
    """
    target = Path(path) if path is not None else agents_config_path()

    if not target.exists():
        raise ConfigurationError(f"Agent configuration not found: {target}")

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Agent configuration is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "agents" not in raw:
        raise ConfigurationError("Agent configuration must contain an 'agents' mapping.")

    defaults: dict[str, Any] = raw.get("defaults") or {}
    configs: dict[str, AgentConfig] = {}

    for agent_id, entry in (raw["agents"] or {}).items():
        entry = entry or {}
        merged = {**defaults, **entry}
        merged.setdefault("agent_id", agent_id)
        merged.setdefault("agent_name", agent_id)
        try:
            configs[agent_id] = AgentConfig(**merged)
        except Exception as exc:
            raise ConfigurationError(
                f"Invalid configuration for agent '{agent_id}': {exc}"
            ) from exc

    _validate_cross_references(configs)
    return configs


def _validate_cross_references(configs: dict[str, AgentConfig]) -> None:
    """
    Checks that only make sense once every agent is loaded.

    Catching these at startup turns a 3am production failure into a failed
    deploy, which is the whole point of a configuration layer.
    """
    for agent_id, cfg in configs.items():
        if not cfg.enabled:
            continue

        # A fallback pointing at a missing or disabled agent is a silent trap:
        # it only surfaces when the primary is already failing.
        if cfg.fallback_agent:
            target = configs.get(cfg.fallback_agent)
            if target is None:
                raise ConfigurationError(
                    f"Agent '{agent_id}' declares fallback_agent "
                    f"'{cfg.fallback_agent}', which is not configured."
                )
            if not target.enabled:
                raise ConfigurationError(
                    f"Agent '{agent_id}' declares fallback_agent "
                    f"'{cfg.fallback_agent}', which is disabled."
                )
            if target.fallback_agent == agent_id:
                raise ConfigurationError(
                    f"Fallback loop between '{agent_id}' and '{cfg.fallback_agent}'."
                )

        if cfg.temperature < 0 or cfg.temperature > 2:
            raise ConfigurationError(
                f"Agent '{agent_id}': temperature {cfg.temperature} is outside 0-2."
            )

        if cfg.tools:
            unknown = [t for t in cfg.tools if t not in KNOWN_TOOLS]
            if unknown:
                raise ConfigurationError(
                    f"Agent '{agent_id}' declares unknown tools: {unknown}. "
                    f"Known: {sorted(KNOWN_TOOLS)}"
                )


@lru_cache(maxsize=4)
def _cached(resolved: str) -> dict[str, AgentConfig]:
    return load_agent_configs(resolved)


def get_agent_configs(path: str | Path | None = None) -> dict[str, AgentConfig]:
    target = Path(path) if path is not None else agents_config_path()
    return _cached(str(target))


def get_agent_config(agent_id: str) -> AgentConfig | None:
    return get_agent_configs().get(agent_id)


def clear_agent_config_cache() -> None:
    _cached.cache_clear()


__all__ = [
    "AgentConfig",
    "agents_config_path",
    "load_agent_configs",
    "get_agent_configs",
    "get_agent_config",
    "clear_agent_config_cache",
]