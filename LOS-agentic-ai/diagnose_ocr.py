"""
OCR performance diagnostic.

Run this on the machine that is slow:

    python diagnose_ocr.py samples/documents/lPan.jpg

It separates model-load cost from per-image inference cost, which is the
question that matters: a slow FIRST call is a warmup problem, a slow EVERY
call is a hardware or configuration problem.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    image = sys.argv[1] if len(sys.argv) > 1 else "samples/documents/lPan.jpg"
    if not Path(image).exists():
        print(f"Image not found: {image}")
        return

    print("=" * 66)
    print("ENVIRONMENT")
    print("=" * 66)
    print(f"  cpu_count            : {os.cpu_count()}")
    for key in (
        "DOCUMENT_OCR_ENGINE", "DOCUMENT_OCR_MAX_SIDE",
        "DOCUMENT_OCR_THREADS", "DOCUMENT_OCR_WARMUP",
    ):
        print(f"  {key:<21}: {os.getenv(key)!r}")

    try:
        import onnxruntime as ort

        print(f"  onnxruntime          : {ort.__version__}")
        print(f"  providers            : {ort.get_available_providers()}")
    except Exception as exc:
        print(f"  onnxruntime          : NOT IMPORTABLE ({exc})")
        return

    from PIL import Image

    img = Image.open(image)
    print(f"  image                : {image} {img.size}")

    print()
    print("=" * 66)
    print("MODEL LOAD (paid once per process)")
    print("=" * 66)
    from rapidocr_onnxruntime import RapidOCR

    started = time.perf_counter()
    engine = RapidOCR()
    load_ms = (time.perf_counter() - started) * 1000
    print(f"  RapidOCR() constructor : {load_ms:>9.0f} ms")

    from app.agents.document_agent import preprocess as P

    arr = P.to_array(P.standard(P.load(image)))
    print(f"  preprocessed size      : {arr.shape[1]}x{arr.shape[0]}")

    print()
    print("=" * 66)
    print("INFERENCE (5 consecutive calls, same image)")
    print("=" * 66)
    times = []
    for i in range(1, 6):
        started = time.perf_counter()
        engine(arr)
        ms = (time.perf_counter() - started) * 1000
        times.append(ms)
        tag = "  <-- includes lazy model init" if i == 1 else ""
        print(f"  call {i}                 : {ms:>9.0f} ms{tag}")

    warm = times[1:]
    print()
    print("=" * 66)
    print("VERDICT")
    print("=" * 66)
    first, avg_warm = times[0], sum(warm) / len(warm)
    print(f"  first call             : {first:>9.0f} ms")
    print(f"  warm average           : {avg_warm:>9.0f} ms")

    if first > avg_warm * 3:
        print()
        print("  -> Model loading dominates. Every request is paying it, which")
        print("     means the engine is being rebuilt per request.")
        print("     Check: is DOCUMENT_OCR_WARMUP=true, is uvicorn running")
        print("     WITHOUT --reload, and is --workers 1?")
    elif avg_warm > 5000:
        print()
        print("  -> Every call is slow, so this is hardware or interference,")
        print("     not warmup. Try in order:")
        print("       1. Exclude the project folder + .venv from antivirus")
        print("          real-time scanning (the usual cause on corporate")
        print("          Windows laptops).")
        print("       2. Set DOCUMENT_OCR_THREADS to your physical core count.")
        print("       3. Lower DOCUMENT_OCR_MAX_SIDE to 960, then 800.")
    else:
        print()
        print("  -> Inference speed is healthy. If the API is still slow, the")
        print("     cost is outside OCR: check that the engine is cached and")
        print("     that only one uvicorn worker is running.")

    print()
    print("=" * 66)
    print("THREAD SETTINGS (oversubscription is the usual culprit)")
    print("=" * 66)
    print("  Small OCR models often run SLOWER with many threads, because")
    print("  synchronisation costs more than the parallelism saves.")
    print()
    for label, kwargs in [
        ("ONNX default (recommended)", None),
        ("intra=2", {"intra_op_num_threads": 2, "inter_op_num_threads": 1}),
        ("intra=4", {"intra_op_num_threads": 4, "inter_op_num_threads": 1}),
        (f"intra={os.cpu_count()} (all cores)",
         {"intra_op_num_threads": os.cpu_count(), "inter_op_num_threads": os.cpu_count()}),
    ]:
        try:
            candidate = RapidOCR() if kwargs is None else RapidOCR(**kwargs)
            candidate(arr)  # warm
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                candidate(arr)
                runs.append((time.perf_counter() - started) * 1000)
            print(f"  {label:<28}: {sum(runs)/len(runs):>8.0f} ms")
        except Exception as exc:
            print(f"  {label:<28}: failed ({type(exc).__name__})")

    print()
    print("  Pick the fastest. Leave DOCUMENT_OCR_THREADS commented out to")
    print("  use the ONNX default, or set it to the winning intra value.")

    print()
    print("=" * 66)
    print("ENGINE CACHING (is one instance reused?)")
    print("=" * 66)
    from app.agents.document_agent.ocr import get_engine

    ids = {id(get_engine()) for _ in range(3)}
    print(f"  distinct engine objects: {len(ids)}  ({'OK, cached' if len(ids) == 1 else 'BROKEN - rebuilt each call'})")


if __name__ == "__main__":
    main()