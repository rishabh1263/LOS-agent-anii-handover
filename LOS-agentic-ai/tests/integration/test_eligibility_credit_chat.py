"""
ELIGIBILITY AND CREDIT IN THE CHAT (owner 2026-10-12): the FOS officer can ask anything on the topic and gets the
RIGHT kind of answer -- never a crash, never the wrong figure:

    the recorded eligibility verdict (DEMO / not-signed-off labelled; read, never recomputed)
    a policy limit from the configured DEMO policy (provider swappable: ELIGIBILITY_POLICY_PROVIDER)
    "how much can he get" / the eligible amount / credit / risk / memo -> the configured route to the credit team
      (applicant_agent.yaml `denied` + `routing`: the FOS agent never serves a credit decision or risk analysis)

On the credit eval suite's FULL synthetic case (evals/credit/suite.py), whose eligibility record is in an older
shape ("foir": "0.35") -- the answer must still not crash.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.testing import fresh_repository
from evals.credit import suite
from tests.integration.master_env import prod  # noqa: F401


@pytest.fixture
def say(tmp_path, monkeypatch, make_token, prod):
    import main
    from tests.integration.test_reupload_supersedes import FOS_SCOPES

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    repository = fresh_repository(tmp_path / "el.sqlite3")
    repository.initialise()
    set_repository(repository)
    suite.build(repository, suite.Setup(result=suite.FULL), stage="CPA")
    repository.grant_access(suite.OFFICER, "APPLICANT", suite.APP)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(subject=suite.OFFICER, scopes=FOS_SCOPES)}"})

    def ask(message):
        from app.agents.applicant.copilot.capabilities import safety

        safety.reset()
        r = c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                                                "case_id": suite.CASE, "applicant_id": suite.APP, "chat_id": "el"})
        assert r.status_code == 200, r.text                       # never a crash
        return r.json()["markdown"]
    yield ask
    set_repository(None)


@pytest.mark.parametrize("q", ["is this customer eligible?", "what is the FOIR on this case?",
                               "why is the customer not eligible?", "which income was used for eligibility?",
                               "kya income kaafi hai loan ke liye", "what obligations were considered?"])
def test_the_recorded_eligibility_verdict_answers(say, q):
    md = say(q)
    assert "Eligibility" in md and "PASS" in md and "not yet signed-off" in md, md


@pytest.mark.parametrize("q", ["how much loan can he get?", "maximum eligible loan amount kitna hai",
                               "how much can the customer borrow?"])
def test_the_eligible_amount_is_routed_to_the_credit_team(say, q):
    md = say(q)
    assert "credit team" in md and "5,00,000" not in md.split("\n")[0], md


@pytest.mark.parametrize("q, where", [("credit assessment ka result kya hai", "Credit process"),
                                      ("credit score kitna hai", "Credit process"),
                                      ("credit memo dikhao", "Credit process"),
                                      ("what is the risk on this case?", "Risk process")])
def test_credit_and_risk_are_routed_never_guessed(say, q, where):
    md = say(q)
    assert where in md and "Do you mean" not in md and "don't know" not in md.lower(), md


def test_a_policy_limit_comes_from_the_demo_policy(say):
    md = say("what is the max LTV?")
    assert "LTV" in md and "DEMO policy" in md, md


def test_an_older_record_shape_never_crashes():
    from app.agents.applicant import eligibility_facts

    assert eligibility_facts._ratio("FOIR", "0.35") == "FOIR: 0.35 (as recorded)."
    assert eligibility_facts._ratio("LTV", None) == "LTV: not computed."
