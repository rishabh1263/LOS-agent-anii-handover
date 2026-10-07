"""
MEMORY BUDGET MEASUREMENT (Phase 3 step 1).

    python -m evals.perf.memory_budget                 # the box as it is now
    python -m evals.perf.memory_budget --load-model    # + what qwen2.5:3b costs resident

Reads only: total/free RAM, resident memory per process group (ollama, python,
postgres), and Ollama's own report of loaded models. --load-model asks Ollama for
one token (the model then stays loaded for its keep-alive) and measures the change;
it refuses to start, and stops, below the memory floor (evals/perf/memguard.py).
Writes runs/memory_budget.json.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx
import psutil

from evals.perf import memguard

ROOT = Path(__file__).resolve().parents[2]


def snapshot(host: str) -> dict:
    vm = psutil.virtual_memory()
    loaded = []
    try:
        for m in httpx.get(f"{host}/api/ps", timeout=3).json().get("models") or []:
            loaded.append({"model": m.get("name"), "size_gb": round((m.get("size") or 0) / memguard.GB, 2)})
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        loaded = [{"error": type(exc).__name__}]
    return {"total_gb": round(vm.total / memguard.GB, 2), "free_gb": round(vm.available / memguard.GB, 2),
            "groups": memguard.groups(), "ollama_loaded": loaded}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--num-ctx", type=int, default=2048)
    p.add_argument("--load-model", action="store_true")
    args = p.parse_args()

    report = {"before": snapshot(args.host)}
    print(json.dumps(report["before"], indent=2))
    if args.load_model:
        try:
            memguard.check("before loading the model")
            started = time.perf_counter()
            r = httpx.post(f"{args.host}/api/generate", timeout=120, json={
                "model": args.model, "prompt": "ok", "stream": False, "keep_alive": "10m",
                "options": {"num_predict": 1, "num_ctx": args.num_ctx}})
            r.raise_for_status()
            report["load_ms"] = round((time.perf_counter() - started) * 1000)
            report["load_duration_ms"] = round((r.json().get("load_duration") or 0) / 1e6)
            time.sleep(1)
            report["after"] = snapshot(args.host)
            report["model_cost_gb"] = round(report["before"]["free_gb"] - report["after"]["free_gb"], 2)
            print(json.dumps({k: report[k] for k in ("load_ms", "load_duration_ms", "model_cost_gb", "after")}, indent=2))
            memguard.check("after loading the model")
        except memguard.LowMemory as stop:
            report["stopped"] = str(stop)
            print(stop)
    out = ROOT / "runs" / "memory_budget.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 2 if "stopped" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
