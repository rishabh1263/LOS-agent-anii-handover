"""
Calibrate the meaning thresholds (app/config/intent_catalogue.yaml `decide`) by LEAVE-ONE-OUT over the catalogue examples
+ reviewed paraphrases -- never on the held-out sets. For each example: remove it, rank the rest (k-NN max cosine, the
runtime rule), and record (top score, margin, correct?). Prints precision / coverage per threshold so `accept` can be
set where a confident pick is right >= the target precision.

    python scripts/calibrate_meaning.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOST = (os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 64):
        req = urllib.request.Request(HOST + "/api/embed", data=json.dumps(
            {"model": "nomic-embed-text", "input": texts[i:i + 64]}).encode(), headers={"Content-Type": "application/json"})
        out += json.loads(urllib.request.urlopen(req, timeout=600).read())["embeddings"]
    return out


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def main() -> int:
    cat = yaml.safe_load((ROOT / "app/config/intent_catalogue.yaml").read_text(encoding="utf-8"))["intents"]
    para = (yaml.safe_load((ROOT / "app/config/intent_paraphrases.yaml").read_text(encoding="utf-8")) or {}) \
        .get("paraphrases", {}) if (ROOT / "app/config/intent_paraphrases.yaml").exists() else {}
    pairs = [(n, str(e)) for n, s in cat.items() for e in s.get("examples") or []]
    pairs += [(n, str(e)) for n, items in para.items() if n in cat for e in items]
    vecs = [norm(v) for v in embed([t for _, t in pairs])]
    rows = []
    for i, (name, _) in enumerate(pairs):
        best: dict[str, float] = {}
        for j, (other, _) in enumerate(pairs):
            if i == j:
                continue
            s = sum(a * b for a, b in zip(vecs[i], vecs[j]))
            if s > best.get(other, -1):
                best[other] = s
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        rows.append((ranked[0][1], ranked[0][1] - ranked[1][1], ranked[0][0] == name, name, ranked[0][0]))
    print(f"{len(pairs)} examples over {len(cat)} intents; overall top-1 {sum(r[2] for r in rows) / len(rows):.3f}")
    for acc in (0.70, 0.74, 0.78, 0.80, 0.82, 0.85, 0.88):
        for margin in (0.0, 0.02, 0.04, 0.06):
            picked = [r for r in rows if r[0] >= acc and r[1] >= margin]
            if picked:
                prec = sum(r[2] for r in picked) / len(picked)
                print(f"accept {acc:.2f} margin {margin:.2f}: coverage {len(picked) / len(rows):.2f} precision {prec:.3f}")
    confusions: dict[tuple[str, str], int] = {}
    for _, _, ok, n, got in rows:
        if not ok:
            confusions[(n, got)] = confusions.get((n, got), 0) + 1
    print("top confusions:", sorted(confusions.items(), key=lambda kv: -kv[1])[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
