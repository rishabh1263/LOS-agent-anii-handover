"""
The crime verification config -- three files under app/config/credit_underwriting/ (missing or broken -> named error):

    crime_verification.yaml           policy: storage, access, vendors, validation, statuses, finalize, report
    crime_verification_wording.yaml   every sentence and label (API messages, screen, report)
    crime_verification_demo.yaml      the synthetic applications, the mock provider's scenarios and coverage

The code holds logic only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
DIR = ROOT / "app" / "config" / "credit_underwriting"


class ConfigError(RuntimeError):
    """A crime verification config file is missing, unreadable, or lacks a key the code asks for."""


@lru_cache(maxsize=4)
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


def cfg() -> dict[str, Any]:
    """The policy in force: rule set crime_verification_policy in the crime_verification schema (2026-11-02, rules live in the
    database; app/agent_db/rule_store.py). crime_verification.yaml is where a version comes from (scripts/rules.py);
    outside production, with no database configured, the file itself."""
    from app.agent_db import rule_store

    value = rule_store.load("crime_verification_policy")
    if not isinstance(value, dict):
        raise ConfigError("the crime_verification policy in force is not a mapping")
    return value


def wording() -> dict[str, Any]:
    return _load("crime_verification_wording")


def demo() -> dict[str, Any]:
    return _load("crime_verification_demo")


def section(name: str) -> dict[str, Any]:
    """One section: a DEPLOYMENT section (agent_databases.yaml rules.sets.crime_verification_policy.file_sections: storage,
    access, ...) from the file -- never the database; every other section from the rules in force."""
    from app.agent_db import rule_store

    if name in rule_store.file_sections("crime_verification_policy"):
        value = rule_store.file_section("crime_verification_policy", name)
    else:
        value = cfg().get(name)
    return value if isinstance(value, dict) else {}


def words(path: str) -> Any:
    """A wording entry by dotted path; a missing one is a named ConfigError, never a silent blank."""
    node: Any = wording()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"crime_verification_wording.yaml has no '{path}'")
        node = node[part]
    return node


def say(path: str, **values: Any) -> str:
    return str(words(path)).format(**values)


def demo_mode() -> bool:
    return bool(cfg().get("demo_mode", False))


def path_from_root(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def reports_dir() -> Path:
    """Where generated reports are kept (CRIME_VERIFICATION_REPORTS_DIR overrides; tests use a temporary folder)."""
    import os

    return path_from_root(os.getenv("CRIME_VERIFICATION_REPORTS_DIR")
                          or str(section("storage").get("reports_dir") or "runtime/crime_verification/reports"))


def reload() -> None:
    from app.agent_db import rule_store

    _load.cache_clear()
    rule_store.forget()
