"""
PENDING WORK (copilot/capabilities/work.py), without HTTP:

  - every item is classified from its RECORDED state: COMPLETED / PROCESSING
    / SYSTEM / USER / REVIEWER; an optional missing slot is not "pending"
  - a queued background job makes a provisional verdict SYSTEM work
  - the wording never claims "done" for what is not recorded as done
  - nothing needs anyone -> said plainly, and only then
  - the action registry decides what the assistant may run by itself
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.applicant.copilot.capabilities import work


class Repo:
    def __init__(self, kyc=(), queued=()):
        self._kyc, self._queued = list(kyc), set(queued)

    def get_current_findings(self, case_id, kind=None):
        return self._kyc if kind == "KYC" else []

    def get_ocr_jobs(self, case_id):
        from app.store.ocr_queue import OcrJobStatus

        return [SimpleNamespace(document_id=d, status=OcrJobStatus.QUEUED) for d in self._queued]


def _doc(document_id, doc_type, status, verdict=None, codes=(), party="PRIMARY_APPLICANT"):
    return {"document_id": document_id, "document_type": doc_type, "status": status,
            "verification_status": verdict, "reason_codes": list(codes), "party_role": party}


CHECKLIST = [{"slot": "ADDRESS_PROOF", "status": "MISSING", "mandatory": True, "accepts": ["PASSPORT"]},
             {"slot": "FORM_16", "status": "MISSING", "mandatory": False, "requirement": "OPTIONAL"}]


def _items(documents, checklist=CHECKLIST, repo=None, can_run=True):
    return work.items_of(documents, checklist, repository=repo or Repo(), case_id="C", can_run=can_run)


def test_each_item_is_owned_by_whoever_moves_it():
    items = _items([_doc("d1", "PAN", "VERIFIED", "PASS"),
                    _doc("d2", "BANK_STATEMENT", "UPLOADED"),
                    _doc("d3", "DRIVING_LICENCE", "REVIEW", "REVIEW", ["NAME_MISMATCH"]),
                    _doc("d4", "PAN", "REJECTED", "FAIL", ["DOCUMENT_TYPE_MISMATCH"], party="CO_APPLICANT")],
                   repo=Repo(kyc=[SimpleNamespace(party_id="P", status="REVIEW", reason_codes=["NAME_MISMATCH"])]))
    owners = {(i["item"], i["owner"]) for i in items}
    assert ("PAN", "COMPLETED") in owners and ("BANK_STATEMENT", "SYSTEM") in owners
    assert ("DRIVING_LICENCE", "REVIEWER") in owners and ("PAN", "USER") in owners
    assert ("ADDRESS_PROOF", "USER") in owners and ("KYC", "REVIEWER") in owners
    assert not any(i["item"] == "FORM_16" for i in items)          # optional + absent: not pending


def test_a_queued_job_makes_a_provisional_verdict_system_work():
    items = _items([_doc("d2", "BANK_STATEMENT", "REVIEW", "REVIEW", ["STATEMENT_DEFERRED"])],
                   checklist=[], repo=Repo(queued={"d2"}))
    assert items[0]["owner"] == "SYSTEM" and items[0]["executable"] is True


def test_nothing_is_ever_called_done_when_it_is_not_recorded_done():
    items = _items([_doc("d2", "BANK_STATEMENT", "PROCESSING")], checklist=[])
    answer, _ = work.compose(work.DO, items, executed={"d2": "PROCESSING"}, can_run=True)
    assert "Still being verified" in answer
    assert not any(w in answer.lower() for w in ("done", "completed all", "all complete"))


def test_nothing_pending_is_said_only_when_nothing_is_pending():
    clean = _items([_doc("d1", "PAN", "VERIFIED", "PASS")], checklist=[])
    answer, _ = work.compose(work.HEALTH, clean, executed={}, can_run=True)
    assert "Everything currently required is complete" in answer
    busy = _items([_doc("d1", "PAN", "VERIFIED", "PASS")])
    answer, _ = work.compose(work.HEALTH, busy, executed={}, can_run=True)
    assert "Everything currently required is complete" not in answer and "Address Proof" in answer


def test_what_can_you_do_offers_only_what_it_can_run():
    items = _items([_doc("d2", "BANK_STATEMENT", "UPLOADED")], checklist=[])
    answer, offer = work.compose(work.CAN, items, executed={}, can_run=True)
    assert "I can verify" in answer and offer["options"] == ["verify all pending documents"]
    items = _items([_doc("d2", "BANK_STATEMENT", "UPLOADED")], checklist=[], can_run=False)
    answer, offer = work.compose(work.CAN, items, executed={}, can_run=False)
    assert offer is None and "nothing I can run" in answer


@pytest.mark.parametrize("message,mode", [
    ("jo pending hai kar do", work.DO), ("do whatever is pending", work.DO),
    ("handle everything", work.DO), ("sab complete karo", work.DO),
    ("abhi kya kar sakte ho?", work.CAN), ("what can you do for me now?", work.CAN),
    ("everything okay with my application?", work.HEALTH), ("koi issue hai kya", work.HEALTH),
    ("what still needs me?", work.NEEDS_ME),
    ("what can you do?", None), ("kis case mein issue hai?", None), ("ab mujhe kya karna hai", None),
])
def test_the_request_kinds(message, mode):
    assert work.request(message) == mode


def test_the_registry_decides_what_runs_unasked():
    assert work.may_run("VERIFY_DOCUMENT", {"upload_document"}) is True
    assert work.may_run("VERIFY_DOCUMENT", {"read_documents"}) is False
    assert work.may_run("UPLOAD_DOCUMENT", {"upload_document"}) is False      # needs the person's file
    assert work.may_run("STAGE_TRANSITION", {"los.write"}) is False           # confirmation required
    assert work.may_run("NOT_AN_ACTION", {"upload_document"}) is False
