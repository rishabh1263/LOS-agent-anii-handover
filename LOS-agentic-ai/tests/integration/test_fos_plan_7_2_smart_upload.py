"""
FOS PLAN 7.2 -- smart upload: a file uploaded inside a case without saying what it is is identified (the existing
classification), said whose it looks like (name on the document vs the parties) and where it was filed; one
question when the type is unknown or the name fits the other party better. Nothing is moved silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.applicant.copilot.capabilities import smart_upload
from tests.integration.test_frontend_contract import demo, make_case  # noqa: F401
from tests.integration.test_reupload_supersedes import RISHABH_PAN, _store, client  # noqa: F401


def _bytes(source):
    return Path(source).read_bytes() if isinstance(source, (str, Path)) else source


def test_an_unnamed_upload_inside_a_case_says_what_whose_and_where(client, demo, monkeypatch):
    monkeypatch.setenv("COPILOT_SMART_UPLOAD", "true")
    _, c = make_case(client, "Rishabh Singh")
    client.post("/api/v1/fos/copilot", json={"action": "OPEN_CASE", "case_id": c})
    r = client.post("/api/v1/fos/copilot", data={"action": "UPLOAD_DOCUMENT"},
                    files=[("files", ("scan.jpg", _bytes(RISHABH_PAN), "application/octet-stream"))])
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "This looks like the applicant's PAN. Filed under Applicant > PAN." in answer, answer


def _case(_store, client):
    a, c = make_case(client, "Priya Verma")
    return c


@pytest.fixture
def two_parties(client, demo, _store, monkeypatch):
    from app.agents.los import co_applicants

    c = _case(_store, client)
    monkeypatch.setattr(co_applicants, "list_for_case",
                        lambda case_id, repository=None: [{"name": "Rishabh Singh", "co_applicant_id": "COAPP-AB12"}])
    return c


def test_a_name_that_fits_the_other_party_asks_one_question(two_parties):
    lines, question, actions = smart_upload.describe(
        two_parties, [{"source_id": "pan.jpg", "document_type": "PAN", "verification": "PASS",
                       "name_on_document": "RISHABH SINGH"}], filed_as_co=False)
    assert question["reason"] == "UPLOAD_PARTY_UNSURE" and "co-applicant" in question["question"]
    assert actions[0]["co_applicant_id"] == "COAPP-AB12" and not lines


def test_an_unknown_type_asks_which_document(two_parties):
    _, question, _ = smart_upload.describe(two_parties, [{"source_id": "x.jpg", "document_type": "UNKNOWN",
                                                          "verification": "FAIL"}], filed_as_co=False)
    assert question["reason"] == "UPLOAD_TYPE_UNKNOWN" and "PAN" in question["options"]


def test_the_own_name_files_quietly(two_parties):
    lines, question, _ = smart_upload.describe(
        two_parties, [{"source_id": "pan.jpg", "document_type": "PAN", "verification": "REVIEW",
                       "name_on_document": "PRIYA VERMA"}], filed_as_co=False)
    assert question is None
    assert lines == ["This looks like the applicant's PAN. Filed under Applicant > PAN. It needs a review."]
