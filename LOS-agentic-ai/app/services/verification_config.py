"""Configuration for the verification gate."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml


def config_path() -> Path:
    override = (os.getenv("DOCUMENTS_CONFIG_PATH") or "").strip()
    if override:
        return Path(override).resolve()
    return (Path(__file__).resolve().parents[1] / "config" / "documents.yaml")


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    try:
        with config_path().open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}


def reload() -> None:
    """Drop the cached configuration. Used by tests and by a config reload."""
    _load.cache_clear()


def _section() -> dict:
    return _load().get("verification", {}) or {}


def scoring_config() -> dict:
    """
    Per-document verification scoring weights.

    Empty when unset, which is the normal state: a verifier supplies its own
    weights and a configuration block only retunes them. Adding a document
    type to the scoring framework should not require touching code.
    """
    return _section().get("scoring", {}) or {}


def is_enabled(document_type: str) -> bool:
    """
    Whether verification applies to this document type.

    The global switch wins: turning it off disables the gate everywhere,
    which is what an incident rollback needs to be able to do in one edit.
    """
    section = _section()
    if not section.get("global", {}).get("enabled", True):
        return False
    per_doc = section.get("documents", {}).get(document_type, {})
    return bool(per_doc.get("enabled", True))


def block_on_review() -> bool:
    return bool(_section().get("global", {}).get("block_on_review", False))


def name_match_threshold() -> float:
    return float(_section().get("global", {}).get("name_match_threshold", 0.85))


__all__ = ["is_enabled", "block_on_review", "name_match_threshold", "reload"]
