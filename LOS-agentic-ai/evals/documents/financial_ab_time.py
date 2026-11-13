"""
OLD vs NEW scanned-page path, in ONE process, alternating per document (2026-11-12) -- the only fair time comparison
on a host whose speed drifts 3-4x between runs.

    python -m evals.documents.financial_ab_time <folder> [--limit N]

OLD = no scan_ocr settings: Tesseract OSD on every page, the per-line classifier, every page read (max_pages).
NEW = config financial_documents.yaml scan_ocr: OSD only as a fallback, upright, early stop.
Reports wall time and the WORK done (OCR passes, OSD calls, pages read) -- the work does not drift with the host.
Prints no document content.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

from app.agents.document_agent import scan_text as ST


def _kind(path: Path) -> str | None:
    n = path.name.lower()
    return "ITR" if n.startswith("itr") else "SALARY" if n.startswith("salary") else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-scans", action="store_true", help="skip files with a usable text layer")
    args = ap.parse_args()

    from app.agents.itr import extract_itr
    from app.agents.salary_slip import extract_salary_slip

    counts = {"ocr": 0, "osd": 0}
    real_orient, real_read, real_cfg = ST.orient, ST._read_tokens, ST._scan_config

    def orient(img):
        counts["osd"] += 1
        return real_orient(img)

    def read(img, *, upright):
        counts["ocr"] += 1
        return real_read(img, upright=upright)

    ST.orient, ST._read_tokens = orient, read
    files = [p for p in sorted(Path(args.folder).iterdir()) if p.is_file() and _kind(p)]
    if args.limit:
        files = files[:args.limit]
    extract_salary_slip(str(files[0]))                                   # warm the engine outside the timings
    rows = []
    for i, f in enumerate(files):
        run = extract_itr if _kind(f) == "ITR" else extract_salary_slip
        order = ("old", "new") if i % 2 == 0 else ("new", "old")
        out = {}
        for mode in order:
            ST._scan_config = real_cfg if mode == "new" else (lambda _t: {})
            counts.update(ocr=0, osd=0)
            t = time.perf_counter()
            res = run(str(f))
            out[mode] = {"ms": (time.perf_counter() - t) * 1000, "ocr": counts["ocr"], "osd": counts["osd"],
                         "status": res.status.value}
        ST._scan_config = real_cfg
        if args.only_scans and out["old"]["ocr"] == 0:
            continue
        rows.append((f.name, out))
        print(f"{f.name:34} old {out['old']['ms']:7.0f} ms ocr={out['old']['ocr']} osd={out['old']['osd']} "
              f"{out['old']['status']:8} | new {out['new']['ms']:7.0f} ms ocr={out['new']['ocr']} "
              f"osd={out['new']['osd']} {out['new']['status']}", flush=True)
    if not rows:
        print("nothing OCR'd")
        return
    for mode in ("old", "new"):
        ms = sorted(r[1][mode]["ms"] for r in rows)
        p95 = ms[min(len(ms) - 1, int(round(0.95 * (len(ms) - 1))))]
        print(f"{mode.upper():3}  docs={len(rows)}  p50={statistics.median(ms):7.0f} ms  p95={p95:7.0f} ms  "
              f"OCR passes={sum(r[1][mode]['ocr'] for r in rows)}  OSD calls={sum(r[1][mode]['osd'] for r in rows)}")


if __name__ == "__main__":
    sys.exit(main())
