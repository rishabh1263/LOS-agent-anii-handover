"""
NEGATIVE TESTS OF THE ELIGIBILITY API (2026-10-15, production readiness): every bad, broken or hostile input gets a
clean 4xx that names the problem -- never a 500, never a stack trace / SQL / path in the reply, never a decision.
Auth: no token -> 401, a token without agents:execute -> 403. The data source is a fake (no database needed).
"""

from __future__ import annotations

import pytest

from app.agents.eligibility.domain.models import EligibilityInput

CASE = "CASE-NEG0000001"
LEAKS = ("Traceback", "File \"", "psycopg", "SELECT ", "site-packages", "KeyError", "TypeError", "ConfigMissing")


class _FakeProvider:
    """One known case; any other id is not found."""

    def get(self, case_id: str) -> EligibilityInput:
        from app.agents.eligibility.data.providers import CaseNotFound

        if case_id != CASE:
            raise CaseNotFound(case_id)
        return EligibilityInput(case_id=CASE, product="HOME_LOAN", age=40, net_monthly_income=80000.0,
                                existing_emi=20000.0, bureau_score=790, property_value=12000000.0,
                                requested_amount=8000000.0, tenure_months=240, rate_percent=9.0,
                                max_dpd_12m=0, write_offs=0, settlements=0)


@pytest.fixture
def client(make_token, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from app.agents.eligibility import agent
    from app.agents.eligibility.data import providers, runs

    monkeypatch.delenv("ELIGIBILITY_DATA_PROVIDER", raising=False)
    monkeypatch.setattr(providers, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(agent, "get_provider", lambda: _FakeProvider())      # agent.py holds its own reference
    monkeypatch.setattr(runs, "enabled", lambda: False)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['agents:execute'])}"})
    return c


def _clean(response, *codes):
    text = response.text
    assert response.status_code in codes, (response.status_code, text[:300])
    assert not [leak for leak in LEAKS if leak in text], text[:300]
    assert '"decision"' not in text or response.status_code < 400, text[:300]


BASE = {"case_id": CASE, "use_saved": False}


def test_a_good_check_works_with_the_fake_case(client):
    r = client.post("/api/v1/eligibility/check", json=BASE)
    assert r.status_code == 200 and r.json()["decision"] in ("APPROVED", "CONDITIONAL")


@pytest.mark.parametrize("case_id", ["", "   ", "x" * 200, "CASE-1' OR '1'='1", "x'; DROP TABLE eligibility_cases;--",
                                     "CASE\u0000-1", "../../etc/passwd", "केस-१२३"])
def test_a_bad_case_id_is_refused_before_anything_is_read(client, case_id):
    _clean(client.post("/api/v1/eligibility/check", json={"case_id": case_id}), 422)


def test_an_unknown_case_is_404_never_a_decision(client):
    _clean(client.post("/api/v1/eligibility/check", json={"case_id": "CASE-NOT-THERE"}), 404)


@pytest.mark.parametrize("change", [
    {"requested_amount": -500000}, {"requested_amount": 0}, {"requested_amount": "ten lakh"},
    {"requested_amount": 1e300}, {"tenure_months": 0}, {"tenure_months": -12}, {"tenure_months": 10000},
    {"tenure_months": 12.5}, {"rate_percent": 0}, {"rate_percent": 500}, {"rate_percent": -9},
    {"product": "CAR_LOAN"}, {"collateral_type": "PALACE"}, {"occupancy_type": "HAUNTED"}, {"existing_exposure": -1},
])
def test_bad_loan_terms_are_422(client, change):
    _clean(client.post("/api/v1/eligibility/check", json={**BASE, **change}), 422)


@pytest.mark.parametrize("income", [
    {"method": "LOTTERY"}, {"gross_salary": 1}, {"method": None},
    {"method": "ITR", "income_as_per_itr": 1, "gross_salary": 5}, {"method": "ITR", "tax": 100},
    {"method": "SALARIED", "gross_salary": -95000}, {"method": "SALARIED", "gross_salary": "abc"},
    {"method": "SALARIED", "gross_salary": 1e20}, {"method": "SALARIED", "gross_salary": 1, "bonus": 5},
    {"method": "SALARIED", "gross_salary": 1, "salary_credit_mode": "UPI"},
    {"method": "GROSS_PROFIT", "turnover": 1, "gross_margin": 1, "audited": "MAYBE"},
    {"method": "SALARIED", "gross_salary": 1, "company_name": "x" * 5000},
    {"method": "SALARIED", "gross_salary": 1, "total_experience_years": 500},
])
def test_bad_income_blocks_are_422(client, income):
    _clean(client.post("/api/v1/eligibility/check", json={**BASE, "income": income}), 422)


