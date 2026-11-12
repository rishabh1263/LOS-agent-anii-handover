"""
Signature PRESENCE check (Phase 3 step 5c, LOS_SIGNATURE_MANDATORY, default off).

A different question from the authenticity verdict in service.py, and kept
apart from it on purpose. That verdict asks "does this match a specimen?" and
is REVIEW without one, always. This one asks only:

  1. is it blank?
  2. is it a handwritten mark -- ink strokes, not printed or typed text, a
     stamp, a single straight line or a dot?
  3. is it sharp and whole -- not blurred, not cut off at the frame edge?

No matching against any other signature. CPU only: ink ratio, row profile
(analysis.py), connected components (OpenCV, already installed for OCR) and
the existing OCR engine to catch a typed name. No new dependency, no model.

    VERIFIED  every test passed
    REJECTED  clearly not a signature, with a reason a person can act on
    REVIEW    unsure -- never auto-approved

The result travels as reason codes on the stored document (no schema change):
SIGNATURE_PRESENCE_<STATUS> plus one code per reason.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Callable

from app.agents.signature import analysis

logger = logging.getLogger(__name__)

FLAG = "LOS_SIGNATURE_MANDATORY"

VERIFIED = "VERIFIED"
REJECTED = "REJECTED"
REVIEW = "REVIEW"

# Status codes, stored on the document.
SIGNATURE_PRESENCE_VERIFIED = "SIGNATURE_PRESENCE_VERIFIED"
SIGNATURE_PRESENCE_REJECTED = "SIGNATURE_PRESENCE_REJECTED"
SIGNATURE_PRESENCE_REVIEW = "SIGNATURE_PRESENCE_REVIEW"

# Reasons.
SIGNATURE_PRESENCE_BLANK = "SIGNATURE_PRESENCE_BLANK"
SIGNATURE_PRESENCE_PRINTED = "SIGNATURE_PRESENCE_PRINTED"
SIGNATURE_PRESENCE_TYPED_TEXT = "SIGNATURE_PRESENCE_TYPED_TEXT"
SIGNATURE_PRESENCE_LINE = "SIGNATURE_PRESENCE_LINE"
SIGNATURE_PRESENCE_DOT = "SIGNATURE_PRESENCE_DOT"
SIGNATURE_PRESENCE_UNREADABLE = "SIGNATURE_PRESENCE_UNREADABLE"
SIGNATURE_PRESENCE_POSSIBLE_TEXT = "SIGNATURE_PRESENCE_POSSIBLE_TEXT"
SIGNATURE_PRESENCE_TOO_MANY_STROKES = "SIGNATURE_PRESENCE_TOO_MANY_STROKES"
SIGNATURE_PRESENCE_LOW_CONTRAST = "SIGNATURE_PRESENCE_LOW_CONTRAST"
SIGNATURE_PRESENCE_OCR_UNAVAILABLE = "SIGNATURE_PRESENCE_OCR_UNAVAILABLE"
SIGNATURE_PRESENCE_BLURRED = "SIGNATURE_PRESENCE_BLURRED"
SIGNATURE_PRESENCE_CUT_OFF = "SIGNATURE_PRESENCE_CUT_OFF"
SIGNATURE_PRESENCE_SLIGHTLY_BLURRED = "SIGNATURE_PRESENCE_SLIGHTLY_BLURRED"
SIGNATURE_PRESENCE_TOUCHES_EDGES = "SIGNATURE_PRESENCE_TOUCHES_EDGES"
SIGNATURE_PRESENCE_NOT_A_SIGNATURE = "SIGNATURE_PRESENCE_NOT_A_SIGNATURE"
SIGNATURE_PRESENCE_PICTURE = "SIGNATURE_PRESENCE_PICTURE"

STATUS_CODE = {VERIFIED: SIGNATURE_PRESENCE_VERIFIED, REJECTED: SIGNATURE_PRESENCE_REJECTED,
               REVIEW: SIGNATURE_PRESENCE_REVIEW}

#: What a person reads (the reason catalogue repeats these).
MESSAGES = {
    SIGNATURE_PRESENCE_BLANK: "Signature is blank",
    SIGNATURE_PRESENCE_PRINTED: "Signature looks printed, not handwritten",
    SIGNATURE_PRESENCE_TYPED_TEXT: "Signature looks like a typed name, not handwritten",
    SIGNATURE_PRESENCE_LINE: "Signature is only a straight line",
    SIGNATURE_PRESENCE_DOT: "Signature is only a dot or a tiny mark",
    SIGNATURE_PRESENCE_UNREADABLE: "Signature image could not be read",
    SIGNATURE_PRESENCE_POSSIBLE_TEXT: "Signature may be printed text; a person will check it",
    SIGNATURE_PRESENCE_TOO_MANY_STROKES: "Signature has unusually many separate marks; a person will check it",
    SIGNATURE_PRESENCE_LOW_CONTRAST: "Signature is too faint to judge; a person will check it",
    SIGNATURE_PRESENCE_OCR_UNAVAILABLE: "Typed-text check could not run; a person will check it",
    SIGNATURE_PRESENCE_BLURRED: "Signature is blurred; please upload a sharp photo or scan",
    SIGNATURE_PRESENCE_CUT_OFF: "Signature is cut off at the edge; please upload the full signature with some margin",
    SIGNATURE_PRESENCE_SLIGHTLY_BLURRED: "Signature is a little blurred; a person will check it",
    SIGNATURE_PRESENCE_TOUCHES_EDGES: "Signature touches the image edges and may be cut; a person will check it",
    SIGNATURE_PRESENCE_NOT_A_SIGNATURE: "This is a document or page, not a signature; upload only the signature",
    SIGNATURE_PRESENCE_PICTURE: "This is a picture, not a signature; upload a photo or scan of the signature on paper",
}


def picture_colours(content: bytes) -> dict[str, float] | None:
    """How much of an OPAQUE image is strongly coloured, and in how many distinct colours. None for a transparent
    image (a canvas drawing: any ink colour is fine there) or one that cannot be read."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                if image.convert("RGBA").getchannel("A").getextrema()[0] < 250:
                    return None
            rgb = image.convert("RGB")
            rgb.thumbnail((256, 256))
            # WHITE BALANCE FIRST (2026-10-28: a phone photo of a black-pen signature was REJECTED as a picture -- the
            # camera's warm / cool cast tinted the whole paper): the paper (the brightest tenth) is made neutral, at
            # most by config signature.picture_white_balance_max per channel; a real picture's light parts are
            # already near white, so its colours stay
            from app.agents.signature import config as _wb_cfg

            arr = np.asarray(rgb, dtype="float64")
            paper_rgb = np.percentile(arr.reshape(-1, 3), 90, axis=0)
            gain = np.clip(paper_rgb.max() / np.maximum(paper_rgb, 1.0), 1.0, _wb_cfg.white_balance_max())
            rgb = Image.fromarray(np.clip(arr * gain, 0, 255).astype("uint8"))
            hsv = np.asarray(rgb.convert("HSV"), dtype="float64").reshape(-1, 3) / 255.0
    except Exception:  # noqa: BLE001 - the main check reports an unreadable image
        return None
    coloured = (hsv[:, 1] > 0.35) & (hsv[:, 2] > 0.25)
    share = float(coloured.mean())
    paper = float((hsv[:, 1] < 0.12).mean())            # white / grey / shadowed paper and dark ink: no colour
    hist = np.histogram(hsv[coloured, 0], bins=12, range=(0.0, 1.0))[0] / max(1, len(hsv))
    return {"colour_share": round(share, 4), "hue_groups": int((hist > 0.02).sum()), "paper_share": round(paper, 4)}

