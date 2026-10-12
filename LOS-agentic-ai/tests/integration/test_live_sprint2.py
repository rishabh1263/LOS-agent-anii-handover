"""
SPRINT 2 -- the real conversation suite (directive phases 2, 3, 4, 7, 8, 9).

The REAL app path: FastAPI /api/v1/fos/copilot in-process (TestClient, FOS officer token), the throwaway test
PostgreSQL store, production flags, real Qwen + nomic (meaning, router, polish, general LLM). Synthetic applicants
only (make_case / kyc_failed fixtures). No test doubles on the chat path.

    LOS_LIVE_LLM_TESTS=1 pytest -m live_llm tests/integration/test_live_sprint2.py -s
    -> runs/sprint2/<SPRINT2_TAG or "latest">.{json,md}

Two parts, each graded by explicit assertions per turn (never by reading the answer for "sounds right"):
  MATRIX   short messages ("docs", "why", "yes", "cancel", "iska?" ...) in their contexts: clear context, no context,
           ambiguous, a pending confirmation, after a switch, after a cancel. Each case is its own chat.
  SESSION  one continuous FOS officer conversation, 25+ turns, two applicants, a correction, a cancel, a real write
           with confirmation and a store read-back, a policy question, a summary.
Per turn: status, latency, model calls (/api/chat), the meaning decision, the audited intent + tools, the reply.
The hard gates (no write without confirmation, no write after a cancel, no other applicant's data, the not-ready
case never moves) are asserted; the per-case pass rates are written to the report for the before/after table.
"""
import json
import os
import re
import time
import urllib.request

import pytest

from tests.integration.master_env import make_case
from tests.integration.master_env import prod  # noqa: F401
from tests.integration.test_master_6_kyc_ab import kyc_failed  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401
from tests.integration.test_live_fos_scenarios import _ollama_up, _production, _warm


# ---------------------------------------------------------------------------------------------------------------
# checks: each returns None (pass) or the reason it failed
# ---------------------------------------------------------------------------------------------------------------
def has(*words):
    return lambda md, ctx: None if any(w.lower() in md.lower() for w in words) else f"lacks any of {words}"


def lacks(*words):
    return lambda md, ctx: next((f"says {w!r}" for w in words if w.lower() in md.lower()), None)


def names(key):                                   # names the case held in ctx[key]
    return lambda md, ctx: None if ctx[key] in md or ctx.get(key + "_name", "\0") in md else f"does not name {key}"


def not_names(key):
    return lambda md, ctx: f"names {key}" if ctx[key] in md or ctx.get(key + "_name", "\0") in md else None


def asks():                                       # a clarification: a question with options or a question mark
    return lambda md, ctx: None if ("?" in md.split("\n")[0] or "](ask:" in md) else "no clarifying question"


def answers():                                    # NOT a clarification / menu: the first line is an answer
    return lambda md, ctx: ("asked instead of answering"
                            if re.match(r"(?i)^(which case|what would you like|do you mean|which one|whose)", md.strip())
                            else None)


def stored(key, field, expected):                 # the store, read back
    def check(md, ctx):
        from app.store import get_repository

        value = str(getattr(get_repository().get_application(ctx[key]), field, None))
        return None if value == str(expected) else f"store {field}={value}, expected {expected}"
    return check


def stage_is(key, stage):
    def check(md, ctx):
        from app.agents.applicant.copilot.capabilities import workspace

        now = str(workspace._stage(ctx[key]))
        return None if now == stage else f"stage {now}, expected {stage}"
    return check


