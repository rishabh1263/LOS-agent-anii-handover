"""
6-MVP CASE WORKSPACE (COPILOT_CASE_WORKSPACE, default off; docs/STEP6_MVP_CASE_WORKSPACE.md).

An officer's OWN cases -- live grants only (`list_granted_cases`), every one re-checked
with the ordinary ownership check -- listed, opened and left by a click (OPEN_CASE /
EXIT_CASE / LIST_CASES) or by words. Inside a case every question is answered for it.

NOTHING PER SENTENCE. Phrases, labels, counts and the name threshold are
applicant_agent.yaml `chatbot.case_workspace`; ordinals ("2", "doosra", "last") are
the conversation ordinal lists (semantic_concepts.yaml) read by `state._ordinal`;
a name is matched fuzzily, and only among the caller's own cases.

ITS OWN STATE. The per-case conversation state resets when the case changes (so two
cases never mix); the workspace keeps only the open case id and the last list, as
public ids, in its own record of the same store.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any

FLAG = "COPILOT_CASE_WORKSPACE"
_ID = {"case": re.compile(r"\bCASE-[0-9A-F]{4,}\b", re.I), "applicant": re.compile(r"\bAPP-[0-9A-Z]{4,}\b", re.I),
       "co_applicant": re.compile(r"\bCOAPP-[0-9A-Z]{2,}\b", re.I)}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("case_workspace") or {}


def _label(key: str, **values: Any) -> str:
    return str((_cfg().get("labels") or {}).get(key, key)).format(**values)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ-]", " ", str(text or "").lower())).strip()


def _says(kind: str, text: str) -> bool:
    """Whether the text contains one of the configured phrases of this kind (whole words)."""
    said = f" {_norm(text)} "
    return any(f" {_norm(p)} " in said for p in (_cfg().get("phrases") or {}).get(kind, []))


# --------------------------------------------------------------------------
# state (labels only)
# --------------------------------------------------------------------------

def _state(subject: str, workspace_id: str | None):
    from app.agents.applicant.copilot.conversation import state as conv

    key = "ws:" + (workspace_id or "default")
    found = conv.STORE.get(subject, key)
    return found or conv.ConversationState(conversation_id=key, subject_key=subject)


def _save(state) -> None:
    from app.agents.applicant.copilot.conversation import state as conv

    conv.STORE.put(state)


# --------------------------------------------------------------------------
# the caller's cases
# --------------------------------------------------------------------------

def _subject(claims: dict[str, Any]) -> str:
    from app.security.auth import get_subject

    return str(get_subject(claims) or "")


def my_cases(claims: dict[str, Any]) -> list[Any]:
    """The caller's openable applications: live grants, each re-checked by the ownership rule."""
    from app.security import access
    from app.store import get_repository

    out = []
    for application in get_repository().list_granted_cases(_subject(claims)):
        try:
            authorize(claims, application.case_id)
        except access.AccessDenied:
            continue
        out.append(application)
    return out


def authorize(claims: dict[str, Any], case_id: str) -> None:
    """
    THE ORDINARY OWNERSHIP CHECK, THEN the conversation layer -- in that order.
    `authorize_conversation` alone checks nothing for a non-service caller (it is a
    layer ON TOP of `authorize`); found by the 6-MVP refusal test. Raises AccessDenied.
    """
    from app.security import access

    access.authorize_claims(claims, case_id=case_id)
    access.authorize_conversation(_subject(claims), access.get_scopes(claims), case_id=case_id)


def _stage(case_id: str) -> str | None:
    from app.agents.los import stages

    stage = stages.resolve(case_id).stage
    return getattr(stage, "value", None)


def _bucket(application: Any) -> str:
    counts = _cfg().get("counts") or {}
    status, stage = str(application.status or "").upper(), (_stage(application.case_id) or "")
    rejected = counts.get("rejected") or {}
    if status in rejected.get("statuses", []) or stage in rejected.get("stages", []):
        return "rejected"
    completed = counts.get("completed") or {}
    if status in completed.get("statuses", []) or (completed.get("stages_past") and stage
                                                   and stage != completed["stages_past"]):
        return "completed"
    return "pending"