@pytest.mark.parametrize("topup", [
    {"type": "MAGIC"}, {"type": "BT_TOPUP", "pos": 1}, {"type": "BT_TOPUP", "pos": 1, "mob": -1},
    {"type": "BT_TOPUP", "pos": 1, "mob": 100000},
    {"type": "INTERNAL_TOPUP", "previous_sanction": 100, "pos": 500, "mob": 30, "original_tenor_months": 120},
    {"type": "BT_TOPUP", "pos": 1000, "mob": 30, "applied_pct": 150},
    {"type": "BT_TOPUP", "pos": 1, "mob": 30, "lan_number": "X"},
    {"type": "BT_TOPUP", "pos": 2000000, "mob": 30, "original_amount": 1500000},
])
def test_bad_topups_are_422(client, topup):
    _clean(client.post("/api/v1/eligibility/check", json={**BASE, "topup": topup}), 422)


def test_a_loss_is_a_valid_profit_after_tax(client):
    r = client.post("/api/v1/eligibility/check", json={**BASE, "income": {"method": "CASH_PROFIT",
                                                                            "profit_after_tax": -100000}})
    assert r.status_code == 200 and r.json()["decision"] == "REJECTED"


@pytest.mark.parametrize("raw", [b"not json at all", b"[1, 2, 3]", b"",
                                 b'{"case_id": "CASE-NEG0000001", "requested_amount": NaN}',
                                 b'{"case_id": "CASE-NEG0000001", "requested_amount": Infinity}'])
def test_broken_bodies_are_4xx(client, raw):
    _clean(client.post("/api/v1/eligibility/check", content=raw, headers={"content-type": "application/json"}),
           400, 422)


@pytest.mark.parametrize("path", ["/api/v1/eligibility/cases/..%2F..%2Fetc%2Fpasswd",
                                  "/api/v1/eligibility/cases/CASE-1'%20OR%20'1'='1",
                                  "/api/v1/eligibility/runs/'%20OR%201=1%20--",
                                  "/api/v1/eligibility/cases/" + "x" * 300])
def test_hostile_paths_are_refused(client, path):
    _clean(client.get(path), 404, 422)


@pytest.mark.parametrize("body", [{"income": "x", "topup": 5}, {"income": {"method": "X"}},
                                  {"income": {"method": "SALARIED", "gross_salary": {"a": [1, 2]}}},
                                  {"income": {"method": "ITR", "income_as_per_itr": "1e400"}}])
def test_the_preview_never_breaks_and_never_returns_infinity(client, body):
    r = client.post("/api/v1/eligibility/preview", json=body)
    _clean(r, 200, 422)
    assert "Infinity" not in r.text and "NaN" not in r.text


def test_the_summary_and_save_validate_like_the_check(client):
    _clean(client.post("/api/v1/eligibility/explain", json={"case_id": ""}), 422)
    _clean(client.post("/api/v1/eligibility/explain/stream", json={"case_id": "CASE-NOT-THERE"}), 404)
    _clean(client.post("/api/v1/eligibility/explain/stream", json={**BASE, "income": {"method": "ZZZ"}}), 422)
    _clean(client.put(f"/api/v1/eligibility/cases/{CASE}/inputs", json={"income": {"method": "NOPE"}}), 422)


def test_no_token_is_401_and_a_token_without_the_scope_is_403(make_token, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    bare = TestClient(main.app)
    for path, body in (("/api/v1/eligibility/check", BASE), ("/api/v1/eligibility/preview", {}),
                       ("/api/v1/eligibility/explain", BASE)):
        assert bare.post(path, json=body).status_code == 401
        weak = bare.post(path, json=body, headers={"Authorization": f"Bearer {make_token(scopes=['read_applicant'])}"})
        assert weak.status_code == 403, (path, weak.status_code)
    assert bare.get("/api/v1/eligibility/form").status_code == 401
