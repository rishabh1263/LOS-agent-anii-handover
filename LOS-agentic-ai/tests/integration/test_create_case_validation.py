"""
POST /api/v1/fos/applicants VALIDATES WHAT IT IS GIVEN (2026-10-13). Written BEFORE the fix -- a real run accepted
(201) a loan amount of "lots", a tenure of "abc", obligations "many", product "CAR_LOAN", employment "STUDENT",
property type "VILLA", valuation "big", date of birth "abc", mobile "123" and an empty name.

The rules are the SAME ones the form edit (PUT /cases/{id}/form) and chat creation already apply
(case_form.check: product_flow.yaml case_form.formats / choices), plus the LTV fields. A field that is not given is
not demanded (an applicant-only case stays possible); the name is.
"""
from __future__ import annotations

import pytest

from tests.integration.test_reupload_supersedes import client  # noqa: F401

APPLICANT = {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": "1990-04-12", "address": "Pune"}
APPLICATION = {"product": "PERSONAL_LOAN", "loan_amount": 500000, "tenure_months": 60, "employment_type": "SALARIED",
               "declared_monthly_obligations": 10000}


def create(client, applicant=None, application=APPLICATION):
    body = {"applicant": {**APPLICANT, **(applicant or {})}}
    if application is not None:
        body["application"] = application
    return client.post("/api/v1/fos/applicants", json=body)


@pytest.mark.parametrize("applicant, application, field", [
    ({}, {**APPLICATION, "loan_amount": "lots"}, "loan_amount"),
    ({}, {**APPLICATION, "tenure_months": "abc"}, "tenure_months"),
    ({}, {**APPLICATION, "declared_monthly_obligations": "many"}, "declared_monthly_obligations"),
    ({}, {**APPLICATION, "product": "CAR_LOAN"}, "product"),
    ({}, {**APPLICATION, "employment_type": "STUDENT"}, "employment_type"),
    ({}, {**APPLICATION, "product": "HOME_LOAN", "property_type": "VILLA"}, "property_type"),
    ({}, {**APPLICATION, "product": "HOME_LOAN", "valuation_value": "big"}, "valuation_value"),
    ({}, {**APPLICATION, "product": "HOME_LOAN", "purchase_price": -10}, "purchase_price"),
    ({"date_of_birth": "abc"}, APPLICATION, "date_of_birth"),
    ({"mobile": "123"}, APPLICATION, "mobile"),
    ({"full_name": ""}, APPLICATION, "full_name"),
    ({"full_name": "1234"}, APPLICATION, "full_name"),
])
def test_an_invalid_field_is_refused_with_its_name(client, applicant, application, field):
    r = create(client, applicant, application)
    assert r.status_code == 422, (field, r.status_code, r.text[:300])
    assert field in r.json()["detail"]["fields"], r.json()


def test_a_valid_case_is_created(client):
    r = create(client)
    assert r.status_code == 201, r.text


def test_a_home_loan_with_property_details_is_created(client):
    r = create(client, application={**APPLICATION, "product": "HOME_LOAN", "loan_amount": 2000000,
                                    "property_type": "residential", "valuation_value": "30,00,000",
                                    "purchase_price": 2800000})
    assert r.status_code == 201, r.text


def test_an_applicant_only_case_is_still_allowed(client):
    assert create(client, application=None).status_code == 201


def test_a_name_with_a_number_is_accepted(client):
    """Test data names cases "Officer Case 3"; a name needs a letter, not a strict human-name pattern."""
    assert create(client, {"full_name": "Officer Case 3"}).status_code == 201


# ---- the PRODUCT decides its fields (from its eligibility policy) ------------------------------------------------
LAP = {"product": "LOAN_AGAINST_PROPERTY", "loan_amount": 3000000, "tenure_months": 120,
       "employment_type": "SELF_EMPLOYED", "declared_monthly_obligations": 5000}
HL = {"product": "HOME_LOAN", "loan_amount": 2000000, "tenure_months": 240, "employment_type": "SALARIED",
      "declared_monthly_obligations": 0}


@pytest.mark.parametrize("application, field", [
    ({**APPLICATION, "property_type": "RESIDENTIAL"}, "property_type"),       # a personal loan has no property
    ({**APPLICATION, "valuation_value": 3000000}, "valuation_value"),
    ({**HL, "property_type": "COMMERCIAL"}, "property_type"),                # a home loan is residential
    ({**LAP, "valuation_value": 8000000}, "property_type"),                  # LAP: the grid needs the type
])
def test_the_product_decides_its_fields(client, application, field):
    r = create(client, application=application)
    assert r.status_code == 422 and field in r.json()["detail"]["fields"], r.text[:300]


@pytest.mark.parametrize("application", [
    {**LAP, "property_type": "COMMERCIAL", "valuation_value": 8000000},
    {**HL, "valuation_value": 3000000},                                      # residential by its policy
    {**HL, "property_type": "RESIDENTIAL", "purchase_price": 2800000},
])
def test_a_products_own_fields_are_accepted(client, application):
    assert create(client, application=application).status_code == 201


def test_the_docs_show_one_example_per_product():
    import main

    examples = main.app.openapi()["paths"]["/api/v1/fos/applicants"]["post"]["requestBody"]["content"][
        "application/json"]["examples"]
    assert set(examples) == {"personal_loan", "home_loan", "loan_against_property"}
    assert "property_type" not in examples["personal_loan"]["value"]["application"]
