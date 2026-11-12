"""
Who is calling and what they may see and do -- from the verified LOS token only (require_jwt).

    permissions   the union of what the caller's scopes give (config access.scopes), plus, in DEMO MODE only, the
                  extra grants of that user (access.demo_grants)
    branches      the applications the user may open (access.branches by the token's `sub`; "*" = all)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends

from app.agents.credit.crime_verification.config import demo_mode, section
from app.agents.credit.crime_verification.services.errors import ServiceError
from app.security.auth import require_jwt
from app.security.access import get_scopes


@dataclass(frozen=True)
class Caller:
    user: str
    permissions: frozenset[str]
    branches: frozenset[str]

    def may(self, permission: str) -> bool:
        return permission in self.permissions

    def sees(self, branch: str) -> bool:
        return "*" in self.branches or branch in self.branches

    def branch_filter(self) -> list[str] | None:
        """For SQL: None = every branch."""
        return None if "*" in self.branches else sorted(self.branches)


def caller_from_claims(claims: dict[str, Any]) -> Caller:
    rules = section("access")
    user = str(claims.get("sub") or "")
    permissions: set[str] = set()
    for scope in get_scopes(claims):
        permissions.update((rules.get("scopes") or {}).get(scope) or [])
    if demo_mode():
        permissions.update((rules.get("demo_grants") or {}).get(user) or [])
    branches = frozenset(str(b) for b in (rules.get("branches") or {}).get(user) or [])
    if set(get_scopes(claims)) & set(rules.get("all_branches_scopes") or []):
        branches = frozenset({"*"})
    return Caller(user=user, permissions=frozenset(permissions), branches=branches)


def caller(claims: dict[str, Any] = Depends(require_jwt)) -> Caller:
    return caller_from_claims(claims)


def require(who: Caller, permission: str) -> None:
    if not who.may(permission):
        raise ServiceError(403, "NOT_PERMITTED", permission=permission.replace("_", " "))
