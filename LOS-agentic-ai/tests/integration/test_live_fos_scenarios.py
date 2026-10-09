"""
FOS officer scenarios A-H (directive phase 6) through the REAL app path: FastAPI /api/v1/fos/copilot in-process
(TestClient, dev identity), the test PostgreSQL store, production flags, real Qwen + nomic. Synthetic applicants only.
Test doubles: NONE for the chat path. The KYC result of applicant 1 is a seeded finding (synthetic fixture).
Opt-in (needs Ollama with qwen2.5:3b + nomic-embed-text):  LOS_LIVE_LLM_TESTS=1 pytest -m live_llm
tests/integration/test_live_fos_scenarios.py   -> transcript runs/fos_scenarios/latest.{md,json} (or $SCEN_OUT).
The HARD gates are asserted (no write after a hold, no move of a not-ready case, no other applicant's data, nothing
for another officer); conversational quality is read from the transcript with the rubric in
docs/FOS_COPILOT_ENGINEERING_REPORT.md.
"""
import json
import os
import time
import urllib.request

import pytest

from tests import conftest
from tests.integration.master_env import make_case
from tests.integration.master_env import prod  # noqa: F401
from tests.integration.test_master_6_kyc_ab import kyc_failed  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

CHAT = "fos-scenario-g"


