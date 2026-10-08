"""
GENERAL QUESTION COVERAGE (evals/general_questions.yaml): every question a field officer asks with NO case open.

The reply is classified from its markdown: "case" (asked which case), "unknown" (said it does not know), "decline"
(a decision the bot does not make), "not_configured" (a company number the config does not hold), else "answer".
The report is written to runs/general_coverage.md; the test fails on any question whose class differs from its
`expect` (not_configured and decline also accept each other: both say who decides, neither guesses).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
BANK = yaml.safe_load((ROOT / "evals" / "general_questions.yaml").read_text(encoding="utf-8"))["categories"]

CLASSES = [
    ("case", re.compile(r"Which case is this about|Open a case first|which case", re.I)),
    ("unknown", re.compile(r"I don't know|I do not know|not in the knowledge base|isn't in the knowledge base|don't have (?:that|this) "
                           r"information|couldn't find|could not find|no answer", re.I)),
    ("not_configured", re.compile(r"^- [^\n]*not set in the eligibility|^No [^.\n]+ is configured", re.I | re.M)),
    ("clarify", re.compile(r"^Do you mean|Whose name do you mean|For which product", re.I | re.M)),
    ("decline", re.compile(r"I don't make credit decisions|credit team decides|is decided by|I can't (?:approve|reject|decide)|"
                           r"cannot (?:approve|reject|decide)|not something I decide|a person must", re.I)),
]
SAME = {("not_configured", "decline"), ("decline", "not_configured")}


def classify(md: str) -> str:
    for name, pattern in CLASSES:
        if pattern.search(md):
            return name
    return "answer"


@pytest.fixture
def gaps(tmp_path, monkeypatch):
    from app.agents.applicant.copilot.capabilities import general

    data = dict(general.cfg())
    monkeypatch.setattr(general, "cfg", lambda: {**data, "gaps_file": str(tmp_path / "gaps.yaml")})


def test_general_question_coverage(client, prod, gaps):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    rows, wrong, full = [], [], []
    for category, items in BANK.items():
        for n, item in enumerate(items):
            from app.agents.applicant.copilot.capabilities import safety

            safety.reset()                     # the rate limit is not what this bank measures
            r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": item["q"],
                                                          "reply_language": "en", "chat_id": f"cov-{category}-{n}"})
            md = r.json().get("markdown", r.text) if r.status_code == 200 else f"HTTP {r.status_code}"
            got = classify(md) if r.status_code == 200 else "error"
            # the golden check: an "answer" must carry the item's key words, else it answered something else
            missing = [w for w in item.get("must") or [] if w.lower() not in md.lower()]
            if got == "answer" and missing:
                got = f"wrong_content (missing {', '.join(missing)})"
            ok = got == item["expect"] or (got, item["expect"]) in SAME
            first = " ".join(ln for ln in md.split("\n") if ln.strip() and not ln.startswith("|"))[:170]
            rows.append((category, item["q"], item["expect"], got, ok, first))
            full.append(f"### {item['q']}  ({category}, expect {item['expect']}, got {got})\n\n{md}\n")
            if not ok:
                wrong.append(f"{category}: {item['q']!r} expected {item['expect']}, got {got}: {first}")
    total, good = len(rows), sum(1 for r in rows if r[4])
    lines = [f"# General question coverage: {good}/{total}", "", "| category | ok | question | expect | got | reply |",
             "|---|---|---|---|---|---|"]
    for cat in BANK:
        mine = [r for r in rows if r[0] == cat]
        lines.insert(2 + list(BANK).index(cat), f"- {cat}: {sum(1 for r in mine if r[4])}/{len(mine)}")
    lines += [f"| {c} | {'✅' if ok else '❌'} | {q} | {e} | {g} | {f.replace('|', '/')} |" for c, q, e, g, ok, f in rows]
    out = ROOT / "runs" / "general_coverage.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out.parent / "general_coverage_replies.md").write_text("\n".join(full), encoding="utf-8")
    assert not wrong, f"{len(wrong)}/{total} wrong (runs/general_coverage.md):\n" + "\n".join(wrong)
