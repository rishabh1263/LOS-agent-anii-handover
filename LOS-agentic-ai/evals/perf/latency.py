"""
LIVE LATENCY BENCHMARK against a running server (no TestClient).

    python -m evals.perf.latency [--base http://127.0.0.1:8010] [--rounds 3]

Measures what a frontend waits for, as wall-clock HTTP time:
  upload_1    one identity document (PAN)
  upload_6    a realistic batch: PAN, DL, voter ID, passport, salary slip, bank statement
  chat        typed questions on a case with documents (P50 / P95 / P99 / max)
  status      GET verification-status and summary (the polling endpoints)
Also the server process RSS when psutil can see it. Prints numbers, writes runs/latency.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from evals.http_e2e import BANK_A, PASSPORT_B, RISHABH_DL, RISHABH_PAN, ROOT, S, VOTER_A, Api, _token

FOS = ("read_applicant read_application read_documents read_verification read_pending_items read_next_action "
       "create_applicant update_applicant create_application upload_document")
BATCH = [("pan.jpg", RISHABH_PAN, "PAN"), ("dl.jpg", RISHABH_DL, "DRIVING_LICENCE"),
         ("voter.jpg", VOTER_A, "VOTER_ID"), ("passport.jpg", PASSPORT_B, "PASSPORT"),
         ("slip.pdf", S / "real_batch/salary_slip.pdf", "SALARY_SLIP"), ("bank.pdf", BANK_A, "BANK_STATEMENT")]
QUESTIONS = ["what is kyc status", "show all documents", "what is pending?", "pan details", "status kya hai?",
             "is my case ready for CPA?", "what should I do next?", "documents verify hue?", "KYC hua?", "hello"]


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))], 1)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "p99": pct(values, 99),
            "max": round(max(values), 1), "mean": round(statistics.mean(values), 1)}


def timed(fn):
    started = time.perf_counter()
    out = fn()
    return (time.perf_counter() - started) * 1000, out


def rss_mb(port: int) -> float | None:
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                return round(psutil.Process(c.pid).memory_info().rss / 1e6)
    except Exception:  # noqa: BLE001
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--uploads", type=int, default=20, help="single-document uploads")
    parser.add_argument("--batches", type=int, default=10, help="six-document batch uploads")
    parser.add_argument("--chat-rounds", type=int, default=4, help="rounds of the 10 questions")
    args = parser.parse_args()
    api = Api(args.base, _token("perf-officer", FOS))
    port = int(args.base.rsplit(":", 1)[-1].split("/")[0])
    out: dict = {"base": args.base, "rss_mb_before": rss_mb(port)}

    one, six, chat, status = [], [], [], []
    import random
    import tempfile

    from PIL import Image

    for i in range(args.uploads):
        # a NEW image every time (one pixel changed) so nothing can be answered from a cache
        im = Image.open(RISHABH_PAN).convert("RGB")
        im.putpixel((0, 0), (random.randrange(256),) * 3)
        fresh = tempfile.mktemp(suffix=".jpg")
        im.save(fresh, quality=95)
        a, c = api.open_case()
        ms, _ = timed(lambda: api.upload(a, c, [(f"pan{i}.jpg", fresh)], ["PAN"]))
        one.append(ms)
    batch = [(n, p) for n, p, _t in BATCH if Path(p).exists()]
    types = [t for n, p, t in BATCH if Path(p).exists()]
    case = None
    for _ in range(args.batches):
        a, c = api.open_case()
        ms, _ = timed(lambda: api.upload(a, c, batch, types))
        six.append(ms)
        case = (a, c)
    a, c = case
    for _ in range(args.chat_rounds):
        for q in QUESTIONS:
            ms, _ = timed(lambda: api.ask(a, c, q))
            chat.append(ms)
        for path in ("verification-status", "summary", "reviews", "risk-signals", "verification-status"):
            ms, r = timed(lambda: api.c.get(f"/api/v1/applications/{c}/{path}"))
            r.raise_for_status()
            status.append(ms)

    out.update({"upload_1_doc_ms": summary(one), "upload_6_docs_ms": summary(six), "chat_ms": summary(chat),
                "status_poll_ms": summary(status), "rss_mb_after": rss_mb(port),
                "batch_documents": [t for n, p, t in BATCH if Path(p).exists()]})
    report = ROOT / "runs" / "latency.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(out, indent=1), encoding="utf-8")
    for key in ("upload_1_doc_ms", "upload_6_docs_ms", "chat_ms", "status_poll_ms"):
        s = out[key]
        print(f"{key:18} n={s['n']:3} P50={s['p50']:8} P95={s['p95']:8} P99={s['p99']:8} max={s['max']:8}")
    print(f"server RSS MB: before={out['rss_mb_before']} after={out['rss_mb_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
