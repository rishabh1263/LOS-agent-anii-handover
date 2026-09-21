"""
Reads the policy files. Caches them. Nothing else.

ONE FILE PER PRODUCT, in app/config/policies/, named after the product:
personal_loan.yaml holds PERSONAL_LOAN. A product with no file is not an
error -- the engine falls back to the applicant-agent checklist, which is
what every product used before policies existed.

WHY A SEPARATE DIRECTORY rather than more keys in applicant_agent.yaml. A
lender's document matrix is reviewed, signed off and versioned by people who
have no business editing an agent's timeouts. Keeping the two apart means a
policy change is a diff a credit officer can read.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] | None = None

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "config" / "policies"


def policies_dir() -> Path:
    return Path(os.getenv("FOS_POLICY_DIR") or _DEFAULT_DIR)


def _load_all() -> dict[str, dict[str, Any]]:
    global _CACHE

    if _CACHE is not None:
        return _CACHE

    with _LOCK:
        if _CACHE is not None:
            return _CACHE

        loaded: dict[str, dict[str, Any]] = {}
        directory = policies_dir()
        if not directory.is_dir():
            logger.info("No policy directory at %s; using the agent checklist",
                        directory)
            _CACHE = loaded
            return _CACHE

        for path in sorted(directory.glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                # A malformed policy file must not take the service down, and
                # must not silently become "no requirements" either -- the
                # engine falls back to the agent checklist and says so.
                logger.error("Policy file %s is not valid YAML: %s", path, exc)
                continue
            except OSError as exc:
                logger.error("Policy file %s could not be read: %s", path, exc)
                continue

            if not isinstance(document, dict):
                logger.error("Policy file %s is not a mapping; ignored", path)
                continue

            product = str(document.get("product") or path.stem).strip().upper()
            if not product:
                logger.error("Policy file %s declares no product; ignored", path)
                continue

            document.setdefault("source_file", path.name)
            loaded[product] = document
            logger.info("Loaded policy %s v%s for %s",
                        document.get("policy_id"),
                        document.get("policy_version"), product)

        _CACHE = loaded
        return _CACHE


def reload() -> None:
    """Drop the cache. For tests, and for an explicit reconfiguration."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def known_products() -> list[str]:
    """Every product with a policy file."""
    return sorted(_load_all())


def policy_for(product: str | None) -> dict[str, Any] | None:
    """
    The policy document for a product, or None when there is no file.

    None is a normal answer, not a failure. It means "this product has no
    signed-off matrix yet", and the engine handles that explicitly rather
    than inventing one.
    """
    key = (product or "").strip().upper()
    if not key:
        return None
    return _load_all().get(key)


__all__ = ["known_products", "policies_dir", "policy_for", "reload"]
