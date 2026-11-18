"""
A GENUINE PAN WHOSE DATE OF BIRTH CANNOT BE READ -- built, not assumed (2026-10-16).

samples/real_batch/pan_bw1.jpg used to stand for "a real PAN whose printed date of birth did not survive the photo":
the old first OCR pass missed it. It did survive: the date is printed clearly under "Date of Birth", and the current
pipeline reads it at 0.96 confidence -- so that photo now (correctly) PASSES. The rule the tests guard is unchanged:
a PAN whose date of birth is NOT read must never pass. This builds that case for certain: the same card with the
printed date (found by OCR) painted over. Written to a private temp file, deleted at interpreter exit; never logged.
"""

from __future__ import annotations

import atexit
import re
import tempfile
from functools import lru_cache
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "samples" / "real_batch" / "pan_bw1.jpg"
_DATE = re.compile(r"\d{2}\s*[/.-]\s*\d{2}\s*[/.-]\s*\d{4}")


@lru_cache(maxsize=1)
def pan_with_unreadable_dob() -> Path:
    from PIL import ImageDraw

    from app.agents.document_agent import preprocess
    from app.agents.document_agent.ocr import get_engine

    image = preprocess.load(str(SOURCE)).convert("RGB")
    standard = preprocess.standard(image)
    tokens, _ = get_engine().read_array(preprocess.to_array(standard), upright=True)
    scale_x, scale_y = image.width / standard.width, image.height / standard.height
    draw = ImageDraw.Draw(image)
    covered = 0
    for t in tokens:
        if _DATE.search(t.text) and t.cy / standard.height < 0.9:        # the printed date, not a camera caption
            pad = (t.y1 - t.y0) * 0.6
            draw.rectangle(((t.x0 - pad) * scale_x, (t.y0 - pad) * scale_y, (t.x1 + pad) * scale_x,
                            (t.y1 + pad) * scale_y), fill=(205, 200, 225))
            covered += 1
    assert covered, "the printed date of birth was not found to cover"
    folder = Path(tempfile.mkdtemp(prefix="pan_dob_"))
    target = folder / "pan_dob_unreadable.jpg"
    image.save(target, "JPEG", quality=92)

    def _cleanup() -> None:
        target.unlink(missing_ok=True)
        folder.rmdir()

    atexit.register(_cleanup)
    return target


__all__ = ["SOURCE", "pan_with_unreadable_dob"]