# Work size: big enough for strokes, small enough to stay cheap on a CPU.
_WORK_EDGE = 512
# Components smaller than this (in work-size pixels) are scan noise.
_NOISE_AREA = 6
# Ink box smaller than this fraction of the frame's longer side: a dot.
_DOT_FRACTION = 0.05
# Ink spread across its minor axis below this many pixels, with a long major
# axis: one straight line.
_LINE_MINOR_STD = 2.5
_LINE_RATIO = 0.06
# A handwritten signature is a few connected strokes; typed text is one
# component per letter. Above this, a person looks.
_MAX_COMPONENTS = 25
# Printed letters share a height and a baseline; handwriting does not.
_PRINTED_MIN_COMPONENTS = 5
_PRINTED_HEIGHT_CV = 0.22
_PRINTED_BASELINE = 0.12
# OCR: letters/digits read at or above this mean confidence count as text.
_OCR_MIN_CHARS = 4
_OCR_TYPED_CONFIDENCE = 0.85
_OCR_POSSIBLE_CONFIDENCE = 0.60
# Edge steepness (blur_index): the steepest stroke edges as a share of the ink-to-paper range per pixel.
# MEASURED 2026-10-15 on 40 synthetic handwriting signatures (8 fonts, pens, paper, JPEG): clean 0.446-1.19;
# Gaussian blur r=1 (work size) 0.305-0.441, r=2 0.189-0.283, r=3 0.132-0.186. Below REJECT nothing is legible
# enough to file; between REJECT and REVIEW a person looks.
_BLUR_REJECT = 0.27          # defaults; the values used are config signature.blur_reject / blur_review
_BLUR_REVIEW = 0.36


