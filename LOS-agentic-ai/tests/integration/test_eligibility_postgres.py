"""
THE ELIGIBILITY DATA IN POSTGRES (2026-10-13): every demo case of app/agents/eligibility/data/demo_seed.sql is loaded
THROUGH PostgresDataProvider and must get its `expected_decision` from the engine; NULL columns reach the engine as None;
the API assesses a case and keeps its errors apart from decisions.

The database (2026-10-28): a FRESH database on the test PostgreSQL server (app.store.testing), built the way production
builds it -- the versioned migrations (data/migrations.py), then the demo seed -- reached through ELIGIBILITY_DSN, and
dropped at the end. No test server -> skipped (said, never faked).
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def database():
    psycopg = pytest.importorskip("psycopg")
    try:
        from app.store.testing import _admin_dsn, _with_db

        admin = _admin_dsn()
    except Exception as exc:  # noqa: BLE001 - no test server here
        pytest.skip(f"no test PostgreSQL server ({type(exc).__name__})")
    from app.agents.eligibility.data import migrations

    name = f"elig_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = _with_db(admin, name)
    try:
        assert migrations.seed_demo(dsn) == 36
        yield dsn
    finally:
        from app.agents.eligibility.data.providers import close_pools

        close_pools()
        migrations.forget()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def provider_env(database, monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_DSN", database)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ELIGIBILITY_DATA_PROVIDER", "postgres")
    return database


def _cases(dsn: str) -> list[tuple[str, str]]:
    from app.agents.eligibility.data.providers import PostgresDataProvider

    return [(c, e) for c, e in PostgresDataProvider(dsn=dsn).case_ids() if c.startswith("CASE-E11D") and e]


def test_the_seed_has_at_least_25_cases_covering_every_decision(provider_env):
    cases = _cases(provider_env)
    assert len(cases) >= 25, len(cases)
    assert {e for _, e in cases} == {"APPROVED", "CONDITIONAL", "REFER", "REJECTED", "DATA_MISSING"}


def test_every_case_gets_its_expected_decision(provider_env):
    """EVERY seeded case, loaded through PostgresDataProvider and decided by the engine -- all mismatches listed."""
    from app.agents.eligibility import agent

    wrong = []
    for case_id, expected in _cases(provider_env):
        result = agent.assess(case_id)
        if result.decision.value != expected:
            wrong.append((case_id, expected, result.decision.value, result.reasons))
    assert not wrong, wrong


def test_null_columns_reach_the_engine_as_none(provider_env):
    from app.agents.eligibility.data.providers import PostgresDataProvider

    data = PostgresDataProvider(dsn=provider_env).get("CASE-E11D00000025")      # "bureau score missing"
    assert data.bureau_score is None and data.product == "LOAN_AGAINST_PROPERTY"
    assert isinstance(data.age, int) and data.age > 0           # derived from dob in code


def test_an_unknown_case_is_not_found_never_a_decision(provider_env):
    from app.agents.eligibility.agent import assess_payload

    out = assess_payload({"case_id": "CASE-FFFFFFFFFFFF"})
    assert out["error"] == "CASE_NOT_FOUND" and out["eligibility"] is None


def test_a_wrong_password_is_unavailable_never_a_decision(provider_env, monkeypatch):
    from app.agents.eligibility.agent import assess_payload

    monkeypatch.setenv("ELIGIBILITY_DSN", "postgresql://nobody:wrong@127.0.0.1:1/none")
    assert assess_payload({"case_id": "CASE-E11D00000001"})["error"] == "DATA_SOURCE_UNAVAILABLE"


# ---- the API -------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(provider_env, auth_headers):
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)
    client.headers.update(auth_headers)
    return client


def test_the_api_checks_a_case_and_returns_the_clean_response(api):
    compact = api.post("/api/v1/eligibility/check", json={"case_id": "CASE-E11D00000011"}).json()
    # THE DEFAULT IS COMPACT (owner 2026-10-14): every figure once; the full shape with detail: true
    assert set(compact) <= {"case_id", "run_id", "decision", "summary", "offer", "checks", "what_it_takes",
                            "next_steps", "working", "missing_fields", "explanation"}, compact
    assert compact["offer"]["amount"] and compact["what_it_takes"] and len(compact["checks"]) == 6
    r = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000011"})     # "LTV binds a home loan"
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers.get("X-Request-ID", "").startswith("elig_")
    assert set(body) == {"case_id", "run_id", "decision", "summary", "final", "loan", "ratios", "limiting_factor",
                         "recommendation", "exposure", "what_it_takes", "rules",
                         "next_steps", "reasons"}, body                              # nothing internal, nothing empty
    # less than requested: the final result is the bucket recommendation (owner 2026-10-14)
    assert body["final"]["amount"] == body["recommendation"]["amount"] and body["final"]["bucket"]
    assert body["decision"] == "CONDITIONAL" and body["limiting_factor"] == "LTV" and body["loan"]["eligible"] == 3200000
    assert "32,00,000" in body["summary"] and "LTV" in body["summary"] and body["next_steps"]
    assert set(body["ratios"]) == {"foir", "ltv"} and body["ratios"]["ltv"]["limit"] == 80
    assert all(x["label"] for x in body["rules"])
    assert [x["rule"] for x in body["rules"]] == ["AGE", "INCOME", "SCORE", "DPD", "FOIR", "LTV"]
    assert {x["result"] for x in body["rules"]} <= {"pass", "fail", "missing", "not_applicable"}
    assert body["reasons"] and body["loan"]["emi"] > 0


def test_every_check_is_saved_and_read_back_as_returned(api):
    first = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000009"}).json()
    assert first["run_id"].startswith("er_")
    again = api.get(f"/api/v1/eligibility/runs/{first['run_id']}")
    assert again.status_code == 200, again.text
    saved = again.json()
    assert saved.pop("checked_at") and saved == first


def test_a_secured_rule_that_does_not_apply_is_said_so(api):
    body = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000001"}).json()
    assert body["decision"] == "APPROVED" and "ltv" not in body
    assert {"rule": "LTV", "label": "Loan to value", "result": "not_applicable"} in body["rules"]


def test_missing_data_is_named_in_the_response(api):
    body = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000025"}).json()
    assert body["decision"] == "DATA_MISSING" and body["missing_fields"] == ["bureau_score"]
    assert {"rule": "SCORE", "label": "Bureau score", "result": "missing"} in body["rules"]


def test_the_api_keeps_errors_apart_from_decisions(api):
    assert api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-FFFFFFFFFFFF"}).status_code == 404
    assert api.get("/api/v1/eligibility/runs/er_nope").status_code == 404


def test_the_api_needs_the_execute_scope(provider_env, make_token):
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {make_token(scopes=['documents:read'])}"})
    assert client.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000001"}).status_code == 403


# -- the officer's pop-up through the API (2026-10-14) ----------------------------------------------------------------

def test_the_income_popup_changes_the_result_and_shows_its_working(api):
    plain = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000010"}).json()
    with_itr = api.post("/api/v1/eligibility/check", json={
        "detail": True, "case_id": "CASE-E11D00000010",
        "income": {"method": "itr", "income_as_per_itr": 3600000, "tax": 600000}}).json()
    assert with_itr["income"]["monthly_income"] == 250000 and with_itr["income"]["annual_income"] == 3000000
    assert with_itr["loan"]["eligible"] > plain["loan"]["eligible"]
    assert with_itr["final"]["amount"] == with_itr["loan"]["eligible"]


@pytest.mark.parametrize("body, field", [
    ({"income": {"method": "ITR", "tax": 5}}, "income"),                                     # required figure absent
    ({"income": {"method": "ITR", "income_as_per_itr": -5}}, "income"),                      # negative
    ({"income": {"method": "ITR", "income_as_per_itr": "abc"}}, "income"),                   # not a number
    ({"income": {"method": "ITR", "income_as_per_itr": 1e20}}, "income"),                    # an extra-zeros typo
    ({"income": {"method": "SALARIED", "gross_salary": 50000, "turnover": 1}}, "income"),    # another method's field
    ({"income": {"method": "PENSION", "gross_salary": 5}}, "income"),                        # unknown method
    ({"topup": {"type": "INTERNAL_TOPUP", "pos": 9, "previous_sanction": 5, "mob": 20,
                "original_tenor_months": 60}}, "topup"),                                      # POS above sanction
    ({"collateral_type": "FARMLAND"}, "collateral_type"),
    ({"existing_exposure": -1}, "existing_exposure"),
])
def test_a_bad_popup_is_a_422_naming_the_field_never_a_decision(api, body, field):
    r = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000012", **body})
    assert r.status_code == 422, r.text
    assert field in r.text


def test_every_swagger_example_runs_as_is(api):
    # the "Try it out" bodies (2026-10-14): each one is a valid request that returns a decision, never a 422
    from app.api.routes.eligibility_api import _CHECK_EXAMPLES

    schema = api.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/eligibility/check"]["post"]["requestBody"]["content"]["application/json"]
    assert set(body.get("examples") or {}) == set(_CHECK_EXAMPLES)
    for name, example in _CHECK_EXAMPLES.items():
        payload = {**example["value"], "explain": False}
        r = api.post("/api/v1/eligibility/check", json=payload)
        assert r.status_code == 200, (name, r.text)
        assert r.json()["decision"] in {"APPROVED", "CONDITIONAL", "REFER", "REJECTED", "DATA_MISSING"}, name


def test_the_officer_can_check_the_case_for_another_product(api):
    as_case = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000011"}).json()
    as_lap = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000011",
                                                         "product": "loan_against_property"}).json()
    assert as_case["loan"]["product"] == "HOME_LOAN" and as_lap["loan"]["product"] == "LOAN_AGAINST_PROPERTY"
    assert as_lap["ratios"]["ltv"]["limit"] != as_case["ratios"]["ltv"]["limit"]       # the product's own LTV limit
    bad = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000011", "product": "GOLD_LOAN"})
    assert bad.status_code == 422 and "GOLD_LOAN" in bad.text and "HOME_LOAN" in bad.text


# -- the eligibility screen (2026-10-14): fetch the case, enter, Save, Trigger ---------------------------------------

def test_the_screen_page_and_its_form_come_from_config(api):
    page = api.get("/api/v1/eligibility/ui")
    assert page.status_code == 200 and "Trigger eligibility" in page.text and "<script>" in page.text
    form = api.get("/api/v1/eligibility/form").json()
    assert set(form["income_methods"]) == {"SALARIED", "ITR", "CASH_PROFIT", "GROSS_PROFIT", "ASSESSED"}
    itr = {f["key"]: f["required"] for f in form["income_methods"]["ITR"]["fields"]}
    assert itr == {"income_as_per_itr": True, "tax": False, "other_income": False}
    # the form IS the config: every top-up field the config lists, in its order (pos / mob among them)
    from app.agents.eligibility import config

    wanted = [f["key"] for f in config.document()["form"]["topup_types"]["BT_TOPUP"]["fields"]]
    assert [f["key"] for f in form["topup_types"]["BT_TOPUP"]["fields"]] == wanted and {"pos", "mob"} <= set(wanted)
    assert "HOME_LOAN" in form["products"]


@pytest.fixture
def clean_saved(database):
    """Saved entries persist: the case's row is removed after the test, so no other test checks on them."""
    import psycopg2

    yield
    connection = psycopg2.connect(database, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM eligibility_inputs WHERE case_id = %s", ("CASE-E11D00000010",))
        connection.commit()
    finally:
        connection.close()


def test_pick_a_case_save_the_entries_and_trigger_on_them(api, clean_saved):
    cases = api.get("/api/v1/eligibility/cases").json()["cases"]
    assert any(c["case_id"] == "CASE-E11D00000010" for c in cases)
    fetched = api.get("/api/v1/eligibility/cases/CASE-E11D00000010").json()
    assert fetched["case"]["product"] == "HOME_LOAN" and fetched["case"]["requested_amount"] == 8000000
    plain = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000010", "use_saved": False}).json()
    entries = {"requested_amount": 3000000, "tenure_months": 240,
               "income": {"method": "itr", "income_as_per_itr": 3600000, "tax": 600000}}
    saved = api.put("/api/v1/eligibility/cases/CASE-E11D00000010/inputs", json=entries)
    assert saved.status_code == 200, saved.text
    assert saved.json()["inputs"]["income"]["method"] == "ITR" and saved.json()["saved_at"]
    again = api.get("/api/v1/eligibility/cases/CASE-E11D00000010").json()
    assert again["saved"]["inputs"]["requested_amount"] == 3000000
    # Trigger with nothing in the body: the SAVED entries are what is checked
    triggered = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000010"}).json()
    assert triggered["loan"]["requested"] == 3000000 and triggered["income"]["monthly_income"] == 250000
    assert triggered["decision"] != plain["decision"] or triggered["loan"] != plain["loan"]
    # entries sent with the check win over the saved ones
    on_screen = api.post("/api/v1/eligibility/check", json={"detail": True, "case_id": "CASE-E11D00000010",
                                                             "requested_amount": 2000000}).json()
    assert on_screen["loan"]["requested"] == 2000000


@pytest.mark.parametrize("body", [
    {"income": {"method": "ITR", "tax": 5}},                 # the method's required figure missing
    {"requested_amount": -1},
    {"tenure_months": 0},
    {"topup": {"type": "BT_TOPUP", "pos": 100, "mob": 3, "previous_sanction": 9}},
])
def test_a_bad_entry_is_never_saved(api, body):
    r = api.put("/api/v1/eligibility/cases/CASE-E11D00000012/inputs", json=body)
    assert r.status_code == 422, r.text


def test_entries_for_an_unknown_case_are_refused(api):
    r = api.put("/api/v1/eligibility/cases/CASE-FFFFFFFFFFFF/inputs", json={"tenure_months": 60})
    assert r.status_code == 404


def test_every_save_example_saves_as_is(api, clean_saved):
    # Swagger's Save bodies (2026-10-14): realistic placeholders, each one valid -- never schema maximums
    from app.api.routes.eligibility_api import _SAVE_EXAMPLES

    schema = api.get("/openapi.json").json()
    put = schema["paths"]["/api/v1/eligibility/cases/{case_id}/inputs"]["put"]["requestBody"]["content"]
    assert set(put["application/json"]["examples"]) == set(_SAVE_EXAMPLES)
    for name, example in _SAVE_EXAMPLES.items():
        r = api.put("/api/v1/eligibility/cases/CASE-E11D00000010/inputs", json=example["value"])
        assert r.status_code == 200, (name, r.text)


# -- the same check as a FORM (Swagger: input boxes + dropdowns, 2026-10-14) ----------------------------------------

def test_the_form_check_has_dropdowns_and_matches_the_json_check(api):
    schema = api.get("/openapi.json").json()
    form = schema["paths"]["/api/v1/eligibility/check-form"]["post"]["requestBody"]["content"]
    props = next(iter(form.values()))["schema"]
    if "$ref" in props:
        props = schema["components"]["schemas"][props["$ref"].split("/")[-1]]
    names = set(props["properties"])
    assert {"income_method", "topup_type", "collateral", "occupancy", "product", "income_amount",
            "income_adjustment", "topup_pos"} <= names and len(names) <= 20          # a short form
    as_form = api.post("/api/v1/eligibility/check-form", data={
        "case_id": "CASE-E11D00000010", "income_method": "ITR", "income_amount": "1500000",
        "income_adjustment": "150000", "detail": "true"}).json()
    as_json = api.post("/api/v1/eligibility/check", json={
        "case_id": "CASE-E11D00000010", "detail": True,
        "income": {"method": "ITR", "income_as_per_itr": 1500000, "tax": 150000}}).json()
    assert as_form["income"] == as_json["income"] and as_form["loan"] == as_json["loan"]


def test_zeros_in_unused_boxes_are_ignored(api):
    # Swagger used to pre-fill every number box: 0 in a box the chosen option does not use changes nothing
    zeros = {box: "0" for box in ("other_deduction", "topup_pos", "topup_mob", "previous_sanction",
                                  "original_tenure_months", "existing_exposure")}
    r = api.post("/api/v1/eligibility/check-form", data={
        "case_id": "CASE-E11D00000010", "income_method": "ITR", "income_amount": "1500000", **zeros})
    assert r.status_code == 200, r.text


def test_a_value_in_a_box_the_option_does_not_use_is_refused(api):
    r = api.post("/api/v1/eligibility/check-form", data={
        "case_id": "CASE-E11D00000010", "income_method": "ITR", "income_amount": "900000",
        "other_deduction": "5000"})
    assert r.status_code == 422 and "other_deduction" in r.text and "ITR" in r.text
