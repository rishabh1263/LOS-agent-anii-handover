"""
EACH DOCUMENT NAMED ONCE (2026-10-11, audit P2-C): the checklist's Signature slot and the mandatory-signature rule
are ONE document -- the customer draft asked for "Signature" twice and the case row counted 5 documents for 4.
Real stored records, synthetic applicant.
"""
from __future__ import annotations

import re

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def test_the_customer_draft_names_each_document_once(client, prod):
    from app.agents.applicant.copilot.answering import officer_tools

    _, case_id = make_case(client, "Draft Person")
    message = officer_tools.customer_message(case_id) or ""
    names = [n.strip(" .") for n in re.split(r",|:", message.split("share", 1)[-1]) if n.strip(" .")]
    listed = [n for n in names if n in ("PAN", "Address Proof", "Bank Statement", "Signature", "Income Proof")]
    assert listed and len(listed) == len(set(listed)), message


def test_the_case_row_counts_distinct_documents(client, prod):
    from app.agents.applicant.copilot.capabilities import workspace
    from app.store import get_repository

    _, case_id = make_case(client, "Row Person")
    label, kind = workspace._row_status(get_repository().get_application(case_id))
    pending = int(re.search(r"(\d+)", label).group(1))
    checklist = {"PAN", "ADDRESS_PROOF", "BANK_STATEMENT", "SIGNATURE"}       # personal loan: required, all missing
    assert kind == "DOCS" and pending == len(checklist), label


def test_the_visit_checklist_lists_each_document_once(client, prod):
    from app.agents.applicant.copilot.answering import officer_tools

    _, case_id = make_case(client, "Visit Person")
    items = [ln for ln in (officer_tools.visit_checklist(case_id) or "").split("\n") if ln.startswith("- [ ]")]
    assert items and len(items) == len(set(items)), items