def _blur_limits() -> tuple[float, float]:
    from app.agents.signature import config as _cfg

    s = _cfg._section()
    try:
        return float(s.get("blur_reject", _BLUR_REJECT)), float(s.get("blur_review", _BLUR_REVIEW))
    except (TypeError, ValueError):
        return _BLUR_REJECT, _BLUR_REVIEW


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _load(content: bytes):
    """Greyscale, dark ink on light paper.

    A TRANSPARENT image (a canvas signature) is read from its ALPHA: whatever was drawn is ink, in any colour --
    white ink on a dark-mode canvas flattened onto white was invisible and REJECTED as blank (2026-10-17, measured
    on drawn signatures). An image whose alpha is all opaque is read from its colours, as before."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(content)) as image:
        if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            lo, hi = alpha.getextrema()
            if lo < 250:                                   # really transparent somewhere: the drawing IS the alpha
                return ImageOps.invert(alpha)
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            return Image.alpha_composite(backdrop, rgba).convert("L")
        return image.convert("L").copy()


def trim_padding(grey):
    """The image without the flat bands a phone gallery / screenshot puts around a photo (config signature.padding_*):
    a band is trimmed only when every line of it is ONE flat colour that clearly differs from the paper in the middle,
    so a canvas, a scan or a photo filling the frame (band = paper) is returned unchanged. Returns (image, trimmed px
    per side)."""
    import numpy as np

    from app.agents.signature import config as _cfg

    on, std_max, min_diff, min_keep = _cfg.padding_limits()
    a = np.asarray(grey, dtype="float64")
    h, w = a.shape
    if not on or h < 8 or w < 8:
        return grey, {}
    middle = a[h // 4: h - h // 4, w // 4: w - w // 4]
    paper = float(np.median(middle))

    def run(lines) -> int:
        n = 0
        for line in lines:
            level = float(line.mean())
            pure = level >= 247 or level <= 8               # a gallery / screenshot band: pure white or pure black
            if not pure or float(line.std()) > std_max or abs(level - paper) < min_diff:
                break
            n += 1
        return n

    top, bottom = run(a), run(a[::-1])
    left, right = run(a.T), run(a.T[::-1])
    if h - top - bottom < min_keep * h:
        top = bottom = 0
    if w - left - right < min_keep * w:
        left = right = 0
    cut = {k: v for k, v in (("top", top), ("bottom", bottom), ("left", left), ("right", right)) if v}
    if not cut:
        return grey, {}
    return grey.crop((left, top, w - right, h - bottom)), cut


def _flat(work):
    """The page with its LIGHTING evened out: each pixel divided by the paper brightness around it (estimated by a
    max filter that removes the strokes, then smoothed). A phone photo's shadow / falloff otherwise reads as ink at
    the frame edge -- genuine captured signatures were rejected as CUT_OFF (2026-10-17). Ink stays dark, paper
    becomes uniform white; a scan (already even) is unchanged."""
    import cv2
    import numpy as np

    g = np.asarray(work, dtype="uint8")
    k = max(15, (min(g.shape) // 12) | 1)
    paper = cv2.dilate(g, np.ones((k, k), np.uint8))
    paper = cv2.GaussianBlur(paper, (0, 0), k / 2)
    flat = np.clip(g.astype("float64") * 255.0 / np.maximum(paper.astype("float64"), 1.0), 0, 255)
    from PIL import Image

    return Image.fromarray(flat.astype("uint8"))


def _work(grey):
    scale = _WORK_EDGE / max(1, max(grey.size))
    if scale < 1:
        grey = grey.resize((max(1, int(grey.width * scale)), max(1, int(grey.height * scale))))
    return grey


def components(ink) -> list[dict[str, float]]:
    """Connected ink components (8-connected), noise removed: [{x, y, w, h, area}]."""
    import cv2
    import numpy as np

    count, _, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    return [{"x": float(s[0]), "y": float(s[1]), "w": float(s[2]), "h": float(s[3]), "area": float(s[4])}
            for s in stats[1:count] if s[4] >= _NOISE_AREA]


def _closed(ink):
    """Ink with 1-2 pixel breaks joined (3x3 morphological closing)."""
    import cv2
    import numpy as np

    return cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)).astype(bool)


_SIGNIFICANT_FRACTION = 0.02
# A document, not a signature: more separate pieces than any signature has (measured 2026-10-17: genuine signatures
# incl. noisy captures <= 37 pieces / <= 25 significant marks; PAN card ~240, printed page ~670)
_DOCUMENT_PARTS = 100
_DOCUMENT_MARKS = 50


def _significant(parts: list[dict[str, float]]) -> list[dict[str, float]]:
    """Pieces at least 2 % the area of the THIRD-largest: marks, not crumbs or specks. Measured against the third,
    not the largest: one huge blob (an ID card's photo) made every letter of the card look like a crumb and the
    card passed as a signature (2026-10-17, caught by the HRMS test)."""
    if not parts:
        return parts
    areas = sorted((p["area"] for p in parts), reverse=True)
    reference = areas[min(2, len(areas) - 1)]
    return [p for p in parts if p["area"] >= _SIGNIFICANT_FRACTION * reference]


def shape(ink) -> dict[str, float]:
    """The ink's extent and how far it spreads across its principal axis."""
    import numpy as np

    ys, xs = np.nonzero(ink)
    if xs.size < 2:
        return {"extent": 0.0, "major_std": 0.0, "minor_std": 0.0}
    coords = np.stack([xs, ys]).astype("float64")
    eig = np.sort(np.linalg.eigvalsh(np.cov(coords)))
    return {"extent": float(max(xs.max() - xs.min(), ys.max() - ys.min()) + 1),
            "major_std": float(np.sqrt(max(eig[1], 0.0))), "minor_std": float(np.sqrt(max(eig[0], 0.0)))}


def printed_layout(parts: list[dict[str, float]]) -> bool:
    """Many components of one height sitting on one baseline: printed letters."""
    import numpy as np

    if len(parts) < _PRINTED_MIN_COMPONENTS:
        return False
    heights = np.array([p["h"] for p in parts])
    bottoms = np.array([p["y"] + p["h"] for p in parts])
    mean_h = float(heights.mean()) or 1.0
    return float(heights.std()) / mean_h < _PRINTED_HEIGHT_CV and float(bottoms.std()) / mean_h < _PRINTED_BASELINE


def ocr_text(grey) -> tuple[int, float]:
    """(letters/digits read, mean confidence) from the existing OCR engine."""
    import numpy as np

    from app.agents.document_agent.ocr import get_engine

    tokens, _ = get_engine().read_array(np.asarray(grey.convert("RGB")))
    chars, weighted = 0, 0.0
    for token in tokens or []:
        text = str(getattr(token, "text", "") or "")
        confidence = float(getattr(token, "confidence", 0.0) or 0.0)
        # A WAVE IS NOT A WORD: a cursive signature's loops read as "mmmr", "nnnn", "uuu" -- one or two letters
        # repeated (measured 2026-10-16: a drawn signature read "mmmr" at 0.64 and went to review as possible
        # text). A typed name always has three or more different characters.
        if len({ch.lower() for ch in text if ch.isalnum()}) < 3:
            continue
        n = sum(ch.isalnum() for ch in text)
        chars += n
        weighted += n * confidence
    return chars, (weighted / chars if chars else 0.0)


def blur_index(work) -> dict[str, float] | None:
    """How soft the stroke edges are, independent of pen colour and paper tone.

    A sharp stroke goes from ink to paper in a pixel or two; a blurred one passes through a wide band of grey. So:
    pixels in the middle of the ink-to-paper range per pixel of solid ink (`mid_per_core`), and the steepest edges
    relative to that range (`edge_steepness`). None when there is too little contrast to say."""
    import numpy as np

    g = np.asarray(work, dtype="float64")
    paper = float(np.percentile(g, 95))
    dark = float(np.percentile(g, 0.5))
    span = paper - dark
    if span < 30:
        return None
    core = g <= dark + 0.3 * span
    mid = (g > dark + 0.3 * span) & (g < paper - 0.3 * span)
    gy, gx = np.gradient(g)
    steep = float(np.percentile(np.hypot(gx, gy), 99.5)) / span
    return {"mid_per_core": round(float(mid.sum()) / max(1.0, float(core.sum())), 3), "edge_steepness": round(steep, 3)}


def _edge_ink(work):
    """Ink for the cut-off test, at THIS image's own ink / paper split (Otsu, kept within 128-215): a fine grey pen
    sits above the fixed 128 line, and a stroke the crop cut through was invisible to the test (measured 2026-10-16
    on a thin grey signature cut in half: VERIFIED)."""
    import cv2
    import numpy as np

    grey = np.asarray(work, dtype="uint8")
    threshold, _ = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return grey < min(215.0, max(128.0, float(threshold)))


def edges_cut(ink, parts: list[dict[str, float]] | None = None, strip: int = 2) -> list[str]:
    """Frame edges the handwriting runs into -- a signature cut off by the photo or the crop.

    A straight border line along an edge, or a thin BOX around the whole image (a form's signature box -- measured
    2026-10-16: a whole signature inside a box was REJECTED as cut off), is not handwriting and is ignored."""
    import numpy as np

    h, w = ink.shape
    mask = ink.copy()
    # Only the FRAME LINES themselves are removed: a row (column) near an edge that is mostly ink is a printed line,
    # never handwriting. Whatever else touches the edge -- a stroke the crop cut through -- still counts, also when
    # it joins the box (a boxed signature cut in half must still read as cut).
    band_y, band_x = max(3, h // 25), max(3, w // 25)
    for r in [*range(band_y), *range(h - band_y, h)]:
        if mask[r].mean() >= 0.6:
            mask[r] = False
    for c in [*range(band_x), *range(w - band_x, w)]:
        if mask[:, c].mean() >= 0.6:
            mask[:, c] = False
    strips = {"top": mask[:strip, :], "bottom": mask[-strip:, :], "left": mask[:, :strip], "right": mask[:, -strip:]}
    touched = [edge for edge, s in strips.items() if int(np.count_nonzero(s)) >= 3]
    return [edge for edge in touched if not _detached_from_body(mask, edge, strip)] if touched else touched


# Ink at a frame edge FAR from the handwriting is not the signature running off the edge: the paper's border, a table
# edge or a shadow in a phone photo (2026-10-22: a whole signature in the middle of a page, with the paper's border in
# one corner, was REJECTED as cut off). Far = no closer to the signature's body than this share of the frame.
_DETACHED_GAP = 0.06
# ...and lies along the edge: it reaches no deeper into the frame than this share (the paper border measured 2 %)
_EDGE_HUG = 0.04
# ...and runs ALONG it: its length on the edge is at least this many times its depth (the border measured 9x; the
# stub a crop leaves of a cut stroke is a blob, about 1x)
_BORDER_ELONGATION = 3.0


def _detached_from_body(mask, edge: str, strip: int) -> bool:
    """Every ink piece touching `edge` lies apart from the signature's body (the pieces touching no edge)."""
    import cv2
    import numpy as np

    h, w = mask.shape
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    pieces = [s for s in stats[1:count] if s[4] >= _NOISE_AREA]

    def touches(s, side):
        x, y, cw, ch = s[0], s[1], s[2], s[3]
        return {"left": x < strip, "top": y < strip, "right": x + cw > w - strip, "bottom": y + ch > h - strip}[side]

    at_edge = [s for s in pieces if touches(s, edge)]
    inner = [s for s in pieces if not any(touches(s, side) for side in ("left", "top", "right", "bottom"))]
    if not at_edge or not inner:
        return False                                   # nothing else to call the signature: the edge ink is it
    largest = max(s[4] for s in inner)
    body = [s for s in inner if s[4] >= 0.05 * largest]
    bx0, by0 = min(s[0] for s in body), min(s[1] for s in body)
    bx1, by1 = max(s[0] + s[2] for s in body), max(s[1] + s[3] for s in body)
    gap = _DETACHED_GAP * max(h, w)

    def apart(s):
        dx = max(bx0 - (s[0] + s[2]), s[0] - bx1, 0)
        dy = max(by0 - (s[1] + s[3]), s[1] - by1, 0)
        return max(dx, dy) > gap

    def hugs_edge(s):
        """A thin strip lying ALONG the edge (a paper border): it reaches only a few % into the frame. A stroke the
        crop cut through runs from the edge INTO the frame -- a cut half signature must stay CUT_OFF."""
        depth = {"left": (s[0] + s[2]) / w, "right": (w - s[0]) / w, "top": (s[1] + s[3]) / h, "bottom": (h - s[1]) / h}
        # a border in a CORNER touches two edges: it is a border when it lies thin along either of them
        sides = [side for side in depth if touches(s, side)]
        along = {"left": s[3], "right": s[3], "top": s[2], "bottom": s[2]}
        across = {"left": s[2], "right": s[2], "top": s[3], "bottom": s[3]}
        return any(depth[side] <= _EDGE_HUG and along[side] >= _BORDER_ELONGATION * across[side] for side in sides)
    return all(apart(s) and hugs_edge(s) for s in at_edge)


def _result(status: str, reasons: list[str], measures: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "codes": [STATUS_CODE[status], *reasons], "reasons": reasons,
            "messages": [MESSAGES[r] for r in reasons], "measures": measures}


SOURCES = ("upload", "draw", "capture")


def check(content: bytes, *, ocr: Callable[[Any], tuple[int, float]] | None = None,
          source: str = "upload") -> dict[str, Any]:
    """Assess one signature image. Never raises; anything unexpected is REVIEW or REJECTED, never VERIFIED.

    `source`: how the image was made -- "upload" (a scan / photo of paper), "capture" (a camera photo taken in the
    app), "draw" (signed on the app's own canvas). A DRAWN signature is handwritten by construction, so the
    typed-text test (OCR, ~1 s) is skipped for it; every other test runs for every source."""
    import numpy as np

    try:
        grey = _load(content)
    except Exception as exc:  # noqa: BLE001 - not an image
        logger.debug("Signature presence: unreadable image: %s", exc)
        return _result(REJECTED, [SIGNATURE_PRESENCE_UNREADABLE], {})
    try:
        grey, _ = trim_padding(grey)                # a gallery's pure white / black bands are not the paper
    except Exception:  # noqa: BLE001 - the image as it came
        pass

    # A PICTURE (an illustration, a colour photo), not ink on paper: rejected before anything else, in milliseconds
    if source != "draw":
        from app.agents.signature import config as _picture_cfg

        colours = picture_colours(content)
        if colours:
            share_limit, groups_limit, mixed_limit, min_paper = _picture_cfg.picture_limits()
            if colours["colour_share"] > share_limit or colours["paper_share"] < min_paper or (
                    colours["hue_groups"] >= groups_limit and colours["colour_share"] > mixed_limit):
                return _result(REJECTED, [SIGNATURE_PRESENCE_PICTURE], colours)

    try:
        stats = analysis.measure(grey)
        work = _work(grey)
        # ink, its pieces and the frame-edge test read the lighting-evened page; sharpness reads the page as taken
        flat = _flat(work)
        ink = np.asarray(flat, dtype="uint8") < 128
        parts = components(ink)
        # BLURRED is judged first: a blurred signature fades below the ink line, and was rejected as BLANK (the
        # wrong reason) or -- at the same blur, half the time -- VERIFIED. A half signature was VERIFIED 38 / 40.
        softness = blur_index(work)
        blur_reject, blur_review = _blur_limits()
        blurred = bool(softness) and softness["edge_steepness"] < blur_reject
        if not parts:
            measures0 = {"ink_ratio": stats.get("ink_ratio", 0.0),
                         **({"edge_steepness": softness["edge_steepness"]} if softness else {})}
            # A FAINT SIGNATURE (light pencil / a pale pen) is not a blank page (2026-10-28, live test: light-grey
            # ink was REJECTED as blank): strokes under a lighter line (config signature.faint_ink_level) -> a person
            # looks (LOW_CONTRAST); only a page with nothing on it at all is blank
            from app.agents.signature import config as _faint_cfg

            level, min_share = _faint_cfg.faint_limits()
            faint = np.asarray(flat, dtype="uint8") < level
            faint_all = components(faint)
            faint_parts = _significant(faint_all)
            # ...and a pale ID card / page is still a document, never a faint signature (the same test as below)
            if len(faint_all) > _DOCUMENT_PARTS or len(_significant(components(_closed(faint)))) > _DOCUMENT_MARKS:
                return _result(REJECTED, [SIGNATURE_PRESENCE_NOT_A_SIGNATURE],
                               {**measures0, "components": len(faint_all)})
            if not blurred and faint_parts and float(faint.mean()) >= min_share:
                return _result(REVIEW, [SIGNATURE_PRESENCE_LOW_CONTRAST],
                               {**measures0, "faint_ink_ratio": round(float(faint.mean()), 5)})
            return _result(REJECTED, [SIGNATURE_PRESENCE_BLURRED if blurred else SIGNATURE_PRESENCE_BLANK], measures0)

        # A DOCUMENT, NOT A SIGNATURE, decided in milliseconds (owner 2026-10-17: "other docs blink me reject"): an ID
        # card / page has hundreds of separate marks (a PAN card ~240, a printed page ~670); a signature -- even a
        # noisy phone capture -- a few dozen at most. Rejected before any OCR (a PAN card took 0.9 s and went to
        # review).
        if len(parts) > _DOCUMENT_PARTS or len(_significant(components(_closed(ink)))) > _DOCUMENT_MARKS:
            return _result(REJECTED, [SIGNATURE_PRESENCE_NOT_A_SIGNATURE],
                           {"ink_ratio": stats.get("ink_ratio", 0.0), "components": len(parts)})

        form = shape(ink)
        frame = float(max(work.size))
        cut = edges_cut(_edge_ink(flat), parts)
        measures = {"ink_ratio": stats.get("ink_ratio", 0.0), "components": len(parts),
                    "extent_fraction": round(form["extent"] / frame, 4),
                    "minor_std": round(form["minor_std"], 3), "major_std": round(form["major_std"], 3),
                    "edges_cut": len(cut)}
        if softness:
            measures.update(blur_index=softness["mid_per_core"], edge_steepness=softness["edge_steepness"])
        if blurred:
            return _result(REJECTED, [SIGNATURE_PRESENCE_BLURRED], measures)

        # a dot first: it is also almost no ink, and "blank" would tell the person the wrong thing
        if form["extent"] / frame < _DOT_FRACTION:
            return _result(REJECTED, [SIGNATURE_PRESENCE_DOT], measures)
        if analysis.is_blank(stats):
            return _result(REJECTED, [SIGNATURE_PRESENCE_BLANK], measures)
        if form["minor_std"] < _LINE_MINOR_STD and form["minor_std"] < _LINE_RATIO * max(form["major_std"], 1e-6):
            return _result(REJECTED, [SIGNATURE_PRESENCE_LINE], measures)
        # CUT OFF: the handwriting runs into one or two frame edges -- half a signature, or a photo that missed part
        if 0 < len(cut) <= 2:
            return _result(REJECTED, [SIGNATURE_PRESENCE_CUT_OFF], measures)

        profile = analysis.stroke_profile(grey)
        printed = printed_layout(parts)
        measures["printed_layout"] = printed
        if not analysis.looks_handwritten(stats, profile):
            return _result(REJECTED, [SIGNATURE_PRESENCE_PRINTED], measures)

        review: list[str] = []
        if float(stats.get("std", 0.0)) < analysis.LOW_CONTRAST_STD:
            review.append(SIGNATURE_PRESENCE_LOW_CONTRAST)
        # strokes counted after joining PIXEL-SIZED gaps: a thin canvas stroke (2 px) scaled to the work size breaks
        # into fragments that are not separate marks (2026-10-17: drawn signatures went to review as "too many
        # strokes"); separate printed letters stay separate
        # ...and only SIGNIFICANT marks count: a piece under 2 % of the largest is a crumb of a hairline or a speck
        # (2026-10-17: a captured signature went to review with 30 fragments of 7-8 px beside strokes of 500-2200 px);
        # separate typed letters are all of one size, so they still count
        marks = len(_significant(components(_closed(ink)))) if len(parts) > _MAX_COMPONENTS else len(parts)
        if marks > _MAX_COMPONENTS:
            review.append(SIGNATURE_PRESENCE_TOO_MANY_STROKES)
        if marks > 3 * _MAX_COMPONENTS and source != "draw":
            # A PAGE / CARD, not a signature (hundreds of separate marks): to a person at once -- reading all its
            # text with the OCR took 7 s and timed out (2026-10-17), and could only have added "possible text"
            measures["ocr_skipped"] = "document-like"
            return _result(REVIEW, list(dict.fromkeys([*review, SIGNATURE_PRESENCE_POSSIBLE_TEXT])), measures)
        if softness and softness["edge_steepness"] < blur_review:
            review.append(SIGNATURE_PRESENCE_SLIGHTLY_BLURRED)
        if len(cut) >= 3:
            # ink on three or four edges: cropped tight to the ink, or cut on every side -- cannot tell which
            review.append(SIGNATURE_PRESENCE_TOUCHES_EDGES)

        from app.agents.signature import config as _sig_cfg

        # THE OCR (~0.4-1 s) RUNS ONLY WHEN IT CAN CHANGE THE ANSWER (owner 2026-10-17: a signature in ~1 s): when the
        # strokes look like PRINTED letters (to reject typed text), or when readable names are set to go to review.
        # A drawn signature, or joined handwriting with readable names allowed, never needs it.
        if source == "draw" or (not printed and _sig_cfg.readable_name_verified()):
            chars, confidence = 0, 0.0
            measures.update(ocr_skipped="draw" if source == "draw" else "handwriting")
        else:
            try:
                chars, confidence = (ocr or ocr_text)(grey)
                measures.update(ocr_chars=chars, ocr_confidence=round(confidence, 3))
            except Exception as exc:  # noqa: BLE001 - OCR down: unsure, never approved
                logger.warning("Signature presence: OCR unavailable: %s", exc)
                review.append(SIGNATURE_PRESENCE_OCR_UNAVAILABLE)
                chars, confidence = 0, 0.0

        reads_as_text = chars >= _OCR_MIN_CHARS
        if reads_as_text and confidence >= _OCR_TYPED_CONFIDENCE and printed:
            # confident text AND printed geometry: two independent tests agree
            return _result(REJECTED, [SIGNATURE_PRESENCE_TYPED_TEXT], measures)
        from app.agents.signature import config as _sig_config

        # a readable NAME is fine (config) -- but only name-length text: a card / page full of text is not a
        # signature whatever its letters look like (a PAN card read 163 characters and passed, 2026-10-17)
        readable_is_fine = _sig_config.readable_name_verified() and chars <= _sig_config.name_max_chars()
        if printed or (reads_as_text and confidence >= _OCR_POSSIBLE_CONFIDENCE and not readable_is_fine):
            # typed-looking letters (separate, uniform, one baseline): a person decides. A READABLE name in joined
            # handwriting is a signature (config signature.readable_name_verified, owner 2026-10-17); off -> review
            review.append(SIGNATURE_PRESENCE_POSSIBLE_TEXT)

        if review:
            return _result(REVIEW, list(dict.fromkeys(review)), measures)
        return _result(VERIFIED, [], measures)
    except Exception as exc:  # noqa: BLE001 - a check that broke established nothing
        logger.warning("Signature presence check failed: %s", exc)
        return _result(REVIEW, [SIGNATURE_PRESENCE_OCR_UNAVAILABLE], {})


def from_codes(codes) -> dict[str, Any] | None:
    """Read a stored presence result back from a document's reason codes; None if it was never checked."""
    codes = [str(c) for c in codes or []]
    status = next((s for s, code in STATUS_CODE.items() if code in codes), None)
    if status is None:
        return None
    reasons = [c for c in codes if c in MESSAGES]
    return {"status": status, "reasons": reasons, "messages": [MESSAGES[r] for r in reasons]}


__all__ = ["FLAG", "MESSAGES", "REJECTED", "REVIEW", "VERIFIED", "check", "enabled", "from_codes"]
