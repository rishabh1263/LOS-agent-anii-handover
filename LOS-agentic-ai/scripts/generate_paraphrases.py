"""
Offline: English paraphrases per intent (Smart Bot plan section 4) -> app/config/intent_paraphrases.yaml.

    python scripts/generate_paraphrases.py            # Qwen (OLLAMA_HOST, qwen2.5:3b), ~40 per intent

Then REVIEWED automatically before they are kept:
  1. mechanical: 2..14 words, no ids / long numbers, de-duplicated, not already a seed example;
  2. MEANING: embedded with nomic-embed-text; a paraphrase is kept only when its nearest intent (by the SEED examples'
     centroids) is its own intent and it is closer to its own than to the next one by a margin -- a paraphrase that
     drifted to another meaning is dropped (and counted).
Nothing here is used at runtime except the YAML it writes; the catalogue's seed examples always stay.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOST = (os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
MODEL = os.getenv("PARAPHRASE_MODEL", "qwen2.5:3b")
PER_INTENT = int(os.getenv("PARAPHRASES_PER_INTENT", "40"))
SKIP_KINDS = {"command"}            # commands stay rule-handled; their examples are enough to recognise them


def post(path: str, body: dict, timeout: float = 600) -> dict:
    """POST to Ollama, retrying a transient 5xx / timeout up to 3 times (the run resumes from runs/paraphrases_raw.json)."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(HOST + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as exc:  # noqa: BLE001 - transient model-server errors
            if attempt == 2:
                raise
            print(f"retry after {type(exc).__name__}", flush=True)
            time.sleep(5 * (attempt + 1))
    return {}


def generate(name: str, spec: dict) -> list[str]:
    """Two calls of 20 (a cut-off JSON list is salvaged quote by quote)."""
    out: list[str] = []
    for style in ("short and informal, some with typos or missing words", "indirect or polite, some as half sentences"):
        prompt = (f"You write test data for a loan-application chat assistant used by field officers at an Indian lender.\n"
                  f"The officer wants to ASK the assistant: {spec['description']}\n"
                  f"Example questions: {'; '.join(spec['examples'][:6])}\n"
                  f"Write 20 more DIFFERENT messages the officer would TYPE TO ASK this ({style}). They are questions or "
                  "requests to the assistant, never answers or statements. No names, ids or amounts. "
                  "Reply only JSON {\"p\": [\"...\"]}.")
        body = post("/api/chat", {"model": MODEL, "stream": False, "format": "json", "keep_alive": "10m",
                                  "options": {"temperature": 0.9, "num_predict": 700, "num_ctx": 2048},
                                  "messages": [{"role": "user", "content": prompt}]})
        raw = body["message"]["content"]
        try:
            items = json.loads(raw).get("p") or []
        except ValueError:
            items = re.findall(r'"([^"\\]{3,120})"', raw)
            items = [x for x in items if x != "p"]
        out += [str(x).strip() for x in items if isinstance(x, str)]
    return out


def clean(text: str, seeds: set[str]) -> str | None:
    t = re.sub(r"\s+", " ", text).strip().strip('"').rstrip()
    words = t.split()
    if not 2 <= len(words) <= 14 or re.search(r"\d{4,}|CASE-|APP-|COAPP-", t) or t.lower() in seeds:
        return None
    return t


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 64):
        out += post("/api/embed", {"model": "nomic-embed-text", "input": [str(t) for t in texts[i:i + 64]],
                                    "keep_alive": "10m"})["embeddings"]
    return out


def norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def main() -> int:
    catalogue = yaml.safe_load((ROOT / "app/config/intent_catalogue.yaml").read_text(encoding="utf-8"))["intents"]
    names = [n for n, s in catalogue.items() if s.get("kind") not in SKIP_KINDS]
    seeds_all = {n: [str(e) for e in catalogue[n]["examples"]] for n in catalogue}
    raw_path = ROOT / "runs" / "paraphrases_raw.json"
    raw: dict[str, list[str]] = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    for n in names:
        if n in raw:
            continue                     # already generated (a rerun only redoes the review)
        started = time.time()
        seeds = {e.lower() for e in seeds_all[n]}
        got = list(dict.fromkeys(c for c in (clean(x, seeds) for x in generate(n, catalogue[n])) if c))
        raw[n] = got
        print(f"{n}: {len(got)} generated in {time.time() - started:.0f}s", flush=True)
        raw_path.parent.mkdir(exist_ok=True)
        raw_path.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")   # saved as it goes
    # MEANING REVIEW: centroids of the SEED examples, then each paraphrase must land on its own intent with a margin
    centroids = {}
    for n in catalogue:
        vecs = [norm(v) for v in embed(seeds_all[n])]
        centroids[n] = norm([sum(c) / len(vecs) for c in zip(*vecs)])
    kept: dict[str, list[str]] = {}
    dropped = 0
    for n, items in raw.items():
        if not items:
            continue
        for text, vec in zip(items, embed(items)):
            v = norm(vec)
            scored = sorted(((sum(a * b for a, b in zip(v, c)), m) for m, c in centroids.items()), reverse=True)
            if scored[0][1] == n and scored[0][0] - scored[1][0] >= 0.02:
                kept.setdefault(n, []).append(text)
            else:
                dropped += 1
    header = ("# GENERATED by scripts/generate_paraphrases.py (model " + MODEL + ") and REVIEWED automatically: each paraphrase\n"
              "# kept only when its meaning lands on its own intent (nomic centroids of the seed examples, margin 0.02).\n"
              "# Spot-check and delete any line that is wrong; the seed examples in intent_catalogue.yaml always stay.\n")
    (ROOT / "app/config/intent_paraphrases.yaml").write_text(
        header + yaml.safe_dump({"paraphrases": kept}, allow_unicode=True, sort_keys=True, width=120), encoding="utf-8")
    print(f"kept {sum(len(v) for v in kept.values())} paraphrases over {len(kept)} intents; dropped {dropped} by the "
          f"meaning review", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
