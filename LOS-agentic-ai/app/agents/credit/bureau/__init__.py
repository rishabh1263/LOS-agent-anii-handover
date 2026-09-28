"""
Bureau access for credit underwriting.

ONE INTERFACE (base.BureauProvider), and today ONE implementation: the demo
provider, which serves synthetic fixtures marked `is_demo`. There is no real
bureau integration in this codebase, and none is pretended: a production
deployment must supply a real provider, and the demo one refuses to run in
production unless ALLOW_DEMO_BUREAU=true is set deliberately.
"""

from __future__ import annotations

from app.agents.credit.bureau.base import (
    BureauError,
    BureauProvider,
    BureauReport,
    BureauTimeout,
)


def get_provider() -> BureauProvider:
    from app.agents.credit import config
    from app.core.exceptions import ConfigurationError

    name = config.bureau_provider()
    if name == "demo":
        if config.is_production() and not config.allow_demo_bureau():
            raise ConfigurationError(
                "The demo bureau provider is configured and ENVIRONMENT is "
                "production. Configure a real bureau provider, or set "
                "ALLOW_DEMO_BUREAU=true to override deliberately.")
        from app.agents.credit.bureau.demo import DemoBureauProvider

        return DemoBureauProvider()
    raise ConfigurationError(f"Unknown bureau provider: {name!r}. "
                             "No such integration exists in this codebase.")


__all__ = ["BureauError", "BureauProvider", "BureauReport", "BureauTimeout", "get_provider"]
