"""TEMPORARY probe (deleted after use): evals/copilot/agent_<set>.yaml through the CURRENT code, in-process, with the
deployment's .env flags (the suite pins them off) and the model on. Run: PROBE_SET=dev pytest -s this file."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from tests.integration.test_frontend_contract import demo  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401

_SERVER = ("COPILOT_GENERAL_LLM", "COPILOT_LLM_REWRITE", "COPILOT_LLM_ROUTER", "COPILOT_MEANING", "COPILOT_POLISH",
           "COPILOT_UNDERSTANDING_LLM", "EMBEDDING_PROVIDER", "KNOWLEDGE_BACKEND", "LOS_STAGE_GATE_IN_SERVICE",
           "LOS_FOS_CPA_KYC_RULE", "LOS_LLM_SUMMARY_ENABLED")


@pytest.fixture
def server_flags(monkeypatch):
    from dotenv import dotenv_values

    from tests.conftest import _PHASE3_FLAGS
    from app.agents.applicant.copilot import agent as _agent
    from app.agents.applicant.copilot.semantics import llm_router as _router

    env = dotenv_values(".env")
    for key in (*_PHASE3_FLAGS, *_SERVER):
        if env.get(key) is not None:
            monkeypatch.setenv(key, str(env[key]).strip())
        else:
            monkeypatch.delenv(key, raising=False)
    _router.clear_cache()
    _agent.KNOWLEDGE_ANSWERS.clear()


def test_probe(client, demo, server_flags, monkeypatch):  # noqa: F811
    from evals.copilot import agent_eval as ev
    from app.agents.applicant.copilot.capabilities import safety

    def post(base, path, body, timeout=180):
        r = client.post(path, json=body)
        return r.json()

    def multipart(base, route, fields, files):
        r = client.post(route, data=fields, files=[(k, (p.name, p.read_bytes(), "application/octet-stream"))
                                                   for k, p in files])
        return r.json()

    if os.environ.get("PROBE_TRACE"):
        import httpx
        import traceback

        real = httpx.AsyncClient.post

        async def traced(self, url, *a, **k):
            t = time.perf_counter()
            try:
                return await real(self, url, *a, **k)
            finally:
                if "11434" in str(url) or "/api/" in str(url):
                    who = [f"{f.filename.split(chr(92))[-1]}:{f.lineno}:{f.name}" for f in traceback.extract_stack()[-6:-1]
                           if "app" in f.filename]
                    print(f"   LLM {time.perf_counter() - t:5.2f}s {str(url)[-12:]} {who[-3:]}", flush=True)

        monkeypatch.setattr(httpx.AsyncClient, "post", traced)
    monkeypatch.setattr(ev, "_post", post)
    monkeypatch.setattr(ev, "_multipart", multipart)
    pan = Path(os.environ["PROBE_PAN"])
    name = os.environ.get("PROBE_SET", "dev")
    spec = yaml.safe_load((ev.ROOT / "evals" / "copilot" / f"agent_{name}.yaml").read_text(encoding="utf-8"))
    only = os.environ.get("PROBE_ONLY")
    applicant_id, case_id = ev.build_case("", pan)
    rows = []
    for conversation in spec["conversations"]:
        if only and conversation["id"] not in only.split(","):
            continue
        chat = f"probe-{int(time.time())}-{conversation['id']}"
        for turn in conversation["turns"]:
            safety.reset()
            started = time.perf_counter()
            body = {"action": "CUSTOM_QUERY", "message": turn["q"], "chat_id": chat, "applicant_id": applicant_id,
                    "case_id": case_id}
            if os.environ.get("PROBE_SAMPLE"):
                import collections
                import sys
                import threading

                hits: collections.Counter = collections.Counter()
                done = threading.Event()
                me = threading.get_ident()

                def sampler():
                    while not done.wait(0.05):
                        for tid, frame in sys._current_frames().items():
                            if tid == me or tid == threading.get_ident():
                                continue
                            chain = []
                            f = frame
                            while f is not None:
                                if "LOS-agentic-ai" in f.f_code.co_filename and "tests" not in f.f_code.co_filename:
                                    chain.append(f"{f.f_code.co_filename.split(chr(92))[-1]}:{f.f_code.co_name}:{f.f_lineno}")
                                f = f.f_back
                            if chain:
                                hits[" < ".join(chain[:4])] += 1

                th = threading.Thread(target=sampler, daemon=True)
                th.start()
                reply = post("", "/api/v1/fos/copilot", body)
                done.set()
                for chain, n in hits.most_common(12):
                    print(f"   SAMPLE {n * 0.05:5.2f}s {chain}", flush=True)
            elif os.environ.get("PROBE_PROFILE"):
                import cProfile
                import io
                import pstats

                prof = cProfile.Profile()
                reply = prof.runcall(post, "", "/api/v1/fos/copilot", body)
                out = io.StringIO()
                stats = pstats.Stats(prof, stream=out).sort_stats("cumulative")
                stats.print_stats(r"(app[\\/]|urllib|socket|select|sleep|wait)", 25)
                print(out.getvalue()[-6000:], flush=True)
            else:
                reply = post("", "/api/v1/fos/copilot", body)
            seconds = round(time.perf_counter() - started, 2)
            text = str(reply.get("markdown") or reply.get("answer") or "")
            shown = "\n".join(l for l in text.split("\n") if "](ask:" not in l and "](action:" not in l).strip()
            why = ev.judge(turn, shown)
            diag = reply.get("diagnostics") or {}
            rows.append({"q": turn["q"], "ok": not why, "why": why, "seconds": seconds, "answer": shown[:400], "diag": diag})
            print("   DIAG", {k: diag.get(k) for k in ("layer", "intent", "agent", "skipped", "steps") if diag.get(k)}, flush=True)
            print(f"{'OK ' if not why else 'XX '} {seconds:5.1f}s {turn['q']!r} -> {shown[:420]!r}"
                  + (f"  {why}" if why else ""), flush=True)
    print(f"PROBE {name}: {sum(r['ok'] for r in rows)}/{len(rows)}")
    Path("runs/probe_dev_tmp.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
