"""
BEFORE OCR: is there anything on the page to read? Measured on a small grey copy in milliseconds, so a blank page or a
smudged photo is refused without paying for OCR (config `quality`).

    edges   share of pixels on a sharp edge (|Laplacian| above `edge_level`) -- print is made of sharp edges
    marks   share of pixels clearly darker than the page (`dark_offset` below its mean) -- ink, sharp or not

    no edges, no marks   -> BLANK      (an empty page, a plain photo)
    no edges, but marks  -> BLURRY     (there is writing, too smudged to read)

MEASURED 2026-10-19 on the synthetic set at 512 px: every page with print had edges >= 0.018 (letters, PAN, invoice
...); blank / plain photo / heavily blurred pages <= 0.004. Marks: blank and plain photo 0.000, a blurred marksheet
0.029.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Quality:
    blank: bool
    blurry: bool
    edges: float
    marks: float
    contrast: float

    def public(self) -> dict:
        return {"edges": round(self.edges, 4), "marks": round(self.marks, 4), "contrast": round(self.contrast, 1)}


def measure(image, spec: dict) -> Quality:
    """`image`: an RGB PIL image."""
    from PIL import Image

    grey = image.convert("L")
    side = int(spec["check_side_px"])
    scale = side / max(grey.size)
    if scale < 1:
        grey = grey.resize((max(3, round(grey.width * scale)), max(3, round(grey.height * scale))), Image.BILINEAR)
    g = np.asarray(grey, dtype="float32")
    if min(g.shape) < 3:
        return Quality(True, False, 0.0, 0.0, 0.0)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    edges = float((np.abs(lap) > float(spec["edge_level"])).mean())
    marks = float((g < g.mean() - float(spec["dark_offset"])).mean())
    no_edges = edges < float(spec["min_edge_share"])
    blank = no_edges and marks < float(spec["min_mark_share"])
    blurry = no_edges and not blank
    return Quality(blank, blurry, edges, marks, float(g.std()))


__all__ = ["Quality", "measure"]
