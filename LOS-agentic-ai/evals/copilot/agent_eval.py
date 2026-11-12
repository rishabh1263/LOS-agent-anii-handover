"""
QWEN AGENT EVAL (2026-10-13): is the ANSWER right, and how long did it take -- against the REAL running API.

    python -m evals.copilot.agent_eval --set dev [--base http://127.0.0.1:8010]

Builds one fresh demo case (Rahul Sharma, PERSONAL_LOAN; PAN, salary slip and bank statement verified; address proof
and signature pending) through the public API, then asks every conversation of evals/copilot/agent_<set>.yaml in its
own chat. A turn passes when the answer holds every `all` group (any one phrase of each, case-insensitive), none of
`never`, and asks back when `question: true`. Writes runs/agent_eval_<set>.json (every turn: answer, layer, tools,
seconds, pass / why). The server must run with authentication off (local dev) -- the same as the Swagger demo.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "samples" / "documents"


def _post(base: str, path: str, body: dict, timeout: float = 180) -> dict:
    request = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=timeout))


def _multipart(base: str, route: str, fields: dict, files: list[tuple[str, Path]]) -> dict:
    boundary = "----agenteval"
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, path in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{path.name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    data = b"".join(parts) + f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(base + route, data, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.load(urllib.request.urlopen(request, timeout=600))


def build_case(base: str, pan: Path) -> tuple[str, str]:
    created = _post(base, "/api/v1/fos/applicants", {
        "applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                      "address": "Pune"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, "tenure_months": 60,
                        "employment_type": "SALARIED", "declared_monthly_obligations": 10000}})
    applicant_id, case_id = created["applicant_id"], created["case_id"]
    # the FOS intake upload (the document-driven eligibility endpoint is retired, 2026-10-13)
    _multipart(base, "/api/v1/fos/documents",
               {"applicant_id": applicant_id, "case_id": case_id},
               [("files", pan), ("files", DOCS / "demo_salary_slip_rahul_sharma.pdf"),
                ("files", DOCS / "demo_bank_statement_rahul_sharma.pdf")])
    return applicant_id, case_id


def judge(turn: dict, answer: str) -> list[str]:
    low = answer.lower()
    why = []
    for group in turn.get("all") or []:
        if not any(str(p).lower() in low for p in group):
            why.append(f"missing one of {group}")
    for bad in turn.get("never") or []:
        if str(bad).lower() in low:
            why.append(f"said {bad!r}")
    if turn.get("question") and "?" not in answer:
        why.append("did not ask back")
    return why


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--set", default="dev")
    # the chat rate limit (applicant_agent.yaml rate_limit_per_minute: 30) answers a back-to-back eval with "you're
    # sending messages very quickly" -- a harness artifact, not an answer; pace the turns like a person would
    p.add_argument("--pause", type=float, default=2.2, help="seconds between turns (rate limit: 30 a minute)")
    p.add_argument("--base", default="http://127.0.0.1:8010")
    p.add_argument("--pan", required=True, help="a PAN image in the applicant's name (synthetic)")
    a = p.parse_args()
    spec = yaml.safe_load((ROOT / "evals" / "copilot" / f"agent_{a.set}.yaml").read_text(encoding="utf-8"))
    applicant_id, case_id = build_case(a.base, Path(a.pan))
    stamp = int(time.time())
    rows, times = [], []
    for conversation in spec["conversations"]:
        chat = f"agent-eval-{stamp}-{conversation['id']}"
        for i, turn in enumerate(conversation["turns"]):
            time.sleep(max(0.0, a.pause))
            started = time.perf_counter()
            reply = _post(a.base, "/api/v1/fos/copilot", {"action": "CUSTOM_QUERY", "message": turn["q"],
                                                          "chat_id": chat, "applicant_id": applicant_id,
                                                          "case_id": case_id})
            seconds = round(time.perf_counter() - started, 2)
            times.append(seconds)
            text = str(reply.get("markdown") or reply.get("answer") or "")
            shown = "\n".join(l for l in text.split("\n") if "](ask:" not in l and "](action:" not in l).strip()
            why = judge(turn, shown)
            diag = reply.get("diagnostics") or {}
            rows.append({"conversation": conversation["id"], "turn": i + 1, "q": turn["q"], "ok": not why,
                         "why": why, "seconds": seconds, "layer": diag.get("layer"), "intent": diag.get("intent"),
                         "answer": shown[:600]})
            print(f"{'OK ' if not why else 'XX '} {seconds:5.1f}s [{diag.get('layer')}] {turn['q']!r}"
                  + (f"  -> {why}" if why else ""))
    times.sort()
    report = {"set": a.set, "turns": len(rows), "correct": sum(r["ok"] for r in rows),
              "latency": {"p50": times[len(times) // 2], "p95": times[min(len(times) - 1, int(len(times) * 0.95))],
                          "max": times[-1]},
              "rows": rows}
    out = ROOT / "runs" / f"agent_eval_{a.set}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{a.set}: {report['correct']}/{report['turns']} correct, latency {report['latency']} -> {out}")


if __name__ == "__main__":
    main()
