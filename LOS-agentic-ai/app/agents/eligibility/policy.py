"""
Where an eligibility policy comes from, kept apart from how it is applied.

THE SEAM THIS MODULE EXISTS FOR. The business team's real policy will
arrive from somewhere this repository does not control yet -- a policy API,
a master policy service, a database table, a master Excel transformed into
configuration. The engine must not care which. It asks for a policy by
product and receives an `EligibilityPolicy`; every provider produces that
same typed object, so replacing the source is a provider change and never
an engine change.

    PolicyProvider.get_policy(product, policy_id=None, version=None)
            |
            +-- ConfigPolicyProvider   app/config/eligibility_policy.yaml
            |                          (the default, holding PL_DUMMY_V1)
            |
            +-- ApiPolicyProvider      the business policy service -- NOT
                                       YET AVAILABLE, and it says so rather
                                       than pretending to connect

Which one is used is configuration: ELIGIBILITY_POLICY_PROVIDER, falling
back to the file's `provider` key, falling back to `config`.

A POLICY THAT CANNOT BE HAD IS AN ANSWER, NOT A CRASH. Every provider
raises `PolicyUnavailable` for a product it has no policy for, for a
version it cannot serve, or for a source it cannot reach; the engine turns
that into a SKIPPED verdict with POLICY_UNAVAILABLE. An eligibility
assessment made against no policy -- or against a guessed one -- is the
one outcome this layer must never produce.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.eligibility import config as policy_file


class PolicyUnavailable(Exception):
    """No policy could be supplied for this request, and why."""


class EligibilityPolicy(BaseModel):
    """
    One product's eligibility policy, whichever provider supplied it.

    EVERY CRITERION IS OPTIONAL, AND NULL MEANS NOT EVALUATED. A policy
    that sets no minimum income has not set a minimum of zero; it has
    said nothing, and the engine reports the criterion as unassessed
    rather than inventing a limit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # -- identity ---------------------------------------------------------
    policy_id: str
    version: str
    product: str
    #: e.g. DEMO_NON_PRODUCTION, CONFIRMED. Carried into every result.
    status: str
    #: Which provider supplied it -- recorded, so an audit can tell a
    #: config-file policy from an API-served one.
    provider: str

    enabled: bool = True
    #: What a breach does to the verdict: REVIEW or FAIL.
    breach_status: str = "REVIEW"

    # -- income -----------------------------------------------------------
    income_basis: str = "NET_PAY"
    minimum_monthly_income: float | None = None
    on_income_review: str = "REVIEW"

    # -- affordability ----------------------------------------------------
    foir_maximum_percent: float | None = None
    foir_fail_above_percent: float | None = None

    # -- the loan ---------------------------------------------------------
    loan_minimum_amount: float | None = None
    loan_maximum_amount: float | None = None
    tenure_minimum_months: int | None = None
    tenure_maximum_months: int | None = None
    #: The product rate. None: the application's captured rate is used.
    annual_rate_percent: float | None = None

    # -- the applicant ----------------------------------------------------
    #: None means employment type is not a criterion of this policy.
    employment_allowed: tuple[str, ...] | None = None

    # -- inputs this system cannot verify on its own ----------------------
    obligations_accepted_sources: tuple[str, ...] = Field(default_factory=tuple)

    # -- loan-to-value: only for a product with collateral ------------------
    #: False means LTV does not apply to this product (an unsecured loan has
    #: no property to measure against) -- reported as NOT_APPLICABLE.
    ltv_enabled: bool = False
    ltv_maximum_percent: float | None = None
    #: Sources whose property value may be used. A sale deed's consideration
    #: price is never one: it is a transacted price, not a valuation.
    property_value_accepted_sources: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def is_production(self) -> bool:
        """Only a policy explicitly marked CONFIRMED is treated as real."""
        return self.status.strip().upper() == "CONFIRMED"


class PolicyProvider(ABC):
    """Supplies eligibility policies. Every implementation returns the same type."""

    #: The name configuration uses to select this provider.
    name: str = ""

    @abstractmethod
    def get_policy(
        self,
        product: str | None,
        policy_id: str | None = None,
        version: str | None = None,
    ) -> EligibilityPolicy:
        """
        The policy for this product.

        `policy_id` and `version`, when given, PIN the request: a provider
        that cannot serve exactly that policy raises PolicyUnavailable
        rather than silently returning a different one -- a case assessed
        under version 1.0 must not be re-assessed under 1.1 because the
        file moved underneath it.
        """


class ConfigPolicyProvider(PolicyProvider):
    """Policies from app/config/eligibility_policy.yaml."""

    name = "config"

    def get_policy(self, product, policy_id=None, version=None):
        key = str(product or "").strip().upper()
        if not key:
            raise PolicyUnavailable("No product on the application, so no policy applies.")

        policies = policy_file.document().get("policies")
        raw = (policies or {}).get(key) if isinstance(policies, dict) else None
        if not isinstance(raw, dict):
            raise PolicyUnavailable(f"No eligibility policy is configured for {key}.")

        policy = _from_config(raw, product=key, provider=self.name)

        if policy_id and policy_id != policy.policy_id:
            raise PolicyUnavailable(
                f"Policy {policy_id} was requested; the configuration holds "
                f"{policy.policy_id}.")
        if version and version != policy.version:
            raise PolicyUnavailable(
                f"{policy.policy_id} version {version} was requested; the "
                f"configuration holds version {policy.version}.")

        return policy


