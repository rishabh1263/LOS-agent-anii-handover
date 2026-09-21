
"""
Document OCR engine abstraction.

Primary OCR engine:
    RapidOCR / PP-OCRv4

Fallback / targeted engine:
    Tesseract

The module provides:
    - OCR engine abstraction
    - RapidOCR implementation
    - Tesseract implementation
    - Process-level engine caching
    - Dedicated OCR executor
    - Async OCR helper
    - Representative model warmup
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Protocol

import cv2
import numpy as np
from dotenv import load_dotenv


# ============================================================================
# ENVIRONMENT
# ============================================================================

def _load_environment() -> None:
    """
    Load .env without overriding explicitly supplied environment variables.
    """
    load_dotenv(override=False)


_load_environment()


# ============================================================================
# HELPERS
# ============================================================================

def _int_env(name: str, default: int) -> int:
    """
    Read an integer environment variable safely.
    """
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================================
# OCR TOKEN
# ============================================================================

try:
    from app.agents.document_agent.schemas import OCRToken
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class OCRToken:
        text: str
        confidence: float
        box: list[list[float]]


# ============================================================================
# OCR ENGINE PROTOCOL
# ============================================================================

class OCREngine(Protocol):
    """
    Common OCR engine interface.
    """

    name: str

    def read(
        self,
        image_path: str | Path,
    ) -> tuple[list[OCRToken], float]:
        ...

    def read_array(
        self,
        array: np.ndarray,
    ) -> tuple[list[OCRToken], float]:
        ...

    def warmup(self) -> float:
        ...


# ============================================================================
# RAPIDOCR
# ============================================================================

def _to_box_token(text, confidence, box) -> OCRToken:
    """Build an OCRToken with real x0/y0/x1/y1 from a 4-point polygon."""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return OCRToken(
        text=str(text),
        confidence=float(confidence),
        x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
    )


class RapidOCREngine:
    """
    RapidOCR / PP-OCRv4 based OCR engine.

    Threading is controlled through:

        DOCUMENT_OCR_THREADS

    Recommended local benchmark value:

        DOCUMENT_OCR_THREADS=4
    """

    name = "rapidocr-onnxruntime(PP-OCRv4)"

    def __init__(self) -> None:
        _load_environment()

        # BOUNDED, not unbounded.
        #
        # Left to itself ONNX Runtime sizes its intra-op pool from the core
        # count, and it does that PER SESSION. With one engine per OCR worker
        # that means workers x cores threads competing for cores threads'
        # worth of machine, and the contention costs real time.
        #
        # Measured end to end on the reference host (10 physical / 16 logical
        # cores, 2 OCR workers), median processing_ms with the model off so
        # the numbers are not blurred by generation:
        #
        #     intra-op threads   1 doc    2 docs   4 docs
        #     unbounded (was)     1577      2438     5002
        #     4                   1132      2174     3525
        #     6                    942      1796     3186
        #
        # DOCUMENT_OCR_THREADS still overrides this, and 0 restores the old
        # unbounded behaviour.
        threads = _int_env(
            "DOCUMENT_OCR_THREADS",
            _default_ocr_threads(),
        )

        try:
            from rapidocr_onnxruntime import RapidOCR

            if threads > 0:
                try:
                    self._engine = RapidOCR(
                        intra_op_num_threads=threads,
                        inter_op_num_threads=1,
                    )
                except TypeError:
                    # Compatibility with RapidOCR versions that do not
                    # expose ONNX Runtime thread arguments.
                    self._engine = RapidOCR()
            else:
                self._engine = RapidOCR()

        except Exception as exc:
            raise RuntimeError(
                "RapidOCR initialization failed. "
                "Make sure rapidocr_onnxruntime is installed correctly."
            ) from exc

        self._warm = False

    # ------------------------------------------------------------------------
    # RESULT CONVERSION
    # ------------------------------------------------------------------------

    @staticmethod
    def _to_tokens(
        result: Any,
    ) -> list[OCRToken]:
        """
        Convert RapidOCR output into the application's OCRToken format.

        RapidOCR generally returns:

            [
                [
                    [x1, y1],
                    [x2, y2],
                    [x3, y3],
                    [x4, y4]
                ],
                "text",
                confidence
            ]
        """

        tokens: list[OCRToken] = []

        if result is None:
            return tokens

        for item in result:
            try:
                if not item or len(item) < 3:
                    continue

                box = item[0]
                text = str(item[1]).strip()
                confidence = float(item[2])

                if not text:
                    continue

                tokens.append(
                    # The schema stores an axis-aligned box as x0/y0/x1/y1.
                    # Passing `box=` silently created an extra field (the model
                    # allows extras) and left every coordinate at 0, which
                    # destroyed all spatial reasoning: labels could not find
                    # their values and low-confidence Devanagari residue won
                    # the name field.
                    _to_box_token(text, confidence, box)
                )

            except Exception:
                # A malformed OCR token must not break the whole document
                # extraction pipeline.
                continue

        return tokens

    # ------------------------------------------------------------------------
    # FILE OCR
    # ------------------------------------------------------------------------

    def read(
        self,
        image_path: str | Path,
    ) -> tuple[list[OCRToken], float]:
        """
        OCR an image from disk.

        Preprocessing matches the production document pipeline.
        """

        from app.agents.document_agent import preprocess

        started = time.perf_counter()

        image = preprocess.standard(
            preprocess.load(image_path)
        )

        array = preprocess.to_array(
            image
        )

        result, _ = self._engine(array)

        elapsed_ms = (
            time.perf_counter() - started
        ) * 1000.0

        return (
            self._to_tokens(result),
            elapsed_ms,
        )

    # ------------------------------------------------------------------------
    # ARRAY OCR
    # ------------------------------------------------------------------------

    def read_array(
        self,
        array: np.ndarray,
    ) -> tuple[list[OCRToken], float]:
        """
        OCR an already decoded/preprocessed image array.

        The measured time covers actual RapidOCR inference.
        """

        started = time.perf_counter()

        result, _ = self._engine(array)

        elapsed_ms = (
            time.perf_counter() - started
        ) * 1000.0

        return (
            self._to_tokens(result),
            elapsed_ms,
        )

    # ------------------------------------------------------------------------
    # REPRESENTATIVE WARMUP
    # ------------------------------------------------------------------------

    def warmup(self) -> float:
        """
        Warm up RapidOCR using a representative document-sized image.

        The warmup intentionally does not use a real customer document.

        Representative dimensions:

            426 x 670

        The warmup is performed only once per engine instance.
        """

        if self._warm:
            return 0.0

        started = time.perf_counter()

        # --------------------------------------------------------------------
        # Representative document canvas
        # --------------------------------------------------------------------

        warmup_image = np.full(
            (426, 670, 3),
            255,
            dtype=np.uint8,
        )

        # --------------------------------------------------------------------
        # Document border
        # --------------------------------------------------------------------

        cv2.rectangle(
            warmup_image,
            (30, 30),
            (640, 395),
            (0, 0, 0),
            2,
        )

        # --------------------------------------------------------------------
        # Header-like region
        # --------------------------------------------------------------------

        cv2.rectangle(
            warmup_image,
            (60, 60),
            (360, 78),
            (0, 0, 0),
            -1,
        )

        # --------------------------------------------------------------------
        # Text-like structures
        # --------------------------------------------------------------------

        for y in range(105, 360, 45):
            cv2.rectangle(
                warmup_image,
                (60, y),
                (500, y + 12),
                (0, 0, 0),
                -1,
            )

        # --------------------------------------------------------------------
        # Right-side smaller text-like blocks
        # --------------------------------------------------------------------

        for y in range(105, 360, 45):
            cv2.rectangle(
                warmup_image,
                (530, y),
                (610, y + 12),
                (0, 0, 0),
                -1,
            )

        # --------------------------------------------------------------------
        # Run through the same read_array path used by production.
        # --------------------------------------------------------------------

        self.read_array(
            warmup_image
        )

        self._warm = True

        return (
            time.perf_counter() - started
        ) * 1000.0


# ============================================================================
# TESSERACT
# ============================================================================

class TesseractEngine:
    """
    Tesseract OCR adapter.

    RapidOCR remains the primary production OCR engine.
    Tesseract is retained for targeted/fallback operations.
    """

    name = "tesseract"

    def __init__(self) -> None:
        self._warm = False

        try:
            import pytesseract

            self._pytesseract = pytesseract

        except Exception as exc:
            raise RuntimeError(
                "pytesseract is not installed."
            ) from exc

    # ------------------------------------------------------------------------
    # ARRAY OCR
    # ------------------------------------------------------------------------

    def read_array(
        self,
        array: np.ndarray,
    ) -> tuple[list[OCRToken], float]:
        """
        Run Tesseract OCR against an image array.
        """

        started = time.perf_counter()

        data = self._pytesseract.image_to_data(
            array,
            output_type=self._pytesseract.Output.DICT,
        )

        tokens: list[OCRToken] = []

        texts = data.get("text", [])
        confidences = data.get("conf", [])
        left = data.get("left", [])
        top = data.get("top", [])
        width = data.get("width", [])
        height = data.get("height", [])

        for i, text in enumerate(texts):
            text = str(text).strip()

            if not text:
                continue

            try:
                confidence = float(
                    confidences[i]
                )
            except Exception:
                confidence = 0.0

            try:
                x = float(left[i])
                y = float(top[i])
                w = float(width[i])
                h = float(height[i])

                box = [
                    [x, y],
                    [x + w, y],
                    [x + w, y + h],
                    [x, y + h],
                ]

            except Exception:
                box = []

            tokens.append(
                OCRToken(
                    text=text,
                    confidence=max(
                        0.0,
                        confidence / 100.0,
                    ),
                    box=box,
                )
            )

        elapsed_ms = (
            time.perf_counter() - started
        ) * 1000.0

        return (
            tokens,
            elapsed_ms,
        )

    # ------------------------------------------------------------------------
    # FILE OCR
    # ------------------------------------------------------------------------

    def read(
        self,
        image_path: str | Path,
    ) -> tuple[list[OCRToken], float]:
        """
        Run Tesseract OCR on an image path.
        """

        from app.agents.document_agent import preprocess

        image = preprocess.load(
            image_path
        )

        image = preprocess.standard(
            image
        )

        array = preprocess.to_array(
            image
        )

        return self.read_array(
            array
        )

    # ------------------------------------------------------------------------
    # WARMUP
    # ------------------------------------------------------------------------

    def warmup(self) -> float:
        """
        Warm up Tesseract with a representative image.
        """

        if self._warm:
            return 0.0

        started = time.perf_counter()

        image = np.full(
            (256, 512, 3),
            255,
            dtype=np.uint8,
        )

        cv2.putText(
            image,
            "DOCUMENT OCR WARMUP",
            (30, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )

        self.read_array(
            image
        )

        self._warm = True

        return (
            time.perf_counter() - started
        ) * 1000.0


# ============================================================================
# ENGINE REGISTRY
# ============================================================================

_ENGINES: dict[str, type] = {
    "rapidocr": RapidOCREngine,
    "tesseract": TesseractEngine,
}


# ============================================================================
# CACHED ENGINE
# ============================================================================

_ENGINE: OCREngine | None = None


#: One engine per OCR worker thread. Declared beside the process-level
#: singleton so the two lifetimes are visible together; reset_engine()
#: clears both.
logger = logging.getLogger(__name__)

_THREAD_LOCAL = threading.local()


def get_engine(
    engine_name: str | None = None,
) -> OCREngine:
    """
    Return the process-level cached OCR engine.

    If engine_name is provided, use that engine name.
    Otherwise use DOCUMENT_OCR_ENGINE.

    One engine PER OCR WORKER THREAD, created once and reused.

    It was previously one instance for the whole process, which forced
    recognition through a single worker: the RapidOCR wrapper holds an ONNX
    session and sharing one across threads is not safe. Measured on a real
    four-document request, that made three OCR calls queue behind each other
    for 16.9 seconds inside a 7.5 second request -- the documents ran
    concurrently and then waited in line at this one door.

    Giving each worker its OWN engine removes the sharing rather than the
    safety: no session is ever touched by two threads. The cost is one model
    in memory per worker, which is why the worker count stays bounded and
    configurable rather than growing with the request.

    Callers outside the OCR pool (warmup, tests, a direct synchronous read)
    share one process-level instance exactly as before.
    """

    global _ENGINE

    thread = threading.current_thread()
    per_thread = thread.name.startswith(_OCR_THREAD_PREFIX)

    if per_thread:
        existing = getattr(_THREAD_LOCAL, "engine", None)
        if existing is not None:
            return existing
    elif _ENGINE is not None:
        return _ENGINE

    if engine_name is None:
        engine_name = os.getenv(
            "DOCUMENT_OCR_ENGINE",
            "rapidocr",
        )

    engine_name = engine_name.strip().lower()

    engine_class = _ENGINES.get(
        engine_name
    )

    if engine_class is None:
        raise ValueError(
            f"Unsupported OCR engine: {engine_name}. "
            f"Available engines: {sorted(_ENGINES)}"
        )

    engine = engine_class()

    if per_thread:
        _THREAD_LOCAL.engine = engine
    else:
        _ENGINE = engine

    return engine


# ============================================================================
# OCR EXECUTOR
# ============================================================================

_OCR_THREAD_PREFIX = "ocr"

# Recognition used to be serialised to ONE worker, because the RapidOCR
# wrapper holds an ONNX session and sharing one across threads is not safe.
# That made this the narrowest point in the whole request: measured on a real
# four-document application, three OCR calls spent 16.9 seconds queueing here
# inside a 7.5 second request. The documents were already concurrent; they
# simply arrived at a one-lane door.
#
# get_engine() now hands each worker in this pool its OWN engine, so no
# session is shared and the serialisation is no longer needed for
# correctness. The boundary that remains is MEMORY: one model per worker.
#
# Bounded and conservative by default -- never more than the machine has
# cores for, and capped well below it, because each engine also runs its own
# ONNX threads and over-subscribing makes every document slower rather than
# faster. DOCUMENT_OCR_WORKERS still overrides it, including back down to 1.
#
# TWO MEASUREMENTS EXIST AND THEY DISAGREE. Both are kept, because the
# disagreement is the useful part: this number is host-dependent and the
# next person to tune it needs to know that, not just the answer.
#
#     16-core host, warm 4-document request, earlier run:
#         workers   1        2        3        4
#         wall   8444 ms  6912 ms  7165 ms  7722 ms
#
#     16-logical host, warm 4-document request, best of two, later run:
#         workers            2        3        4
#         wall            7780 ms  6297 ms  6002 ms
#         summed OCR     10087 ms 11137 ms 16768 ms
#
# The second run puts the knee at three rather than two. The runs are not
# reconcilable from here -- different load, and the later one saw large
# run-to-run variance (11819 ms cold against 7780 ms warm on the same
# configuration) -- so the default follows the more recent measurement and
# the override exists for hosts where it does not hold.
#
# WHAT DOES NOT CHANGE with the count: `_default_ocr_threads` divides the
# machine between the workers, so three workers each get fewer ONNX threads
# rather than three engines all claiming the whole box. That derivation is
# why the summed OCR time rises while the wall time falls -- more overlap,
# each pass individually slower. Wall time is what a caller waits for.
#
# Cold start still costs one model load per worker, which is why startup
# warms them all rather than paying it inside a request.
def _default_ocr_workers() -> int:
    return max(1, min(3, os.cpu_count() or 1))


def _default_ocr_threads() -> int:
    """
    ONNX intra-op threads per engine, divided among the OCR workers.

    ONNX Runtime sizes this pool from the core count PER SESSION, so with one
    engine per worker the process asks for workers x cores threads. Splitting
    the machine between the workers instead is what removes the contention.

    os.cpu_count() reports logical CPUs, and the second thread of an SMT pair
    buys very little on this workload, so the physical count is approximated
    as half. On the reference host -- 16 logical, 2 workers -- that gives 4,
    which matched the measured optimum (see RapidOCREngine.__init__).

    Never returns less than 2: a single-threaded recogniser is slower than
    the contention this is avoiding.

    ONE WORKER IS NOT AN IMPROVEMENT ON THIS. Serialising recognition to a
    single worker was measured at 29316 ms for four documents against 3525 ms
    for two workers -- the documents simply queue.
    """
    logical = os.cpu_count() or 4
    workers = max(1, _int_env("DOCUMENT_OCR_WORKERS", _default_ocr_workers()))
    return max(2, (logical // 2) // workers)


_OCR_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, _int_env("DOCUMENT_OCR_WORKERS", _default_ocr_workers())),
    thread_name_prefix=_OCR_THREAD_PREFIX,
)


# Everything around recognition -- PDF rasterisation, pypdf parsing, image
# decoding, the financial extractors -- is independent per request and merely
# blocking. It gets its own pool so a slow document cannot stall the event
# loop, and so several documents can be prepared while one of them is being
# recognised.
_DOCUMENT_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, _int_env("DOCUMENT_WORKERS", 4)),
    thread_name_prefix="doc",
)


# Rasterisation runs a poppler subprocess and is independent of recognition,
# so the next page can be prepared while the current one is being read.
#
# It gets its OWN pool rather than reusing the document pool. A document
# worker submits a render and then waits for it; if both lived in one pool,
# enough concurrent documents would occupy every worker waiting on renders
# that could never be scheduled. Work only ever flows document -> render and
# document -> OCR, never back, so there is no cycle to deadlock on.
_RENDER_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, _int_env("DOCUMENT_RENDER_WORKERS", 2)),
    thread_name_prefix="render",
)


def get_ocr_executor() -> ThreadPoolExecutor:
    """
    Return the dedicated OCR executor.

    CPU-heavy OCR work is isolated from the application's general async
    executor.
    """

    return _OCR_EXECUTOR


def get_document_executor() -> ThreadPoolExecutor:
    """Return the executor for blocking, non-recognition document work."""

    return _DOCUMENT_EXECUTOR


def get_render_executor() -> ThreadPoolExecutor:
    """Return the executor used to rasterise PDF pages ahead of recognition."""

    return _RENDER_EXECUTOR



def warmup_all() -> float:
    """
    Build and warm one engine on EVERY OCR worker, at startup.

    Each worker lazily builds its own engine on first use, so without this
    the second worker pays a full model load inside the first request that
    happens to need it -- measured at roughly 2.4 seconds of cold latency
    that the pool was supposed to remove.

    A barrier is what forces one task onto each thread: submitting N tasks to
    an N-worker pool otherwise lets one fast worker take several of them and
    leaves the rest cold.

    Returns the wall-clock milliseconds spent. Never raises: a warm-up
    failure must not stop the service from starting, and the first real
    request will surface any genuine engine problem itself.
    """
    workers = _OCR_EXECUTOR._max_workers
    started = time.perf_counter()

    barrier = threading.Barrier(workers, timeout=120)

    def prepare() -> None:
        engine = get_engine()
        # Hold every worker here until all of them have an engine, so no
        # single thread can satisfy more than one of these tasks.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        engine.warmup()

    futures = [_OCR_EXECUTOR.submit(prepare) for _ in range(workers)]
    for future in futures:
        try:
            future.result(timeout=180)
        except Exception as exc:
            logger.warning("OCR worker warmup failed: %r", exc)

    # AND THE SHARED ENGINE, which is the one the document pipeline uses.
    #
    # get_engine() hands a per-thread engine to callers ON an OCR worker
    # thread and the single process-level engine to everyone else. The loop
    # above runs on the OCR workers, so it warms only the per-thread ones --
    # while workflow._recognise() calls get_engine() from a DOCUMENT worker,
    # gets the shared engine, and passes that to the OCR pool.
    #
    # The result was that startup warmed engines the main path never touches
    # and the shared one was still built cold inside the first real request.
    # Measured on the reference host: first request 1847 ms of OCR against
    # 506-536 ms for every request after it.
    #
    # Warming it here is what the startup warm-up was for. Called from the
    # lifespan thread -- not an OCR worker -- so get_engine() returns the
    # shared instance, exactly as the request path will.
    try:
        get_engine().warmup()
    except Exception as exc:
        logger.warning("Shared OCR engine warmup failed: %r", exc)

    return (time.perf_counter() - started) * 1000


def submit_ocr(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Run `func` on the OCR executor from a synchronous caller and wait.

    Document workers use this to hand recognition to the serialised OCR
    worker. Calling it from the OCR thread itself would deadlock a
    single-worker pool waiting on itself, so that case runs inline.
    """

    if threading.current_thread().name.startswith(_OCR_THREAD_PREFIX):
        return func(*args, **kwargs)

    return _OCR_EXECUTOR.submit(func, *args, **kwargs).result()


# ============================================================================
# ASYNC HELPERS
# ============================================================================

async def run_ocr(
    func: Callable[..., Any],
    *args: Any,
) -> Any:
    """
    Run a synchronous OCR/document function on the dedicated OCR executor.
    """

    import asyncio

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        get_ocr_executor(),
        func,
        *args,
    )


async def run_document(
    func: Callable[..., Any],
    *args: Any,
) -> Any:
    """
    Run a blocking document function off the event loop.

    The whole synchronous document pipeline goes through here, so an OCR or
    rasterisation pass can never stall unrelated requests -- including the
    readiness probe the platform depends on.
    """

    import asyncio

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        get_document_executor(),
        func,
        *args,
    )


# ============================================================================
# ENGINE RESET
# ============================================================================

def reset_engine() -> None:
    """
    Reset the cached OCR engine.

    Intended for tests or controlled process reinitialization.

    Do not call this for every document.
    """

    global _ENGINE, _THREAD_LOCAL

    _ENGINE = None
    # Every worker's instance goes too: a reset that left the pool holding
    # the old engines would be a reset in name only.
    _THREAD_LOCAL = threading.local()

