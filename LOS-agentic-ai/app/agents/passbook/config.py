"""The passbook service's rules and limits: app/config/passbook.yaml, read once. A missing key is an error by name."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parents[2] / "config" / "passbook.yaml"
REQUIRED = [("limits", "max_upload_bytes"), ("limits", "max_pdf_pages"), ("limits", "max_image_pixels"),
            ("limits", "ocr_max_side"), ("limits", "ocr_det_max_side"), ("limits", "processing_timeout_seconds"),
            ("limits", "result_cache_seconds"),
            ("limits", "result_cache_entries"), ("classification", "min_score"), ("classification", "markers"),
            ("classification", "other_documents"), ("account_number", "min_digits"),
            ("account_number", "max_digits"), ("account_number", "lengths"), ("verification", "min_ocr_confidence"),
            ("verification", "branch_match_min_similarity"), ("verification", "name_min_letters"),
            ("api", "fields"), ("api", "extract_statuses")]


class ConfigMissing(RuntimeError):
    pass


@lru_cache(maxsize=1)
def document() -> dict[str, Any]:
    return yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}


def missing() -> list[str]:
    doc = document()
    return [".".join(path) for path in REQUIRED if path[1] not in (doc.get(path[0]) or {})]


def get() -> dict[str, Any]:
    gaps = missing()
    if gaps:
        raise ConfigMissing(f"app/config/passbook.yaml is missing: {', '.join(gaps)}")
    return document()


__all__ = ["ConfigMissing", "PATH", "document", "get", "missing"]
