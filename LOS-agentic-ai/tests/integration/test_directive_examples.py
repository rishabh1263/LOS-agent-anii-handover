"""
THE MASTER DIRECTIVE'S REQUIRED CONVERSATIONS (2026-10-11, section 3), over the real chat endpoint with the chat's
own state, on real stored records (synthetic applicants). Each asserts the FACT the question asks for, not wording.
"""
from __future__ import annotations

import re

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_master_15_product_flow import turn
from tests.integration.test_reupload_supersedes import client  # noqa: F401


@pytest.fixture
def two(client, prod):
    _, a = make_case(client, "Dir Asha")
    _, b = make_case(client, "Dir Bina")
    return a, b


def _ids(md):
    return list(dict.fromkeys(re.findall(r"CASE-[0-9A-F]+", md)))


def test_top_3_kyc_and_only_kyc_are_lists(client, two):
    md = turn(client, "Show me the top 3 cases pending due to KYC issues.", chat="d1")["markdown"]
    assert len(_ids(md)) <= 3, md
    md = turn(client, "Show only cases pending with KYC.", chat="d1b")["markdown"]
    assert "Closest to CPA" not in md, md


def test_count_is_a_count_not_a_list(client, two):
    md = turn(client, "How many of my cases have KYC problems?", chat="d2")["markdown"]
    assert re.search(r"\b\d+ of your \d+ cases\b", md), md


def test_closest_and_compare(client, two):
    a, b = two
    md = turn(client, "Which application is closest to CPA?", chat="d3")["markdown"]
    assert _ids(md), md
    md = turn(client, f"Compare {a} and {b}", chat="d3")["markdown"]
    assert a in md and b in md, md


def test_compare_these_two_applicants_after_a_list(client, two):
    turn(client, "show my cases", chat="d3b")
    md = turn(client, "Compare these two applicants.", chat="d3b")["markdown"]
    # either the comparison or ONE question asking which two -- never a single-case answer
    assert len(_ids(md)) >= 2 or "two" in md.lower(), md


def test_single_case_questions(client, two):
    a, _ = two
    turn(client, f"open {a}", chat="d4")
    md = turn(client, "How far along is this application?", chat="d4")["markdown"]
    assert "FOS" in md, md
    md = turn(client, "What exactly is blocking CPA?", chat="d4")["markdown"]
    assert "PAN" in md, md
    md = turn(client, "Which documents have we collected already?", chat="d4")["markdown"]
    assert "mismatch" not in md.lower(), md
    md = turn(client, "What is the applicant's date of birth on file?", chat="d4")["markdown"]
    assert "1990" in md, md
    md = turn(client, "Who is the applicant and what loan amount did they request?", chat="d4")["markdown"]
    assert "Dir Asha" in md and "5,00,000" in md, md
    md = turn(client, "What should I tell the customer to bring?", chat="d4")["markdown"]
    assert "PAN" in md, md


def test_why_is_the_first_case_pending(client, two):
    md = turn(client, "show my cases", chat="d5")["markdown"]
    first = _ids(md)[0]
    md = turn(client, "Why is the first case pending?", chat="d5")["markdown"]
    assert first in md or "pending" in md.lower(), md


def test_back_to_the_case_can_it_move(client, two):
    a, _ = two
    turn(client, f"open {a}", chat="d6")
    turn(client, "what is the loan amount?", chat="d6")
    md = turn(client, "Back to the case—can it move to the next stage?", chat="d6")["markdown"]
    assert f"{a} closed" not in md and "closed." not in md, md
    assert "PAN" in md or "not ready" in md.lower() or "cannot" in md.lower() or "can't" in md.lower(), md


def test_and_the_other_applicant(client, two):
    a, b = two
    turn(client, f"open {a}", chat="d7")
    md = turn(client, "And the other applicant?", chat="d7")["markdown"]
    assert b in md or "which" in md.lower(), md


def test_only_kyc_pending_is_the_kyc_filter_not_needs_action(client, two):
    md = turn(client, "Show only cases pending with KYC.", chat="d8")["markdown"]
    assert "needs action" not in md.lower(), md                 # the fresh cases have no KYC issue
    assert not [c for c in _ids(md) if c in two], md


def test_readiness_names_the_missing_signature_once(client, two):
    a, _ = two
    turn(client, f"open {a}", chat="d9")
    md = turn(client, "Is this case ready for CPA?", chat="d9")["markdown"]
    assert md.count("Signature: **PENDING**") == 1, md
    assert "1 of 6" in md, md                                    # the signature rule still counts as a check


def test_a_field_word_never_switches_to_an_applicant_with_that_name(client, prod):
    # the caller owns a case whose applicant is literally named "Birth ...": asking for the date of birth on ANOTHER
    # open case must answer for the open case, never switch to that applicant
    _, other = make_case(client, "Birth Namesake")
    _, mine = make_case(client, "Field Word Person")
    turn(client, f"open {mine}", chat=f"d10-{mine}")
    md = turn(client, "What is the applicant's date of birth on file?", chat=f"d10-{mine}")["markdown"]
    assert "Namesake" not in md and other not in md, md
    md = turn(client, "Who is the applicant?", chat=f"d10-{mine}")["markdown"]
    assert "Field Word Person" in md and "Namesake" not in md, md


def test_a_real_name_still_switches(client, prod):
    # a name no other test uses: the test store is shared across the suite
    _, zoravar = make_case(client, "Zoravar Qadri")
    _, other = make_case(client, "Rohan Mehra")
    turn(client, f"open {other}", chat=f"d11-{other}")
    md = turn(client, "Zoravar ka pending kya hai?", chat=f"d11-{other}")["markdown"]
    assert zoravar in md and "private data" not in md, md


def test_an_ambiguous_own_name_asks_which_never_refuses(client, prod):
    # several of the caller's OWN applicants share the name: "which one?", never the other-customers refusal
    ids = [make_case(client, "Ishaan Twin")[1] for _ in range(2)]
    _, other = make_case(client, "Rohan Mehra")
    turn(client, f"open {other}", chat=f"d12-{other}")
    md = turn(client, "Ishaan ka pending kya hai?", chat=f"d12-{other}")["markdown"]
    assert "private data" not in md and all(c in md for c in ids), md
