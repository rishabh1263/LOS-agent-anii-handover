"""
"top 2 cases", "last 3", "pichle 3 case dikhao", "show 2 cases", "last case" ... -- the case list at the size asked
(case_list.understand; case_list.yaml phrases). Six cases on file; every wording must show exactly N rows.
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

NAMES = ("Rahul Sharma", "Priya Verma", "Aniket Patil", "Sneha Joshi", "Vikas Rao", "Meena Iyer")
SIZED = [("top 2 cases", 2), ("last 3 cases", 3), ("last 3", 3), ("show top 2 cases", 2),
         ("give me last 3 cases", 3), ("latest 2 cases", 2), ("first 3 cases", 3), ("oldest 2 cases", 2),
         ("recent 4 cases", 4), ("mere last 3 case", 3), ("pichle 3 case dikhao", 3), ("top 2 case dikhao", 2),
         ("show 2 cases", 2), ("last two cases", 2), ("top three cases", 3), ("my last 3 cases", 3),
         ("2 cases dikhao", 2), ("last 3 pending cases", 3), ("last case", 1), ("latest case", 1),
         ("most recent case", 1), ("first case", 1), ("show me only 3 cases", 3), ("newest 3", 3),
         ("aakhri 2 cases", 2), ("sirf 4 case dikhao", 4), ("oldest case", 1)]


def rows(md: str) -> int:
    return sum(1 for line in md.split("\n") if line.startswith("| ") and "CASE-" in line)


@pytest.fixture
def six(client, prod):
    for name in NAMES:
        make_case(client, name)
    return client


def ask(c, message, chat):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                                            "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def test_every_wording_shows_the_size_asked(six):
    wrong = []
    for i, (message, n) in enumerate(SIZED):
        md = ask(six, message, f"topn-{i}")
        if rows(md) != n or "Which case is this about" in md:
            wrong.append(f"{message!r}: {rows(md)} rows (want {n}): {md[:120]!r}")
    assert not wrong, "\n".join(wrong)


def test_kyc_filter_with_a_count(six):
    md = ask(six, "top 2 kyc cases", "topn-kyc")
    assert "KYC" in md and rows(md) <= 2


@pytest.mark.parametrize("message", ["ek case ka status batao", "what is the status of 2 cases ago", "ek case banao"])
def test_a_count_is_not_always_a_list(six, message):
    assert "Showing 1-1 of 6" not in ask(six, message, f"neg-{message}")


def test_a_bare_top_n_asks_which_order_then_shows_that_many(six):
    # owner decision A (2026-10-08): "top 5" alone -- top by what? one question, the pick shows 5
    asked = ask(six, "top 5", "topn-bare")
    assert "needing action" in asked and rows(asked) == 0
    assert rows(ask(six, "1", "topn-bare")) == 5
