"""
PHASE 3 STEP 6i -- case actions (COPILOT_CASE_ACTIONS, default off; docs/STEP6I_CASE_ACTIONS.md).

VIEW_DOCUMENT (signed 5-minute link, same subject only, re-checked, no path), RAISE QUERY (a draft from
the actual reason; created ONLY after Send), no customer channel -> copyable text + MARK_QUERY_SENT,
TRACK QUERY, NEW CASE (a UI button, nothing created in chat), out of scope -> 403, flag off -> 422.
"""

from __future__ import annotations

import pytest

from app.store.models import Document, DocumentStatus
from tests.integration.test_fos_stage_boundary import FOS_SCOPES, open_case
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

COPILOT = "/api/v1/fos/copilot"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COPILOT_CASE_ACTIONS", "true")


def post(client, status=200, **body):
    r = client.post(COPILOT, json=body)
    assert r.status_code == status, r.text
    return r.json()


def rejected_pan(store, applicant_id, case_id):
    doc = Document(document_id=f"{case_id}:{applicant_id}:pan1", case_id=case_id, applicant_id=applicant_id,
                   document_type="PAN", status=DocumentStatus.REJECTED, reason_codes=["DOCUMENT_UNREADABLE"])
    store.save_document(doc)
    from app.store.documents import get_document_store

    get_document_store().put(doc.document_id, b"%PDF-1.4 test bytes", content_type="application/pdf")
    return doc


def test_flag_off_refuses_the_actions(client):
    a, c = open_case(client)
    post(client, 422, applicant_id=a, case_id=c, action="LIST_QUERIES")


def test_new_case_is_a_ui_button_only(client, on):
    a, c = open_case(client)
    body = post(client, applicant_id=a, case_id=c, message="naya case banana hai")
    assert body["intent"] == "NEW_CASE" and body["actions"] == [{"type": "OPEN_UI_NEW_CASE", "label": "New case"}]


def test_view_document_gives_a_signed_link_that_only_the_same_person_can_open(client, on, _store, make_token):
    a, c = open_case(client)
    doc = rejected_pan(_store, a, c)
    body = post(client, applicant_id=a, case_id=c, action="VIEW_DOCUMENT", document_id=doc.document_id)
    url = body["actions"][0]["url"]
    assert url.startswith("/api/v1/fos/documents/view?token=") and doc.document_id not in url and "\\" not in url
    assert body["actions"][0]["expires_in"] == 300
    opened = client.get(url)
    assert opened.status_code == 200 and opened.content == b"%PDF-1.4 test bytes"
    mine = client.headers.copy()
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    assert client.get(url).status_code == 403                                   # another person: refused
    client.headers.clear()
    client.headers.update(mine)
    assert client.get(url + "x").status_code == 403                             # tampered: refused


def test_an_expired_link_is_refused(client, on, _store, monkeypatch):
    a, c = open_case(client)
    doc = rejected_pan(_store, a, c)
    url = post(client, applicant_id=a, case_id=c, action="VIEW_DOCUMENT", document_id=doc.document_id)["actions"][0]["url"]
    import time as _time

    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 301)
    assert client.get(url).status_code == 403


def test_raise_query_drafts_first_and_creates_only_after_send(client, on, _store):
    a, c = open_case(client)
    rejected_pan(_store, a, c)
    from app.agents.los import queries

    draft = post(client, applicant_id=a, case_id=c, message="query raise karo")
    assert draft["intent"] == "RAISE_QUERY_DRAFT" and "Draft query" in draft["answer"]
    assert queries.list_queries(c) == []                                        # nothing created by a draft
    send = next(x for x in draft["actions"] if x["label"] == "Send")
    created = post(client, applicant_id=a, case_id=c, action="RAISE_QUERY", confirm=True, query=send["query"])
    assert created["intent"] == "RAISE_QUERY" and len(queries.list_queries(c)) == 1
    # no customer channel: a copyable message + "mark as sent"
    mark = next(x for x in created["actions"] if x["type"] == "MARK_QUERY_SENT")
    assert mark["copy_text"] == send["query"]["text"] and "Mark as sent" in created["answer"]
    done = post(client, applicant_id=a, case_id=c, action="MARK_QUERY_SENT", query_id=mark["query_id"])
    assert done["intent"] == "MARK_QUERY_SENT"
    assert queries.list_queries(c)[0]["status"] == "OPEN"                        # the lifecycle is not changed


def test_raise_without_confirm_never_creates(client, on, _store):
    a, c = open_case(client)
    rejected_pan(_store, a, c)
    from app.agents.los import queries

    post(client, applicant_id=a, case_id=c, action="RAISE_QUERY", query={"text": "x", "target_type": "CASE",
                                                                         "query_type": "CLARIFICATION"})
    assert queries.list_queries(c) == []


def test_track_queries_lists_them(client, on, _store):
    a, c = open_case(client)
    rejected_pan(_store, a, c)
    draft = post(client, applicant_id=a, case_id=c, message="query raise karo")
    post(client, applicant_id=a, case_id=c, action="RAISE_QUERY", confirm=True,
         query=next(x for x in draft["actions"] if x["label"] == "Send")["query"])
    body = post(client, applicant_id=a, case_id=c, message="queries dikhao")
    assert body["intent"] == "LIST_QUERIES" and body["queries"][0]["status"] == "OPEN"
    assert body["queries"][0]["days_open"] == 0


def test_another_persons_case_is_refused(client, on, make_token):
    mine = client.headers.copy()
    client.headers.update({"Authorization": f"Bearer {make_token(subject='someone-else', scopes=FOS_SCOPES)}"})
    a, c = open_case(client)
    client.headers.clear()
    client.headers.update(mine)
    post(client, 403, applicant_id=a, case_id=c, action="LIST_QUERIES")
