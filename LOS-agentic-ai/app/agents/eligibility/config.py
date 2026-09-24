"""
Eligibility policy FILE loading, and which provider is in use.

THIS MODULE READS A FILE AND NOTHING MORE. What the file means -- which
product's policy, which criteria, what a null threshold does -- belongs to
`policy.py`, where the `ConfigPolicyProvider` turns this document into an
`EligibilityPolicy`. Keeping the two apart is what lets a future
`ApiPolicyProvider` replace the file without the engine noticing.

Same loader shape as the KYC and income policy loaders: one way to read a
policy file in this project.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.constants import CONFIG_DIR

DEFAULT_POLICY_FILENAME = "eligibility_policy.yaml"

#: The provider used when neither the environment nor the file names one.
DEFAULT_PROVIDER = "config"


def policy_path() -> Path:
    override = os.getenv("ELIGIBILITY_POLICY_PATH")
    if override:
        return Path(override)
    return Path(CONFIG_DIR) / DEFAULT_POLICY_FILENAME


@lru_cache(maxsize=1)
def _load(path_text: str) -> dict[str, Any]:
    """
    The policy document, or an empty one.

    NEVER RAISES. Eligibility is one stage among several; an unreadable
    policy file must not take a document pipeline down with it. An empty
    document has no policies in it, which the provider reports as
    POLICY_UNAVAILABLE -- the truth about an unreadable file.
    """
    path = Path(path_text)
    if not path.exists():
        return {}

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def reset_policy_cache() -> None:
    """Drop the cached document. For tests and controlled reloads."""
    _load.cache_clear()


def document() -> dict[str, Any]:
    """The whole policy file, as loaded."""
    return _load(str(policy_path()))


def provider_name() -> str:
    """
    Which policy provider is active.

    The environment wins over the file, so a deployment can point at the
    policy service without editing a checked-in file.
    """
    chosen = (os.getenv("ELIGIBILITY_POLICY_PROVIDER") or "").strip().lower()
    if chosen:
        return chosen
    return str(document().get("provider") or DEFAULT_PROVIDER).strip().lower()


__all__ = [
    "DEFAULT_PROVIDER", "document", "policy_path", "provider_name",
    "reset_policy_cache",
]
