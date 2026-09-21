"""
Accuracy and latency benchmark for the Document Agent.

Names are compared on a space-insensitive key because the OCR engine
inconsistently emits spaces inside names ("LAXMISANTOSHGUPTA"). Every other
field is compared exactly.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from app.agents.document_agent import extract_document
from app.agents.document_agent.normalize import name_key

SAMPLES = Path("samples/documents")
TRUTH = json.loads(Path("samples/ground_truth.json").read_text())
NAME_FIELDS = {"name", "father_name", "guardian_name"}
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def matches(field: str, expected, actual) -> bool:
    if expected is None:
        return actual is None
    if field in NAME_FIELDS:
        return name_key(str(actual)) == name_key(str(expected))
    if isinstance(expected, list):
        return sorted(map(str, actual or [])) == sorted(map(str, expected))
    return str(actual) == str(expected)


def main() -> None:
    totals = {"exact": 0, "missing": 0, "invalid": 0, "wrong": 0, "fields": 0}
    per_type: dict[str, dict] = {}
    failures: list[str] = []

    print(f"{'sample':<22}{'type':<18}{'status':<9}{'fields':<9}{'total_ms':>9}")
    print("-" * 70)

    for filename, truth in TRUTH.items():
        path = SAMPLES / filename
        if not path.exists():
            continue

        latencies, results = [], []
        for _ in range(RUNS):
            result = extract_document(str(path))
            latencies.append(result.processing.total_ms)
            results.append(result)
        result = results[-1]

        doc_type = truth["document_type"]
        bucket = per_type.setdefault(
            doc_type,
            {"exact": 0, "fields": 0, "lat": [], "ocr": [], "ext": [], "docs": 0, "ok_docs": 0},
        )
        bucket["docs"] += 1
        bucket["lat"].extend(latencies)
        bucket["ocr"].append(result.processing.ocr_ms)
        bucket["ext"].append(result.processing.extraction_ms + result.processing.validation_ms)

        correct = 0
        for field, expected in truth["fields"].items():
            totals["fields"] += 1
            bucket["fields"] += 1
            got = result.value(field)
            entry = result.fields.get(field)

            if matches(field, expected, got):
                totals["exact"] += 1
                bucket["exact"] += 1
                correct += 1
            elif got is None:
                totals["missing"] += 1
                failures.append(f"{filename}:{field} MISSING (expected {expected!r})")
            elif entry and entry.validation.value == "INVALID":
                totals["invalid"] += 1
                failures.append(f"{filename}:{field} INVALID {got!r}")
            else:
                totals["wrong"] += 1
                failures.append(f"{filename}:{field} WRONG {got!r} != {expected!r}")

        if correct == len(truth["fields"]):
            bucket["ok_docs"] += 1

        print(
            f"{filename:<22}{result.document_type.value:<18}{result.status.value:<9}"
            f"{correct}/{len(truth['fields']):<7}{statistics.mean(latencies):>9.0f}"
        )

    def pct(a, b):
        return 100.0 * a / b if b else 0.0

    print("\n" + "=" * 70)
    print("ACCURACY")
    print("=" * 70)
    print(f"  Field accuracy      : {pct(totals['exact'], totals['fields']):.1f}%  "
          f"({totals['exact']}/{totals['fields']})")
    print(f"  Missing field rate  : {pct(totals['missing'], totals['fields']):.1f}%")
    print(f"  Invalid field rate  : {pct(totals['invalid'], totals['fields']):.1f}%")
    print(f"  Wrong-value rate    : {pct(totals['wrong'], totals['fields']):.1f}%")

    print("\n" + "=" * 70)
    print(f"BY DOCUMENT TYPE   (runs per sample: {RUNS})")
    print("=" * 70)
    for doc_type, b in per_type.items():
        lat = sorted(b["lat"])
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        print(f"\n  {doc_type}")
        print(f"    samples tested    : {b['docs']}")
        print(f"    field accuracy    : {pct(b['exact'], b['fields']):.1f}% ({b['exact']}/{b['fields']})")
        print(f"    all-fields-correct: {b['ok_docs']}/{b['docs']} documents")
        print(f"    avg latency       : {statistics.mean(lat):.0f} ms")
        print(f"    p95 latency       : {p95:.0f} ms")
        print(f"    avg OCR           : {statistics.mean(b['ocr']):.0f} ms")
        print(f"    avg extract+valid : {statistics.mean(b['ext']):.2f} ms")

    print("\n" + "=" * 70)
    print("FAILURES")
    print("=" * 70)
    if failures:
        for f in failures:
            print("  " + f)
    else:
        print("  none")


if __name__ == "__main__":
    main()