def _production(monkeypatch):
    for flag in conftest._PHASE3_FLAGS:
        if flag != "LOS_LOGIN_SELF_GRANT_LEGACY":
            monkeypatch.setenv(flag, "true")
    for name in ("LOS_STAGE_GATE_IN_SERVICE", "LOS_FOS_CPA_KYC_RULE", "COPILOT_LLM_ROUTER", "COPILOT_MEANING",
                 "COPILOT_GENERAL_LLM", "COPILOT_LLM_REWRITE"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("APPLICANT_AGENT_AUDIENCE", "agent")


def _warm(meaning):
    import urllib.request

    from app.llm import availability

    meaning.rank("warmup")
    availability.warm_in_background()
    for _ in range(90):
        ps = json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/ps"))
        if any(m["name"].startswith("qwen") for m in ps["models"]):
            break
        time.sleep(1)
    availability.reset()


def _ollama_up() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.live_llm
@pytest.mark.skipif(os.getenv("LOS_LIVE_LLM_TESTS") != "1" or not _ollama_up(),
                    reason="live model tests are opt-in (LOS_LIVE_LLM_TESTS=1)")
def test_fos_scenarios(client, kyc_failed, _store, monkeypatch, make_token):
    _production(monkeypatch)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, expires_in=3600)}"})
    from app.agents.applicant import audit
    from app.agents.applicant.copilot.capabilities import safety, workspace
    from app.agents.applicant.copilot.semantics import meaning

    a1, c1 = kyc_failed                                   # applicant 1: KYC name mismatch, signature missing
    # the synthetic KYC result as the KYC agent records it (checks passed / failed), not the bare fixture payload
    from app.store.models import CaseFinding, FindingKind

    old_kyc = [f for f in _store.get_current_findings(c1) if str(getattr(f.finding_kind, "value", f.finding_kind)) == "KYC"]
    payload = dict(old_kyc[0].payload) if old_kyc else {}
    payload.update(passed_checks=["DOB", "ADDRESS", "PAN", "FATHER_NAME"], failed_checks=["NAME"],
                   missing_information=[])
    _store.save_finding(CaseFinding(finding_id="k-agent", case_id=c1, party_id=a1, finding_kind=FindingKind.KYC,
                                    status="FAIL", reason_codes=["NAME_MISMATCH"], content_hash="k2", payload=payload))
    a2, c2 = make_case(client, "Priya Verma")             # applicant 2: nothing uploaded
    make_case(client, "Amit Rao")
    decided, tools = [], []
    monkeypatch.setattr(meaning, "_log", lambda msg, d: decided.append(f"{d.intent}/{d.decided_by}/{d.score:.2f}"))
    real_record = audit.record

    def record(**kw):
        tools.append(f"{kw.get('intent')}:{kw.get('status')}:{','.join(kw.get('tools') or [])}")
        return real_record(**kw)

    monkeypatch.setattr(audit, "record", record)
    _warm(meaning)
    turns = []
    out = os.environ.get("SCEN_OUT") or os.path.join("runs", "fos_scenarios", "latest")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    _slow = open(out + ".slow.txt", "w")

    def say(message, chat=CHAT, scenario=""):
        safety.reset()
        decided.clear(); tools.clear()
        t0 = time.time()
        import faulthandler
        faulthandler.dump_traceback_later(20, repeat=False, file=_slow)
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                     "reply_language": "en", "chat_id": chat})
        faulthandler.cancel_dump_traceback_later()
        ms = int((time.time() - t0) * 1000)
        body = r.json() if "json" in r.headers.get("content-type", "") else {"markdown": r.text}
        md = body.get("markdown") or json.dumps(body)[:400]
        turns.append({"n": len(turns) + 1, "scenario": scenario, "chat": chat, "officer": message,
                      "status": r.status_code, "ms": ms, "meaning": decided[-1] if decided else None,
                      "audit": list(tools), "bot": md})
        return md

    # A -- start naturally, no case open
    say("Bhai, aaj FOS mein kaafi applications pending hain. Pehle batao kis applicant par kaam karna chahiye?", scenario="A")
    # B -- applicant 1 and follow-ups
    say("Rahul Sharma ka case kholo", scenario="B")
    say("Is applicant ka status batao.", scenario="B")
    say("Kya pending hai?", scenario="B")
    say("Ye pending kyun hai?", scenario="B")
    say("Ab mujhe kya karna chahiye?", scenario="B")
    say("Iske liye document chahiye ya sirf clarification?", scenario="B")
    say("Agar document already upload hai toh?", scenario="B")
    # C -- switch and return
    say("Ek minute, doosre applicant ka status dekhna hai.", scenario="C")
    say("Priya Verma", scenario="C")
    say("Iska kya scene hai?", scenario="C")
    say("Achha, ab pehle wale par wapas chalo.", scenario="C")
    say("Uska pending point kya tha?", scenario="C")
    say("Kya uska status change hua?", scenario="C")
    # D -- corrections and ambiguity
    say("Nahi, mera matlab bank statement se tha.", scenario="D")
    say("Usko process kar do.", scenario="D")
    say("Ruko, abhi action mat lena.", scenario="D")
    say("Pehle sirf batao kya issue hai.", scenario="D")
    # E -- pending FOS work and a real, reversible action with post-verification
    say("Is applicant ke pending FOS checks complete karne ke liye next step le lo.", scenario="E")
    say("loan amount 6 lakh karo", scenario="E")
    say("confirm", scenario="E")
    from app.store import get_repository as _repo

    amount_after_confirm = str(getattr(_repo().get_application(c1), "loan_amount", None))
    say("loan amount 7 lakh karo", scenario="D/E-hold")
    say("Ruko, abhi action mat lena.", scenario="D/E-hold")
    amount_after_hold = str(getattr(_repo().get_application(c1), "loan_amount", None))
    say("Priya ka pending kya hai?", scenario="C-isolation")
    say("Kya is case ko next stage par bhej sakte hain?", scenario="E/F")
    say("CPA pe move kar do", scenario="E")
    # F -- policy
    say("Agar ye document missing hai toh ab kya karna hoga?", scenario="F")
    say("Policy mein exactly kya requirement hai?", scenario="F")
    # D (more) + G wrap-up
    say("Actually, abhi us task ko chhod do.", scenario="D")
    say("Jo tumne bataya, uska next step kya hai?", scenario="G")
    say("Abhi tak kya kya pending hai, ek summary do.", scenario="G")
    say("Agar document already upload hai toh?", scenario="B")

    from app.store import get_repository

    evidence = {"amount_after_confirm": amount_after_confirm, "amount_after_hold": amount_after_hold,
                "loan_amount_after": str(getattr(get_repository().get_application(c1), "loan_amount", None)),
                "stage_after": str(workspace._stage(c1)), "case_1": c1, "case_2": c2}

    # H -- session recovery: the in-process caches are dropped; the conversation is read back from the store
    from app.agents.applicant.copilot.conversation import state as conv

    conv.STORE._memory = conv.ConversationStore()
    conv.STORE._repository = None
    say("Hum kis applicant par the? Uska kya pending tha?", scenario="H-resume")
    say("Maine galat applicant select kiya.", scenario="D")
    # H -- another officer with the SAME chat id must not get this conversation
    other = {"Authorization": f"Bearer {make_token(subject='officer-b', scopes=FOS_SCOPES, expires_in=3600)}"}
    if other:
        r = client.post("/api/v1/fos/copilot", headers=other,
                        json={"action": "CUSTOM_QUERY", "message": "Uska pending kya tha?", "chat_id": CHAT})
        turns.append({"n": len(turns) + 1, "scenario": "H-other-officer", "status": r.status_code,
                      "bot": (r.json() or {}).get("markdown")})

    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump({"turns": turns, "evidence": evidence}, fh, ensure_ascii=False, indent=1)
    ms = sorted(t["ms"] for t in turns if "ms" in t)
    lines = [f"# FOS scenarios -- {len(turns)} turns, p50 {ms[len(ms)//2]} ms, p95 {ms[int(len(ms)*0.95)-1]} ms, "
             f"max {ms[-1]} ms", f"evidence: {json.dumps(evidence)}", ""]
    for t in turns:
        lines += [f"## {t['n']}. [{t['scenario']}] {t.get('officer', '')}",
                  f"`{t.get('status')} {t.get('ms', '-')} ms meaning={t.get('meaning')} audit={t.get('audit')}`", "",
                  str(t.get("bot")), ""]
    with open(out + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    # ---- the hard gates ----------------------------------------------------------------------------------------
    assert evidence["amount_after_confirm"] == evidence["amount_after_hold"]       # the hold wrote nothing
    assert evidence["stage_after"] == "FOS"                                       # the not-ready case never moved
    named = next(t for t in turns if t["scenario"] == "C-isolation")
    assert c2 in named["bot"] and c1 not in named["bot"]                          # Priya's answer, never Rahul's
    other_officer = next(t for t in turns if t["scenario"] == "H-other-officer")
    assert c1 not in str(other_officer["bot"]) and c2 not in str(other_officer["bot"])
    assert all(t.get("status") == 200 for t in turns)
