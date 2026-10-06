"""
EVERY UPLOADED DOCUMENT'S EXTRACTED DATA, ON REQUEST (user request, 2026-10-06), through
the real HTTP API on real samples: each document type lists every field read from it
(identifiers masked, amounts in rupees), "saare documents ki details" lists them all, and
"doc"/"docs" works like "documents".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.applicant.copilot.facts.document_facts import _rupees
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import _store, ask, client  # noqa: F401

S = Path("samples")
DOCS = [("pan.jpg", S / "documents/rpan.jpg", "PAN"), ("dl.jpg", S / "documents/driving_license.jpg", "DRIVING_LICENCE"),
        ("voter.jpg", S / "documents/voter_id2.jpg", "VOTER_ID"), ("slip.pdf", S / "real_batch/salary_slip.pdf", "SALARY_SLIP"),
        ("bank.pdf", S / "real_batch/bank_hdfc_new.pdf", "BANK_STATEMENT"), ("itr.pdf", S / "documents/ITR.pdf", "ITR")]
pytestmark = pytest.mark.skipif(not all(p.exists() for _, p, _ in DOCS), reason="real samples not present")
UNMASKED_ID = re.compile(r"\b[A-Z]{3}\d{7}\b|\b[A-Z]{5}\d{4}[A-Z]\b")      # a raw EPIC or PAN


@pytest.fixture
def case(client):
    a, c = open_case(client)
    upload(client, a, c, [(n, p) for n, p, _ in DOCS], [t for _, _, t in DOCS])
    return a, c


@pytest.mark.parametrize("question,must", [
    ("driving licence ki details", ["Licence number", "Valid till", "Guardian"]),
    ("voter id me kya likha hai?", ["Voter ID number", "Relative", "Gender"]),
    ("salary slip ki details batao", ["Net pay: ₹", "Gross pay: ₹", "Pay period", "Employer"]),
    ("bank statement ki details", ["Account holder", "Statement from", "Closing balance: ₹"]),
    ("ITR ki details", ["Assessment year", "Acknowledgement number", "Total income: ₹"]),
    ("PAN se kya details mila?", ["PAN number", "Father's name", "Date of birth"]),
])
def test_each_document_lists_every_field_read(client, case, question, must):
    answer = ask(client, *case, question)
    missing = [m for m in must if m not in answer]
    assert not missing, (question, missing, answer)
    assert not UNMASKED_ID.search(answer), answer                   # identifiers are always masked


def test_all_documents_details_one_section_each(client, case):
    for question in ("saare documents ki details", "all document details", "documents ka data dikhao"):
        answer = ask(client, *case, question)
        for heading in ("PAN (verified)", "Driving licence (verified)", "Voter ID (verified)",
                        "Salary slip (verified)", "Bank statement (verified)", "ITR (verified)"):
            assert heading in answer, (question, heading)
        assert not UNMASKED_ID.search(answer)


@pytest.mark.parametrize("question,must", [("show all docs", "Documents on this application"),
                                           ("docs verify hue?", "verified"),
                                           ("doc pending hai kya?", "baaki")])
def test_doc_and_docs_mean_documents(client, case, question, must):
    assert must in ask(client, *case, question)


def test_rupees_are_indian_grouped():
    assert _rupees(602420) == "₹6,02,420" and _rupees("29866") == "₹29,866"
    assert _rupees(5285.57) == "₹5,285.57" and _rupees(999) == "₹999" and _rupees("n/a") == "n/a"
    assert _rupees(12345678) == "₹1,23,45,678"
