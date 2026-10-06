"""
A RE-UPLOAD REPLACES, IT DOES NOT ACCUMULATE (user bug report, 2026-10-05).

Uploaded PAN + licence in the chat, asked KYC: right. Refreshed the chat (same
case), uploaded again: KYC compared THREE PANs of three different people --
every earlier verified document of the party joined the comparison unless it
had the same FILE NAME -- and answered with a chain of "but the PAN says ...",
the document list showed the PAN twice, "pan details" gave no details, and the
answers carried internal scores ("The recorded score is 8").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.test_fos_stage_boundary import FOS_SCOPES, open_case, upload  # noqa: F401
from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.testing import fresh_repository

RISHABH_PAN = Path("samples/documents/rpan.jpg")
RISHABH_DL = Path("samples/documents/driving_license.jpg")
OTHER_PAN = Path("samples/lPan.jpg")
pytestmark = pytest.mark.skipif(
    not (RISHABH_PAN.exists() and RISHABH_DL.exists() and OTHER_PAN.exists()),
    reason="real samples not present")

#: What a chat answer must never carry: file names, storage paths, internal
#: codes, scores.
_BACKEND = re.compile(r"\.(jpe?g|png|pdf)\b|runtime[/\\]|uploads[/\\]|[A-Z]+_[A-Z_]{3,}|\bscore\b|confidence\s+\d",
                      re.I)


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.delenv("FOS_KYC_ON_UPLOAD", raising=False)
    monkeypatch.delenv("APPLICANT_AGENT_SHOW_SCORES", raising=False)
    agent_config.reload()
    repository = fresh_repository(tmp_path / "reupload.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(make_token):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


def ask(client, applicant_id, case_id, message):
    r = client.post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                 "action": "CUSTOM_QUERY", "message": message})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert not _BACKEND.search(answer), answer
    return answer


def _kyc_sources(body):
    return sorted({s["source_id"] for f in body["kyc"].get("fields") or [] for s in f.get("sources") or []})


def test_a_new_pan_replaces_the_old_one_after_a_chat_refresh(client, _store):
    applicant_id, case_id = open_case(client)
    first = upload(client, applicant_id, case_id, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)],
                   ["PAN", "DRIVING_LICENCE"]).json()
    assert first["kyc"]["status"] == "PASS"
    assert not _BACKEND.search(first["answer"]), first["answer"]
    assert ask(client, applicant_id, case_id, "what is kyc status") == "Your KYC check passed."

    # the chat is refreshed; the same case gets another person's PAN under another file name
    second = upload(client, applicant_id, case_id, [("pan_new.jpg", OTHER_PAN)], ["PAN"]).json()
    assert not _BACKEND.search(second["answer"]), second["answer"]

    # KYC compared the NEW PAN with the licence -- the replaced PAN took no part
    assert _kyc_sources(second) == ["dl.jpg", "pan_new.jpg"]
    assert second["kyc"]["status"] == "REVIEW" and "NAME_MISMATCH" in second["kyc"]["reason_codes"]

    kyc = ask(client, applicant_id, case_id, "what is kyc status")
    assert "RISHABH AJIT SINGH" in kyc and "LAXMI SANTOSH GUPTA" in kyc
    assert kyc.count("but the") == 3                  # one "but" per field: name, DOB, father
    assert "says RISHABH AJIT SINGH, but the driving licence says RISHABH" not in kyc

    # one PAN on the case; the replaced one kept for audit as SUPERSEDED
    listed = ask(client, applicant_id, case_id, "which documents are uploaded")
    assert listed.count("PAN") == 1, listed
    stored = _store.list_documents(case_id, include_superseded=True)
    assert sorted(d.status.value for d in stored if d.document_type == "PAN") == ["SUPERSEDED", "VERIFIED"]

    # "pan details" gives the details of the CURRENT PAN, identifier masked
    for question in ("give me pan details", "pan details", "PAN ki details"):
        details = ask(client, applicant_id, case_id, question)
        assert "LAXMI SANTOSH GUPTA" in details and "RISHABH" not in details
        assert "XXXXXX" in details and "date of birth" in details.lower()


def test_two_pans_in_one_upload_are_both_current_and_kyc_reports_the_conflict(client, _store):
    applicant_id, case_id = open_case(client)
    body = upload(client, applicant_id, case_id, [("a.jpg", RISHABH_PAN), ("b.jpg", OTHER_PAN)],
                  ["PAN", "PAN"]).json()
    assert _kyc_sources(body) == ["a.jpg", "b.jpg"]
    assert body["kyc"]["status"] == "REVIEW"
    assert all(d.status.value != "SUPERSEDED" for d in _store.list_documents(case_id, include_superseded=True))


def test_kyc_details_are_field_by_field_and_never_contradict_the_result(client):
    """'what is kyc details' (user report 2026-10-05): a PASSED KYC listed address, PAN and
    income as 'did not match' -- they were NOT COMPARED (one document each); the Hinglish
    reply dropped the fields and read out the score; 'kyc detail batao' was not understood."""
    applicant_id, case_id = open_case(client)
    upload(client, applicant_id, case_id, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)],
           ["PAN", "DRIVING_LICENCE"])
    for question in ("what is kyc details", "kyc details", "give me kyc details"):
        answer = ask(client, applicant_id, case_id, question)
        assert answer.startswith("Your KYC check passed. Field by field:"), answer
        assert "- name: matched (on the PAN and driving licence)" in answer
        assert "did not match" not in answer
        assert "address: not compared (only the driving licence carries it)" in answer
    for question in ("kyc ki details", "kyc detail batao", "kyc details kya hai"):
        answer = ask(client, applicant_id, case_id, question)
        assert answer.startswith("Aapka KYC complete ho gaya hai."), answer
        assert "- name: matched" in answer and "score" not in answer.lower()

    # a mismatch: the differing fields say so, with what each document says
    upload(client, applicant_id, case_id, [("pan2.jpg", OTHER_PAN)], ["PAN"])
    answer = ask(client, applicant_id, case_id, "kyc details")
    assert answer.startswith("Your KYC check needs review. Field by field:"), answer
    assert "- name: did not match across the PAN and driving licence" in answer
    assert "What differs:" in answer and "LAXMI SANTOSH GUPTA" in answer
