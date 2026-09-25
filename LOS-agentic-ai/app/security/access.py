"""
Resource authorisation: may THIS caller touch THIS applicant or case?

THE GAP THIS CLOSES. Authentication proved who a caller is, and
`permissions.check_ownership` proved that a case belonged to the applicant
named in the request. Nothing tied the CALLER to that applicant: any token
with a read scope could read any applicant's case by supplying the matching
pair of ids -- ids the caller chooses.

THE OWNERSHIP MODEL, from what the auth design already has:

    SERVICE PRINCIPALS   a token carrying the read-all scope
                         (`permissions.read_all_scope`, `los.read`) may read
                         any case; one carrying the write-all scope
                         (`permissions.write_all_scope`, `los.write`) may
                         read and write any case. These are the existing
                         service-account scopes; nothing new is invented.

    EVERYONE ELSE        may access an applicant or case only through a GRANT
                         bound to their JWT subject (`sub`). A grant is
                         recorded when that subject creates the applicant or
                         the case (`record_ownership`), and a grant on an
                         applicant covers every case of that applicant.

    NO SUBJECT, NO GRANT, UNKNOWN RESOURCE  ->  refused. Fail closed.

WHAT IS NEVER TRUSTED. The applicant_id and case_id a caller sends. They
select what to check; they never decide the answer. A case named with the
wrong applicant is refused exactly as a case the caller does not own, and
both are refused in the same words as a case that does not exist, because
confirming that a case exists under someone else is itself a disclosure.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from fastapi import Depends, HTTPException, status

from app.security.auth import get_scopes, get_subject, require_jwt

logger = logging.getLogger(__name__)

APPLICANT = "APPLICANT"
CASE = "CASE"


class AccessDenied(Exception):
    """The caller may not access this applicant or case."""

    def __init__(self, code: str = "CASE_NOT_ACCESSIBLE",
                 message: str = "You are not authorized to access this case."):
        super().__init__(message)
        self.code = code
        self.message = message


# ==========================================================================
# SERVICE PRINCIPALS
# ==========================================================================

def read_all_scope() -> str:
    from app.agents.applicant import config

    return config.read_all_scope()


def write_all_scope() -> str:
    from app.agents.applicant import config

    return config.write_all_scope()


def is_service(scopes: Iterable[str], *, write: bool) -> bool:
    """Whether these scopes make the caller a service principal for this."""
    held = set(scopes or ())
    if write_all_scope() in held:
        return True
    return not write and read_all_scope() in held


# ==========================================================================
# THE DECISION
# ==========================================================================

def authorize(subject: str | None, scopes: Iterable[str], *,
              applicant_id: str | None = None, case_id: str | None = None,
              write: bool = False, creating: bool = False) -> None:
    """
    Raise AccessDenied unless this caller may access the named resource.

    `creating` allows a resource that does not exist YET -- the caller is
    about to create it and will be granted it (`record_ownership`). It never
    allows touching an existing resource the caller does not hold.
    """
    from app.store import RepositoryError, get_repository

    try:
        repository = get_repository()
    except RepositoryError as exc:
        raise AccessDenied("CASE_STORE_UNAVAILABLE", str(exc)) from exc

    # CONSISTENCY FIRST, FOR EVERYONE. A case named with the wrong applicant
    # is refused even for a service principal: the pair must be true.
    application = repository.get_application(case_id) if case_id else None
    if application is not None and applicant_id \
            and application.applicant_id != applicant_id:
        raise AccessDenied()

    if is_service(scopes, write=write):
        return

    if not subject:
        raise AccessDenied()

    if case_id:
        if application is not None:
            if (repository.has_access(subject, CASE, case_id)
                    or repository.has_access(subject, APPLICANT,
                                             application.applicant_id)):
                return
            raise AccessDenied()
        if not creating:
            raise AccessDenied()
        # A NEW case: whoever may create it must hold its applicant, unless
        # the applicant is new as well.

    if applicant_id:
        if repository.get_applicant(applicant_id) is None:
            if creating:
                return
            raise AccessDenied()
        if repository.has_access(subject, APPLICANT, applicant_id):
            return
        raise AccessDenied()

    if not case_id:
        # NOTHING NAMED. Creating, that is a brand-new case: /los/process
        # generates random ids (flow.py, parties.py) and the caller is
        # granted them once persisted. Reading, there is nothing to grant.
        if creating:
            return
        raise AccessDenied()


def authorize_claims(claims: dict[str, Any], **kwargs: Any) -> None:
    """`authorize` for a request's JWT claims."""
    authorize(get_subject(claims), get_scopes(claims), **kwargs)


def record_ownership(subject: str | None, *, applicant_id: str | None = None,
                     case_id: str | None = None) -> None:
    """
    Grant the creating subject access to what it just created.

    Never raises: a grant that could not be written leaves the caller
    WITHOUT access to it (fail closed), which the next request reports.
    """
    if not subject:
        return
    try:
        from app.store import get_repository

        repository = get_repository()
        if applicant_id:
            repository.grant_access(subject, APPLICANT, applicant_id)
        if case_id:
            repository.grant_access(subject, CASE, case_id)
    except Exception as exc:
        logger.warning("Could not record ownership for a new resource: %s",
                       type(exc).__name__)


def http_denied(exc: AccessDenied, request_id: str | None = None) -> HTTPException:
    """The route-level refusal, in the shape the Copilot routes already use."""
    detail: dict[str, Any] = {"code": "CASE_ACCESS_DENIED",
                              "message": "You are not authorized to access "
                                         "this case."}
    if exc.code == "CASE_STORE_UNAVAILABLE":
        detail["code"] = exc.code
    if request_id:
        detail["request_id"] = request_id
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ==========================================================================
# ROUTE-LEVEL SCOPE
# ==========================================================================

def require_any_scope(*scopes: str, write: bool = False) -> Callable[..., dict[str, Any]]:
    """
    A route dependency: a valid JWT carrying ANY of `scopes`, or the
    service scope for this kind of access. 403 INSUFFICIENT_SCOPE otherwise.

    Returns the claims, so a handler can take it in place of `require_jwt`.
    """
    wanted = tuple(s for s in scopes if s)

    def dependency(claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
        held = set(get_scopes(claims))
        if held & set(wanted) or is_service(held, write=write):
            return claims
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "INSUFFICIENT_SCOPE",
                    "message": "This operation requires one of the scopes: "
                               + ", ".join(wanted
                                           + ((write_all_scope(),) if write
                                              else (read_all_scope(),))) + "."},
        )

    dependency.__name__ = "require_scope_" + "_".join(
        s.replace(":", "_").replace(".", "_") for s in wanted) or "require_scope"
    return dependency


__all__ = ["APPLICANT", "CASE", "AccessDenied", "authorize",
           "authorize_claims", "http_denied", "is_service",
           "read_all_scope", "record_ownership", "require_any_scope",
           "write_all_scope"]
