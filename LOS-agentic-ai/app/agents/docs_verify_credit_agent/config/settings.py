"""
Runtime settings of the Credit document policy agent -- environment only, no secrets.

    DOCS_CREDIT_POLICY_DIR          where the Credit document policies live (default: ../policies)
    DOCS_CREDIT_ALLOW_SAMPLE_POLICY true / false; default true ONLY when ENVIRONMENT is a development one
                                    (development / dev / local / test) -- a SAMPLE policy never decides in production
    DOCS_CREDIT_REQUIRE_AUDIT       true (default): an evaluation whose audit record cannot be written is an error,
                                    never a result
    DOCS_CREDIT_TIMEOUT_SECONDS     the evaluation's deadline (default 10)
    DOCS_CREDIT_SCOPES              the scopes that may evaluate (default "los.credit.underwrite")
    DOCS_CREDIT_STAGE               the LOS stage whose policy is evaluated (default CREDIT)
    DOCS_CREDIT_RULES_OFF           rules switched off on top of the policy's `rules` map, e.g. "RULE-07,RULE-11"
                                    (RULE-10, the audit, is never switched off)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEV = {"development", "dev", "local", "test"}
_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    policy_dir: Path
    allow_sample_policy: bool
    require_audit: bool
    timeout_seconds: float
    scopes: tuple[str, ...]
    environment: str
    target_stage: str = "CREDIT"
    rules_off: tuple[str, ...] = ()


def get() -> Settings:
    """Read on every call: tests and operators change the environment without a restart."""
    env = (os.getenv("ENVIRONMENT") or "production").strip().lower()
    default_dir = Path(__file__).resolve().parents[1] / "policies"
    try:
        timeout = float(os.getenv("DOCS_CREDIT_TIMEOUT_SECONDS") or 10)
    except ValueError:
        timeout = 10.0
    scopes = tuple(s for s in (os.getenv("DOCS_CREDIT_SCOPES") or "los.credit.underwrite").split() if s)
    return Settings(policy_dir=Path(os.getenv("DOCS_CREDIT_POLICY_DIR") or default_dir),
                    allow_sample_policy=_flag("DOCS_CREDIT_ALLOW_SAMPLE_POLICY", env in _DEV),
                    require_audit=_flag("DOCS_CREDIT_REQUIRE_AUDIT", True),
                    timeout_seconds=max(1.0, timeout), scopes=scopes, environment=env,
                    target_stage=(os.getenv("DOCS_CREDIT_STAGE") or "CREDIT").strip().upper(),
                    rules_off=tuple(r.strip().upper() for r in (os.getenv("DOCS_CREDIT_RULES_OFF") or "").split(",")
                                    if r.strip() and r.strip().upper() != "RULE-10"))


__all__ = ["Settings", "get"]
