"""
MASTER SPEC section 5 -- ask anything inside an open case, every dev flag on: case fields, people, stage and CPA,
time, counts, document contents, why, process knowledge, multi-question, glossary both ways, out of scope.
Every reply is checked against the facts on record (never a number / id the case does not have).
"""

from __future__ import annotations

import re

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.master_env import make_case, prod, say  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def opened(client, prod, _store):
    a, c = make_case(client, "Rahul Sharma")
    _store.save_document(Document(document_id=f"{c}:{a}:pan", case_id=c, applicant_id=a, party_id=a,
                                  document_type="PAN", status=DocumentStatus.VERIFIED))
    _store.save_document(Document(document_id=f"{c}:{a}:dl", case_id=c, applicant_id=a, party_id=a,
                                  document_type="DRIVING_LICENCE", status=DocumentStatus.REJECTED,
                                  reason_codes=["DOCUMENT_UNREADABLE"]))
    say(client, f"{c} kholo")
    return a, c


CASES = [
    # (message, words the answer must contain -- any one of each tuple)
    ("loan amount kitna hai?", [("500000", "5,00,000", "5 lakh")]),
    ("applicant ka naam kya hai", [("Rahul Sharma",)]),
    ("case kis stage pe hai", [("FOS",)]),
    ("CPA ke liye ready hai?", [("CPA",), ("not", "Not", "checks passed")]),
    ("kitne documents verified hain", [("1",)]),
    ("driving licence kyu reject hua", [("unreadable", "read", "clear")]),
    ("case kab bana", [("day", "today", "2026", "created", "din")]),
    ("PAN aur bank statement ka status batao", [("PAN",), ("Bank Statement", "bank statement")]),
    ("FOIR ka matlab kya hai", [("Fixed Obligation", "obligation", "FOIR")]),
    ("address proof mein kya chalega", [("address", "Address")]),
    ("aaj mausam kaisa hai", [("case", "loan", "help", "can")]),
]


@pytest.mark.parametrize("message,needs", CASES, ids=[m for m, _ in CASES])
def test_coverage(client, opened, message, needs):
    _, c = opened
    md = say(client, message)
    for alternatives in needs:
        assert any(w in md for w in alternatives), (message, md)
    ids = set(re.findall(r"\b(?:CASE|APP)-[0-9A-F]{12}\b", md))
    assert ids <= set(opened), (message, ids)                          # no id the case does not have
    assert "Not ready for CPA" not in md or "KYC" in md or "checks" in md
