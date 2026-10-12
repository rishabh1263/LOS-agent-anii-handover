"""
DOCUMENT PIPELINE BENCHMARK -- upload -> verification -> extraction -> KYC, measured in process.

    python scripts/bench_documents.py [--runs 3] [--out runs/bench/documents.json] [--sets one,two,...]

What it measures: `flow.process_application` -- the same call the LOS upload route makes -- on fixed fixture
sets: 1, 2, 4 and 5 documents, the complete HOME_LOAN and default (LAP falls back to it) FOS sets, and edge cases
(scanned PDF, large file, malformed file, unreadable image). Per run: wall time, the pipeline's own per-stage
timings (OCR, classification, verification, extraction, KYC, profile match), per-document totals, status per
document, process CPU time and peak RSS (psutil). OCR engines are warmed first, as the server does at startup.

What it does NOT do: it writes nothing (process_application does not persist; the route does), calls no LLM
(summarise / use_llm_summary off), and prints no document content -- only types, statuses and timings, because
the sample fixtures are real documents.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
S = ROOT / "samples"


def _signature() -> bytes:
    """A synthetic signature photo (the same shape the signature tests use)."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (640, 240), 250)
    rng, pixels = random.Random(3), image.load()
    for _ in range(image.width * image.height // 6):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-18, 18)))
    draw = ImageDraw.Draw(image)
    draw.line([(60 + i * 17, 120 + int(40 * ((i % 5) - 2) / 2)) for i in range(30)], fill=20, width=5)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _large_pdf() -> bytes:
    """A large text PDF: the HDFC statement's pages repeated (pypdf), about 40 pages."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(S / "real_batch/bank_hdfc_new.pdf"))
    writer = PdfWriter()
    while len(writer.pages) < 40:
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _file(rel: str) -> bytes:
    return (S / rel).read_bytes()


# (name, expected type, bytes) -- built lazily so a missing optional library fails one set, not the run
PAN = ("pan.jpg", "PAN", lambda: _file("documents/lPan.jpg"))
DL = ("dl.jpg", "DRIVING_LICENCE", lambda: _file("documents/driving_license.jpg"))
VOTER = ("voter.jpg", "VOTER_ID", lambda: _file("documents/voter_id2.jpg"))
BANK_TEXT = ("bank.pdf", "BANK_STATEMENT", lambda: _file("real_batch/bank_hdfc_new.pdf"))
BANK_SCAN = ("bank_scanned.pdf", "BANK_STATEMENT", lambda: _file("real_batch/bank_sbi_scanned.pdf"))
SLIP = ("salary.pdf", "SALARY_SLIP", lambda: _file("documents/demo_salary_slip_rahul_sharma.pdf"))
ITR = ("itr.pdf", "ITR", lambda: _file("documents/ITR.pdf"))
SIGN = ("signature.png", "SIGNATURE", _signature)
LARGE = ("bank_large.pdf", "BANK_STATEMENT", _large_pdf)
MALFORMED = ("broken.pdf", "BANK_STATEMENT", lambda: b"%PDF-1.4\n" + os.urandom(4096))
UNREADABLE = ("blank.jpg", "PAN", lambda: _blank_jpg())


def _blank_jpg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (800, 500), (250, 250, 250)).save(out, "JPEG")
    return out.getvalue()


SETS = {
    "one": [PAN],
    "two": [PAN, DL],
    "four": [PAN, DL, BANK_TEXT, SIGN],
    "five": [PAN, DL, BANK_TEXT, SIGN, SLIP],
    # applicant_agent.yaml documents.HOME_LOAN: required PAN, BANK_STATEMENT, ADDRESS_PROOF, SIGNATURE;
    # optional ITR, SALARY_SLIP, EMPLOYMENT_PROOF (no fixture) -- the complete set an officer would upload
    "home_loan_full": [PAN, BANK_TEXT, DL, SIGN, ITR, SLIP],
    # LOAN_AGAINST_PROPERTY has no FOS entry: documents.default applies (PAN, ADDRESS_PROOF, BANK_STATEMENT,
    # SIGNATURE). Sale deed is a CPA requirement (stage_requirements.yaml) and not an FOS upload type.
    "lap_full_default": [PAN, VOTER, BANK_TEXT, SIGN],
    "edge_scanned_pdf": [BANK_SCAN],
    "edge_large_pdf": [LARGE],
    "edge_malformed": [MALFORMED],
    "edge_unreadable_image": [UNREADABLE],
}


async def _one_run(spec, request_id: str) -> dict:
    import psutil

    from app.agents.los import flow
    from app.agents.los.flow import UploadedDocument, process_application

    # THE PIPELINE'S OWN STAGE TIMINGS: the public response carries only `processing_ms`; the envelope's
    # `processing` dict (and each document's) is what `_aggregate_timings` builds -- captured, never changed
    seen: dict = {}
    real_aggregate = flow._aggregate_timings

    def capture(documents):
        seen["documents"] = [dict(d.get("processing") or {}) for d in documents]
        seen["processing"] = real_aggregate(documents)
        return seen["processing"]

    flow._aggregate_timings = capture
    uploads = [UploadedDocument(source_id=f"{request_id}-{i}", filename=name, content=make(), expected_type=kind)
               for i, (name, kind, make) in enumerate(spec)]
    proc = psutil.Process()
    cpu0 = sum(proc.cpu_times()[:2])
    started = time.perf_counter()
    error = None
    try:
        result = await process_application(uploads, request_id=request_id, summarise=False,
                                           use_llm_summary=False)
    except Exception as exc:  # noqa: BLE001 - a failed run is a measured outcome, not a crash of the benchmark
        result, error = {}, f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        flow._aggregate_timings = real_aggregate
    wall = (time.perf_counter() - started) * 1000
    cpu = (sum(proc.cpu_times()[:2]) - cpu0) * 1000
    documents = result.get("documents") or []
    return {
        "wall_ms": round(wall, 1), "cpu_ms": round(cpu, 1), "rss_mb": round(proc.memory_info().rss / 2**20, 1),
        "error": error, "status": result.get("status"),
        "processing": {**(seen.get("processing") or {}), "processing_ms": result.get("processing_ms")},
        "documents": [{"type": d.get("expected_type") or d.get("document_type"),
                       "classified": d.get("document_type"),
                       "status": (d["verification"].get("status") if isinstance(d.get("verification"), dict)
                                  else d.get("verification") or d.get("status")),
                       "ms": (timing.get("total_ms") or round(sum(float(v or 0) for k, v in timing.items()
                                                                  if k.endswith("_ms")), 1))}
                      for d, timing in zip(documents, (seen.get("documents") or []) + [{}] * len(documents))],
        "document_stages": seen.get("documents") or [],
    }


def _stats(values: list[float]) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {}
    out = {"n": len(values), "median": round(statistics.median(values), 1), "min": values[0], "max": values[-1]}
    if len(values) >= 20:                                    # p95 only where it means something
        out["p95"] = values[int(0.95 * (len(values) - 1))]
    return out


async def main(runs: int, out: Path, wanted: list[str]) -> None:
    from app.agents.document_agent import ocr
    from app.store import set_repository
    from app.store.testing import fresh_repository

    # AN ISOLATED, EMPTY STORE (LOS_TEST_PG_DSN): the pipeline's best-effort store reads never reach the dev DB
    if not os.getenv("LOS_TEST_PG_DSN"):
        raise SystemExit("set LOS_TEST_PG_DSN to a test PostgreSQL (the benchmark never uses the dev store)")
    set_repository(fresh_repository())
    warm_ms = ocr.warmup_all()
    report = {"warmup_ms": round(warm_ms, 1), "runs": runs, "cpu_count": os.cpu_count(),
              "ocr_workers": ocr.get_ocr_executor()._max_workers,
              "document_workers": ocr.get_document_executor()._max_workers, "sets": {}}
    for name in wanted:
        spec = SETS[name]
        samples = []
        for r in range(runs):
            samples.append(await _one_run(spec, f"bench-{name}-{r}"))
        stage_keys = sorted({k for s in samples for k in s["processing"]})
        report["sets"][name] = {
            "documents": [k for _, k, _ in spec],
            "wall_ms": _stats([s["wall_ms"] for s in samples]),
            "cpu_ms": _stats([s["cpu_ms"] for s in samples]),
            "peak_rss_mb": max(s["rss_mb"] for s in samples),
            "stages_ms_median": {k: round(statistics.median([float(s["processing"].get(k) or 0) for s in samples]), 1)
                                 for k in stage_keys},
            "errors": [s["error"] for s in samples if s["error"]],
            "last": {"status": samples[-1]["status"], "documents": samples[-1]["documents"]},
        }
        line = report["sets"][name]
        print(f"{name:22s} docs={len(spec)} wall median={line['wall_ms'].get('median')} ms "
              f"(min {line['wall_ms'].get('min')}, max {line['wall_ms'].get('max')}) "
              f"cpu={line['cpu_ms'].get('median')} ms rss={line['peak_rss_mb']} MB "
              f"status={line['last']['status']} errors={len(line['errors'])}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("written", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default="runs/bench/documents.json")
    parser.add_argument("--sets", default=",".join(SETS))
    args = parser.parse_args()
    asyncio.run(main(args.runs, ROOT / args.out, [s for s in args.sets.split(",") if s]))