# ---------------------------------------------------------------------------------------------------------------
# the matrix: (id, category, setup turns, the short message, checks)
# A1 = Rahul Sharma: KYC name mismatch (bank statement ROHIT VERMA), address proof + signature pending.
# A2 = Priya Verma: nothing uploaded.
# ---------------------------------------------------------------------------------------------------------------
MATRIX = [
    # clear context -> answer from the context
    ("docs-ctx", "short:clear", ["open {c1}", "what is pending?"], "docs", [answers(), has("Address Proof", "pending", "Signature")]),
    ("status-ctx", "short:clear", ["open {c1}"], "status", [answers(), names("c1")]),
    ("pending-ctx", "short:clear", ["open {c1}"], "pending", [answers(), has("Address Proof", "Signature")]),
    ("why-after-pending", "short:clear", ["open {c1}", "what is pending?"], "why", [answers(), has("KYC", "name", "match", "mandatory", "required", "needed")]),
    ("kyun-after-kyc", "short:clear", ["open {c1}", "kyc status?"], "kyun", [answers(), has("ROHIT VERMA", "name", "match", "differ")]),
    ("next-ctx", "short:clear", ["open {c1}", "what is pending?"], "next", [answers(), has("Upload", "next step", "Next step")]),
    ("aur-after-pan", "short:clear", ["open {c1}", "is the PAN verified?"], "aur bank statement?", [answers(), has("Bank Statement")]),
    ("iska-after-switch", "ref:switch", ["open {c1}", "open {c2}"], "iska pending kya hai?", [names("c2"), not_names("c1")]),
    ("pehle-wale", "ref:switch", ["open {c1}", "open {c2}", "pehle wale par wapas chalo"], "status", [names("c1"), not_names("c2")]),
    ("woh-wala", "ref:switch", ["open {c1}", "open {c2}"], "woh pehle wala case kholo", [names("c1")]),
    # no context -> one concise question, nothing guessed
    ("docs-nocase", "short:nocontext", [], "docs", [asks()]),
    # no case open: "which case?" with the officer's OWN case list is the right clarification (never another's)
    ("why-nocase", "short:nocontext", [], "why", [asks()]),
    ("yes-nothing-pending", "action:yes", [], "yes", [lacks("moved", "updated", "done.", "saved")]),
    ("cancel-nothing", "action:cancel", [], "cancel", [lacks("cancelled the", "was cancelled")]),
    ("iska-nocase", "short:nocontext", [], "iska status?", [asks()]),
    # a pending confirmation: yes / no / haan / nahi / cancel resolve it, nothing else
    ("yes-confirms", "action:yes", ["open {c1}", "loan amount 6 lakh karo"], "yes", [stored("c1", "loan_amount", "600000")]),
    ("haan-confirms", "action:yes", ["open {c1}", "loan amount 6 lakh karo"], "haan", [stored("c1", "loan_amount", "600000")]),
    ("no-declines", "action:no", ["open {c1}", "loan amount 7 lakh karo"], "no", [stored("c1", "loan_amount", "500000")]),
    ("nahi-declines", "action:no", ["open {c1}", "loan amount 7 lakh karo"], "nahi", [stored("c1", "loan_amount", "500000")]),
    ("cancel-pending", "action:cancel", ["open {c1}", "loan amount 7 lakh karo"], "cancel", [stored("c1", "loan_amount", "500000"), has("cancel", "not changed", "won't", "will not", "nothing")]),
    ("yes-after-cancel", "action:cancel", ["open {c1}", "loan amount 7 lakh karo", "cancel"], "yes", [stored("c1", "loan_amount", "500000")]),
    ("yes-not-blanket", "action:yes", ["open {c1}", "what is pending?"], "yes", [stage_is("c1", "FOS")]),
    ("kar-do-ambiguous", "action:ambiguous", ["open {c1}"], "kar do", [asks(), stage_is("c1", "FOS")]),
    ("move-not-ready", "action:refuse", ["open {c1}"], "CPA pe move kar do", [stage_is("c1", "FOS"), has("can't move", "not ready", "cannot", "can not", "pending", "fixed first")]),
    # isolation: the other applicant's data never leaks into the open case's answer
    ("named-other", "isolation", ["open {c1}"], "Priya ka pending kya hai?", [names("c2"), not_names("c1")]),
]