def _name(applicant_id: str, *, short: bool) -> str:
    from app.store import get_repository

    applicant = get_repository().get_applicant(applicant_id)
    full = str(getattr(applicant, "full_name", None) or "").strip()
    if not full:
        return "--"
    if short and _cfg().get("name_in_list", "first_name_last_initial") == "first_name_last_initial":
        parts = full.split()
        return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")
    return full


def _row_status(application: Any) -> tuple[str, str]:
    """(label, kind) -- a KYC issue first, then documents pending, else ready."""
    from app.agents.applicant import workflow
    from app.agents.los import kyc_gate
    from app.store import get_repository

    repository = get_repository()
    try:
        if kyc_gate.evaluate(application.case_id, repository)["status"] == "BLOCKED":
            return _label("row_kyc"), "KYC"
    except Exception:  # noqa: BLE001 - a row is a summary; the case itself has the detail
        pass
    try:
        applicant = repository.get_applicant(application.applicant_id)
        ready = workflow.readiness(applicant, application, repository.list_documents(application.case_id))
        if ready["status"] == "READY_FOR_CPA":
            return _label("row_ready"), "READY"
        docs = [i for i in ready.get("blocking_items") or [] if str(i.get("type", "")).upper() == "DOCUMENT"]
        if docs:
            return _label("row_docs", n=len(docs)), "DOCS"
    except Exception:  # noqa: BLE001
        pass
    return _label("row_other"), "OTHER"


