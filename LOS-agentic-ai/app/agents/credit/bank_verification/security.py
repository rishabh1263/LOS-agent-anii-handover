"""
Who is calling and what they may do (config roles).

PRODUCTION (demo_mode false): the caller is the verified LOS token (app.security.auth.require_jwt). Its scopes give the
role (config security.role_by_scope, first match); an AGENCY caller's agency is the token's claim named by
security.agency_claim -- never a request field. Demo headers are ignored.

DEMO MODE: the role is SIMULATED -- `X-Demo-Role` (CPA / AGENCY / VIEWER) and `X-Demo-User`, or the one demo user
(security.default_role) when none is sent.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.agents.credit.bank_verification.config import cfg, demo_mode, say, section


@dataclass(frozen=True)
class Caller:
    role: str
    user: str
    agency_id: str | None = None      # production: the agency an AGENCY caller belongs to (from its token)

    @property
    def actor(self) -> str:
        suffix = str(section("security").get("simulated_suffix", "")) if demo_mode() else ""
        return f"{self.role.lower()}:{self.user}{suffix}"

    def may(self, permission: str) -> bool:
        return permission in set((cfg().get("roles") or {}).get(self.role) or [])


def _from_token(credentials: HTTPAuthorizationCredentials | None) -> Caller:
    from app.security.access import get_scopes
    from app.security.auth import require_jwt

    claims = require_jwt(credentials)                        # 401 without a valid LOS token
    held = set(get_scopes(claims))
    spec = section("security")
    role = next((str(r["role"]) for r in spec.get("role_by_scope") or [] if str(r.get("scope")) in held), None)
    if role is None or role not in (cfg().get("roles") or {}) or role == spec.get("default_role"):
        raise HTTPException(403, {"code": "UNKNOWN_ROLE", "message": say("errors.UNKNOWN_ROLE", role=role or "—")})
    agency = claims.get(str(spec.get("agency_claim") or "agency_id")) if role == "AGENCY" else None
    if role == "AGENCY" and not agency:
        raise HTTPException(403, {"code": "AGENCY_NOT_IDENTIFIED", "message": say("errors.AGENCY_NOT_IDENTIFIED")})
    return Caller(role, str(claims.get("sub") or "")[:60], str(agency) if agency else None)


def _bearer():
    from app.security.auth import bearer_scheme

    return bearer_scheme


def caller(x_demo_role: str | None = Header(default=None), x_demo_user: str | None = Header(default=None),
           credentials: HTTPAuthorizationCredentials | None = Security(_bearer())) -> Caller:
    if not demo_mode():
        return _from_token(credentials)
    role = (x_demo_role or str(section("security").get("default_role", "VIEWER"))).strip().upper()
    if role not in (cfg().get("roles") or {}):
        raise HTTPException(403, {"code": "UNKNOWN_ROLE", "message": say("errors.UNKNOWN_ROLE", role=role)})
    user = (x_demo_user or f"demo-{role.lower()}").strip()[:40]
    return Caller(role, user)


def require(who: Caller, permission: str) -> None:
    if not who.may(permission):
        raise HTTPException(403, {"code": "FORBIDDEN_FOR_ROLE",
                                  "message": say("errors.FORBIDDEN_FOR_ROLE", role=who.role,
                                                 permission=permission.replace("_", " "))})


def own_agency(who: Caller, agency_id: str | None) -> None:
    """Production: an agency acts only for itself -- the agency named in a finalize is the caller's own (403)."""
    if who.agency_id is not None and agency_id != who.agency_id:
        raise HTTPException(403, {"code": "NOT_YOUR_AGENCY", "message": say("errors.NOT_YOUR_AGENCY")})
