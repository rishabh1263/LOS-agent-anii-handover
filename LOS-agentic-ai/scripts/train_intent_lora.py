"""
ROUTE 2 (2026-10-12): LoRA on a small Qwen (Qwen2.5-0.5B-Instruct) with a sequence-classification head, trained on
the intent catalogue examples + the paraphrase bank, CPU only. Runs in the separate training environment
(requirements-train.txt), never in the app's own environment.

    <venv-train>/python scripts/train_intent_lora.py --export-data runs/intent_train.json   (app venv: dumps the data)
    <venv-train>/python scripts/train_intent_lora.py --train runs/intent_train.json        (trains + evaluates E and F)

Model selection: the epoch with the best accuracy on the development set E; set F is reported once, at the end.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "Qwen/Qwen2.5-0.5B-Instruct"
OUT = ROOT / "app" / "models" / "intent_lora"


def export(path: str) -> None:
    sys.path.insert(0, str(ROOT))
    import yaml

    from app.agents.applicant.copilot.semantics import meaning

    intents = meaning.catalogue().get("intents") or {}
    extra = (meaning._load("intent_paraphrases.yaml").get("paraphrases") or {})
    rows = [{"text": str(e), "label": n} for n, s in intents.items() for e in s.get("examples") or []]
    rows += [{"text": str(e), "label": n} for n, items in extra.items() if n in intents for e in items or []]
    sets = {k: yaml.safe_load((ROOT / "evals" / "copilot" / f"meaning_heldout_{k}.yaml").read_text(encoding="utf-8"))["items"]
            for k in ("e", "f")}
    Path(path).write_text(json.dumps({"train": rows, "labels": sorted(intents), "e": sets["e"], "f": sets["f"]},
                                     ensure_ascii=False), encoding="utf-8")
    print(f"exported {len(rows)} training rows, {len(intents)} labels")


def train(path: str, epochs: int = 4, lr: float = 3e-4, batch: int = 16) -> None:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(0)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    labels = data["labels"]
    lid = {n: i for i, n in enumerate(labels)}
    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForSequenceClassification.from_pretrained(BASE, num_labels=len(labels), torch_dtype=torch.float32)
    model.config.pad_token_id = tok.pad_token_id
    model = get_peft_model(model, LoraConfig(task_type="SEQ_CLS", r=16, lora_alpha=32, lora_dropout=0.05,
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
    model.print_trainable_parameters()
    train_rows = data["train"]
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr)

    def predict(items):
        model.eval()
        out = []
        with torch.no_grad():
            for i in range(0, len(items), 32):
                enc = tok([x["q"] for x in items[i:i + 32]], padding=True, truncation=True, max_length=48,
                          return_tensors="pt")
                probs = torch.softmax(model(**enc).logits, dim=-1)
                for p in probs:
                    k = int(p.argmax())
                    out.append((labels[k], float(p[k])))
        return out

    def accuracy(items):
        pred = predict(items)
        return sum(p == x["intent"] for (p, _), x in zip(pred, items)), pred

    best = (-1, None)
    started = time.perf_counter()
    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(len(train_rows)).tolist()
        total = 0.0
        for i in range(0, len(order), batch):
            rows = [train_rows[j] for j in order[i:i + batch]]
            enc = tok([r["text"] for r in rows], padding=True, truncation=True, max_length=48, return_tensors="pt")
            y = torch.tensor([lid[r["label"]] for r in rows])
            loss = model(**enc, labels=y).loss
            loss.backward()
            opt.step()
            opt.zero_grad()
            total += float(loss) * len(rows)
        dev, _ = accuracy(data["e"])
        print(f"epoch {epoch}: loss {total / len(train_rows):.3f}  dev E {dev}/{len(data['e'])}  "
              f"({time.perf_counter() - started:.0f}s)", flush=True)
        if dev > best[0]:
            best = (dev, epoch)
            OUT.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(OUT)
            tok.save_pretrained(OUT)
            (OUT / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    # FINAL: reload the selected epoch and report set F once
    from peft import PeftModel

    base = AutoModelForSequenceClassification.from_pretrained(BASE, num_labels=len(labels), torch_dtype=torch.float32)
    base.config.pad_token_id = tok.pad_token_id
    model = PeftModel.from_pretrained(base, OUT)
    f_ok, f_pred = accuracy(data["f"])
    t0 = time.perf_counter()
    predict(data["f"][:20])
    per_q = (time.perf_counter() - t0) / 20 * 1000
    report = {"base": BASE, "selected_epoch": best[1], "dev_e": f"{best[0]}/{len(data['e'])}",
              "heldout_f": f"{f_ok}/{len(data['f'])}", "ms_per_question_cpu": round(per_q, 1),
              "f_misses": [{"q": x["q"], "want": x["intent"], "got": p, "p": round(c, 2)}
                           for (p, c), x in zip(f_pred, data["f"]) if p != x["intent"]],
              "seconds": round(time.perf_counter() - started, 1)}
    (OUT / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print("ROUTE2 " + json.dumps({k: v for k, v in report.items() if k != "f_misses"}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-data")
    ap.add_argument("--train")
    ap.add_argument("--epochs", type=int, default=4)
    a = ap.parse_args()
    if a.export_data:
        export(a.export_data)
    elif a.train:
        train(a.train, epochs=a.epochs)
