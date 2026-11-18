"""The marksheet service's rules and limits: app/config/marksheet.yaml, read once. A missing key is an error by name."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parents[2] / "config" / "marksheet.yaml"
REQUIRED = [("limits", "max_upload_bytes"), ("limits", "max_pdf_pages"), ("limits", "max_image_pixels"),
            ("limits", "min_side_px"), ("limits", "ocr_max_side"), ("limits", "processing_timeout_seconds"),
            ("limits", "result_cache_seconds"), ("limits", "result_cache_entries"),
            ("quality", "check_side_px"), ("quality", "edge_level"), ("quality", "min_edge_share"),
            ("quality", "dark_offset"), ("quality", "min_mark_share"), ("quality", "min_text_boxes"),
            ("education", "min_score"), ("education", "markers"), ("name", "labels"),
            ("name", "not_the_candidate"), ("name", "stop_words"), ("name", "min_letters"), ("name", "max_words"),
            ("name", "min_confidence"), ("verdict", "require_name_for_verified")]
LISTS = ("other_documents", "class_12", "class_10", "board_name_noise", "boards")


class ConfigMissing(RuntimeError):
    pass


@lru_cache(maxsize=1)
def document() -> dict[str, Any]:
    return yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}


def missing() -> list[str]:
    doc = document()
    gaps = [".".join(path) for path in REQUIRED if path[1] not in (doc.get(path[0]) or {})]
    return gaps + [key for key in LISTS if not doc.get(key)]


def get() -> dict[str, Any]:
    gaps = missing()
    if gaps:
        raise ConfigMissing(f"app/config/marksheet.yaml is missing: {', '.join(gaps)}")
    return document()


__all__ = ["ConfigMissing", "PATH", "document", "get", "missing"]
