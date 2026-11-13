"""
THE PASSBOOK OCR ENGINE: PP-OCRv6 small (RapidOCR 3.x, ONNX Runtime, CPU, models bundled in the package -- offline).

CHOSEN ON EVIDENCE (2026-10-15, 128 labelled synthetic passbooks + 120 identity documents, same images, 4 threads,
memory arena on): share of true field values present in the OCR text --
                        passbook            identity           passbook P50 / P95
    PP-OCRv4 (current)  97.1 % (IFSC 89 %)  96.4 %             1196 / 1903 ms
    PP-OCRv6 small      99.8 % (IFSC 100 %) 96.6 %             1655 / 1830 ms
v6 small is the more accurate within the 2-3 s budget. PP-OCRv5 / v6 medium could not be measured: their model
host is blocked on this network, and models are only taken from the official package.

One engine per OCR worker thread (an ONNX session is not shared across threads mid-run); created on first use and
reused for the process lifetime -- never per request. If RapidOCR 3.x is absent the process's shared PP-OCRv4 engine
is used instead and the response says which engine read the page.

DESKEW WITHOUT A SECOND PASS. The recogniser returns each text line as a quadrilateral. A phone photo tilted a few
degrees puts a label and its value 40-60 px apart vertically -- different "rows". The median tilt of the wide boxes is
measured and every box is turned back by it before rows are formed: no extra OCR, a few microseconds.
"""

from __future__ import annotations

import logging
import math
import os
import threading

import numpy as np

from app.agents.document_agent.schemas import OCRToken

logger = logging.getLogger(__name__)
_local = threading.local()
NAME_V6 = "rapidocr-3(PP-OCRv6-small)"


def _threads() -> int:
    try:
        return max(1, int(os.getenv("PASSBOOK_OCR_THREADS", "4")))
    except ValueError:
        return 4


def _arena() -> bool:
    """PASSBOOK_OCR_ARENA (default OFF). MEASURED 2026-10-16, 60 passbooks, one engine: arena on 1096 / 1262 ms
    (P50 / P95) at ~680 MB per engine; off 1435 / 1700 ms at ~86 MB. Five passbook engines (three OCR workers + the
    two sideways threads) with it on took the server to 7.2 GB and into paging on a 16 GB host -- PAN reads went
    from 1 s to 4-17 s. Off keeps passbook verify + extract ~1.5 s, inside its 2-3 s budget; turn on only on a host
    with the memory for it."""
    return (os.getenv("PASSBOOK_OCR_ARENA", "false") or "false").strip().lower() == "true"


def _v6(box_thresh: float | None = None):
    """The PP-OCRv6 engine of this thread; `box_thresh` (detector box score) gives a second engine that keeps
    fainter lines -- pale / coloured print on a tinted page."""
    key = "v6" if box_thresh is None else f"v6_{box_thresh}"
    engine = getattr(_local, key, None)
    if engine is None:
        from rapidocr import ModelType, OCRVersion, RapidOCR

        from app.agents.passbook import config as _config

        # the detector runs at config limits.ocr_det_max_side (long side); recognition reads the full-size crops
        det_side = int(_config.get()["limits"]["ocr_det_max_side"])
        engine = RapidOCR(params={
            "Det.limit_type": "max", "Det.limit_side_len": det_side,
            "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.SMALL,
            "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.SMALL,
            "Global.use_cls": False, "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": _threads(),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": _arena(),
            **({"Det.box_thresh": float(box_thresh)} if box_thresh is not None else {}),
        })
        setattr(_local, key, engine)
    return engine


def available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _tilt(quads: list[np.ndarray]) -> float:
    """Median angle (radians) of the top edges of wide text boxes; 0 when there are too few to say."""
    angles = []
    for q in quads:
        q = np.asarray(q, dtype="float64")
        width = float(np.hypot(*(q[1] - q[0])))
        height = float(np.hypot(*(q[3] - q[0])))
        if width > 3 * max(height, 1.0):
            angles.append(math.atan2(q[1][1] - q[0][1], q[1][0] - q[0][0]))
    return float(np.median(angles)) if len(angles) >= 3 else 0.0


def _tokens(quads, texts, scores, image_size) -> tuple[list[OCRToken], float]:
    angle = _tilt(quads)
    cx, cy = image_size[0] / 2, image_size[1] / 2
    cos, sin = math.cos(-angle), math.sin(-angle)
    out = []
    for q, text, score in zip(quads, texts, scores):
        q = np.asarray(q, dtype="float64")
        if abs(angle) > 0.005:
            x, y = q[:, 0] - cx, q[:, 1] - cy
            q = np.stack([x * cos - y * sin + cx, x * sin + y * cos + cy], axis=1)
        out.append(OCRToken(text=str(text), confidence=float(score), x0=float(q[:, 0].min()),
                            y0=float(q[:, 1].min()), x1=float(q[:, 0].max()), y1=float(q[:, 1].max())))
    return out, math.degrees(angle)


def read(image, box_thresh: float | None = None) -> tuple[list[OCRToken], dict]:
    """OCR one RGB PIL image -> (tokens in deskewed page coordinates, info {engine, tilt_degrees})."""
    array = np.asarray(image)[:, :, ::-1].copy()
    if available():
        # the stage flags are passed EVERY call: RapidOCR keeps the last call's flags on the engine, so a
        # recognition-only call (recognise_line) would otherwise leave detection off for this one
        result = _v6(box_thresh)(array, use_det=True, use_cls=False, use_rec=True)
        quads = list(result.boxes) if result.boxes is not None else []
        tokens, tilt = _tokens(quads, list(result.txts or []), list(result.scores or []), image.size)
        return tokens, {"engine": NAME_V6, "tilt_degrees": round(tilt, 2)}
    from app.agents.document_agent.ocr import get_engine

    engine = get_engine()
    tokens, _ = engine.read_array(array, upright=True)
    return tokens, {"engine": getattr(engine, "name", "rapidocr"), "tilt_degrees": 0.0}


def recognise_line(image) -> tuple[float, str]:
    """(confidence, text) of ONE line image -- recognition only, no detection (tens of ms). Used to tell which way a
    sideways page is turned from a few cropped lines."""
    array = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    if not available():
        return 0.0, ""
    try:
        result = _v6()(array, use_det=False, use_cls=False, use_rec=True)
    except Exception:  # noqa: BLE001 - a crop that cannot be read is no evidence
        return 0.0, ""
    texts, scores = list(getattr(result, "txts", None) or []), list(getattr(result, "scores", None) or [])
    return (float(scores[0]), str(texts[0])) if texts and scores else (0.0, "")


def warm() -> None:
    """Load the model now (startup), so the first request does not pay for it."""
    if available():
        from PIL import Image

        read(Image.new("RGB", (320, 120), (255, 255, 255)))


__all__ = ["NAME_V6", "available", "read", "warm"]
