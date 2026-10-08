"""
MASTER SPEC section 12 -- the self-check: the 30 conversations of selfcheck_conversations.py, every dev flag on,
on /fos/copilot under the reply contract. Wrong answers must be 0: every turn's checks, plus the global ones
(only markdown + tts; no stack trace / template braces / raw codes / emoji; tts never carries a link or a full id;
nothing of officer B). The clarify rate is reported.
"""

from __future__ import annotations

import os
import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.selfcheck_conversations import CONVERSATIONS
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

GLOBAL_NOT = [r"Traceback", r"\{[a-z_]+\}", r"\bNone\b(?! of your)", r"\bundefined\b", r"\b(ADDRESS_PROOF|BANK_STATEMENT)\b",
              "[\U0001F300-\U0001FAFF☀-➿]"]
TTS_NOT = [r"\]\(", r"\b(CASE|APP)-[0-9A-F]{8,}\b", r"\*\*", r"\|"]


@pytest.fixture
def world(client, prod, make_token, monkeypatch):
    from app.agents.applicant.copilot.capabilities import safety

    real = safety._cfg
    monkeypatch.setattr(safety, "_cfg", lambda: {**real(), "rate_limit_per_minute": 100000})
    _, rahul = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    from fastapi.testclient import TestClient

    import main

    other = TestClient(main.app)
    other.headers.update({"Authorization": f"Bearer {make_token(subject='officer-b', scopes=FOS_SCOPES)}"})
    _, theirs = make_case(other, "Zoravar Khanna")
    return {"A": rahul, "B": theirs}


def test_thirty_conversations_zero_wrong_answers(client, world):
    from app.agents.applicant.copilot.capabilities import abuse_guard

    wrong, turns, clarified = [], 0, 0
    for name, script in CONVERSATIONS:
        abuse_guard.reset()
        chat = "sc-" + name.split()[0]
        for message, must, must_not, *rest in script:
            tts_must = rest[0] if rest else []
            text = message.format(**world)
            r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text,
                                                         "reply_language": "en", "chat_id": chat})
            turns += 1
            if r.status_code != 200:
                wrong.append((name, text, f"HTTP {r.status_code}", r.text[:300]))
                continue
            body = r.json()
            md, tts = body.get("markdown", ""), body.get("tts", "")
            plain = md.replace("**", "")                                  # checks read the words, not the bold
            shown = re.sub(r"\]\((?:action|ask):[^)]*\)", "]", plain)      # link targets are not shown text
            problems = []
            if set(body) != {"request_id", "markdown", "tts"}:
                problems.append(f"fields {sorted(body)}")
            for group in must:
                if not any(re.search(p, plain, re.M) for p in group):
                    problems.append(f"missing {group}")
            for p in list(must_not) + GLOBAL_NOT:
                if re.search(p, shown):
                    problems.append(f"forbidden {p!r}")
            for p in TTS_NOT:
                if re.search(p, tts):
                    problems.append(f"tts has {p!r}")
            for group in tts_must:
                if not any(re.search(p, tts) for p in group):
                    problems.append(f"tts missing {group}")
            if re.search(r"\[[^\]\n]{1,30}\](?!\()", plain):
                problems.append("a [label] that is not a link")
            if "zoravar" in (md + tts).lower() or world["B"] in md + tts:
                problems.append("officer B's data")
            if re.search(r"(?i)which (case|one)|kaunsa|did you mean", md):
                clarified += 1
            if os.getenv("DUMP_SELFCHECK"):
                print(f"\n[{name}] >>> {text}\n{md}\n--- tts: {tts}")
            if problems:
                wrong.append((name, text, "; ".join(problems), md[:500]))
    print(f"\nself-check: {len(CONVERSATIONS)} conversations, {turns} turns, wrong={len(wrong)}, "
          f"clarify rate={clarified / max(turns, 1):.1%}")
    assert not wrong, "\n\n".join(f"[{n}] {m!r}: {p}\n{md}" for n, m, p, md in wrong)
