"""
CONFIRMED ACTIONS FROM THE CONVERSATION (2026-10-06).

When the copilot PROPOSES an action that needs confirmation (an action carrying
`requires_confirmation: true`, e.g. RAISE_QUERY), the conversation state keeps the
STRUCTURED action -- never a phrase -- as the pending interaction. A later "yes /
haan / kar do / ok" confirms THAT action; "no / nahi / mat karo" declines it. The
words never select or build an action: the action comes only from server-side state
the copilot itself proposed, so typing a phrase can never trigger a write.

EXECUTION IS THE SAME PATH AS THE BUTTON: the same authorization (case ownership,
write scope, per-stage permission inside the domain service), an idempotency key
(a repeated "yes" cannot create a second record), and a READ-BACK -- the reply says
what the store now holds, never what the call hoped.

A new confirmable action is added by registering an executor here; the dialogue
layer does not change.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ActionRefused(Exception):
    """The caller may not do this, or the action is no longer valid. `message` is user-safe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _raise_query(action: dict[str, Any], *, claims: dict[str, Any], case_id: str,
                 idempotency_key: str, request_id: str) -> dict[str, Any]:
    from app.agents.los import queries
    from app.security import access
    from app.security.auth import get_scopes, get_subject

    try:
        access.authorize_claims(claims, case_id=case_id, write=True)
    except access.AccessDenied:
        raise ActionRefused("CASE_ACCESS_DENIED", "You are not authorized to act on this case.") from None
    body = dict(action.get("body") or {})
    try:
        created = queries.raise_query(
            case_id, target_type=str(body.get("target_type") or "CASE"),
            query_type=str(body.get("query_type") or "CLARIFICATION"), text=str(body.get("text") or ""),
            actor=get_subject(claims), scopes=set(get_scopes(claims)), target_id=body.get("target_id"),
            party_id=body.get("party_id"), evidence_refs=list(body.get("evidence_refs") or []),
            idempotency_key=idempotency_key, request_id=request_id, target_stage=body.get("target_stage"),
            severity=body.get("severity"), subject=body.get("subject"))
    except queries.QueryError as exc:
        public = exc.public()
        raise ActionRefused(str(public.get("code") or public.get("error") or "QUERY_REFUSED"),
                            str(public.get("message") or "The query could not be raised.")) from None
    # READ-BACK: the query as the store now holds it
    query_id = (created or {}).get("query_id")
    stored = next((q for q in queries.list_queries(case_id) if q.get("query_id") == query_id), None)
    if not stored:
        raise ActionRefused("NOT_RECORDED", "The query was not recorded. Please try again.")
    where = f" to {stored['target_stage']}" if stored.get("target_stage") else ""
    status = str(stored.get("status") or "open").lower()
    if (created or {}).get("result") == "EXISTING":
        answer = (f"This query is already {status} ({stored['query_id']}): \"{stored.get('text')}\". "
                  f"I haven't raised a second one.")
    else:
        answer = f"Done -- I raised the query{where} ({stored['query_id']}): \"{stored.get('text')}\" It is {status} now."
    return {"answer": answer, "result": {"action": "RAISE_QUERY", "outcome": (created or {}).get("result"),
                                         "query": stored}}


def _query_ready(action: dict[str, Any]) -> bool:
    """A query is confirmable only once it says what is asked (a blank offer needs wording first)."""
    return bool(str((action.get("body") or {}).get("text") or "").strip())


#: action id -> (executor, ready). Only actions listed here can be confirmed from the chat.
EXECUTORS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[[dict[str, Any]], bool]]] = {
    "RAISE_QUERY": (_raise_query, _query_ready),
}

#: What the conversation state keeps of a proposed action -- what the executor needs, nothing more.
_KEPT = ("action", "action_id", "label", "method", "endpoint", "body", "target_stage", "requires_confirmation")


def _kind(action: dict[str, Any]) -> str:
    return str(action.get("action") or action.get("action_id") or "")


def confirmable(actions: list[Any] | None) -> list[dict[str, Any]]:
    """The proposed actions a typed confirmation may carry out (requires_confirmation + an executor)."""
    out = []
    for a in actions or []:
        if isinstance(a, dict) and a.get("requires_confirmation") and a.get("available", True) \
                and _kind(a) in EXECUTORS and EXECUTORS[_kind(a)][1](a):
            out.append(a)
    return out


def stored(action: dict[str, Any]) -> dict[str, Any]:
    return {k: action[k] for k in _KEPT if k in action}


def execute(action: dict[str, Any], *, case_id: str, **kwargs: Any) -> dict[str, Any]:
    entry = EXECUTORS.get(_kind(action))
    if entry is None:
        raise ActionRefused("NOT_CONFIRMABLE", "That can't be done from the chat.")
    # THE ACTION BELONGS TO THIS CASE: a proposal made on one case is never carried
    # out on another (the conversation state is per case as well).
    endpoint = str(action.get("endpoint") or "")
    if endpoint and f"/cases/{case_id}/" not in endpoint + "/":
        raise ActionRefused("ACTION_CASE_MISMATCH", "That request was made for a different case. "
                                                    "Nothing was done.")
    return entry[0](action, case_id=case_id, **kwargs)


__all__ = ["ActionRefused", "EXECUTORS", "confirmable", "execute", "stored"]
