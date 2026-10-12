"""
TRAIN THE MEANING ADAPTER (2026-10-12) -- a linear map on top of the FROZEN nomic-embed-text vectors, learned from
the intent catalogue's own examples, so LOS paraphrases of one intent sit closer together and different intents
further apart. numpy only (no torch, no GPU, no new model download).

    python scripts/train_meaning_adapter.py                 # train, select, write app/models/meaning_adapter.npy
    python scripts/train_meaning_adapter.py --evaluate      # baseline vs adapter on the held-out set E only

Method: prototype softmax (supervised, temperature tau) over the catalogue intents, W initialised to the identity and
regularised towards it (lambda * ||W - I||^2) so the cosine scale the decision thresholds were calibrated on is kept.
MODEL SELECTION uses leave-one-out k-NN accuracy on the TRAINING bank only; set E (evals/copilot/meaning_heldout_e.yaml)
is never seen before the final report.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "app" / "models" / "meaning_adapter.npy"
META = ROOT / "app" / "models" / "meaning_adapter.json"


def _bank():
    from app.agents.applicant.copilot.semantics import meaning
    from app.knowledge.retriever import _cached_embed_all

    embedder = meaning._embedder()
    if embedder is None:
        sys.exit("the embedding model (Ollama nomic-embed-text) is not reachable")
    intents = meaning.catalogue().get("intents") or {}
    extra = (meaning._load("intent_paraphrases.yaml").get("paraphrases") or {})
    pairs = [(n, str(e)) for n, spec in intents.items() for e in spec.get("examples") or []]
    pairs += [(n, str(e)) for n, items in extra.items() if n in intents for e in items or []]
    vectors = np.array(_cached_embed_all(embedder, [t for _, t in pairs]), dtype=np.float64)
    return embedder, [n for n, _ in pairs], _normalise(vectors)


def _normalise(x):
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-12)


def _loo_knn(z, labels):
    """Leave-one-out k-NN (max cosine to another example) accuracy -- how meaning.rank decides."""
    s = z @ z.T
    np.fill_diagonal(s, -9)
    lab = np.array(labels)
    return float(np.mean(lab[np.argmax(s, axis=1)] == lab))


def train(x, labels, *, lam, tau=0.05, epochs=300, lr=0.05, seed=0):
    classes = sorted(set(labels))
    idx = {c: i for i, c in enumerate(classes)}
    y = np.array([idx[c] for c in labels])
    d = x.shape[1]
    w = np.eye(d)
    m, v = np.zeros_like(w), np.zeros_like(w)
    for step in range(1, epochs + 1):
        z = _normalise(x @ w.T)
        protos = _normalise(np.stack([z[y == k].mean(axis=0) for k in range(len(classes))]))
        logits = z @ protos.T / tau
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        p[np.arange(len(y)), y] -= 1.0
        gz = p @ protos / tau / len(y)                          # d loss / d z (prototypes held fixed per step)
        u = x @ w.T                                             # through the normalisation
        n = np.linalg.norm(u, axis=1, keepdims=True) + 1e-12
        gu = (gz - (np.sum(gz * u, axis=1, keepdims=True) / n ** 2) * u) / n
        g = gu.T @ x + 2 * lam * (w - np.eye(d))
        m = 0.9 * m + 0.1 * g
        v = 0.999 * v + 0.001 * g * g
        w -= lr * (m / (1 - 0.9 ** step)) / (np.sqrt(v / (1 - 0.999 ** step)) + 1e-8)
    return w


def _topk(query, bank, labels, k=1):
    s = bank @ query
    best = {}
    for lab, sc in zip(labels, s):
        if sc > best.get(lab, -9):
            best[lab] = sc
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:k]


def evaluate(embedder, x, labels, w):
    import yaml

    items = yaml.safe_load((ROOT / "evals" / "copilot" / "meaning_heldout_e.yaml").read_text(encoding="utf-8"))["items"]
    q = _normalise(np.array([embedder.embed(i["q"]) for i in items], dtype=np.float64))
    out = {}
    for name, mat in (("baseline", None), ("adapter", w)):
        bank = x if mat is None else _normalise(x @ mat.T)
        qq = q if mat is None else _normalise(q @ mat.T)
        top1 = top3 = 0
        margins, rows = [], []
        for i, item in enumerate(items):
            ranked = _topk(qq[i], bank, labels, k=3)
            names = [n for n, _ in ranked]
            top1 += names[0] == item["intent"]
            top3 += item["intent"] in names
            margins.append(float(ranked[0][1] - ranked[1][1]))
            rows.append({"q": item["q"], "want": item["intent"], "got": names[0], "score": round(float(ranked[0][1]), 3)})
        out[name] = {"top1": f"{top1}/{len(items)}", "top3": f"{top3}/{len(items)}",
                     "median_margin": round(float(np.median(margins)), 3),
                     "misses": [r for r in rows if r["want"] != r["got"]]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()
    started = time.perf_counter()
    embedder, labels, x = _bank()
    print(f"bank: {len(labels)} examples, {len(set(labels))} intents, dim {x.shape[1]}")
    if args.evaluate:
        w = np.load(OUT)
        print(json.dumps(evaluate(embedder, x, labels, w), indent=1))
        return
    base = _loo_knn(x, labels)
    print(f"baseline leave-one-out k-NN: {base:.3f}")
    best = (base, None, None)
    for lam in (0.01, 0.003, 0.001):
        w = train(x, labels, lam=lam)
        # model selection on the TRAINING bank only: leave-one-out k-NN with the adapter applied
        score = _loo_knn(_normalise(x @ w.T), labels)
        drift = float(np.linalg.norm(w - np.eye(w.shape[0])) / np.sqrt(w.shape[0]))
        print(f"lambda {lam}: leave-one-out k-NN {score:.3f}, drift from identity {drift:.3f}")
        if score > best[0] + 0.005:
            best = (score, lam, w)
    if best[2] is None:
        print("no adapter beat the baseline on leave-one-out: nothing written")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.save(OUT, best[2].astype(np.float32))
    report = {"trained": time.strftime("%Y-%m-%d %H:%M"), "embedding_model": "nomic-embed-text",
              "examples": len(labels), "intents": len(set(labels)), "lambda": best[1],
              "loo_knn_baseline": round(base, 3), "loo_knn_adapter": round(best[0], 3),
              "heldout_e": evaluate(embedder, x, labels, best[2]),
              "seconds": round(time.perf_counter() - started, 1)}
    META.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "heldout_e"}, indent=1))
    print("HELD-OUT E:", json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "misses"}
                                     for k, v in report["heldout_e"].items()}))


if __name__ == "__main__":
    main()
