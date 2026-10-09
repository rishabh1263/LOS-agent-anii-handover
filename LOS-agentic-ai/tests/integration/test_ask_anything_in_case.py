"""
INSIDE AN OPENED CASE, UPLOAD AND ASK ANYTHING (user, 2026-10-07: "docu upload, case related kuch bhi puch sakta
hu, makkhan chalna chahiye"). Every demo flag on, as in production use. The officer opens a case once; after that
no applicant_id / case_id is sent -- not for questions and not for an upload.
"""

from __future__ import annotations

import pytest

from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import RISHABH_DL, RISHABH_PAN, _store, client  # noqa: F401

COPILOT = "/api/v1/fos/copilot"


def _bytes(source):
    from pathlib import Path

    return Path(source).read_bytes() if isinstance(source, (str, Path)) else source


@pytest.fixture
def opened(client, demo):
    a, c = make_case(client, "Rishabh Singh")
    assert client.post(COPILOT, json={"action": "OPEN_CASE", "case_id": c}).json()["intent"] == "CASE_OPENED"
    return a, c


def ask(client, message):
    r = client.post(COPILOT, json={"action": "CUSTOM_QUERY", "message": message})
    assert r.status_code == 200, r.text
    return r.json()


def test_an_upload_without_ids_goes_to_the_opened_case(client, opened):
    a, c = opened
    r = client.post(COPILOT, data={"action": "UPLOAD_DOCUMENT", "document_types": ["PAN", "DRIVING_LICENCE"]},
                    files=[("files", ("pan.jpg", _bytes(RISHABH_PAN), "application/octet-stream")),
                           ("files", ("dl.jpg", _bytes(RISHABH_DL), "application/octet-stream"))])
    assert r.status_code == 200, r.text
    assert r.json()["case_id"] == c
    uploaded = ask(client, "kaunse documents upload hue?")
    assert uploaded["intent"] == "DOCUMENTS_UPLOADED" and "Driving Licence" in uploaded["answer"]


def test_an_upload_without_ids_and_no_open_case_is_still_refused(client, demo):
    r = client.post(COPILOT, data={"action": "UPLOAD_DOCUMENT"},
                    files=[("files", ("pan.jpg", _bytes(RISHABH_PAN), "application/octet-stream"))])
    assert r.status_code == 422 and r.json()["detail"]["error"] == "INVALID_REQUEST"


@pytest.mark.parametrize("message, intent", [
    ("kyu atka hai?", None),                         # why the CASE is stuck -- never "why" of the last answer
    ("CPA mein kab jayega?", "READINESS"),
    ("ready hai kya?", "READINESS"),
    ("co-applicant hai kya?", "APPLICANT_PROFILE"),
    ("is there a co-applicant?", "APPLICANT_PROFILE"),
    ("which documents are uploaded?", "DOCUMENTS_UPLOADED"),
    ("kya baaki hai?", "DOCUMENTS_PENDING"),
    ("loan kitna hai?", "APPLICANT_PROFILE"),
])
def test_case_questions_are_routed(client, opened, message, intent):
    ask(client, "KYC ka kya status hai?")            # a KYC answer first: "kyu atka" must not follow it
    body = ask(client, message)
    if intent:
        assert body["intent"] == intent, (message, body["intent"], body["answer"])
    else:
        assert body["intent"] not in ("KYC_RESULT", "UNKNOWN"), body["answer"]
    # answered for the OPENED case; the "📍 CASE-x" line is only shown when the case changes (config
    # case_workspace.case_header: on_change, owner 2026-10-09), so the case is read from the envelope
    assert body["case_id"] == opened[1], (message, body.get("case_id"))


def test_a_passed_kyc_gets_no_fix_the_mismatch_step(client, opened, _store):
    from app.store.models import CaseFinding, FindingKind

    a, c = opened
    _store.save_finding(CaseFinding(finding_id="k-pass", case_id=c, party_id=a, finding_kind=FindingKind.KYC,
                                    status="PASS", reason_codes=[], content_hash="k-pass"))
    answer = ask(client, "KYC ka kya status hai?")["answer"]
    assert "fix the mismatch" not in answer
