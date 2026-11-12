"""
THE CLEAN CARD (2026-10-13; answering/card.py): the same facts, easier to read -- fixed icons on the engine's own bold
status words, one Next line, housekeeping lines dropped, the KYC tables summarised unless the detail is asked for.
"""
from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import card


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_CARD", "true")


def test_a_status_word_gets_its_fixed_icon_and_the_housekeeping_line_goes():
    out = card.apply("**PAN** is **verified**. Its issuing authority has not been independently confirmed.",
                     {"intent": "DOCUMENT_VERIFICATION"})
    assert "✅ **verified**" in out and "issuing authority" not in out


def test_one_next_line():
    out = card.apply("**Next step:** The next step is to collect and upload the missing document: Address Proof.")
    assert out == "👉 **Next:** Collect and upload the missing document: Address Proof."


def test_the_buttons_are_never_touched():
    links = "[Upload Address Proof](action:upload?doc=ADDRESS_PROOF)"
    out = card.apply(f"Still pending:\n- **Address Proof**\n\n{links}", {"intent": "PENDING_ITEMS"})
    assert out.endswith(links)


def test_the_name_match_preamble_is_dropped_from_a_pending_list():
    md = ('Application name matches the verified PAN (Permanent Account Number): "A" = "A".\n'
          "- Name verified: A (matches on PAN). Saved on the application.\nStill pending:\n- **Signature**")
    out = card.apply(md, {"intent": "PENDING_ITEMS"})
    assert out.startswith("**Still pending:**") and "Name verified" not in out


@pytest.mark.parametrize("question, table", [("and what about KYC?", False), ("KYC details table dikhao", True)])
def test_the_kyc_table_only_when_the_detail_is_asked_for(question, table):
    md = "The customer's KYC check **passed**.\n\n**KYC, Applicant**\n\n| Field | Result |\n|---|---|\n| Name | Match |"
    out = card.apply(md, {"intent": "KYC_RESULT", "_question": question})
    assert ("| Name | Match |" in out) is table
    assert "**The customer" not in out              # never a bold line nested around a bold word


def test_no_fact_is_added_or_changed():
    md = "CPA readiness: 5 of 12 checks passed.\n- Address Proof: **PENDING**, not uploaded yet."
    out = card.apply(md, {"intent": "READINESS"})
    assert "5 of 12" in out and "Address Proof" in out and "⏳ **pending**" in out


def test_off_is_unchanged(monkeypatch):
    monkeypatch.setenv("COPILOT_CARD", "false")
    md = "**PAN** is **verified**."
    assert card.apply(md) == md
