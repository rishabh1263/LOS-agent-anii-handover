"""
Eligibility config: the file every limit comes from, and the guard that keeps a demo config out of production.

THE RULES LIVE IN THE DATABASE (2026-11-02, owner: "rules db me config rahenge"): the version in force of the rule
set `eligibility_policy` in the eligibility schema (app/agent_db/rule_store.py). `app/config/eligibility_config.yaml`
is where a version comes from (scripts/rules.py import); outside production the file is used directly when no
database is configured. It holds the five rules' limits, the per-product terms and the data provider's name; the
engine reads them through here and contains no number itself.

A DEMO CONFIG NEVER RUNS IN PRODUCTION. `status` other than CONFIRMED is allowed in development / dev / local / test,
in a named non-production environment (staging, uat, qa, preprod, sandbox) only with ELIGIBILITY_ALLOW_DEMO_POLICY=true,
and never in production or with ENVIRONMENT unset (fail closed) -- `validate_for_startup` stops the service first.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from app.core.constants import CONFIG_DIR

DEFAULT_FILENAME = "eligibility_config.yaml"
CONFIRMED = "CONFIRMED"

_DEV_ENVIRONMENTS = {"development", "dev", "local", "test"}
_NON_PRODUCTION = {"staging", "uat", "qa", "preprod", "sandbox"}


def config_path() -> Path:
    override = os.getenv("ELIGIBILITY_CONFIG_PATH")
    return Path(override) if override else Path(CONFIG_DIR) / DEFAULT_FILENAME


RULE_SET = "eligibility_policy"
_SAID: set[str] = set()


def reset_cache() -> None:
    """Drop the cached document. For tests and controlled reloads."""
    from app.agent_db import rule_store

    rule_store.forget()


def document() -> dict[str, Any]:
    """The rules in force, or an empty document (never raises: no rules -> no products -> DATA_MISSING, and
    validate_for_startup stops the service first, naming why)."""
    from app.agent_db import rule_store

    try:
        loaded = rule_store.load(RULE_SET)
    except Exception as exc:  # noqa: BLE001 - said once; the empty document refuses every decision
        if str(exc) not in _SAID:
            _SAID.add(str(exc))
            import logging

            logging.getLogger(__name__).error("eligibility rules unavailable: %s", exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def rule_set_hash() -> str | None:
    """The hash of the rules in force (stored with every run)."""
    from app.agent_db import rule_store

    try:
        return rule_store.load_with_hash(RULE_SET)[0]
    except Exception:  # noqa: BLE001
        return None


class ConfigMissing(KeyError):
    """A setting the agent needs is not in eligibility_config.yaml -- never replaced by a number in the code."""


def setting(*path: str) -> Any:
    """ONE SETTING, STRICTLY: setting("rules", "max_dpd_12m"). The config file is the only source of every value; a
    missing key raises ConfigMissing naming its path (2026-10-15, owner: "kuch bhi hardcode nahi") -- a silent
    fallback in the code would decide with a number nobody configured."""
    from app.agent_db import rule_store

    if path and path[0] in rule_store.file_sections(RULE_SET):           # a deployment section: its file, always
        return _setting_in({path[0]: rule_store.file_section(RULE_SET, path[0])}, path)
    return _setting_in(document(), path)


def _setting_in(node: Any, path: tuple[str, ...]) -> Any:
    for i, key in enumerate(path):
        if not isinstance(node, dict) or key not in node or node[key] is None and i < len(path) - 1:
            raise ConfigMissing(f"eligibility_config.yaml has no {'.'.join(path[: i + 1])}")
        node = node[key]
    return node


def limit(name: str) -> float:
    """An input typo guard (section INPUT LIMITS)."""
    return float(setting("input_limits", name))


def rules() -> dict[str, Any]:
    return dict(document().get("rules") or {})


def products() -> dict[str, dict[str, Any]]:
    return {str(k).upper(): dict(v or {}) for k, v in (document().get("products") or {}).items()}


def product(name: str | None) -> dict[str, Any] | None:
    return products().get(str(name or "").strip().upper())


def message(key: str, **values: Any) -> str:
    template = str((document().get("messages") or {}).get(key) or key)
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def provider_name() -> str:
    """postgres | los. The environment wins over the file, so a deployment switches without editing it."""
    chosen = (os.getenv("ELIGIBILITY_DATA_PROVIDER") or "").strip().lower()
    return chosen or str(document().get("data_provider") or "postgres").strip().lower()


def version() -> str:
    return str(document().get("version") or "unknown")


def status() -> str:
    return str(document().get("status") or "UNCONFIRMED").strip().upper()


# ---------------------------------------------------------------------------
# the production guard
# ---------------------------------------------------------------------------

def _environment() -> str:
    """The deployment environment AS AUTHENTICATION READS IT: unset is production (fail closed)."""
    return (os.getenv("ENVIRONMENT") or "production").strip().lower()


def demo_allowed() -> bool:
    environment = _environment()
    if environment in _DEV_ENVIRONMENTS:
        return True
    return environment in _NON_PRODUCTION and \
        (os.getenv("ELIGIBILITY_ALLOW_DEMO_POLICY") or "").strip().lower() == "true"


def usable() -> bool:
    """Whether this config may decide anything here (CONFIRMED, or a demo where demos are allowed)."""
    return status() == CONFIRMED or demo_allowed()


#: EVERY SETTING THE AGENT READS -- checked at startup, so a missing one stops the service before it serves instead of
#: failing an officer's check later (2026-10-15, production readiness). Keep in step with the code's config.setting().
REQUIRED_SETTINGS: tuple[tuple[str, ...], ...] = (
    ("version",), ("status",), ("data_provider",),
    ("postgres", "table"), ("postgres", "connect_timeout_seconds"), ("postgres", "statement_timeout_ms"),
    ("postgres", "pool_size"), ("postgres", "runs_table"), ("postgres", "inputs_table"), ("los", "bureau_timeout_seconds"),
    ("rules", "min_age"), ("rules", "max_age"), ("rules", "max_age_at_maturity"), ("rules", "min_income"),
    ("rules", "min_score"), ("rules", "refer_margin"), ("rules", "foir_limit_pct"), ("rules", "max_dpd_12m"),
    ("rules", "dpd_refer_upto"), ("rules", "reject_write_offs"), ("rules", "reject_settlements"),
    ("rules", "round_down_to"), ("rules", "checks"), ("mandatory_fields",), ("mandatory_with_ltv",),
    ("products",), ("ltv_grid",), ("ltv_slabs",),
    ("income_methods",), ("assessed", "emi_counted_in_foir"), ("topup",),
    ("recommendation", "buckets"), ("recommendation", "tenure_option"),
    ("input_limits", "max_rupees"), ("input_limits", "max_tenure_months"), ("input_limits", "max_rate_percent"),
    ("input_limits", "max_months_on_book"), ("input_limits", "max_tenor_months"),
    ("input_limits", "max_experience_years"), ("input_limits", "max_applied_pct"), ("input_limits", "id_pattern"),
    ("form",), ("explanation", "enabled"), ("explanation", "timeout_seconds"), ("explanation", "num_predict"),
    ("explanation", "sentences"), ("explanation", "cache_size"), ("explanation", "keep_alive"),
    ("explanation", "max_chars"), ("explanation", "languages"), ("explanation", "instructions"),
    ("limit_names",), ("messages",), ("response",),
)
#: per income method / top-up type, the settings their calculators read
_PER_METHOD = ("period", "required")


def missing_settings(doc: dict[str, Any] | None = None) -> list[str]:
    """Every required setting absent from the rules in force -- or from `doc`, a version about to be stored
    (dotted paths); [] when complete."""
    if doc is not None:
        document = lambda: doc                                                     # noqa: E731 - this check's view
    else:
        document = globals()["document"]
    absent = []
    for path in REQUIRED_SETTINGS:
        try:
            _setting_in(document(), path)
        except ConfigMissing:
            absent.append(".".join(path))
    for method in (document().get("income_methods") or {}):
        absent += [f"income_methods.{method}.{k}" for k in _PER_METHOD
                   if k not in ((document().get("income_methods") or {}).get(method) or {})]
    for kind in (document().get("topup") or {}):
        if not ((document().get("topup") or {}).get(kind) or {}).get("mob_ladder"):
            absent.append(f"topup.{kind}.mob_ladder")
    for key in ("turnover_pct", "rental_considered_pct"):
        if "GROSS_PROFIT" in (document().get("income_methods") or {}) and key not in document()["income_methods"]["GROSS_PROFIT"]:
            absent.append(f"income_methods.GROSS_PROFIT.{key}")
    for key in ("pf_add_back", "form16_gap_pct"):
        if "SALARIED" in (document().get("income_methods") or {}) and key not in document()["income_methods"]["SALARIED"]:
            absent.append(f"income_methods.SALARIED.{key}")
    return absent


def validate_rules(doc: Any) -> None:
    """A version of the rules about to be stored (scripts/rules.py import): every required setting present."""
    if not isinstance(doc, dict):
        raise ValueError("the eligibility rules must be a mapping")
    absent = missing_settings(doc)
    if absent:
        raise ValueError(f"missing {', '.join(absent)}")


def validate_for_startup() -> str:
    """Called before the service serves: rules not readable, an incomplete config, or a demo config outside dev,
    stops startup."""
    from app.agent_db import rule_store

    try:
        rule_store.load(RULE_SET)
    except Exception as exc:  # noqa: BLE001 - the reason, plainly
        raise RuntimeError(f"ELIGIBILITY RULES UNAVAILABLE: {exc}") from None
    absent = missing_settings()
    if absent:
        raise RuntimeError(f"ELIGIBILITY CONFIG INCOMPLETE ({config_path().name}): missing {', '.join(absent)}")
    try:
        from app.agents.eligibility.calc import rules as _rules

        _rules.checks()                                    # an unknown rule / kind in rules.checks is named here
    except ConfigMissing as exc:
        raise RuntimeError(f"ELIGIBILITY CONFIG INVALID: {exc}") from None
    if not usable():
        raise RuntimeError(f"ELIGIBILITY CONFIG NOT APPROVED for environment '{_environment()}': "
                           f"{config_path().name} is {status()}, not {CONFIRMED}. Replace it with the confirmed rules.")
    if status() != CONFIRMED:
        return f"DEMO ({version()}) -- environment '{_environment()}' only, refused in production"
    return f"{CONFIRMED} ({version()})"


# ---------------------------------------------------------------------------
# a product's terms, as the rest of the service reads them
# ---------------------------------------------------------------------------

def product_terms(name: str | None) -> SimpleNamespace | None:
    """
    One product's limits under the attribute names the Copilot's policy answers and the create-case form read
    (loan_maximum_amount, ltv_enabled, ...). None for a product with no terms, or when this config may not be used here.
    """
    spec = product(name)
    if spec is None or not usable():
        return None
    r = rules()
    ltv = spec.get("ltv_limit_pct")
    return SimpleNamespace(
        policy_id=f"ELIGIBILITY_{str(name).upper()}", version=version(), status=status(), product=str(name).upper(),
        is_production=status() == CONFIRMED, income_basis="NET_PAY",
        employment_allowed=None,                    # not one of the five rules: never said as a limit
        age_minimum_years=r.get("min_age"), age_maximum_years=r.get("max_age"),
        age_maximum_at_maturity_years=r.get("max_age_at_maturity"),
        minimum_monthly_income=r.get("min_income"), minimum_bureau_score=r.get("min_score"),
        foir_maximum_percent=r.get("foir_limit_pct"), annual_rate_percent=spec.get("rate_percent"),
        loan_minimum_amount=spec.get("min_amount"), loan_maximum_amount=spec.get("max_amount"),
        tenure_minimum_months=spec.get("min_tenure_months"), tenure_maximum_months=spec.get("max_tenure_months"),
        ltv_enabled=ltv is not None, ltv_maximum_percent=ltv, ltv_grid=(),
        property_types=tuple(str(t).upper() for t in spec.get("property_types") or []),
        ltv_default_property_type=(str(spec["default_property_type"]).upper()
                                   if spec.get("default_property_type") else None))


__all__ = ["CONFIRMED", "ConfigMissing", "REQUIRED_SETTINGS", "config_path", "limit", "missing_settings", "setting", "demo_allowed", "document", "message", "product", "product_terms",
           "products", "provider_name", "reset_cache", "rules", "status", "usable", "validate_for_startup",
           "version"]
