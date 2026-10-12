"""
LIVE SMOKE (2026-10-12, before the frontend handoff): one officer conversation through the REAL chat path --
/api/v1/fos/copilot in-process, production flags, real Qwen + nomic, synthetic applicants. Opt-in:
    LOS_LIVE_LLM_TESTS=1 pytest -s tests/integration/test_live_smoke_chat.py
Transcript with per-turn latency -> runs/live_smoke/latest.md. HARD checks only: no server error, nothing leaks an
internal error, the case data in answers is the case's own, out-of-scope is refused. Quality is read from the transcript.
"""
import os
import re
import time
from pathlib import Path

import pytest

from tests.integration.master_env import make_case
from tests.integration.master_env import prod  # noqa: F401
from tests.integration.test_live_fos_scenarios import _ollama_up, _warm
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401
from tests import conftest

CHAT = "live-smoke"


@pytest.mark.live_llm
@pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                    reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")
def test_live_smoke_conversation(client, prod, monkeypatch):  # noqa: F811
    for flag in conftest._PHASE3_FLAGS:
        if flag != "LOS_LOGIN_SELF_GRANT_LEGACY":
            monkeypatch.setenv(flag, "true")
    for name in ("LOS_STAGE_GATE_IN_SERVICE", "LOS_FOS_CPA_KYC_RULE", "COPILOT_LLM_ROUTER", "COPILOT_MEANING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    from app.agents.applicant.copilot.semantics import meaning

    _warm(meaning)
    _, case_id = make_case(client, "Rahul Sharma")
    _, other_id = make_case(client, "Priya Verma", loan_amount=750000)

    rows = []

    def say(text):
        started = time.perf_counter()
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text,
                                                      "reply_language": "en", "chat_id": CHAT})
        ms = (time.perf_counter() - started) * 1000
        md = r.json().get("markdown") or str(r.json()) if r.headers.get("content-type", "").startswith(
            "application/json") else r.text
        rows.append((text, r.status_code, ms, md))
        assert r.status_code < 500, (text, r.text)
        assert not re.search(r"Traceback|KeyError|NoneType|Internal Server Error", md), (text, md)
        return md

    say("show my cases")
    say(f"open {case_id}")
    pending = say("kya pending hai")
    say("why?")
    say("where does this one stand right now")
    loan = say("kitna loan maanga hai")
    say("who is the applicant")
    say("is this case ready for CPA?")
    say("what about the co-applicant?")
    say("is the customer eligible? how much can he borrow?")
    say("what does the credit memo say")
    say("raise a query: bank statement for last 6 months needed")
    say("cancel")
    say("raise deviation for low cibil score")
    say("cancel")
    say("what is FOIR?")
    say("what comes after CPA in the process?")
    say("write me a poem")
    say("close")
    say("which of my cases are pending for KYC?")
    say("show me the next two")

    out = Path(os.getenv("SMOKE_OUT", "runs/live_smoke/latest.md"))
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Live smoke {time.strftime('%Y-%m-%d %H:%M')}\n"]
    for text, status, ms, md in rows:
        lines.append(f"## > {text}\n_HTTP {status}, {ms:.0f} ms_\n\n{md}\n")
    times = sorted(ms for _, _, ms, _ in rows)
    lines.append(f"\n**p50 {times[len(times) // 2]:.0f} ms, max {times[-1]:.0f} ms, turns {len(rows)}**\n")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSMOKE p50 {times[len(times) // 2]:.0f} ms max {times[-1]:.0f} ms -> {out}")

    assert "5,00,000" in loan and other_id not in loan          # the open case's own data, not the other case's
    assert case_id in pending or "pending" in pending.lower()
    assert "poem" not in rows[17][3].lower().split("\n")[0] or "can't" in rows[17][3].lower() \
        or "cannot" in rows[17][3].lower() or "only" in rows[17][3].lower()
