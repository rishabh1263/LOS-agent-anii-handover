"""
A calibration harness for the document quality thresholds.

WHY THIS EXISTS. Every threshold in `app/agents/document_agent/quality.py`
is calibrated against 19 readable sample images and a synthetic Gaussian
blur gradient. That is enough to be confident no GOOD document is
flagged — every threshold sits below the corpus minimum with margin — and
it says nothing at all about where a BAD one starts. The true-positive
point of every metric is currently unknown.

This harness does not guess it. It measures a labelled real-world corpus
and prints the distribution of each metric within each defect class, so
the point where extraction actually starts failing can be READ off data
instead of chosen.

IT CHANGES NOTHING. It imports the analyser and reads its `metrics`. It
does not write configuration, it does not touch a threshold, and it has
no side effect on the service. The numbers in `quality.py` must not move
until a run of this harness justifies each one.

WHAT TO COLLECT. Roughly 300-500 real phone photographs of Indian ID
documents. Per class, at least: 50 genuinely blurred, 50 low-contrast or
faded, 50 under-exposed, 50 over-exposed, 50 with laminate glare, 30
skewed beyond 5 degrees, 30 truncated at a known edge, 30 low-resolution,
and 100 good controls spanning devices and lighting.

THE OCR LABEL IS THE POINT. A defect label alone cannot calibrate a
threshold: it says an image is blurred, not whether the blur mattered.
`extracted` — did the document's required fields actually come out —
is what turns a distribution into a threshold. WARNING belongs where
extraction starts falling; SEVERE where it has effectively stopped.

USAGE

    python tools/quality_calibration.py --corpus <dir> [--json out.json]

The corpus is either a directory per label:

    corpus/good/*.jpg
    corpus/blur/*.jpg
    corpus/glare/*.jpg

or a manifest, which also carries the OCR outcome:

    corpus/manifest.csv
        path,label,extracted
        images/a.jpg,blur,false
        images/b.jpg,good,true

`extracted` is optional per row; rows without it are counted under
"unlabelled" and excluded from the success rates.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The defect classes worth separating. `good` is the control.
LABELS = (
    "good", "blur", "low_contrast", "under_exposed", "over_exposed",
    "glare", "skew", "crop", "low_resolution",
)

#: Every metric the analyser records, in the order a reader wants them.
METRICS = ("focus", "resolution", "contrast", "exposure", "glare",
           "skew", "crop", "noise")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


class Sample(NamedTuple):
    """One labelled image."""

    path: Path
    label: str
    #: Did the required fields extract? None when the corpus did not say.
    extracted: bool | None


class Measurement(NamedTuple):
    sample: Sample
    metrics: dict[str, float]
    #: (reason_code, severity) pairs. Severity matters: an INFO finding
    #: stays out of `reason_codes` and out of the response, so counting
    #: it beside a SEVERE would overstate what the thresholds do today.
    findings: tuple[tuple[str, str], ...]
    worst: str
    analysed: bool


# ==========================================================================
# READING A CORPUS
# ==========================================================================


def _as_bool(text: str) -> bool | None:
    value = (text or "").strip().lower()
    if value in {"true", "yes", "y", "1", "pass", "ok"}:
        return True
    if value in {"false", "no", "n", "0", "fail"}:
        return False
    return None


def from_manifest(manifest: Path) -> list[Sample]:
    """Samples from a CSV carrying path, label and the OCR outcome."""
    samples: list[Sample] = []

    with manifest.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("path") or "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = manifest.parent / path
            samples.append(Sample(
                path=path,
                label=(row.get("label") or "unlabelled").strip().lower(),
                extracted=_as_bool(row.get("extracted", "")),
            ))

    return samples


def from_directories(corpus: Path) -> list[Sample]:
    """
    Samples from a directory-per-label layout.

    No OCR outcome is available this way, so every threshold this corpus
    can justify is a false-positive bound only.
    """
    samples: list[Sample] = []

    for child in sorted(corpus.iterdir()):
        if not child.is_dir():
            continue
        for image in sorted(child.rglob("*")):
            if image.suffix.lower() in IMAGE_SUFFIXES:
                samples.append(Sample(path=image, label=child.name.lower(),
                                      extracted=None))

    return samples


def read_corpus(corpus: Path) -> list[Sample]:
    manifest = corpus / "manifest.csv"
    if manifest.exists():
        return from_manifest(manifest)
    return from_directories(corpus)


# ==========================================================================
# MEASURING
# ==========================================================================


def measure(samples: Iterable[Sample]) -> list[Measurement]:
    """
    Run the production analyser over every sample.

    THE PRODUCTION ANALYSER, not a copy of it. A harness that
    reimplements the metric calibrates something the service does not
    compute.
    """
    from PIL import Image

    from app.agents.document_agent import quality

    out: list[Measurement] = []

    for sample in samples:
        try:
            with Image.open(sample.path) as image:
                image.load()
                report = quality.analyse(image)
        except Exception as exc:  # pragma: no cover - corpus hygiene
            print(f"  ! could not read {sample.path}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        out.append(Measurement(
            sample=sample,
            metrics=dict(report.metrics),
            findings=tuple((f.reason_code, f.severity)
                           for f in report.findings),
            worst=report.worst,
            analysed=report.analysed,
        ))

    return out


# ==========================================================================
# REPORTING
# ==========================================================================


def distribution(values: list[float]) -> dict[str, float | int]:
    """count, min, p10, p25, median, p75, p90, max."""
    if not values:
        return {"count": 0}

    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        # Nearest-rank. With 30-50 samples per class an interpolated
        # percentile invents precision the corpus does not have.
        index = max(0, min(len(ordered) - 1,
                           int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    return {
        "count": len(ordered),
        "min": round(ordered[0], 4),
        "p10": round(percentile(0.10), 4),
        "p25": round(percentile(0.25), 4),
        "median": round(statistics.median(ordered), 4),
        "p75": round(percentile(0.75), 4),
        "p90": round(percentile(0.90), 4),
        "max": round(ordered[-1], 4),
    }


def extraction_rate(measurements: list[Measurement]) -> dict[str, Any]:
    """
    How often the required fields actually came out, where labelled.

    This is what turns a distribution into a threshold: the point where
    this rate starts falling is where WARNING belongs, and the point
    where it has effectively reached zero is where SEVERE belongs.
    """
    labelled = [m for m in measurements if m.sample.extracted is not None]
    succeeded = sum(1 for m in labelled if m.sample.extracted)

    return {
        "labelled": len(labelled),
        "unlabelled": len(measurements) - len(labelled),
        "extracted": succeeded,
        "failed": len(labelled) - succeeded,
        "rate": (round(succeeded / len(labelled), 4) if labelled else None),
    }


def report(measurements: list[Measurement]) -> dict[str, Any]:
    """The whole corpus, by label, by metric."""
    labels = sorted({m.sample.label for m in measurements})

    by_label: dict[str, Any] = {}
    for label in labels:
        group = [m for m in measurements if m.sample.label == label]

        by_label[label] = {
            "samples": len(group),
            "unanalysable": sum(1 for m in group if not m.analysed),
            "extraction": extraction_rate(group),
            "metrics": {
                metric: distribution(
                    [m.metrics[metric] for m in group if metric in m.metrics])
                for metric in METRICS
            },
            # What the CURRENT thresholds say about this class. On a
            # `good` corpus every one of these should be zero; on a
            # defect class, the count is the metric's recall as it
            # stands today.
            "current_findings": _finding_counts(group),
        }

    return {
        "totals": {
            "samples": len(measurements),
            "labels": labels,
            "missing_labels": [l for l in LABELS if l not in labels],
            "extraction": extraction_rate(measurements),
        },
        "by_label": by_label,
    }


def _finding_counts(group: list[Measurement]) -> dict[str, int]:
    """
    What the CURRENT thresholds flag on this class, by code AND severity.

    Severity is not decoration. An INFO finding is excluded from
    `reason_codes` and never reaches a caller, so counting it beside a
    SEVERE would read as a false positive the service does not actually
    make. On a `good` corpus the WARNING and SEVERE counts should be
    zero; the INFO counts may not be, and that is by design.
    """
    counts: dict[str, int] = {}
    for measurement in group:
        for code, severity in measurement.findings:
            key = f"{code}[{severity}]"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def render(summary: dict[str, Any]) -> str:
    """The same report, for a terminal."""
    lines: list[str] = []
    totals = summary["totals"]

    lines.append(f"{totals['samples']} images, "
                 f"{len(totals['labels'])} labels")
    if totals["missing_labels"]:
        lines.append(f"NOT REPRESENTED: {', '.join(totals['missing_labels'])}")

    overall = totals["extraction"]
    if overall["labelled"]:
        lines.append(f"extraction labelled on {overall['labelled']}, "
                     f"overall rate {overall['rate']}")
    else:
        lines.append("NO EXTRACTION LABELS -- this corpus can only bound "
                     "false positives, not locate a threshold.")

    header = f"  {'metric':<12}{'count':>6}{'min':>10}{'p10':>10}" \
             f"{'p25':>10}{'median':>10}{'p75':>10}{'p90':>10}{'max':>10}"

    for label, data in summary["by_label"].items():
        lines.append("")
        rate = data["extraction"]["rate"]
        lines.append(f"[{label}] {data['samples']} images"
                     + (f", extraction rate {rate}" if rate is not None else "")
                     + (f", {data['unanalysable']} unanalysable"
                        if data["unanalysable"] else ""))
        lines.append(header)
        for metric, dist in data["metrics"].items():
            if not dist.get("count"):
                continue
            lines.append(
                f"  {metric:<12}{dist['count']:>6}{dist['min']:>10}"
                f"{dist['p10']:>10}{dist['p25']:>10}{dist['median']:>10}"
                f"{dist['p75']:>10}{dist['p90']:>10}{dist['max']:>10}")
        if data["current_findings"]:
            lines.append("  flagged today: " + ", ".join(
                f"{code}={n}" for code, n in data["current_findings"].items()))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure a labelled image corpus against the document quality "
            "metrics. Reports distributions only -- it never changes a "
            "threshold."
        ),
    )
    parser.add_argument("--corpus", required=True, type=Path,
                        help="directory holding manifest.csv, or one "
                             "sub-directory per label")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the full report here")
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        parser.error(f"no such directory: {args.corpus}")

    samples = read_corpus(args.corpus)
    if not samples:
        parser.error(f"no labelled images found under {args.corpus}")

    summary = report(measure(samples))

    print(render(summary))

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nfull report written to {args.json}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