# ---------------------------------------------------------------------------------------------------------------
# the continuous session: (message, checks)
# ---------------------------------------------------------------------------------------------------------------
SESSION = [
    ("Good morning! Aaj kis case par pehle kaam karun?", [answers()]),
    ("{c1_name} ka case kholo", [names("c1")]),
    ("status batao", [answers(), names("c1")]),
    ("pending", [has("Address Proof", "Signature", "pending")]),
    ("why", [answers(), has("KYC", "name", "match", "required", "mandatory")]),
    ("KYC mein kya issue hai?", [has("ROHIT VERMA")]),
    ("docs", [answers()]),
    ("is signature mandatory?", [has("mandatory", "required")]),
    ("next", [answers(), has("Upload", "Next step", "next step")]),
    ("Ek minute, {c2_name} ka case check karo", [names("c2"), not_names("c1")]),
    # the header line is shown only when the case changes (case_header: on_change): Priya's own pending list (her
    # PAN is pending; Rahul's is verified) and never Rahul's case
    ("iska pending kya hai?", [has("PAN"), not_names("c1")]),
    ("Ab pehle wale par wapas chalo", [names("c1"), not_names("c2")]),
    ("uska status change hua?", [names("c1")]),
    ("loan amount 6 lakh karo", [has("6,00,000", "600000", "6 lakh", "confirm", "Confirm")]),
    ("haan", [stored("c1", "loan_amount", "600000")]),
    ("loan amount kitna hai ab?", [has("6,00,000", "600000")]),
    ("loan amount 7 lakh karo", [has("confirm", "Confirm", "7,00,000")]),
    ("ruko, cancel karo", [stored("c1", "loan_amount", "600000")]),
    ("yes", [stored("c1", "loan_amount", "600000")]),
    ("nahi mera matlab bank statement ka status", [has("Bank Statement")]),
    ("Is this case ready for CPA?", [has("not ready", "checks passed", "can't move", "Not ready")]),
    ("CPA pe move kar do", [stage_is("c1", "FOS")]),
    ("what documents are needed for personal loan?", [has("PAN", "Bank Statement")]),
    ("can I upload aadhaar instead of pan?", [has("No", "only PAN", "accepts only")]),
    ("what is api", [has("can't share")]),
    ("thanks", [answers()]),
    ("Abhi tak kya kya pending hai, ek summary do", [answers(), has("Address Proof", "Signature", "KYC")]),
]


