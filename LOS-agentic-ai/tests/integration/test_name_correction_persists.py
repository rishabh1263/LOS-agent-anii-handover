"""
A misspelled applicant name, corrected, is the name every downstream read uses (master prompt §9, 2026-10-10).

Persisted-state checks, not a changed input field: the DB row, the form re-fetch, the case list the officer saw
BEFORE the edit (its per-user row cache), the chat list, and the KYC application-name check all read the corrected
name. The PAN/OCR name never overwrites it. Synthetic applicants only.
"""
from __future__ import annotations

import re

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_master_15_product_flow import FOS, _href, turn
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def _listed_names(client) -> list[str]:
    return [r["applicant_name"] for r in client.get("/api/v1/fos/cases").json()["rows"]]


def test_a_name_corrected_on_the_form_is_the_name_everywhere(client, prod):
    from app.store import get_repository

    applicant_id, case_id = make_case(client, "Rahul Shrma")
    assert "Rahul Shrma" in _listed_names(client)              # the list is read (and cached) BEFORE the edit
    turn(client, "show my cases", chat="nc")
    r = client.put(f"/api/v1/fos/cases/{case_id}/form", json={"changes": {"full_name": "Rahul Sharma"}})
    assert r.status_code < 300, r.text

    assert get_repository().get_applicant(applicant_id).full_name == "Rahul Sharma"          # persisted
    assert client.get(f"/api/v1/fos/cases/{case_id}/form").json()["values"]["full_name"] == "Rahul Sharma"
    names = _listed_names(client)
    assert "Rahul Sharma" in names and "Rahul Shrma" not in names, names                      # no stale list row
    listed = turn(client, "show my cases", chat="nc")["markdown"]
    assert "Rahul S." in listed and "Shrma" not in listed

    from app.agents.los import kyc_gate

    match = kyc_gate.application_name_match(case_id, applicant_id, "PRIMARY_APPLICANT", get_repository())
    assert match["declared"] == "Rahul Sharma"                 # the KYC name check reads the corrected name


def test_a_name_corrected_in_chat_is_persisted_after_confirm(client, prod):
    from app.store import get_repository

    applicant_id, case_id = make_case(client, "Priya Vrma")
    assert "Priya Vrma" in _listed_names(client)
    turn(client, f"open {case_id}", chat="nc2")
    proposal = turn(client, "change applicant name to Priya Verma", chat="nc2")["markdown"]
    assert "Priya Verma" in proposal and "Confirm" in proposal
    assert get_repository().get_applicant(applicant_id).full_name == "Priya Vrma"            # nothing before Confirm
    done = client.post(FOS, json={"action_link": _href(proposal, "Confirm"), "chat_id": "nc2", "reply_language": "en"})
    assert done.status_code == 200 and "Updated" in done.json()["markdown"], done.text
    assert get_repository().get_applicant(applicant_id).full_name == "Priya Verma"
    names = _listed_names(client)
    assert "Priya Verma" in names and "Priya Vrma" not in names, names
    events = client.get(f"/api/v1/fos/cases/{case_id}/activity").json()["events"]
    assert any(e["type"] == "FORM_UPDATED" and re.search(r"full_name", e["summary"]) for e in events)


# ---- concurrent edits, stale screens, duplicates, failed writes (master prompt §9) ------------------------------
def test_two_edits_of_different_fields_at_once_both_survive(client, prod):
    """save() used to write whole records read earlier: the slower writer put the other's field back."""
    import threading

    from app.agents.applicant.copilot.capabilities import case_form
    from app.store import get_repository

    applicant_id, case_id = make_case(client, "Kiran Ptil")
    claims = {"sub": "test-subject"}
    barrier = threading.Barrier(2)

    def edit(changes):
        barrier.wait()
        case_form.save(case_id, changes, claims, "req-par", "ui")

    threads = [threading.Thread(target=edit, args=({"full_name": "Kiran Patil"},)),
               threading.Thread(target=edit, args=({"mobile": "9822012345"},))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    person = get_repository().get_applicant(applicant_id)
    assert person.full_name == "Kiran Patil" and person.mobile == "9822012345"


def test_a_stale_form_does_not_overwrite_a_newer_correction(client, prod):
    from app.store import get_repository

    applicant_id, case_id = make_case(client, "Meena Jshi")
    first = client.put(f"/api/v1/fos/cases/{case_id}/form",
                       json={"changes": {"full_name": "Meena Joshi"}, "expected": {"full_name": "Meena Jshi"}})
    assert first.status_code == 200 and first.json()["changed"] == ["full_name"], first.text
    # a second screen, opened before the correction, still shows the misspelling
    stale = client.put(f"/api/v1/fos/cases/{case_id}/form",
                       json={"changes": {"full_name": "Meena Joshee"}, "expected": {"full_name": "Meena Jshi"}})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "STALE_FORM", stale.text
    assert stale.json()["detail"]["current"] == {"full_name": "Meena Joshi"}
    assert get_repository().get_applicant(applicant_id).full_name == "Meena Joshi"          # nothing written


def test_a_duplicate_request_writes_and_logs_once(client, prod):
    _, case_id = make_case(client, "Anil Kmar")
    body = {"changes": {"full_name": "Anil Kumar"}}
    one = client.put(f"/api/v1/fos/cases/{case_id}/form", json=body)
    two = client.put(f"/api/v1/fos/cases/{case_id}/form", json=body)
    assert one.json()["changed"] == ["full_name"] and two.status_code == 200 and two.json()["changed"] == []
    events = client.get(f"/api/v1/fos/cases/{case_id}/activity").json()["events"]
    assert sum(1 for e in events if e["type"] == "FORM_UPDATED") == 1


def test_a_failed_write_leaves_neither_half(client, prod, monkeypatch):
    """The applicant UPDATE succeeds, the application UPDATE fails: both roll back."""
    import pytest as _pytest

    from app.store import get_repository
    from app.store.repository import RepositoryError

    applicant_id, case_id = make_case(client, "Sunil Rao")
    repo = get_repository()
    conn = repo._connect()
    real = type(conn).execute

    def failing(self, sql, args=()):
        if sql.lstrip().upper().startswith("UPDATE APPLICATIONS"):
            from app.store.sql_repo import StoreDbError

            raise StoreDbError("simulated failure")
        return real(self, sql, args)

    monkeypatch.setattr(type(conn), "execute", failing)
    with _pytest.raises(RepositoryError):
        repo.update_case_fields(case_id, {"full_name": "Sunil Raao"}, {"loan_amount": "650000"})
    monkeypatch.undo()
    assert repo.get_applicant(applicant_id).full_name == "Sunil Rao"
    assert float(repo.get_application(case_id).loan_amount) == 500000


def test_a_chat_confirm_after_someone_else_changed_the_name_is_refused(client, prod):
    from app.store import get_repository

    applicant_id, case_id = make_case(client, "Ravi Vrma")
    turn(client, f"open {case_id}", chat="st")
    proposal = turn(client, "change applicant name to Ravi Verma", chat="st")["markdown"]
    # meanwhile another officer corrects it differently on the form
    assert client.put(f"/api/v1/fos/cases/{case_id}/form",
                      json={"changes": {"full_name": "Ravi Varma"}}).status_code == 200
    done = client.post(FOS, json={"action_link": _href(proposal, "Confirm"), "chat_id": "st", "reply_language": "en"})
    assert done.status_code == 200 and "Not saved" in done.json()["markdown"], done.text
    assert get_repository().get_applicant(applicant_id).full_name == "Ravi Varma"
