"""
BENCHMARK: bank statements and ITRs through the real extraction (Phase 1 baseline; docs/financial_documents/).

    python scripts/bench_financial_documents.py --kind bank --folder <dir> [--repeat 2] [--json out.json]
    python scripts/bench_financial_documents.py --kind itr  --folder <dir>

PRINTS NO DOCUMENT CONTENT: a document is named by a short hash of its file name; only shapes are reported -- pages,
how each page was routed (text layer / OCR), status, time, rows read, whether the arithmetic reconciles, peak memory.
Run on an idle machine: the first pass is the cold start (models loading), the next ones are warm.

ACCURACY IS NOT MEASURED HERE: that needs a hand-verified ground truth per document (docs/financial_documents/
PHASE0_AUDIT.md, G8), never our own output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SUFFIXES = {"bank": {".pdf"}, "itr": {".pdf", ".jpg", ".jpeg", ".png"}}


def _doc_id(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:8]


class _PeakRss:
    """The process's peak resident memory while a block runs (sampled every 50 ms)."""

    def __enter__(self):
        import psutil

        self._p, self.peak, self._stop = psutil.Process(), 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self._p.memory_info().rss)
            self._stop.wait(0.05)

    def __exit__(self, *_):
        self._stop.set()
        self._t.join()


def _routing(path: Path) -> tuple[int, int]:
    from app.agents.bank_statement import page_routing as R

    try:
        return R.route_counts(R.inspect_pages(str(path)))
    except Exception:  # noqa: BLE001 - an image or an unreadable PDF
        return (0, 0)


def run_one(kind: str, path: Path) -> dict:
    text_pages, ocr_pages = _routing(path) if path.suffix.lower() == ".pdf" else (0, 1)
    with _PeakRss() as mem:
        started = time.perf_counter()
        if kind == "bank":
            from app.agents.bank_statement import extract_bank_statement

            r = extract_bank_statement(str(path))
            facts = {"status": r.status.value, "source": r.source_kind.value, "rows": r.transaction_count,
                     "reconciliation": r.reconciliation, "rows_disagreeing": r.rows_disagreeing_with_balance,
                     "pages_unread": r.pages_unread}
        else:
            from app.agents.itr.extract import extract_itr

            r = extract_itr(str(path))
            fields = ("pan", "assessment_year", "form_number", "acknowledgement_number", "total_income",
                      "tax_payable_or_refundable")
            facts = {"status": r.status.value, "source": r.source_kind,
                     "fields_read": sum(1 for f in fields if getattr(r, f, None) is not None), "fields": len(fields)}
        ms = (time.perf_counter() - started) * 1000
    return {"doc": _doc_id(path), "pages": text_pages + ocr_pages, "text_pages": text_pages, "ocr_pages": ocr_pages,
            "ms": round(ms, 1), "peak_mb": round(mem.peak / 2**20), **facts}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=["bank", "itr"], required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--repeat", type=int, default=2, help="passes (the first is cold)")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    files = sorted(f for f in Path(args.folder).iterdir() if f.suffix.lower() in SUFFIXES[args.kind])
    if not files:
        print("no documents")
        return 1
    import platform
    import os

    passes = []
    for n in range(max(1, args.repeat)):
        rows = [run_one(args.kind, f) for f in files]
        passes.append(rows)
        label = "COLD" if n == 0 else f"WARM {n}"
        ms = [r["ms"] for r in rows]
        print(f"\n== pass {n + 1} ({label}): {len(rows)} documents  P50 {_pct(ms, .5):.0f} ms  P95 {_pct(ms, .95):.0f} ms"
              f"  max {max(ms):.0f} ms  peak RSS {max(r['peak_mb'] for r in rows)} MB")
        for r in rows:
            print("   " + "  ".join(f"{k}={v}" for k, v in r.items()))
    report = {"kind": args.kind, "documents": len(files), "machine": {"cpu_count": os.cpu_count(),
              "python": platform.python_version(), "platform": platform.platform()}, "passes": passes}
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    statuses = statistics.multimode([r["status"] for r in passes[-1]])
    print(f"\nmost common status: {statuses}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
