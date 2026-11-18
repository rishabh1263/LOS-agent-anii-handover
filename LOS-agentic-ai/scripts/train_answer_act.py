"""
TRAIN THE ANSWER-ACT MODEL (2026-11-04; semantics/answer_act.py).

    python scripts/train_answer_act.py              # train on copilot_answer_acts.yaml, measure, save
    python scripts/train_answer_act.py --evaluate   # measure the saved model only

Softmax regression in numpy (no new dependency) on [sentence embedding | hashed n-grams]. The feature weights and the
regulariser are CHOSEN BY 5-FOLD CROSS-VALIDATION ON THE TRAINING BANK ONLY; the frozen held-out set
evals/copilot/answer_act_heldout.yaml is only measured (any training row equal to a held-out sentence is dropped and
counted). Reported: held-out accuracy for n-grams alone, embedding alone and both, by act and by style.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HELDOUT = ROOT / "evals" / "copilot" / "answer_act_heldout.yaml"
#: --model: which learned text model -- (module, held-out file, label key in the held-out items)
MODELS = {"answer_act": ("answer_act", "answer_act_heldout.yaml", "act"),
          "tone": ("tone", "tone_heldout.yaml", "tone")}
LABEL = "act"


def _setup(which: str = "answer_act"):
    global HELDOUT, LABEL
    from dotenv import load_dotenv
    import importlib

    load_dotenv(ROOT / ".env")
    module, heldout, LABEL = MODELS[which]
    HELDOUT = ROOT / "evals" / "copilot" / heldout
    mod = importlib.import_module(f"app.agents.applicant.copilot.semantics.{module}")

    embedder = mod._embedder()
    if embedder is None:
        raise SystemExit("the embedding model is not reachable (Ollama nomic-embed-text)")
    return mod, embedder


def train(x: np.ndarray, y: list[str], classes: list[str], *, lam: float, epochs: int = 500, lr: float = 0.05,
          temperature: float = 0.1):
    idx = np.array([classes.index(c) for c in y])
    n, d, k = x.shape[0], x.shape[1], len(classes)
    rng = np.random.default_rng(0)
    w, b = rng.normal(0, 0.01, (d, k)), np.zeros(k)
    counts = np.bincount(idx, minlength=k).astype(float)
    weight = (n / (k * np.maximum(counts, 1)))[idx]
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
    return w, b


def predict(x, w, b, temperature=0.1):
    logits = (x @ w + b) / temperature
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    return p / p.sum(axis=1, keepdims=True)


def build(aa, texts, embs, dims, emb_w, ng_w, use_emb=True, use_ng=True):
    rows = []
    for t, e in zip(texts, embs):
        part = []
        if use_emb:
            part += [z * emb_w for z in e]
        if use_ng:
            part += [z * ng_w for z in aa.ngram_vector(t, dims)]
        rows.append(part)
    return np.asarray(rows)


def cv_score(x, y, classes, lam, folds=5):
    rng = np.random.default_rng(1)
    order = rng.permutation(len(y))
    ok = 0
    for f in range(folds):
        test = order[f::folds]
        train_i = np.setdiff1d(order, test)
        w, b = train(x[train_i], [y[i] for i in train_i], classes, lam=lam, epochs=300)
        p = predict(x[test], w, b)
        ok += sum(classes[int(j)] == y[i] for j, i in zip(p.argmax(axis=1), test))
    return ok / len(y)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--model", default="answer_act", choices=sorted(MODELS))
    a = ap.parse_args()
    aa, embedder = _setup(a.model)
    spec = aa._cfg().get("model") or {}
    dims = int(spec.get("hash_dims", 4096))
    path = ROOT / str(spec.get("path") or "models/answer_act.npz")

    held = yaml.safe_load(HELDOUT.read_text(encoding="utf-8"))["items"]
    held_set = {aa.clean(i["q"]) for i in held}
    pairs = aa.examples()
    train_pairs = [(t, c) for t, c in pairs if aa.clean(t) not in held_set]
    leaked = len(pairs) - len(train_pairs)
    texts, y = [t for t, _ in train_pairs], [c for _, c in train_pairs]
    classes = sorted(set(y))
    embs = [aa.embed(t, embedder) for t in texts]
    h_texts, h_y = [i["q"] for i in held], [i[LABEL] for i in held]
    h_embs = [aa.embed(t, embedder) for t in h_texts]
    emb_size = len(embs[0])

    if a.evaluate:
        d = np.load(path, allow_pickle=False)
        chosen = {"emb_w": float(d["emb_w"]), "ng_w": float(d["ng_w"]), "lam": None}
        wf, bf = d["w_full"], d["b_full"]
        classes = [str(c) for c in d["classes"]]
    else:
        # choose the feature weights + regulariser on the TRAINING bank only (5-fold CV)
        best = None
        for emb_w, ng_w in ((1.0, 0.0), (0.0, 1.0), (1.0, 0.4), (1.0, 0.6), (1.0, 1.0)):
            x = build(aa, texts, embs, dims, emb_w, ng_w, use_emb=emb_w > 0, use_ng=ng_w > 0)
            for lam in (1e-4, 1e-3, 1e-2):
                s = cv_score(x, y, classes, lam)
                print(json.dumps({"cv": round(s, 3), "emb_w": emb_w, "ng_w": ng_w, "lam": lam}))
                if best is None or s > best[0]:
                    best = (s, emb_w, ng_w, lam)
        _, emb_w, ng_w, lam = best
        chosen = {"emb_w": emb_w, "ng_w": ng_w, "lam": lam, "cv": round(best[0], 3)}
        # the full model always carries both parts so the n-gram fallback exists; a 0 weight is nudged to keep shape
        ew, nw = (emb_w or 1e-6), (ng_w or 0.6)
        xf = build(aa, texts, embs, dims, ew, nw)
        wf, bf = train(xf, y, classes, lam=lam)
        xn = build(aa, texts, embs, dims, ew, nw, use_emb=False)
        wn, bn = train(xn, y, classes, lam=lam)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, w_full=wf, b_full=bf, w_ngram=wn, b_ngram=bn, classes=np.array(classes), temperature=0.1,
                 dims=dims, emb_w=ew, ng_w=nw, emb_size=emb_size)
        chosen.update(emb_w=ew, ng_w=nw)
        print(json.dumps({"trained_on": len(y), "held_out_rows_dropped": leaked, "acts": len(classes),
                          "chosen": chosen, "saved": str(path.relative_to(ROOT))}))

    # ---- measure on the frozen held-out set
    def report(name, x, w, b):
        p = predict(x, w, b)
        pred = [classes[int(j)] for j in p.argmax(axis=1)]
        conf = p.max(axis=1)
        by_act, by_style = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
        for item, pr in zip(held, pred):
            for bucket, key in ((by_act, item[LABEL]), (by_style, item.get("style", "?"))):
                bucket[key][0] += pr == item[LABEL]
                bucket[key][1] += 1
        acc = sum(pr == t for pr, t in zip(pred, h_y)) / len(h_y)
        thr = float(spec.get("min_confidence", 0.55))
        sure = [(pr, t) for pr, t, c in zip(pred, h_y, conf) if c >= thr]
        out = {"model": name, "accuracy": round(acc, 3), "n": len(h_y),
               "sure_share": round(len(sure) / len(h_y), 3),
               "sure_accuracy": round(sum(p == t for p, t in sure) / max(len(sure), 1), 3),
               "by_act": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_act.items())},
               "by_style": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_style.items())}}
        print(json.dumps(out, ensure_ascii=False))
        return pred, conf

    ew, nw = chosen["emb_w"], chosen["ng_w"]
    pred, conf = report("embedding+ngrams (saved)", build(aa, h_texts, h_embs, dims, ew, nw), wf, bf)
    if not a.evaluate:
        for name, ue, un in (("ngrams only", False, True), ("embedding only", True, False)):
            xt = build(aa, texts, embs, dims, 1.0, 1.0, use_emb=ue, use_ng=un)
            w, b = train(xt, y, classes, lam=chosen["lam"])
            report(name, build(aa, h_texts, h_embs, dims, 1.0, 1.0, use_emb=ue, use_ng=un), w, b)
    misses = [{"q": i["q"], "want": i[LABEL], "got": p, "p": round(float(c), 2)}
              for i, p, c in zip(held, pred, conf) if p != i[LABEL]]
    print(json.dumps({"misses": misses}, ensure_ascii=False, indent=0))


if __name__ == "__main__":
    main()
