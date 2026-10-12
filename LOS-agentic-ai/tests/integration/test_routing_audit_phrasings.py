"""
NEAREST-INTENT MIS-ROUTES (2026-10-11, audit P2-K): five phrasings the audit saw answered with the wrong fact, over
the real chat endpoint on an open case (synthetic applicant, real stored record).
"""
from __future__ import annotations

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def _md(client, app_id, case_id, message):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": app_id, "case_id": case_id, "action": "CUSTOM_QUERY",
                                                 "message": message, "reply_language": "en"})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def test_how_far_along_answers_the_stage(client, prod):
    app_id, case_id = make_case(client, "Progress Person")
    md = _md(client, app_id, case_id, "how far along is this application?")
    assert "FOS" in md and "stage" in md.lower(), md


def test_date_of_birth_answers_the_date(client, prod):
    app_id, case_id = make_case(client, "Birth Person")
    md = _md(client, app_id, case_id, "what's the customer's date of birth on file?")
    assert "1990" in md or "12-04" in md or "12 Apr" in md, md


def test_compound_question_answers_both_halves(client, prod):
    app_id, case_id = make_case(client, "Both Person")
    md = _md(client, app_id, case_id, "who is the applicant and what loan did they ask for?")
    assert "Both Person" in md and "Personal Loan" in md, md


def test_collected_documents_lists_the_uploads_not_kyc(client, prod):
    app_id, case_id = make_case(client, "Collect Person")
    md = _md(client, app_id, case_id, "which documents did we already collect?")
    assert "mismatch" not in md.lower(), md
    assert "upload" in md.lower() or "no document" in md.lower() or "none" in md.lower(), md


def test_blocking_cpa_answers_the_readiness_blockers(client, prod):
    app_id, case_id = make_case(client, "Blocked Person")
    md = _md(client, app_id, case_id, "what exactly is blocking CPA?")
    assert "PAN" in md or "pending" in md.lower() or "missing" in md.lower(), md
