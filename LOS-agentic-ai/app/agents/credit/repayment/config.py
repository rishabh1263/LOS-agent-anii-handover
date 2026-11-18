"""Credit underwriting config: the four files under app/config/credit_underwriting/ (missing or broken -> named error)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DIR = Path(__file__).resolve().parents[3] / "config" / "credit_underwriting"
FILES = ("vendor_fields", "policy", "settings", "wording")


class ConfigError(RuntimeError):
    """A credit underwriting config file is missing or unreadable."""


@lru_cache(maxsize=8)
def _load(name: str) -> dict[str, Any]:
    path = DIR / f"{name}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"unreadable config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} is not a mapping")
    return data


def vendor() -> dict[str, Any]:
    return _load("vendor_fields")


def policy() -> dict[str, Any]:
    return _load("policy")


def wording() -> dict[str, Any]:
    """Every sentence the officer reads (wording.yaml)."""
    return _load("wording")


def settings() -> dict[str, Any]:
    return _load("settings")


def rule(code: str) -> dict[str, Any]:
    """One rule's limits from policy.yaml (KeyError names the rule when it is absent)."""
    rules = policy().get("rules") or {}
    if code not in rules:
        raise ConfigError(f"policy.yaml has no rule {code}")
    return rules[code]


def reload() -> None:
    """Forget the cached files (tests, or after an edit)."""
    _load.cache_clear()


def check() -> None:
    """Every file present and readable -- called at startup so a broken config stops the service by name."""
    for name in FILES:
        _load(name)


__all__ = ["ConfigError", "check", "policy", "reload", "rule", "settings", "vendor", "wording"]
