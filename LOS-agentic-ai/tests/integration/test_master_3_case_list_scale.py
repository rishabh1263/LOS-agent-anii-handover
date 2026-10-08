"""
MASTER SPEC section 3 -- 200+ cases: one page per chat reply (default 5, max 20), server-side paging with a
separate count, filters / search / sort, the NL phrases from config, "aur dikhao" remembered, the paginated
endpoint with the same scope, and a cache so paging does not recompute 200 statuses.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from app.store.models import Applicant, Application, Document, DocumentStatus, utcnow
from tests.integration.test_frontend_contract import demo  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

FOS = "/api/v1/fos/copilot"
N = 205


@pytest.fixture
def many(_store, demo, monkeypatch):
    for flag in ("COPILOT_CASE_LIST_PAGING", "COPILOT_MD_TTS_CONTRACT", "COPILOT_LANGUAGE_LOCK",
                 "COPILOT_PROFESSIONAL_FORMAT"):
        monkeypatch.setenv(flag, "true")
    from app.agents.applicant.copilot.capabilities import case_list

    case_list.clear_cache()
    start = utcnow() - timedelta(days=N)
    cases = []
    for i in range(N):
        a, c = f"APP-{i:012X}", f"CASE-{i:012X}"
        when = start + timedelta(days=i)
        _store.save_applicant(Applicant(applicant_id=a, full_name=f"Person{i} Kumar", mobile="9876543210",
                                        date_of_birth="1990-01-01", address="1 MG Road, Pune"))
        _store.save_application(Application(case_id=c, applicant_id=a, product="PERSONAL_LOAN",
                                            loan_amount="500000", created_at=when, updated_at=when))
        _store.grant_access("test-subject", "CASE", c)
        cases.append((a, c))
    a, c = cases[7]                                                   # one case with every document verified
    for slot in ("PAN", "ADDRESS_PROOF", "BANK_STATEMENT"):
        _store.save_document(Document(document_id=f"{c}:{slot}", case_id=c, applicant_id=a, party_id=a,
                                      document_type=slot, status=DocumentStatus.VERIFIED))
    return cases


def ask(client, message, **extra):
    r = client.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", **extra})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def rows(md: str) -> list[str]:
    return [ln for ln in md.split("\n") if ln.startswith("| ") and "action:open_case" in ln]


def test_list_all_is_a_summary_one_page_and_showing_x_of_n(client, many):
    started = time.perf_counter()
    md = ask(client, "list all case")
    cold = time.perf_counter() - started
    assert len(rows(md)) == 5 and f"of {N}" in md and "You have" in md and "action:list_more" in md, md
    started = time.perf_counter()
    more = ask(client, "aur dikhao")
    warm = time.perf_counter() - started
    assert "Showing 6-10" in more and len(rows(more)) == 5
    assert warm < max(1.0, cold / 2), (cold, warm)                    # the statuses come from the cache


@pytest.mark.parametrize("message,first_row", [("last 3", "CASE-0000000000CC"), ("sabse purane", "CASE-000000000000")])
def test_order_phrases(client, many, message, first_row):
    md = ask(client, message)
    assert rows(md)[0].split(" | ")[1] == first_row, md
    if message == "last 3":
        assert len(rows(md)) == 3


def test_top_n_caps_at_the_max_page(client, many):
    assert len(rows(ask(client, "top 50 cases"))) == 20


def test_ready_wale_filter(client, many, monkeypatch):
    from app.agents.applicant.copilot.capabilities import workspace

    real = workspace._row_status
    monkeypatch.setattr(workspace, "_row_status", lambda a: ("Ready for CPA", "READY")
                        if a.case_id == "CASE-000000000007" else real(a))
    md = ask(client, "ready wale")
    assert "1 of your cases match" in md and "CASE-000000000007" in md, md


def test_agle_5_pages_the_same_filter(client, many):
    ask(client, "pending docs wale")
    md = ask(client, "agle 5")
    assert "Showing 6-10" in md and "match (documents pending)" in md, md


def test_a_row_number_on_the_second_page_opens_that_row(client, many):
    ask(client, "sabse purane")
    ask(client, "aur dikhao")
    md = ask(client, "7")
    assert "CASE-000000000006" in md.split("\n")[0] or "CASE-000000000006" in md


def test_the_page_size_comes_from_config(client, many, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_list

    monkeypatch.setitem(case_list.cfg(), "page_size", 7)
    assert len(rows(ask(client, "my cases"))) == 7


def test_the_panel_endpoint_pages_searches_and_keeps_scope(client, many, make_token):
    body = client.get("/api/v1/fos/cases", params={"size": 20, "page": 1, "sort": "oldest"}).json()
    assert body["total"] == N and len(body["rows"]) == 20 and body["rows"][0]["case_id"] == "CASE-000000000014"
    found = client.get("/api/v1/fos/cases", params={"search": "Person12 Kumar"}).json()
    assert [r["case_id"] for r in found["rows"]] == ["CASE-00000000000C"]
    assert client.get("/api/v1/fos/cases", params={"size": 500}).json()["size"] == 20
    from fastapi.testclient import TestClient

    import main

    other = TestClient(main.app)
    other.headers.update({"Authorization": f"Bearer {make_token(subject='other-officer', scopes=FOS_SCOPES)}"})
    assert other.get("/api/v1/fos/cases").json()["total"] == 0


def test_a_plain_order_is_a_sql_page_with_a_separate_count(client, many, _store, monkeypatch):
    seen = {}
    real = _store.list_granted_cases

    def spy(subject, **kw):
        seen.setdefault("limits", []).append(kw.get("limit"))
        return real(subject, **kw)

    monkeypatch.setattr(_store, "list_granted_cases", spy)
    client.get("/api/v1/fos/cases", params={"sort": "recent", "size": 5})
    assert seen["limits"] == [5]                                     # never the 205 rows
