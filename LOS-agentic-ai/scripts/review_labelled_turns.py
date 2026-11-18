"""
REVIEW THE LABELLED TURNS (2026-10-14) -- the turn log's "asked back, then understood" pairs, before any model
learns from them (runtime/labelled_turns.jsonl; observability/turn_log.py).

    python scripts/review_labelled_turns.py                 # list the unreviewed pairs, numbered
    python scripts/review_labelled_turns.py --approve 1,3   # mark these reviewed (they will be trained on)
    python scripts/review_labelled_turns.py --reject 2      # drop these (a wrong pair)
    python scripts/review_labelled_turns.py --relabel 4=pending_documents   # correct the intent, and approve it

Then retrain: python scripts/train_meaning_classifier.py  (it reports the frozen held-out scores before you switch).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "runtime" / "labelled_turns.jsonl"


def _rows() -> list[dict]:
    if not PATH.exists():
        return []
    return [json.loads(line) for line in PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _numbers(text: str) -> set[int]:
    return {int(x) for x in str(text or "").split(",") if x.strip().isdigit()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="")
    ap.add_argument("--reject", default="")
    ap.add_argument("--relabel", action="append", default=[], help="N=intent")
    a = ap.parse_args()
    rows = _rows()
    pending = [i for i, r in enumerate(rows) if r.get("reviewed") is False]
    if not (a.approve or a.reject or a.relabel):
        if not pending:
            print("nothing to review")
        for n, i in enumerate(pending, 1):
            print(f"{n:3d}. {rows[i]['q']!r:60s} -> {rows[i]['intent']}")
        return
    pick = {n: pending[n - 1] for n in range(1, len(pending) + 1)}
    for spec in a.relabel:
        n, _, intent = spec.partition("=")
        if n.strip().isdigit() and int(n) in pick and intent.strip():
            rows[pick[int(n)]].update(intent=intent.strip(), reviewed=True)
    for n in _numbers(a.approve):
        if n in pick:
            rows[pick[n]]["reviewed"] = True
    drop = {pick[n] for n in _numbers(a.reject) if n in pick}
    kept = [r for i, r in enumerate(rows) if i not in drop]
    PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    print(f"reviewed now: {sum(1 for r in kept if r.get('reviewed'))}, still to review: "
          f"{sum(1 for r in kept if r.get('reviewed') is False)}, dropped: {len(drop)}")


if __name__ == "__main__":
    sys.exit(main())