class ApiPolicyProvider(PolicyProvider):
    """
    Policies from the business team's policy service.

    NOT IMPLEMENTED, AND DELIBERATELY SO. The service does not exist yet,
    and a provider that fabricated a response -- or quietly fell back to
    the config file -- would let an assessment claim a source it never
    consulted. Selecting this provider today produces POLICY_UNAVAILABLE
    on every assessment, which is the truth.

    WHAT IMPLEMENTING IT MEANS: call the service, map its response onto
    `EligibilityPolicy` (the same type the config provider returns), and
    raise PolicyUnavailable on any failure. Nothing else in the eligibility
    package changes.
    """

    name = "api"

    def get_policy(self, product, policy_id=None, version=None):
        raise PolicyUnavailable(
            "ELIGIBILITY_POLICY_PROVIDER=api is selected, but no policy "
            "service has been integrated yet.")


#: Provider name -> class. The extension point for a new source.
_PROVIDERS: dict[str, type[PolicyProvider]] = {
    ConfigPolicyProvider.name: ConfigPolicyProvider,
    ApiPolicyProvider.name: ApiPolicyProvider,
}


def register_provider(provider: type[PolicyProvider]) -> None:
    """Make a new policy source selectable by its `name`."""
    _PROVIDERS[provider.name] = provider


def get_provider() -> PolicyProvider:
    """
    The provider configuration selects.

    AN UNKNOWN NAME IS NOT A FALLBACK TO THE FILE. A typo in
    ELIGIBILITY_POLICY_PROVIDER must not silently serve the demo policy to
    a deployment that believes it is reading the real one.
    """
    name = policy_file.provider_name()
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise PolicyUnavailable(
            f"Unknown eligibility policy provider '{name}'. "
            f"Known: {', '.join(sorted(_PROVIDERS))}.")
    return provider()


def get_policy(product: str | None, policy_id: str | None = None,
               version: str | None = None) -> EligibilityPolicy:
    """The policy for this product, from whichever provider is active."""
    return get_provider().get_policy(product, policy_id=policy_id, version=version)


# ---------------------------------------------------------------------------
# config document -> EligibilityPolicy
# ---------------------------------------------------------------------------


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _from_config(raw: dict[str, Any], *, product: str, provider: str) -> EligibilityPolicy:
    income = _section(raw, "income")
    foir = _section(raw, "foir")
    loan = _section(raw, "loan")
    tenure = _section(raw, "tenure")
    interest = _section(raw, "interest")
    employment = _section(raw, "employment")
    obligations = _section(raw, "obligations")
    ltv = _section(raw, "ltv")

    allowed = employment.get("allowed")

    try:
        return EligibilityPolicy(
            policy_id=str(raw.get("policy_id") or ""),
            version=str(raw.get("version") or ""),
            product=str(raw.get("product") or product).upper(),
            status=str(raw.get("status") or "UNCONFIRMED"),
            provider=provider,
            enabled=bool(raw.get("enabled", True)),
            breach_status=str(raw.get("breach_status") or "REVIEW").upper(),
            income_basis=str(income.get("basis") or "NET_PAY").upper(),
            minimum_monthly_income=income.get("minimum_monthly_income"),
            on_income_review=str(income.get("on_income_review") or "REVIEW").upper(),
            foir_maximum_percent=foir.get("maximum_percent"),
            foir_fail_above_percent=foir.get("fail_above_percent"),
            loan_minimum_amount=loan.get("minimum_amount"),
            loan_maximum_amount=loan.get("maximum_amount"),
            tenure_minimum_months=tenure.get("minimum_months"),
            tenure_maximum_months=tenure.get("maximum_months"),
            annual_rate_percent=interest.get("annual_rate_percent"),
            employment_allowed=(tuple(str(a).upper() for a in allowed)
                                if isinstance(allowed, list) else None),
            obligations_accepted_sources=tuple(
                str(s).upper() for s in (obligations.get("accepted_sources") or [])),
            ltv_enabled=bool(ltv.get("enabled", False)),
            ltv_maximum_percent=ltv.get("maximum_percent"),
            property_value_accepted_sources=tuple(
                str(s).upper() for s in (ltv.get("accepted_value_sources") or [])),
        )
    except Exception as exc:
        # A malformed file is a policy nobody can apply.
        raise PolicyUnavailable(f"The configured policy for {product} is malformed: {exc}") from exc


__all__ = [
    "ApiPolicyProvider", "ConfigPolicyProvider", "EligibilityPolicy",
    "PolicyProvider", "PolicyUnavailable", "get_policy", "get_provider",
    "register_provider",
]
