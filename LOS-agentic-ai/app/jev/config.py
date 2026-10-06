"""JEV configuration: app/config/jev.yaml, each operational value overridable by environment."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).resolve().parents[1] / "config" / "jev.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with open(_PATH, encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("jev") or {}


def reload() -> None:
    _load.cache_clear()


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def enabled() -> bool:
    raw = _env("JEV_ENABLED").lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return bool(_load().get("enabled", False))


def _provider() -> dict[str, Any]:
    return _load().get("provider") or {}


def base_url() -> str:
    return (_env("JEV_BASE_URL") or str(_provider().get("base_url") or "")).rstrip("/")


def api_key() -> str:
    return _env("JEV_API_KEY")


def model() -> str:
    return _env("JEV_MODEL") or str(_provider().get("model") or "")


def path() -> str:
    return str(_provider().get("path") or "/v1/systemone")


def timeout_seconds() -> float:
    try:
        return float(_env("JEV_TIMEOUT_SECONDS") or _provider().get("timeout_seconds") or 8.0)
    except ValueError:
        return 8.0


def retries() -> int:
    return max(0, int(_provider().get("retries", 1) or 0))


def confidence_source() -> str:
    raw = (_env("JEV_CONFIDENCE_SOURCE") or str(_provider().get("confidence_source") or "auto")).upper()
    if raw == "AUTO":
        return "TOP_PROBABILITY" if is_local() else "PROVIDER"
    return raw if raw in {"PROVIDER", "TOP_PROBABILITY"} else "PROVIDER"


def threshold(name: str) -> float:
    env = _env(f"JEV_CONFIDENCE_{name.upper()}")
    try:
        return float(env or (_load().get("thresholds") or {})[name])
    except (KeyError, ValueError, TypeError):
        return {"auto": 0.85, "review": 0.65}[name]


def fallback() -> str:
    return (_env("JEV_FALLBACK") or str(_load().get("fallback") or "HUMAN_REVIEW")).upper()


def trigger_enabled(name: str) -> bool:
    return bool((_load().get("triggers") or {}).get(name, False))


def state_limits() -> tuple[int, int]:
    s = _load().get("state") or {}
    return int(s.get("max_value_chars", 160)), int(s.get("max_state_chars", 12000))


def question_set(scope: str) -> dict[str, Any]:
    found = (_load().get("question_sets") or {}).get(scope)
    if not found:
        raise KeyError(f"No JEV question set '{scope}'")
    return found


def actions() -> dict[str, dict[str, Any]]:
    return _load().get("actions") or {}


def provider_name() -> str:
    url = base_url()
    if not url:
        return "NONE"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return f"jev-api@{host}"


def is_local(url: str | None = None) -> bool:
    host = (url or base_url()).split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in {"localhost", "127.0.0.1", "::1"}
