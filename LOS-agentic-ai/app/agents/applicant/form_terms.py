"""
THE CREATE FORM'S PRODUCT TERMS -- which products need a property type, and its choices -- read from the SAME
eligibility terms the create API checks (fos_api._property_errors).

Kept OUTSIDE the chatbot package (owner, 2026-10-13: "eligibility ko chatbot se alag rakhna hai"): the chatbot's case
form (copilot/capabilities/case_form.py) asks this module and never imports eligibility itself.
"""
from __future__ import annotations

from typing import Any


def property_types_by_product(products: list[str]) -> dict[str, dict[str, Any]]:
    """product -> {choices, required, default} for the products whose terms use a property type (LTV): required when
    the product has no default type."""
    from app.agents.eligibility import config as eligibility_config

    out: dict[str, dict[str, Any]] = {}
    for product in products:
        terms = eligibility_config.product_terms(product)
        if terms is None or not terms.ltv_enabled:
            continue
        known = sorted(set(terms.property_types) | ({terms.ltv_default_property_type} if terms.ltv_default_property_type else set()))
        if known:
            out[product] = {"choices": known, "required": not terms.ltv_default_property_type,
                            "default": terms.ltv_default_property_type or None}
    return out


__all__ = ["property_types_by_product"]
