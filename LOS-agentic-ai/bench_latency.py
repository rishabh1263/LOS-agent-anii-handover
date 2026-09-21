"""Before/after latency benchmark across all three layers."""
from __future__ import annotations
import asyncio, json, statistics, sys, time
from pathlib import Path

SAMPLES = ["lPan.jpg", "rpan.jpg", "original.jpg", "f3.jpg", "driving_license.jpg"]
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def main():
    from app.agents.document_agent import preprocess as P
    from app.agents.document_agent.ocr import get_engine
    from app.agents.document_agent.pipeline import extract_document

    engine = get_engine()
    t = time.perf_counter(); engine.warmup(); warm_ms = (time.perf_counter() - t) * 1000
    print(f"startup warmup: {warm_ms:.0f} ms\n")

    print(f"{'sample':<22}{'direct_ocr':>11}{'extract_doc':>13}{'spacing':>9}{'calls':>7}{'status':>10}")
    print("-" * 74)
    totals = []
    for name in SAMPLES:
        path = Path("samples/documents") / name
        if not path.exists():
            continue
        arr = P.to_array(P.standard(P.load(str(path))))
        engine.read_array(arr)
        ocr = statistics.mean(
            [engine.read_array(arr)[1] for _ in range(RUNS)]
        )
        extract_document(str(path))
        results = [extract_document(str(path)) for _ in range(RUNS)]
        end = statistics.mean(r.processing.total_ms for r in results)
        sp = statistics.mean(r.processing.name_spacing_ms for r in results)
        calls = results[-1].processing.name_spacing_calls
        totals.append(end)
        print(f"{name:<22}{ocr:>11.0f}{end:>13.0f}{sp:>9.0f}{calls:>7}{results[-1].status.value:>10}")

    print("\n" + "=" * 74)
    print(f"extract_document  avg {statistics.mean(totals):.0f} ms | "
          f"max {max(totals):.0f} ms | target <= 2000 ms")


if __name__ == "__main__":
    main()