def _main_blocker(application: Any) -> str | None:
    from app.agents.applicant import workflow
    from app.agents.los import kyc_gate
    from app.store import get_repository

    repository = get_repository()
    try:
        lines = kyc_gate.blocking_summary(kyc_gate.evaluate(application.case_id, repository))
        if lines:
            return lines[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        applicant = repository.get_applicant(application.applicant_id)
        items = workflow.readiness(applicant, application,
                                   repository.list_documents(application.case_id)).get("blocking_items") or []
        if items:
            return str(items[0].get("detail") or items[0].get("label") or items[0].get("code"))
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------

def _base(request_id: str, intent: str, answer: str, **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "intent": intent, "answer": answer, "category": "CASE_ONLY",
            "query_type": "CASE_FACT", "response_source": "STRUCTURED", "documents": [], "actions": [],
            "errors": [], "tools_invoked": ["application.list"], "suggested_questions": [], **extra}


def list_view(claims: dict[str, Any], request_id: str, state, *, page: int = 0) -> dict[str, Any]:
    cases = my_cases(claims)
    if not cases:
        state.listed_case_ids = []
        return _base(request_id, "CASE_LIST", _label("none"), workspace_view={"case_list": []})
    size = int(_cfg().get("page_size", 10))
    counts = {"pending": 0, "completed": 0, "rejected": 0}
    for application in cases:
        counts[_bucket(application)] += 1
    head = [_label("total", n=len(cases))] + [_label(k, n=v) for k, v in counts.items() if v]   # a 0 is not shown
    # GROUPED BY APPLICANT: one applicant can have several cases. Groups keep the newest-first order of
    # their newest case; the numbering runs across groups, so "2" / "doosra" pick the row as shown.
    order: dict[str, int] = {}
    for application in cases:
        order.setdefault(str(application.applicant_id), len(order))
    cases = sorted(cases, key=lambda a: order[str(a.applicant_id)])     # stable: newest first inside a group
    per_applicant = {key: sum(1 for a in cases if str(a.applicant_id) == key) for key in order}
    window = cases[page * size:(page + 1) * size]
    rows, lines, groups = [], [], []
    for number, application in enumerate(window, page * size + 1):
        status, kind = _row_status(application)
        stage = _stage(application.case_id) or "--"
        name = _name(application.applicant_id, short=True)
        if not groups or groups[-1]["applicant_id"] != application.applicant_id:
            n = per_applicant[str(application.applicant_id)]
            count = _label("group_one") if n == 1 else _label("group_many", n=n)
            groups.append({"applicant_id": application.applicant_id, "applicant_name": name, "case_count": n,
                           "case_ids": []})
            lines += ([""] if len(groups) > 1 else []) + [
                _label("group", applicant_id=application.applicant_id, name=name, count=count)]
        groups[-1]["case_ids"].append(application.case_id)
        rows.append({"number": number, "case_id": application.case_id, "applicant_id": application.applicant_id,
                     "applicant_name": name, "stage": stage, "status_label": status, "status_kind": kind,
                     "emoji": status.split(" ")[0], "action": {"type": "open_case", "case_id": application.case_id}})
        lines.append(_label("row", number=number, case_id=application.case_id, name=name,
                            applicant_id=application.applicant_id, stage=stage, status=status))
    more = (page + 1) * size < len(cases)
    state.listed_case_ids = [r["case_id"] for r in rows]
    answer = "\n".join([_label("header") + " -- " + " · ".join(head), ""] + lines
                       + ([""] + [_label("more")] if more else []) + ["", _label("ask")])
    return _base(request_id, "CASE_LIST", answer, workspace_view={
        "case_list": rows, "applicant_groups": groups,
        "counts": {"total": len(cases), **{k: v for k, v in counts.items() if v}},
        "page": page, "has_more": more},
        suggested_questions=[f"{r['number']}" for r in rows][:3])


def open_view(application: Any, request_id: str, state) -> dict[str, Any]:
    state.active_case_id = application.case_id
    # 6c: the cases opened this session, oldest first ("pehle wala case" goes back one)
    history = [c for c in (state.case_history or []) if c != application.case_id]
    state.case_history = (history + [application.case_id])[-10:]
    name = _name(application.applicant_id, short=False)
    blocker = _main_blocker(application)
    lines = [_label("opened", case_id=application.case_id, name=name),
             _label("snapshot_stage", stage=_stage(application.case_id) or "--"),
             _label("snapshot_blocker", blocker=blocker) if blocker else _label("snapshot_clear"),
             "", _label("suggest", question=_cfg().get("suggested_question", "Kya baaki hai?"))]
    return _base(request_id, "CASE_OPENED", "\n".join(lines), case_id=application.case_id,
                 applicant_id=application.applicant_id, workspace_view={"workspace": workspace_block(application.case_id)})


def workspace_block(case_id: str) -> dict[str, Any]:
    quick = list(_cfg().get("quick_questions") or [])
    return {"case_id": case_id, "header": _label("in_case", case_id=case_id),
            "buttons": [{"type": "exit_case", "label": "🔒 Exit"}, {"type": "switch_case", "label": "🔁 Switch case"}]
            + [{"type": "ask", "label": q, "message": q} for q in quick]}


# --------------------------------------------------------------------------
# selection: number / ordinal / id / name -- among the caller's own cases only
# --------------------------------------------------------------------------

@dataclass
class Selection:
    application: Any = None
    candidates: list[Any] = field(default_factory=list)


def select(text: str, claims: dict[str, Any], state) -> Selection:
    from app.agents.applicant.copilot.conversation import state as conv

    cases = my_cases(claims)
    by_case = {a.case_id.upper(): a for a in cases}
    listed = [by_case[c.upper()] for c in state.listed_case_ids if c.upper() in by_case]
    if listed:
        # "2 kholo" / "doosra wala case open karo": the open / case words (config phrases) are dropped so
        # the ordinal reader sees only "2" / "doosra"
        # (not the switch words: "doosra" in "doosra case" IS the ordinal)
        bare = " ".join(w for w in _norm(text).split() if w not in _noise(("open", "case_word", "filler"))) or text
        for candidate in (text, bare):
            index = conv._ordinal(candidate, len(listed))
            if index is not None and 0 <= index < len(listed):
                return Selection(listed[index])
    for kind, pattern in _ID.items():
        found = pattern.search(text or "")
        if not found:
            continue
        token = found.group(0).upper()
        if kind == "case":
            hits = [a for a in cases if a.case_id.upper().startswith(token)]
        elif kind == "applicant":
            hits = [a for a in cases if str(a.applicant_id).upper().startswith(token)]
        else:
            hits = [a for a in cases if str(getattr(a, "co_applicant_id", "") or "").upper().startswith(token)]
        return Selection(hits[0]) if len(hits) == 1 else Selection(candidates=hits)
    return _by_name(text, cases)


def _noise(kinds: tuple[str, ...] = ("open", "case_word", "list", "switch", "filler")) -> set[str]:
    """The open / case / list / switch words (config phrases) -- not part of a name or an ordinal."""
    phrases = _cfg().get("phrases") or {}
    return {w for kind in kinds for p in phrases.get(kind, []) for w in _norm(p).split()}


def _by_name(text: str, cases: list[Any]) -> Selection:
    words = [w for w in _norm(text).split() if w not in _noise() and len(w) >= 3]
    if not words:
        return Selection()
    threshold = float(_cfg().get("name_match_threshold", 0.84))
    scored = []
    for application in cases:
        full = _name(application.applicant_id, short=False).lower().split()
        best = max((difflib.SequenceMatcher(None, w, n).ratio() for w in words for n in full), default=0.0)
        if best >= threshold:
            scored.append((best, application))
    if not scored:
        return Selection()
    top = max(s for s, _ in scored)
    hits = [a for s, a in scored if s >= top - 0.02]
    return Selection(hits[0]) if len(hits) == 1 else Selection(candidates=hits)


def which_view(candidates: list[Any], request_id: str, state) -> dict[str, Any]:
    options = [f"{a.case_id} ({_name(a.applicant_id, short=True)})" for a in candidates[:5]]
    state.listed_case_ids = [a.case_id for a in candidates[:5]]
    answer = _label("which") + "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
    return _base(request_id, "CASE_SELECTION", answer, query_type="CLARIFICATION",
                 clarification_required={"reason": "CASE_AMBIGUOUS", "question": _label("which"), "options": options},
                 workspace_view={"case_list": [{"case_id": a.case_id, "action": {"type": "open_case", "case_id": a.case_id}}
                                             for a in candidates[:5]]})


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """What the route does: a finished reply, or the case to answer the question for."""
    reply: dict[str, Any] | None = None
    case_id: str | None = None
    applicant_id: str | None = None
    #: the question named a case other than the open one: answer it, then ask to switch
    other_case: str | None = None
    #: inside the open case: decorate the answer with its header and buttons
    in_case: str | None = None


def handle(action: str, message: str, case_id: str | None, claims: dict[str, Any], request_id: str,
           context: dict[str, Any] | None) -> Turn:
    from app.security import access

    subject = _subject(claims)
    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    state = _state(subject, workspace_id)
    text = message or ""

    def done(reply: dict[str, Any]) -> Turn:
        reply["context"] = {**(reply.get("context") or {}), "workspace_id": workspace_id or "default"}
        _save(state)
        return Turn(reply=reply)

    def opened(application: Any) -> Turn:
        return done(open_view(application, request_id, state))

    if action == "EXIT_CASE" or (action == "CUSTOM_QUERY" and state.active_case_id and _says("exit", text)):
        closed, state.active_case_id = state.active_case_id, None
        reply = list_view(claims, request_id, state)
        if closed:
            reply["answer"] = _label("closed", case_id=closed) + "\n\n" + reply["answer"]
        return done(reply)
    if action == "LIST_CASES" or (action == "CUSTOM_QUERY" and (_says("list", text) or _says("switch", text))):
        # the list (or a switch) closes the open case first: never two cases at once
        closed, state.active_case_id = state.active_case_id, None
        reply = list_view(claims, request_id, state)
        if closed:
            reply["answer"] = _label("closed", case_id=closed) + "\n\n" + reply["answer"]
        return done(reply)
    if action == "CUSTOM_QUERY" and state.listed_case_ids and _says("more", text) and len(_norm(text).split()) <= 3:
        ids = [a.case_id for a in my_cases(claims)]
        first = state.listed_case_ids[0]
        size = int(_cfg().get("page_size", 10))
        page = ids.index(first) // size + 1 if first in ids else 0
        return done(list_view(claims, request_id, state, page=page))
    if action == "CUSTOM_QUERY" and _says("previous", text):
        # "pehle wala case" (6c): the case opened before the current one, re-checked like any open
        earlier = [c for c in (state.case_history or []) if c != state.active_case_id]
        if earlier:
            from app.store import get_repository

            authorize(claims, earlier[-1])
            return opened(get_repository().get_application(earlier[-1]))
    if action == "OPEN_CASE":
        if not case_id:
            return done(_base(request_id, "CASE_SELECTION", _label("not_found")))
        authorize(claims, case_id)                                  # the same 403 as any case not yours
        from app.store import get_repository

        return opened(get_repository().get_application(case_id))

    if action == "CUSTOM_QUERY" and not case_id:
        names_a_case = any(p.search(text) for p in _ID.values())
        picking = bool(state.listed_case_ids) and len(_norm(text).split()) <= 4
        if names_a_case or _says("open", text) or picking:
            chosen = select(text, claims, state)
            if chosen.application is not None:
                if state.active_case_id and chosen.application.case_id != state.active_case_id \
                        and not _says("open", text) and names_a_case and len(_norm(text).split()) > 2:
                    # a question about ANOTHER case while inside one: answered, never switched silently
                    _save(state)
                    return Turn(case_id=chosen.application.case_id, applicant_id=chosen.application.applicant_id,
                                other_case=chosen.application.case_id)
                return opened(chosen.application)
            if chosen.candidates:
                return done(which_view(chosen.candidates, request_id, state))
            if names_a_case or _says("open", text):
                return done(_base(request_id, "CASE_SELECTION", _label("not_found")))
    if not case_id and state.active_case_id:
        from app.store import get_repository

        application = get_repository().get_application(state.active_case_id)
        if application is not None:
            _save(state)
            return Turn(case_id=application.case_id, applicant_id=application.applicant_id,
                        in_case=application.case_id)
    _save(state)
    return Turn()


def decorate(result: dict[str, Any], turn: Turn) -> dict[str, Any]:
    """The answer inside a case: the 📍 line first, the workspace block attached."""
    if not isinstance(result, dict):
        return result
    if turn.in_case:
        # the "📍 CASE-xxx" line is added by the route LAST (in_case_header), after any view that rewrites the
        # answer (document actions, style) -- found by the frontend contract test
        result["workspace_in_case"] = turn.in_case
        # published as presentation.workspace by presentation.build (answering/presentation.py)
        result["workspace_view"] = {**(result.get("workspace_view") or {}), "workspace": workspace_block(turn.in_case)}
    if turn.other_case:
        result["answer"] = str(result.get("answer") or "") + "\n\n" + _label("other_case", case_id=turn.other_case)
    return result


def in_case_header(published: dict[str, Any]) -> dict[str, Any]:
    """The "📍 CASE-xxx" first line of an answer given inside an opened case (applied last by the route)."""
    case_id = published.pop("workspace_in_case", None) if isinstance(published, dict) else None
    if case_id:
        published["answer"] = _label("in_case", case_id=case_id) + "\n" + str(published.get("answer") or "")
    return published


__all__ = ["FLAG", "Selection", "Turn", "decorate", "enabled", "handle", "in_case_header", "list_view", "my_cases",
           "open_view", "select", "workspace_block"]
