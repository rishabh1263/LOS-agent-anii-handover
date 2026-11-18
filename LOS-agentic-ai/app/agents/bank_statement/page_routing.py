"""
PAGE-LEVEL ROUTING of a financial PDF (app/agents/bank_statement/page_routing.py, 2026-11-02, Phase 1; config app/config/financial_documents.yaml):
each page is read from its own text layer, or OCR'd -- decided PER PAGE, not per document.

    inspect_pages(path) -> [PageInfo]       one per page: characters, printable share, image coverage, rotation, route
    route_counts(pages) -> (digital, scanned)

A page needs OCR when its text layer
    - is too short (fewer than min_chars), or
    - is garbage (printable share below printable_ratio_min: a broken font map), or
    - is a SCAN WITH A TYPED HEADER: page images cover image_coverage_min of the page and the text holds fewer than
      text_chars_with_image characters (a stamp or a header line over a scanned transaction table).
Nothing here reads a value from the page: only the page's shape. No content is logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG = Path(__file__).resolve().parents[2] / "config" / "financial_documents.yaml"

DIGITAL, OCR = "TEXT_LAYER", "OCR"


@dataclass(frozen=True)
class PageInfo:
    number: int                    # 1-based
    chars: int
    printable_ratio: float
    image_coverage: float          # share of the page covered by embedded page images (0..1)
    rotation: int
    route: str                     # TEXT_LAYER | OCR
    reason: str                    # why: TEXT | NO_TEXT | GARBLED_TEXT | SCAN_WITH_HEADER


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    routing = data.get("page_routing")
    if not isinstance(routing, dict):
        raise RuntimeError(f"{CONFIG.name} has no page_routing")
    return routing


def _printable_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    good = sum(1 for c in chars if c.isprintable() and c != "�" and not 0xE000 <= ord(c) <= 0xF8FF)
    return good / len(chars)


def _image_coverage(page: Any, min_side: float) -> float:
    """Share of the page covered by its embedded images (overlaps counted once on a coarse grid)."""
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    boxes = []
    for info in page.get_image_info():
        x0, y0, x1, y1 = info.get("bbox", (0, 0, 0, 0))
        x0, y0 = max(x0, page.rect.x0), max(y0, page.rect.y0)
        x1, y1 = min(x1, page.rect.x1), min(y1, page.rect.y1)
        if x1 - x0 >= min_side and y1 - y0 >= min_side:
            boxes.append((x0, y0, x1, y1))
    if not boxes:
        return 0.0
    # the union on a 50 x 50 grid: two overlapping images are not counted twice
    steps, covered = 50, 0
    w, h = page.rect.width / steps, page.rect.height / steps
    for i in range(steps):
        for j in range(steps):
            cx, cy = page.rect.x0 + (i + 0.5) * w, page.rect.y0 + (j + 0.5) * h
            if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
                covered += 1
    return covered / (steps * steps)


def classify(chars: int, printable_ratio: float, image_coverage: float) -> tuple[str, str]:
    """(route, reason) of one page from its measurements -- the routing rule, in one place."""
    s = settings()
    if chars < int(s["min_chars"]):
        return OCR, "NO_TEXT"
    if printable_ratio < float(s["printable_ratio_min"]):
        return OCR, "GARBLED_TEXT"
    if image_coverage >= float(s["image_coverage_min"]) and chars < int(s["text_chars_with_image"]):
        return OCR, "SCAN_WITH_HEADER"
    return DIGITAL, "TEXT"


def inspect_pages(path: str) -> list[PageInfo]:
    """Every page's measurements and route. Raises what PyMuPDF raises for a file it cannot open."""
    import pymupdf

    s = settings()
    out: list[PageInfo] = []
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            chars = len(text.strip())
            ratio = _printable_ratio(text)
            coverage = _image_coverage(page, float(s["min_image_side_pt"]))
            route, reason = classify(chars, ratio, coverage)
            out.append(PageInfo(index, chars, round(ratio, 3), round(coverage, 3), int(page.rotation or 0), route,
                                reason))
    return out


def route_counts(pages: list[PageInfo]) -> tuple[int, int]:
    """(pages read from their text layer, pages that need OCR)."""
    scanned = sum(1 for p in pages if p.route == OCR)
    return len(pages) - scanned, scanned


__all__ = ["DIGITAL", "OCR", "PageInfo", "classify", "inspect_pages", "route_counts", "settings"]
