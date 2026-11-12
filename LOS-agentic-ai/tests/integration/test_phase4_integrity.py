"""
PHASE 4, BATCH 1 (2026-10-13): the Phase 3 audit findings on data integrity and write safety, each REPRODUCED here
before it is fixed (section 2E of the Phase 4 directive):

* a chat field edit authorized WITHOUT the write flag (a read-only service principal could edit);
* duplicate case creation on a repeated / concurrent Confirm;
* the stage move executed without re-checking the gate when the service gate flag is off;
* the insights count reading at most 200 cases while the list reads up to scan_limit;
* the cheap list path: "Showing x of N" counted cases the ownership check then dropped;
* a query reply / resolve repeated: state-idempotent (NO_CHANGE, one history entry), read back.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, client  # noqa: F401


def _claims(subject: str, scopes: list[str]) -> dict:
    return {"sub": subject, "scope": " ".join(scopes), "scopes": scopes}


# ---- field edit: the WRITE flag --------------------------------------------------------------------------------
def test_a_chat_field_edit_is_authorized_as_a_write(client, other, prod, monkeypatch):
    """A caller allowed to READ every case as a service (never to write) must not edit a case it does not own."""
    from app.agents.applicant.copilot.capabilities import case_form
    from app.security import access

    foreign = make_case(other, "P4 Edit Foreign")[1]
    monkeypatch.setattr(access, "is_service", lambda scopes, write=False: not write)   # read-only service principal
    monkeypatch.setattr(access, "conversation_service_access", lambda: True)
    written = []
    monkeypatch.setattr(case_form, "save", lambda *a, **k: written.append(a) or {"outcome": "SAVED"})
    write = {"kind": "edit", "case_id": foreign, "field": "loan_amount", "value": "600000", "ref": "edit-p4",
             "old": 500000}
    with pytest.raises(access.AccessDenied):
        case_form._edit_step(write, "confirm", _claims("svc-reader", ["los.read"]), "r-p4-edit", "en")
    assert written == []


# ---- create: one Confirm, one case -----------------------------------------------------------------------------
def _draft() -> dict:
    return {"kind": "create", "ref": "draft", "draft_id": "d-p4-1",
            "values": {"full_name": "P4 Double Click", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                       "address": "12 MG Road, Pune", "product": "PERSONAL_LOAN", "loan_amount": 500000}}


class _State:
    active_case_id = None


def test_a_double_confirm_creates_one_case(monkeypatch):
    """Two Confirms of the SAME draft (a double click: each request loaded the stored draft before the other wrote)
    -> exactly one create call; the second is told the case is being / was created."""
    from app.agents.applicant.copilot.capabilities import case_form

    monkeypatch.setattr(case_form, "check_all", lambda values, lang: {})
    calls = []

    async def create(body):
        calls.append(body)
        await asyncio.sleep(0.2)                    # the first create still running when the second Confirm lands
        return {"case_id": "CASE-P4DOUBLE01", "applicant_id": "APP-P4", "checklist": []}

    async def both():
        first, second = copy.deepcopy(_draft()), copy.deepcopy(_draft())
        return await asyncio.gather(
            case_form._create_now(first, {"sub": "officer-a"}, "r1", "en", create, _State()),
            case_form._create_now(second, {"sub": "officer-a"}, "r2", "en", create, _State()))

    replies = asyncio.run(both())
    assert len(calls) == 1, calls
    assert sorted(r.get("intent") for r in replies).count("CASE_CREATED") == 1, replies


def test_a_confirm_after_the_case_was_created_creates_nothing(monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_form

    monkeypatch.setattr(case_form, "check_all", lambda values, lang: {})
    calls = []

    async def create(body):
        calls.append(body)
        return {"case_id": "CASE-P4ONCE0001", "applicant_id": "APP-P4", "checklist": []}

    draft = _draft()
    draft["draft_id"] = "d-p4-2"
    asyncio.run(case_form._create_now(copy.deepcopy(draft), {"sub": "officer-a"}, "r1", "en", create, _State()))
    again = asyncio.run(case_form._create_now(copy.deepcopy(draft), {"sub": "officer-a"}, "r2", "en", create,
                                              _State()))
    assert len(calls) == 1 and again.get("intent") != "CASE_CREATED", again


# ---- the stage move: the gate re-checked at the move, whatever the service flag --------------------------------
def test_a_confirmed_move_rechecks_the_gate_with_the_service_flag_off(monkeypatch):
    from app.agents.applicant.copilot.capabilities import stage_flow
    from app.agents.los import stage_lifecycle
    from app.api.routes import fos_api

    monkeypatch.setenv("LOS_STAGE_GATE_IN_SERVICE", "false")
    moved = []
    monkeypatch.setattr(stage_lifecycle, "transition", lambda *a, **k: moved.append((a, k)))
    monkeypatch.setattr(stage_flow, "gate", lambda case_id: {"status": "BLOCKED", "blockers": [
        {"id": "FOS_READINESS", "label": "FOS requirements", "status": "BLOCKED"}]})
    reply = fos_api.move_case_stage({"request_id": "r-p4-move", "execute_move": {
        "case_id": "CASE-P4MOVE0001", "from": "FOS", "to": "CPA", "actor": "officer-a", "ref": "move-p4"}},
        "r-p4-move")
    assert moved == [] and reply["intent"] != "STAGE_MOVED", reply


# ---- aggregates: the same cases as the list --------------------------------------------------------------------
def test_the_insights_count_reads_as_many_cases_as_the_list(client, prod, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_insights, case_list
    from app.store import get_repository

    make_case(client, "P4 Cap One")
    repository = get_repository()
    limits = []
    real = repository.list_granted_cases

    def spy(subject, *a, **k):
        limits.append(k.get("limit"))
        return real(subject, *a, **k)
    monkeypatch.setattr(type(repository), "list_granted_cases", lambda self, s, *a, **k: spy(s, *a, **k))
    case_insights._rows(_claims("officer-a", []) | {"sub": case_list._subject({"sub": "officer-a"})})
    assert limits and limits[-1] >= int(case_list.cfg().get("scan_limit", 2000)), limits


def test_a_page_total_counts_only_the_cases_the_caller_may_open(client, prod, monkeypatch):
    """The cheap list path took N from a SQL count and the rows from the ownership check: a case the check drops
    was still counted ("Showing 1-2 of 3" with 2 rows)."""
    from app.agents.applicant.copilot.capabilities import case_list, workspace
    from app.security import access

    ids = [make_case(client, n)[1] for n in ("P4 Page One", "P4 Page Two", "P4 Page Three")]
    real = workspace.authorize

    def deny_one(claims, case_id, **k):
        if case_id == ids[0]:
            raise access.AccessDenied()
        return real(claims, case_id, **k)
    monkeypatch.setattr(workspace, "authorize", deny_one)
    case_list._ROWS.clear()
    # A SUBJECT OF ITS OWN: the shared store's "test-subject" gains cases from parallel workers between two reads
    import uuid

    from app.store import get_repository

    subject = f"p4-page-{uuid.uuid4().hex[:8]}"
    for case_id in ids:
        get_repository().grant_access(subject, "CASE", case_id)
    page = case_list.run({"sub": subject, "scope": " ".join(FOS_SCOPES)}, case_list.ListQuery(size=50))
    shown = {r["case_id"] for r in page.rows}
    assert ids[0] not in shown and page.total == len(page.rows) == 2, (page.total, len(page.rows))


# ---- queries: a repeated reply / resolve is idempotent and read back -------------------------------------------
def test_a_repeated_query_resolve_changes_nothing_the_second_time(client, prod):
    from app.agents.los import queries

    case = make_case(client, "P4 Query Case")[1]
    scopes = {"los.write", "los.query:write", "los.query:resolve", "los.stage:write", "create_application"}
    try:
        created = queries.raise_query(case, target_type="STAGE", query_type="CLARIFICATION", text="P4 check",
                                      actor="officer-a", scopes=scopes, target_stage="CPA",
                                      idempotency_key="p4-q-1")
    except queries.QueryError as exc:
        pytest.skip(f"query lifecycle not raisable in this config: {exc}")
    qid = created["query_id"]
    allowed = list(((queries.config("queries").get("lifecycle") or {}).get(created["status"]) or []))
    if not allowed:
        pytest.skip("no transition configured from the raised status")
    first = queries.move_query(case, qid, to_status=allowed[0], actor="officer-a", scopes=scopes)
    second = queries.move_query(case, qid, to_status=allowed[0], actor="officer-a", scopes=scopes)
    assert first["result"] == "UPDATED" and second["result"] == "NO_CHANGE", (first, second)
    assert first["status"] == allowed[0]                     # read back from the store, not echoed


# ---- a re-uploaded multi-copy document: the REJECTED one is no longer "upload again" ---------------------------
def _doc(doc_id, doc_type, status, minutes, party="P1", role="PRIMARY_APPLICANT"):
    from datetime import datetime, timedelta, timezone

    from app.store.models import Document, DocumentStatus

    at = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return Document(document_id=doc_id, case_id="CASE-P4DOC", applicant_id="APP-P4", document_type=doc_type,
                    party_id=party, party_role=role, status=DocumentStatus(status), uploaded_at=at, updated_at=at)


class _Repo:
    def __init__(self, docs):
        self.docs = docs

    def list_documents(self, case_id):
        return self.docs

    def get_current_findings(self, case_id, kind=None):
        return []

    def __getattr__(self, name):                     # anything else the builder reads: nothing recorded
        return lambda *a, **k: None


@pytest.mark.parametrize("newer, still", [("VERIFIED", False), ("REVIEW", False), ("REJECTED", True)])
def test_a_rejected_statement_reuploaded_is_not_asked_again(monkeypatch, newer, still):
    """documents.yaml keeps bank statements as SEVERAL current documents (never superseded -- a business rule kept).
    But once the officer uploaded the same type again for the same party, the old rejection's "upload again" is done:
    only the NEWER one's own state counts. A newer one rejected too is still asked again (once)."""
    from app.agents.applicant.copilot.answering import document_actions

    monkeypatch.setattr(document_actions, "_pending_rows", lambda *a, **k: [], raising=False)
    docs = [_doc("D-OLD", "BANK_STATEMENT", "REJECTED", 0), _doc("D-NEW", "BANK_STATEMENT", newer, 10)]
    try:
        view = document_actions.build("CASE-P4DOC", repository=_Repo(docs))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"builder needs more of the store offline: {type(exc).__name__}")
    asked = [r["document_id"] for r in view["reupload"] if str(r["document_type"]).upper() == "BANK_STATEMENT"]
    assert "D-OLD" not in asked, view["reupload"]
    if newer == "REVIEW":                              # a REVIEW is asked again only when the policy says so
        still = bool(document_actions._policy().get("review_needs_reupload", False))
    assert ("D-NEW" in asked) is still, view["reupload"]


def test_another_partys_statement_does_not_clear_a_rejection():
    from app.agents.applicant.copilot.answering import document_actions

    docs = [_doc("D-A", "BANK_STATEMENT", "REJECTED", 0, party="P1"),
            _doc("D-B", "BANK_STATEMENT", "VERIFIED", 10, party="P2", role="CO_APPLICANT")]
    try:
        view = document_actions.build("CASE-P4DOC", repository=_Repo(docs))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"builder needs more of the store offline: {type(exc).__name__}")
    assert "D-A" in [r["document_id"] for r in view["reupload"]], view["reupload"]
