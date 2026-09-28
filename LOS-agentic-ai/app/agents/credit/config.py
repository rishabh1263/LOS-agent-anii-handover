"""
Credit Underwriting Agent -- settings.

ONE SOURCE: the `credit_agent` entry in agents.yaml (AgentConfig keeps extra
keys). An environment variable may override a value for a deployment; the
code carries only safe defaults, used when the entry omits a key.

Nothing here is a secret, and nothing here is a business threshold: those
live in underwriting_policy.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.constants import CONFIG_DIR

AGENT_ID = "credit_agent"

DEFAULT_SCOPE = "los.credit.underwrite"
DEFAULT_STAGES = ("CREDIT",)
DEFAULT_POLICY_FILE = "underwriting_policy.yaml"
DEFAULT_BUREAU_FIXTURES = "demo/bureau_demo.yaml"


def _entry() -> Any:
    from app.orchestration.agent_config import get_agent_config

    try:
        return get_agent_config(AGENT_ID)
    except Exception:  # pragma: no cover - a broken agents.yaml fails elsewhere
        return None


def _get(name: str, default: Any) -> Any:
    entry = _entry()
    if entry is None:
        return default
    value = getattr(entry, name, None)
    if value is None:
        value = (getattr(entry, "model_extra", None) or {}).get(name)
    return default if value is None else value


def enabled() -> bool:
    override = os.getenv("CREDIT_AGENT_ENABLED")
    if override is not None:
        return override.lower() == "true"
    return bool(_get("enabled", False))


def version() -> str:
    return str(_get("version", "0.1.0-demo"))


def required_scope() -> str:
    """The scope a caller must hold to run underwriting (G5)."""
    return str(os.getenv("CREDIT_REQUIRED_SCOPE") or _get("required_scope", DEFAULT_SCOPE))


def allowed_stages() -> tuple[str, ...]:
    """The LOS stages a case may be in when underwriting runs (G5)."""
    override = os.getenv("CREDIT_ALLOWED_STAGES")
    if override:
        return tuple(s.strip().upper() for s in override.split(",") if s.strip())
    stages = _get("allowed_stages", list(DEFAULT_STAGES))
    return tuple(str(s).upper() for s in (stages or DEFAULT_STAGES))


def max_tool_calls() -> int:
    """The hard bound on tool executions in one run."""
    return int(_get("max_iterations", 10))


def max_replans() -> int:
    return int(_get("max_replans", 2))


def timeout_seconds() -> float:
    """The agent's own run deadline. Below the orchestration `timeout`, so the
    agent stops cleanly (state intact) before the orchestrator cancels it."""
    return float(_get("deadline_seconds", 20.0))


def bureau_provider() -> str:
    return str(os.getenv("CREDIT_BUREAU_PROVIDER") or _get("bureau_provider", "demo")).lower()


def bureau_timeout_seconds() -> float:
    return float(_get("bureau_timeout_seconds", 3.0))


def bureau_retries() -> int:
    """Retries on a TRANSIENT bureau failure only. Never more than one."""
    return max(0, min(1, int(_get("bureau_retries", 1))))


def llm_enabled() -> bool:
    override = os.getenv("CREDIT_MEMO_LLM_ENABLED")
    if override is not None:
        return override.lower() == "true"
    return bool(_get("llm_enabled", True))


def memo_timeout_seconds() -> float:
    return float(_get("memo_timeout_seconds", 2.5))


def policy_path() -> Path:
    override = os.getenv("UNDERWRITING_POLICY_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / str(_get("policy_file", DEFAULT_POLICY_FILE))


def bureau_fixtures_path() -> Path:
    override = os.getenv("CREDIT_DEMO_BUREAU_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_BUREAU_FIXTURES


def environment() -> str:
    return os.getenv("ENVIRONMENT", "development").lower()


def is_production() -> bool:
    return environment() in {"production", "prod"}


def allow_unsigned_policy() -> bool:
    """Escape hatch for staging. Never set this true in production."""
    return os.getenv("ALLOW_UNSIGNED_UNDERWRITING_POLICY", "false").lower() == "true"


def allow_demo_bureau() -> bool:
    """A demo bureau in production is refused unless deliberately allowed."""
    return os.getenv("ALLOW_DEMO_BUREAU", "false").lower() == "true"


__all__ = [
    "AGENT_ID", "allow_demo_bureau", "allow_unsigned_policy", "allowed_stages",
    "bureau_fixtures_path", "bureau_provider", "bureau_retries",
    "bureau_timeout_seconds", "enabled", "environment", "is_production",
    "llm_enabled", "max_replans", "max_tool_calls", "memo_timeout_seconds",
    "policy_path", "required_scope", "timeout_seconds", "version",
]
