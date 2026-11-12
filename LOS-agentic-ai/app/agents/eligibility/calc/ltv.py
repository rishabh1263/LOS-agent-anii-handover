"""
The LTV limit that applies to a case -- from config only (sections PRODUCTS AND LTV): the collateral x occupancy grid,
else the product's `ltv_limit_pct`; for housing loans the RBI slabs by loan size cap it further.
"""

from __future__ import annotations

from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.domain.models import EligibilityInput


def limit_for(inp: EligibilityInput, terms: dict[str, Any]) -> tuple[float | None, str]:
    """The LTV limit: the collateral x occupancy grid when both are known, else the product's ltv_limit_pct.
    (None, "product") when the product has no collateral (LTV does not apply)."""
    base = terms.get("ltv_limit_pct")
    if base is None:
        return None, "product"
    grid = (config.document().get("ltv_grid") or {}).get(str(inp.product or "").upper()) or {}
    by_collateral = grid.get(str(inp.collateral_type or "").upper()) or {}
    cell = by_collateral.get(str(inp.occupancy_type or "").upper())
    if cell is not None:
        return float(cell), f"{str(inp.collateral_type).upper()} / {str(inp.occupancy_type).upper()}"
    return float(base), "product"


def slabs(product: str | None) -> list[tuple[float, float | None, float]]:
    """The product's LTV slabs as (from, up_to, pct), in order; [] when none (config ltv_slabs)."""
    rows, low, out = (config.document().get("ltv_slabs") or {}).get(str(product or "").upper()) or [], 0.0, []
    for row in rows:
        high = None if row.get("up_to") is None else float(row["up_to"])
        out.append((low, high, float(row["pct"])))
        low = high if high is not None else low
    return out


def slab_pct(product: str | None, total: float) -> float | None:
    """The slab LTV % for a total exposure; None without slabs."""
    for low, high, pct in slabs(product):
        if total > low and (high is None or total <= high) or (low == 0 and total <= (high or total)):
            return pct
    return None


def slab_cap(product: str | None, property_value: float, policy_pct: float) -> float | None:
    """The largest TOTAL exposure that satisfies its own slab (and the policy limit); None without slabs.
    For each slab (from, up_to]: min(up_to, property x min(policy, slab %)) -- usable only when it lands in the slab."""
    best = None
    for low, high, pct in slabs(product):
        amount = property_value * min(policy_pct, pct) / 100.0
        if high is not None:
            amount = min(amount, high)
        if amount > low and (best is None or amount > best):
            best = amount
    return best


__all__ = ["limit_for", "slab_cap", "slab_pct", "slabs"]