@pytest.mark.live_llm
@pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                    reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")
def test_sprint2_live(client, kyc_failed, _store, monkeypatch, make_token):
    _production(monkeypatch)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, expires_in=3600)}"})
    import httpx

    from app.agents.applicant import audit
    from app.agents.applicant.copilot.capabilities import safety
    from app.agents.applicant.copilot.semantics import meaning

    a1, c1 = kyc_failed
    a2, c2 = make_case(client, "Priya Verma")
    ctx = {"c1": c1, "c2": c2, "c1_name": "Rahul Sharma", "c2_name": "Priya Verma"}
    decided, tools, calls = [], [], {"n": 0}
    monkeypatch.setattr(meaning, "_log", lambda msg, d: decided.append(f"{d.intent}/{d.decided_by}/{d.score:.2f}"))
    real_record = audit.record

    def record(**kw):
        tools.append(f"{kw.get('intent')}:{kw.get('status')}:{','.join(kw.get('tools') or [])}")
        return real_record(**kw)

    monkeypatch.setattr(audit, "record", record)
    real_post = httpx.AsyncClient.post

    async def counted(self, url, *a, **k):
        if str(url).endswith("/api/chat"):
            calls["n"] += 1
        return await real_post(self, url, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "post", counted)
    _warm(meaning)

    def say(message, chat):
        safety.reset()
        decided.clear(); tools.clear(); calls["n"] = 0
        t0 = time.time()
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message.format(**ctx),
                                                     "reply_language": "en", "chat_id": chat})
        body = r.json() if "json" in r.headers.get("content-type", "") else {"markdown": r.text}
        return {"officer": message.format(**ctx), "status": r.status_code, "ms": int((time.time() - t0) * 1000),
                "model_calls": calls["n"], "meaning": decided[-1] if decided else None, "audit": list(tools),
                "bot": body.get("markdown") or json.dumps(body)[:400]}

    def reset_amount():
        from app.store import get_repository

        repo = get_repository()
        app = repo.get_application(c1)
        app.loan_amount = 500000
        repo.save_application(app)

    def grade(turn, checks):
        problems = [p for p in (chk(turn["bot"], ctx) for chk in checks) if p]
        if turn["status"] != 200:
            problems.append(f"HTTP {turn['status']}")
        turn["problems"] = problems
        turn["passed"] = not problems
        return turn

    # ---- MATRIX ----
    matrix = []
    for n, (cid, category, setup, message, checks) in enumerate(MATRIX):
        reset_amount()
        chat = f"s2-m{n}-{cid}"
        history = [say(s, chat) for s in setup]
        turn = grade(say(message, chat), checks)
        matrix.append({"id": cid, "category": category, "setup": [h["officer"] for h in history],
                       "setup_replies": [h["bot"][:300] for h in history], **turn})

    # ---- SESSION ----
    reset_amount()
    session = [grade(say(m, "s2-session"), checks) for m, checks in SESSION]

    # ---- REPORT ----
    out = os.path.join("runs", "sprint2", os.environ.get("SPRINT2_TAG", "latest"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    by_cat: dict = {}
    for m in matrix:
        by_cat.setdefault(m["category"], [0, 0])
        by_cat[m["category"]][0] += m["passed"]
        by_cat[m["category"]][1] += 1
    allt = matrix + session
    ms = sorted(t["ms"] for t in allt)
    summary = {"matrix": f"{sum(m['passed'] for m in matrix)}/{len(matrix)}",
               "session": f"{sum(t['passed'] for t in session)}/{len(session)}",
               "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
               "p50_ms": ms[len(ms) // 2], "p95_ms": ms[int(len(ms) * 0.95) - 1], "max_ms": ms[-1],
               "model_calls_total": sum(t["model_calls"] for t in allt),
               "turns_with_model_call": sum(1 for t in allt if t["model_calls"])}
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "matrix": matrix, "session": session, "cases": {"c1": c1, "c2": c2}},
                  fh, ensure_ascii=False, indent=1)
    lines = [f"# Sprint 2 live suite -- {out}", "", "```", json.dumps(summary, indent=1), "```", "", "## Matrix", ""]
    for m in matrix:
        lines += [f"### {'PASS' if m['passed'] else 'FAIL'} {m['id']} [{m['category']}] setup={m['setup']} -> {m['officer']!r}",
                  f"`{m['ms']} ms, model_calls={m['model_calls']}, meaning={m['meaning']}, audit={m['audit']}`"
                  + (f"  **problems: {m['problems']}**" if m["problems"] else ""), "", m["bot"], ""]
    lines += ["## Continuous session", ""]
    for n, t in enumerate(session, 1):
        lines += [f"### {n}. {'PASS' if t['passed'] else 'FAIL'} > {t['officer']}",
                  f"`{t['ms']} ms, model_calls={t['model_calls']}, meaning={t['meaning']}, audit={t['audit']}`"
                  + (f"  **problems: {t['problems']}**" if t["problems"] else ""), "", t["bot"], ""]
    with open(out + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("SPRINT2", json.dumps(summary))

    # ---- HARD GATES (never relaxed) ----
    gates = {"no_write_without_confirmation_or_after_cancel": [
                 t for t in matrix if t["category"] in ("action:no", "action:cancel") and not t["passed"]],
             "isolation": [t for t in matrix if t["category"] in ("isolation", "ref:switch") and any(
                 "names c1" in p or "names c2" in p for p in t["problems"])],
             "not_ready_never_moves": [t for t in matrix + session if any("stage" in p for p in t["problems"])],
             "http_errors": [t for t in allt if t["status"] != 200]}
    assert not any(gates.values()), {k: [(t.get("id") or t["officer"], t["problems"]) for t in v]
                                     for k, v in gates.items() if v}
