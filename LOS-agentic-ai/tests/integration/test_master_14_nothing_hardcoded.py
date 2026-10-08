"""
MASTER SPEC section 14 -- proof that the words are config.

1. A SCAN of the copilot code for user-facing sentences in string literals (docstrings, logs, raised errors,
   regexes and SQL excluded). The master-spec modules must have NONE; the older modules may keep only the
   sentences recorded in the baseline (a ratchet: nothing new anywhere -- the baseline only shrinks).
   Re-record after moving sentences to config:  WRITE_HARDCODED_BASELINE=1 pytest this file
2. CONFIG CHANGES CHANGE BEHAVIOUR with no code change: page size 7, a new FAQ item, a new synonym, the tts
   sentence limit, a new action-link label, a reply wording.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [ROOT / "app/agents/applicant/copilot", ROOT / "app/api/routes/fos_api.py",
           ROOT / "app/api/routes/copilot_api.py"]
#: built for the master spec: no hardcoded sentence at all
STRICT = {"app/agents/applicant/copilot/answering/contract.py", "app/agents/applicant/copilot/answering/realtime.py",
          "app/agents/applicant/copilot/capabilities/case_list.py", "app/agents/applicant/copilot/capabilities/faq.py",
          "app/agents/applicant/copilot/answering/kyc_table.py",
          "app/agents/applicant/copilot/answering/readiness_report.py",
          # MASTER SPEC 15 / 16: the product flow, the form, downloads, import, the abuse rule
          "app/agents/applicant/copilot/capabilities/product_flow.py",
          "app/agents/applicant/copilot/capabilities/case_form.py",
          "app/agents/applicant/copilot/capabilities/exports.py",
          "app/agents/applicant/copilot/capabilities/importer.py",
          "app/agents/applicant/copilot/capabilities/abuse_guard.py"}
BASELINE = ROOT / "tests/data/hardcoded_sentences_baseline.json"
_LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "log", "warn"}


def _is_sentence(text: str) -> bool:
    s = text.strip()
    if len(s.split()) < 4 or not re.search(r"[A-Za-zऀ-ॿ]{3,}", s):
        return False
    if re.search(r"\\[sdwbS]|\(\?|\[\^|\|\w+\||^\^|\$$", s):                  # a regex
        return False
    if re.match(r"(?i)\s*(select|insert|update|delete|create|alter|with)\s", s):     # SQL
        return False
    if re.fullmatch(r"[\w\s./:{}\-]+", s) and not re.search(r"[.?!:]\s*$", s) and s[:1].islower():
        return False                                                               # a phrase list entry / key
    return bool(re.search(r"[.?!।]\s*$", s) or (s[:1].isupper() and " " in s))


def _sentences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        # docstrings
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            skip.add(id(node.body[0].value))
        # logs, raised errors, warnings, asserts: developer text (the allow-list of the spec)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _LOG_CALLS or name.endswith(("Error", "Exception", "Denied")) or name == "HTTPException":
                for sub in ast.walk(node):
                    skip.add(id(sub))
        if isinstance(node, (ast.Raise, ast.Assert)):
            for sub in ast.walk(node):
                skip.add(id(sub))
        # field(description=...) / Field(...) docs for the OpenAPI
        if isinstance(node, ast.keyword) and node.arg in ("description", "summary", "detail", "message", "examples"):
            for sub in ast.walk(node.value):
                skip.add(id(sub))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip \
                and _is_sentence(node.value):
            found.append(" ".join(node.value.split())[:160])
    return found


def _scan() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for base in SCANNED:
        for path in ([base] if base.is_file() else sorted(base.rglob("*.py"))):
            rel = path.relative_to(ROOT).as_posix()
            found = _sentences(path)
            if found:
                out[rel] = sorted(set(found))
    return out


def test_no_new_hardcoded_sentences():
    found = _scan()
    if os.getenv("WRITE_HARDCODED_BASELINE") == "1":
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({k: v for k, v in found.items() if k not in STRICT}, indent=1,
                                       ensure_ascii=False), encoding="utf-8")
    strict = {k: v for k, v in found.items() if k in STRICT}
    assert not strict, f"master-spec modules must take their words from config: {strict}"
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    new = {k: sorted(set(v) - set(baseline.get(k, []))) for k, v in found.items() if k not in STRICT}
    new = {k: v for k, v in new.items() if v}
    assert not new, f"new hardcoded user-facing sentences (move them to config): {json.dumps(new, indent=1)}"


def test_the_baseline_is_reported():
    """The legacy count the final report states (an open item: it should only go down)."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    assert sum(len(v) for v in baseline.values()) < 5000


# ---- config changes change behaviour ------------------------------------------------------------------------------
from tests.integration.master_env import make_case, prod, say  # noqa: E402,F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: E402,F401


def test_page_size_7(client, prod, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_list

    for i in range(9):
        make_case(client, f"Person{i} Kumar")
    monkeypatch.setitem(case_list.cfg(), "page_size", 7)
    md = say(client, "my cases")
    assert md.count("(action:open_case?id=") == 7 and "Showing 1-7 of 9" in md


def test_a_new_synonym_for_a_list_filter(client, prod, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_list

    make_case(client, "Rahul Sharma")
    phrases = dict(case_list.cfg()["phrases"])
    phrases["filter"] = {**phrases["filter"], "kyc_issue": list(phrases["filter"]["kyc_issue"]) + ["kyc gadbad wale"]}
    monkeypatch.setitem(case_list.cfg(), "phrases", phrases)
    md = say(client, "kyc gadbad wale")
    assert "match (" in md and "issue)" in md, md                          # read as the KYC-issue filter


def test_the_tts_sentence_limit(client, prod, monkeypatch):
    from app.agents.applicant.copilot.answering import contract

    make_case(client, "Rahul Sharma")
    monkeypatch.setitem(contract.cfg()["tts"], "max_sentences", 1)
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "my cases",
                                                 "reply_language": "en"}).json()
    assert len(re.findall(r"[.!?](?:\s|$)", r["tts"])) == 1, r["tts"]


def test_an_action_link_label(client, prod, monkeypatch):
    from app.agents.applicant.copilot.answering import contract

    _, c = make_case(client, "Rahul Sharma")
    links = dict(contract.cfg()["action_links"])
    links["open_case"] = {**links["open_case"], "labels": {"en": "Kholo ji"}}
    monkeypatch.setitem(contract.cfg(), "action_links", links)
    assert f"[Kholo ji](action:open_case?id={c})" in say(client, "my cases")


def test_a_reply_wording(client, prod, monkeypatch):
    from app.agents.applicant import config

    make_case(client, "Rahul Sharma")
    labels = dict(config.chatbot("case_workspace")["labels"])
    labels["not_found_applicant"] = {"en": "No such applicant among your cases."}
    monkeypatch.setitem(config.chatbot("case_workspace"), "labels", labels)
    assert say(client, "APP-FFFFFFFFFFFF ke cases") == "No such applicant among your cases."
