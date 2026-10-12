"""
"SHOW ME 2 RECENT KYC ISSUE CASES" (owner probe 2026-10-12) over the real chat endpoint, for an officer of its own
(synthetic applicants): only cases with a KYC issue, the newest first, exactly the number asked; a count is a count.
"""
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.store.models import CaseFinding, FindingKind
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store  # noqa: F401


def _kyc_fail(store, a, c):
    store.save_finding(CaseFinding(
        finding_id=f"k-{c}", case_id=c, party_id=a, finding_kind=FindingKind.KYC, status="FAIL",
        reason_codes=["NAME_MISMATCH"], content_hash=f"k-{c}",
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "RAHUL SHARMA"}, {"document_type": "BANK_STATEMENT", "value": "ROHIT VERMA"}]}]}))


@pytest.fixture
def officer(make_token, prod, _store):
    import main
    from app.agents.applicant.copilot.capabilities import case_list

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(subject='kyc-recent-' + uuid.uuid4().hex[:8], scopes=FOS_SCOPES)}"})
    made = {}
    for name in ("Old Kyc", "Mid Kyc", "Clean Case", "New Kyc"):         # created oldest -> newest
        a, cid = make_case(c, name)
        made[name] = cid
        if "Kyc" in name:
            _kyc_fail(_store, a, cid)
        time.sleep(1.1)
    case_list.clear_cache()
    return c, made


def _ask(c, made, q):
    md = c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": q, "reply_language": "en",
                                             "chat_id": "kr-" + uuid.uuid4().hex[:6]}).json()["markdown"]
    shown = list(dict.fromkeys(re.findall(r"CASE-[0-9A-F]+", md)))
    return md, [k for x in shown for k, v in made.items() if v == x]


@pytest.mark.parametrize("q", ["show me 2 recent kyc issue related isssue", "latest 2 kyc problem wale cases dikhao",
                               "top 2 newest cases with kyc mismatch", "2 recent KYC issues"])
def test_two_recent_kyc_issue_cases(officer, q):
    md, names = _ask(*officer, q)
    assert names == ["New Kyc", "Mid Kyc"], md


def test_more_than_there_are_shows_all_of_them(officer):
    md, names = _ask(*officer, "show me 5 recent kyc issues")
    assert names == ["New Kyc", "Mid Kyc", "Old Kyc"], md


def test_a_count_is_a_count(officer):
    md, _ = _ask(*officer, "how many of my cases have kyc issues?")
    assert md.startswith("3 of your 4 cases"), md
