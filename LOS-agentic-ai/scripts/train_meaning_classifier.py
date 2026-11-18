"""
THE MEANING CLASSIFIER (2026-10-14) -- a CPU-trained tool / intent classifier on top of the embeddings the meaning
layer already uses (nomic-embed + the LOS adapter). Multinomial logistic regression in numpy: trains in seconds, answers
in microseconds, no new dependency.

    python scripts/train_meaning_classifier.py            # train on the bank (+ labelled logs), evaluate, save
    python scripts/train_meaning_classifier.py --evaluate # evaluate the saved model only

TRAINING DATA: the intent catalogue's examples + reviewed paraphrases (the bank meaning.rank searches), plus labelled
officer turns in runtime/labelled_turns.jsonl ({"q": ..., "intent": ...} per line) when present -- real usage is how
it gets better. NEVER the frozen held-out sets: evals/copilot/meaning_heldout_e.yaml and _f.yaml are only MEASURED,
comparing three readers on them: today's nearest-example ranking (k-NN), the classifier, and their blend.

Saves models/meaning_classifier.npz (weights, intents, the bank key it was trained on). The meaning layer uses it only
when config `decide.classifier.enabled` is true -- switch it on only when the report shows it winning on E and F.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL = ROOT / "models" / "meaning_classifier.npz"
LABELLED = ROOT / "runtime" / "labelled_turns.jsonl"
HELDOUT = [ROOT / "evals" / "copilot" / f"meaning_heldout_{s}.yaml" for s in ("e", "f", "g", "h")]


def _embedder():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from app.agents.applicant.copilot.semantics import meaning

    embedder = meaning._embedder()
    if embedder is None:
        raise SystemExit("the embedding model is not reachable (Ollama nomic-embed-text)")
    return meaning, embedder


def _training(meaning, embedder) -> tuple[np.ndarray, list[str], int, int]:
    intents, vectors, texts = meaning._bank(embedder)
    held = {str(i["q"]).strip().lower() for p in HELDOUT for i in yaml.safe_load(p.read_text(encoding="utf-8"))["items"]}
    keep = [i for i, t in enumerate(texts) if str(t).strip().lower() not in held]   # a bank row equal to a held-out
    leaked = len(texts) - len(keep)                                                 # sentence is left out, counted
    x, y = [list(vectors[i]) for i in keep], [intents[i] for i in keep]
    extra = 0
    if LABELLED.exists():
        for line in LABELLED.read_text(encoding="utf-8").splitlines():
            row = json.loads(line) if line.strip() else None
            if not row or not row.get("intent") or str(row.get("q", "")).strip().lower() in held:
                continue                                    # a held-out sentence is never trained on
            if row.get("reviewed") is False:
                continue                                    # turn-log pairs only after a person approved them
            x.append(meaning._adapted(embedder.embed(meaning._clean(row["q"]))))
            y.append(str(row["intent"]))
            extra += 1
    return np.asarray(x, dtype=np.float64), y, extra, leaked


def train(x: np.ndarray, y: list[str], *, lam: float = 1e-3, epochs: int = 600, lr: float = 0.05,
          temperature: float = 0.05) -> dict:
    """Softmax regression on unit vectors (cosine logits / temperature), class-balanced, L2, Adam."""
    classes = sorted(set(y))
    idx = np.array([classes.index(c) for c in y])
    n, d, k = x.shape[0], x.shape[1], len(classes)
    rng = np.random.default_rng(0)
    w = rng.normal(0, 0.01, (d, k))
    b = np.zeros(k)
    counts = np.bincount(idx, minlength=k)
    weight = (n / (k * counts))[idx]                         # every intent counts equally, however many examples
    onehot = np.eye(k)[idx]
    m = {"w": np.zeros_like(w), "b": np.zeros_like(b)}
    v = {"w": np.zeros_like(w), "b": np.zeros_like(b)}
    for t in range(1, epochs + 1):
        logits = (x @ w + b) / temperature
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        g = (p - onehot) * weight[:, None] / (n * temperature)
        grads = {"w": x.T @ g + lam * w, "b": g.sum(axis=0)}
        for key, param in (("w", w), ("b", b)):
            m[key] = 0.9 * m[key] + 0.1 * grads[key]
            v[key] = 0.999 * v[key] + 0.001 * grads[key] ** 2
            param -= lr * (m[key] / (1 - 0.9 ** t)) / (np.sqrt(v[key] / (1 - 0.999 ** t)) + 1e-8)
    return {"w": w, "b": b, "classes": classes, "temperature": temperature}


def proba(model: dict, x: np.ndarray) -> np.ndarray:
    logits = (x @ model["w"] + model["b"]) / model["temperature"]
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    return p / p.sum(axis=1, keepdims=True)


def evaluate(meaning, embedder, model: dict) -> dict:
    """Top-1 accuracy on each frozen held-out set: k-NN (today), classifier, blend."""
    report = {}
    classes = model["classes"]
    for path in HELDOUT:
        items = yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
        knn_ok = clf_ok = blend_ok = 0
        for item in items:
            ranked = meaning.rank(item["q"], embedder)
            knn = ranked[0][0] if ranked else None
            q = np.asarray([meaning._adapted(embedder.embed(meaning._clean(item["q"])))])
            p = proba(model, q)[0]
            clf = classes[int(p.argmax())]
            cos = dict(ranked)
            blend = max(classes, key=lambda c: cos.get(c, 0.0) + 0.1 * float(np.log(p[classes.index(c)] + 1e-9)))
            knn_ok += knn == item["intent"]
            clf_ok += clf == item["intent"]
            blend_ok += blend == item["intent"]
        report[path.stem] = {"n": len(items), "knn_today": round(knn_ok / len(items), 3),
                             "classifier": round(clf_ok / len(items), 3), "blend": round(blend_ok / len(items), 3)}
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", action="store_true", help="evaluate the saved model only")
    a = ap.parse_args()
    meaning, embedder = _embedder()
    if a.evaluate:
        data = np.load(MODEL, allow_pickle=False)
        model = {"w": data["w"], "b": data["b"], "classes": [str(c) for c in data["classes"]],
                 "temperature": float(data["temperature"])}
    else:
        x, y, extra, leaked = _training(meaning, embedder)
        model = train(x, y)
        train_acc = float((np.array(model["classes"])[proba(model, x).argmax(axis=1)] == np.array(y)).mean())
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        np.savez(MODEL, w=model["w"], b=model["b"], classes=np.array(model["classes"]),
                 temperature=model["temperature"])
        print(json.dumps({"trained_on": len(y), "from_labelled_logs": extra, "held_out_rows_skipped": leaked, "intents": len(model["classes"]),
                          "train_accuracy": round(train_acc, 3), "saved": str(MODEL.relative_to(ROOT))}))
    print(json.dumps(evaluate(meaning, embedder, model), indent=1))


if __name__ == "__main__":
    main()
