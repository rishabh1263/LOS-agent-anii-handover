"""
USER-REPORTED WRONG ANSWERS (2026-10-06), through the real HTTP API:

  "Why is the signature under review?"  was answered with the CASE's hold (a name
                                        mismatch) instead of the signature's own reason
  "case fos me hai ?"                   was answered with the review reason, not the stage
  "what about PAN"                      was not understood without an earlier turn
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.semantics.intents import classify
from tests.integration.test_fos_stage_boundary import open_case, upload
from tests.integration.test_reupload_supersedes import OTHER_PAN, RISHABH_DL, _store, ask, client  # noqa: F401
from tests.integration.test_signature_chat import _signature


@pytest.mark.parametrize("question,intent,doc", [
    ("Why is the signature under review?", "DOCUMENT_VERIFICATION", "SIGNATURE"),
    ("signature review kyun hai?", "DOCUMENT_VERIFICATION", "SIGNATURE"),
    ("case fos me hai ?", "APPLICATION_STAGE", None),
    ("is the case at FOS?", "APPLICATION_STAGE", None),
    ("case kis stage me hai?", "APPLICATION_STAGE", None),
    ("what about PAN", "DOCUMENT_VERIFICATION", "PAN"),
    # neighbours that must NOT move
    ("why is the application in review?", "CASE_HISTORY", None),
    ("what is FOS?", "STAGE_PROCESS", None),
    ("what happens at CPA?", "STAGE_PROCESS", None),
])
def test_classification(question, intent, doc):
    c = classify(question)
    assert c.intent.value == intent and (doc is None or c.document_type == doc), (question, c)


def test_the_answers_are_about_what_was_asked(client, tmp_path):
    a, c = open_case(client)
    upload(client, a, c, [("pan.jpg", OTHER_PAN), ("dl.jpg", RISHABH_DL),
                          ("sign.jpg", _signature(tmp_path / "sign.jpg"))], ["PAN", "DRIVING_LICENCE", "SIGNATURE"])
    signature = ask(client, a, c, "Why is the signature under review?")
    assert signature.startswith("Signature") and "reference signature" in signature, signature
    stage = ask(client, a, c, "case fos me hai ?")
    assert "FOS" in stage and "does not match" not in stage, stage
    pan = ask(client, a, c, "what about PAN")
    assert pan.startswith("PAN"), pan
