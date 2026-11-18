"""
THE TWO EXTRA SOURCES THE DEDUPE RULES READ (owner, 2026-10-23: "make all rules work"):
  RCU / fraud cases     -> R09 (H13_FRAUD_HISTORY)      RCU_PROVIDER
  collateral registry   -> R10 (H14_COLLATERAL_REUSE)   COLLATERAL_PROVIDER

NO LIVE CONTRACT EXISTS FOR EITHER YET. Each answers by the confirmed customer's AP number:
  provider "demo"  -> synthetic records (app/config/demo/mifin_demo.yaml `rcu:` / `collateral:`), development only;
                      also the default whenever MIFIN_PROVIDER=demo (one demo world), never in production
  anything else    -> UNAVAILABLE: the rule is NOT_EVALUATED (data unavailable) -- never a pass
Only counts and statuses leave this module: no property address, case narrative, name or identifier.
"""

from __future__ import annotations

import os
from typing import Any


def _production() -> bool:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower() in {"production", "prod"}


def _provider(env: str) -> str:
    chosen = (os.getenv(env) or "").strip().lower()
    if not chosen and (os.getenv("MIFIN_PROVIDER") or "").strip().lower() == "demo":
        chosen = "demo"
    return "" if (chosen == "demo" and _production()) else chosen


def _demo(section: str) -> dict[str, Any]:
    from app.agents.dedupe import demo_sources

    return {str(k).upper(): v for k, v in (demo_sources.data().get(section) or {}).items()}


def rcu(customer_id: str | None, adverse: list[str]) -> dict[str, Any]:
    """{"status": AVAILABLE | UNAVAILABLE, "records", "adverse", "statuses"} for one confirmed customer."""
    if _provider("RCU_PROVIDER") != "demo" or not customer_id:
        return {"status": "UNAVAILABLE", "records": None, "adverse": None, "statuses": []}
    cases = list(_demo("rcu").get(str(customer_id).upper()) or [])
    statuses = [str(c.get("status") or "UNKNOWN").upper() for c in cases if isinstance(c, dict)]
    bad = {str(x).upper() for x in adverse}
    return {"status": "AVAILABLE", "records": len(statuses), "adverse": sum(s in bad for s in statuses),
            "statuses": statuses}


def collateral(customer_id: str | None) -> dict[str, Any]:
    """{"status", "properties", "shared"}: the customer's pledged properties and how many are also pledged by
    ANOTHER customer (the reuse R10 looks for)."""
    if _provider("COLLATERAL_PROVIDER") != "demo" or not customer_id:
        return {"status": "UNAVAILABLE", "properties": None, "shared": None}
    props = [p for p in (_demo("collateral").get(str(customer_id).upper()) or []) if isinstance(p, dict)]
    return {"status": "AVAILABLE", "properties": len(props),
            "shared": sum(int(p.get("other_customers") or 0) > 0 for p in props)}


__all__ = ["collateral", "rcu"]
