"""The case form exposes property_type (2026-10-22), so a LAP case can be opened from the UI form.

Its choices and whether a product needs it come from the eligibility terms -- the SAME terms the create API checks
(fos_api property errors) -- never a second list. Also: the mobile strip rule is applied until stable, and the
customer-history summary names checks in words (no rule ids, no demo note).
"""
from __future__ import annotations

import pytest

from app.agents.applicant.copilot.capabilities import case_form
from app.agents.eligibility import config as eligibility_config
from tests.integration.test_reupload_supersedes import client  # noqa: F401

APPLICANT = {"full_name": "Test Person", "mobile": "9876543210", "date_of_birth": "1990-04-12", "address": "Pune"}


def _field(schema: dict, name: str) -> dict:
    return next(f for f in schema["fields"] if f["name"] == name)


def test_the_schema_offers_property_type_per_product_from_the_eligibility_terms(client):
    schema = client.get("/api/v1/fos/form-schema").json()
    field = _field(schema, "property_type")
    assert field["group"] == "application" and field["required"] is False      # required only per product
    by_product = field["rules"]["by_product"]
    for product, spec in by_product.items():
        terms = eligibility_config.product_terms(product)
        known = set(terms.property_types) | ({terms.ltv_default_property_type} if terms.ltv_default_property_type else set())
        assert set(spec["choices"]) == known
        assert spec["required"] is (not terms.ltv_default_property_type)
    for product in case_form.choices("product"):
        terms = eligibility_config.product_terms(product)
        if terms is None or not terms.ltv_enabled:
            assert product not in by_product                                  # e.g. a personal loan: no property
    assert sorted(field["choices"]) == sorted({c for s in by_product.values() for c in s["choices"]})


def test_a_product_that_needs_a_type_without_a_default_is_marked_required(client):
    by_product = _field(client.get("/api/v1/fos/form-schema").json(), "property_type")["rules"]["by_product"]
    needs = [p for p, s in by_product.items() if s["required"]]
    assert needs, "the eligibility terms name at least one product (LAP) that needs a property type"
    for product in needs:
        body = {"applicant": APPLICANT, "application": {"product": product, "loan_amount": 1500000,
                                                        "property_value": 5000000}}
        refused = client.post("/api/v1/fos/applicants", json=body)
        assert refused.status_code == 422 and "property_type" in refused.json()["detail"]["fields"]
        body["application"]["property_type"] = by_product[product]["choices"][0]
        assert client.post("/api/v1/fos/applicants", json=body).status_code == 201


@pytest.mark.parametrize("raw", ["+91 98765 43210", "+91-98765-43210", "919876543210", "98765 43210"])
def test_the_mobile_strip_rule_is_applied_until_stable(raw):
    assert case_form.check("mobile", raw, "en") == ("9876543210", None)


def test_property_type_is_a_choice_in_the_form_check():
    allowed = case_form.choices("property_type")
    if not allowed:
        pytest.skip("no eligibility terms with a property type in this environment")
    assert case_form.check("property_type", allowed[0].lower(), "en") == (allowed[0], None)
    value, why = case_form.check("property_type", "villa", "en")
    assert value is None and why


def test_the_history_summary_names_checks_in_words_without_rule_ids_or_a_demo_note():
    from app.agents.dedupe import history_summary

    assert history_summary._readable("DATA_UNAVAILABLE:H06_ADVERSE_HISTORY") == "adverse history (data unavailable)"
    assert history_summary._readable("LOAN_HISTORY_INCOMPLETE") == "loan history incomplete"


def test_rupees_are_written_in_the_indian_grouping():
    from app.agents.dedupe.history_summary import _inr

    got = [_inr(v) for v in (312400, 11200, 999, 10000000, "2500000.00", "abc")]
    assert got == ["3,12,400", "11,200", "999", "1,00,00,000", "25,00,000", "abc"]

