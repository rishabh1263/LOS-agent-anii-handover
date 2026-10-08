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


def _pick(value: Any) -> Any:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.pick(value)


def quick_questions() -> list[str]:
    """The quick-question buttons, in the locked reply language (a plain list is used as is)."""
    value = _cfg().get("quick_questions") or []
    picked = _pick(value) if isinstance(value, dict) else value
    return [str(q) for q in (picked if isinstance(picked, list) else [picked])]


def _label(key: str, **values: Any) -> str:
    # a label may be one string or a {language: text} map -- the locked reply language picks (FOS plan section 2)
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.pick((_cfg().get("labels") or {}).get(key, key)).format(**values)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ-]", " ", str(text or "").lower())).strip()


def _says(kind: str, text: str) -> bool:
    """Whether the text contains one of the configured phrases of this kind (whole words)."""
    said = f" {_norm(text)} "
    matched = [p for p in (_cfg().get("phrases") or {}).get(kind, []) if f" {_norm(p)} " in said]
    if kind != "list":
        return bool(matched)
    if matched:
        # "mere case ka details" names a TOPIC beyond the list (FOS plan 1.4): it is a question about the case,
        # never the list -- a list phrase counts only when every other word is a list word
        rest = said
        for phrase in sorted(matched, key=len, reverse=True):
            rest = rest.replace(f" {_norm(phrase)} ", " ")
        return set(rest.split()) <= _list_words()
    return _only_asks_for_the_list(text)


def _list_words() -> set[str]:
    rule = _cfg().get("list_rule") or {}
    return {_norm(w) for key in ("case_nouns", "cues", "allowed") for w in rule.get(key) or []} \
        | {w for p in (_cfg().get("phrases") or {}).get("list", []) for w in _norm(p).split()}


def _asks_more_than_the_switch(text: str) -> bool:
    """Whether a message naming "the other case" also asks something (any word beyond those phrases / fillers)."""
    phrases = _cfg().get("phrases") or {}
    plain = f" {_norm(text)} "
    for phrase in sorted(phrases.get("other_case") or [], key=len, reverse=True):
        plain = plain.replace(f" {_norm(phrase)} ", " ")
    rest = [w for w in plain.split() if w not in _noise() and w not in {"mere", "mera", "my", "the", "of"}]
    return bool(rest)


def _asks_beyond_the_id(text: str) -> bool:
    """
    "CASE-852C ka PAN status" asks something; "APP-B597 ke cases" / "CASE-852C kholo" only name it. The ids,
    the open / case / list / filler words (config phrases) and the list words are taken out; anything left is
    a question about that case.
    """
    plain = _norm(text)
    for pattern in _ID.values():
        plain = pattern.sub(" ", plain)
    return bool([w for w in plain.split() if w not in _noise() and w not in _list_words()])


def _only_asks_for_the_list(text: str) -> bool:
    """
    FOS plan 1.2: "all case ka list", "sab case dikhao", "mere kitne case hai", "give a list of case" -- a case
    noun, a list cue, and NOTHING ELSE but list words (applicant_agent.yaml case_workspace.list_rule). "case ka
    status dikhao" is not a list: "status" is not a list word.
    """
    rule = _cfg().get("list_rule") or {}
    words = _norm(text).split()
    nouns = {_norm(w) for w in rule.get("case_nouns") or []}
    cues = {_norm(w) for w in rule.get("cues") or []}
    return bool(words) and bool(nouns & set(words)) and bool(cues & set(words)) and set(words) <= _list_words()


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


def active_case(claims: dict[str, Any], workspace_id: str | None = None) -> Any:
    """The application the caller has open in this workspace (re-authorized now), else None."""
    from app.security import access
    from app.store import get_repository

    case_id = _state(_subject(claims), workspace_id).active_case_id
    if not case_id:
        return None
    try:
        authorize(claims, case_id)
    except access.AccessDenied:
        return None
    return get_repository().get_application(case_id)


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


def _over_target(case_id: str) -> str | None:
    """'⏰ 5d in FOS (target 3)' when the case is past its stage's turnaround target (timeline.py), else None."""
    try:
        from app.agents.applicant.copilot.capabilities import timeline

        if not timeline.enabled():
            return None
        t = timeline.build(case_id)
        if not t.get("over_target"):
            return None
        return _label("row_over_target", days=t["days_in_stage"], stage=t["stage"], target=t["target_days"])
    except Exception:  # noqa: BLE001 - a row is a summary; the case itself has the detail
        return None


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
        over = _over_target(application.case_id)
        if over:
            status = f"{status} · {over}"                  # FOS plan 7.3: past the stage's turnaround target
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


def paged_view(claims: dict[str, Any], request_id: str, state, query=None, *, ask: str | None = None) -> dict[str, Any]:
    """
    MASTER SPEC section 3 (COPILOT_CASE_LIST_PAGING): ONE page of the caller's own cases -- filtered, sorted,
    "Showing x-y of N", the options as links. The query is remembered for "aur dikhao".
    """
    from app.agents.applicant.copilot.answering import contract, language_lock
    from app.agents.applicant.copilot.capabilities import case_list, product_flow

    query = query or case_list.ListQuery()
    query.with_counts = True                     # the summary line names what needs action across all cases
    page = case_list.run(claims, query)
    lang = language_lock.current() or "en"
    state.list_query = page.query.as_dict()
    state.list_offset = page.page * page.size
    state.listed_case_ids = [r["case_id"] for r in page.rows]
    # section 15.2: the portfolio follow-up ("Do you want to know about a particular case?") closes the list
    closing = ([ask] if ask else product_flow.closing_for_list(state, page.query.as_dict())) if page.rows else None
    # the question travels apart (faq_block), after the table: no rewrite step touches its Yes / No links
    answer = case_list.render(page, lang, seed=state.turn_id or 0, closing=[] if closing else None)
    rows = [{**r, "action": {"type": "open_case", "case_id": r["case_id"]},
             "link": contract.link("open_case", lang, id=r["case_id"])} for r in page.rows]
    reply = _base(request_id, "CASE_LIST", answer, workspace_view={
        "case_list": rows, "counts": {"total": page.all_total, **page.counts}, "matching": page.total,
        "page": page.page, "page_size": page.size, "has_more": page.has_more, "query": page.query.as_dict()})
    if closing:
        reply["faq_block"] = "\n".join(closing)
    return reply


def _list(claims: dict[str, Any], request_id: str, state, text: str = "", *, more: bool = False) -> dict[str, Any]:
    """The list: paged (section 3) when on, else the earlier grouped list."""
    from app.agents.applicant.copilot.capabilities import case_list

    if not case_list.enabled():
        return list_view(claims, request_id, state)
    if more:
        query = case_list.understand(text, state.list_query or {}) or case_list.ListQuery.from_dict(state.list_query)
        if query.page == case_list.ListQuery.from_dict(state.list_query).page and state.list_query:
            query.page += 1
        return paged_view(claims, request_id, state, query)
    return paged_view(claims, request_id, state, case_list.understand(text) if text else None)


def open_view(application: Any, request_id: str, state) -> dict[str, Any]:
    """
    FINAL FIX A1 / A4 -- less is more: the case brief (answering/case_brief.py: case + name + stage / the main status
    or blocker with its reason / "Next step:") and at most `case_brief.max_suggestions` links chosen by state -- the
    workflow step first (query the customer / move to CPA / query FOS), then a question. No downloads, no FAQ here.
    """
    from app.agents.applicant.copilot.answering import case_brief, contract, language_lock
    from app.agents.applicant.copilot.capabilities import product_flow, stage_flow

    state.active_case_id = application.case_id
    # 6c: the cases opened this session, oldest first ("pehle wala case" goes back one)
    history = [c for c in (state.case_history or []) if c != application.case_id]
    state.case_history = (history + [application.case_id])[-10:]
    lang = language_lock.current() or "en"
    brief = case_brief.build(application.case_id, lang)
    lines = brief["text"].split("\n")
    if contract.enabled():
        # MASTER SPEC section 11: what changed since this chat last looked at the case (only when something did)
        from app.agents.applicant.copilot.answering import realtime

        try:
            since = realtime.since_last(state.subject_key, state.conversation_id, application.case_id, lang)
        except Exception:  # noqa: BLE001 - a convenience line never fails the open
            since = None
        if since:
            lines.insert(1, since)
    reply = _base(request_id, "CASE_OPENED", "\n".join(lines), case_id=application.case_id,
                  applicant_id=application.applicant_id, workspace_view={"workspace": workspace_block(application.case_id)})
    reply["brief"] = True                        # officer_tools adds no second review under it
    reply["tts_text"] = "\n".join(lines[1:])     # the voice: the status and the next step (never the id line)
    limit = int(case_brief._cfg().get("max_suggestions", 2))
    links = (stage_flow.offers(application.case_id, lang) + [contract.ask(q) for q in brief["suggestions"]])[:limit]
    asked = product_flow.after_open(state)
    question = asked.split("\n")[0] if asked else ""   # picked from the flow's list -> "What do you want to know?"
    block = "\n".join(x for x in [question, " · ".join(links)] if x)
    if block:
        reply["faq_block"] = block
    reply["no_case_links"] = True                 # downloads / Show in UI only on a summary or when asked
    return reply


def workspace_block(case_id: str) -> dict[str, Any]:
    quick = quick_questions()
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
    #: which kind of id / name the text named ("case", "applicant", "co_applicant", "name"): picks the neutral
    #: not-found line (MASTER SPEC section 2 -- never confirming that it exists outside the caller's scope)
    named: str | None = None


def _co_applicant_cases(token: str, cases: list[Any]) -> list[Any]:
    """The caller's cases this co-applicant id is on (the co_applicants record, then the legacy column)."""
    try:
        from app.agents.los import co_applicants

        on = {c.upper() for c in co_applicants.cases_for(token)}
    except Exception:  # noqa: BLE001 - no record: only the legacy column below
        on = set()
    return [a for a in cases if a.case_id.upper() in on
            or str(getattr(a, "co_applicant_id", "") or "").upper().startswith(token)]


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
        offset = int(getattr(state, "list_offset", 0) or 0)
        number = re.fullmatch(r"\s*(\d{1,3})\s*", bare)
        if offset and number and offset < int(number.group(1)) <= offset + len(listed):
            return Selection(listed[int(number.group(1)) - offset - 1])      # row "7" on the second page
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
            hits = _co_applicant_cases(token, cases)
        return Selection(hits[0], named=kind) if len(hits) == 1 else Selection(candidates=hits, named=kind)
    if _says("this_applicant", text) and state.active_case_id:
        # "is applicant ke case dikhao": every case of the OPEN case's applicant, within scope
        current = by_case.get(state.active_case_id.upper())
        if current is not None:
            hits = [a for a in cases if a.applicant_id == current.applicant_id]
            return Selection(hits[0], named="applicant") if len(hits) == 1 else Selection(candidates=hits,
                                                                                          named="applicant")
    found = _by_name(text, cases)
    found.named = "name"
    return found


def not_found_view(request_id: str, named: str | None) -> dict[str, Any]:
    """The same neutral line for unknown and not-yours (labels.not_found_<kind>, else not_found)."""
    labels = _cfg().get("labels") or {}
    key = f"not_found_{named}" if named and f"not_found_{named}" in labels else "not_found"
    return _base(request_id, "CASE_SELECTION", _label(key))


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
    #: the question to answer instead of the typed text (the one asked before "which case?" was answered)
    message: str | None = None


def handle(action: str, message: str, case_id: str | None, claims: dict[str, Any], request_id: str,
           context: dict[str, Any] | None) -> Turn:
    from app.security import access

    subject = _subject(claims)
    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    state = _state(subject, workspace_id)
    state.picking = False                    # set only by product_flow.step for this turn (never persisted)
    text = message or ""
    if action == "CUSTOM_QUERY" and case_id and state.active_case_id and case_id != state.active_case_id:
        # THE OPENED CASE WINS (FOS plan 1.8): a frontend that sends the same case_id on every request (the one
        # from login) never overrides the case the officer opened here; the stale id is ignored, never read
        case_id = None

    def done(reply: dict[str, Any]) -> Turn:
        reply["context"] = {**(reply.get("context") or {}), "workspace_id": workspace_id or "default"}
        _save(state)
        return Turn(reply=reply)

    def opened(application: Any) -> Turn:
        flow = dict(state.flow or {})
        question = flow.pop("after_pick", None)
        if question:
            # "Application status" was asked with no case open -> "which case?" -> this pick: the case opens and the
            # ORIGINAL question is answered for it (never a second "what do you want to know?")
            state.flow = flow
            state.active_case_id = application.case_id
            history = [c for c in (state.case_history or []) if c != application.case_id]
            state.case_history = (history + [application.case_id])[-10:]
            _save(state)
            return Turn(case_id=application.case_id, applicant_id=application.applicant_id,
                        in_case=application.case_id, message=str(question))
        return done(open_view(application, request_id, state))

    if action == "CUSTOM_QUERY":
        # MASTER SPEC section 15.2: the answer to the product flow's pending question (Yes / No, Pending / Done);
        # anything else typed drops the question and is handled as usual
        from app.agents.applicant.copilot.capabilities import case_list as _flow_list
        from app.agents.applicant.copilot.capabilities import product_flow as _product_flow

        flowed = _product_flow.step(text, state)
        if flowed and flowed.get("answer"):
            reply = _base(request_id, "PRODUCT_FLOW", flowed["answer"], query_type="CONVERSATION", tools_invoked=[])
            if flowed.get("block"):
                reply["faq_block"] = flowed["block"]          # the option links, untouched by any rewrite step
            return done(reply)
        if flowed and flowed.get("list"):
            state.active_case_id = None
            return done(paged_view(claims, request_id, state, _flow_list.ListQuery(**flowed["list"])))
        if _product_flow.asks_show_in_ui(text):
            # FINAL FIX A: "show in UI" typed -- the action itself (the frontend opens the same case / list)
            reply = _base(request_id, "SHOW_IN_UI", "", case_id=state.active_case_id, query_type="CONVERSATION",
                          tools_invoked=[])
            heading, _, links = _product_flow.show_in_ui_reply(state.active_case_id, state.list_query).partition("\n")
            reply["answer"], reply["faq_block"], reply["no_case_links"] = heading, links, True
            return done(reply)
        if _product_flow.asks_download(text):
            # 15.3: "excel download karo" -- the links for the open case (scope re-checked when opened), else
            # for the last list; the links travel apart so no rewrite step touches them
            reply = _base(request_id, "DOWNLOADS", "", case_id=state.active_case_id, query_type="CONVERSATION",
                          tools_invoked=[])
            heading, _, links = _product_flow.downloads_reply(state.active_case_id, state.list_query).partition("\n")
            reply["answer"], reply["faq_block"] = heading, links
            reply["no_case_links"] = True                    # the links are the answer: not repeated below it
            return done(reply)

    if action == "EXIT_CASE" or (action == "CUSTOM_QUERY" and state.active_case_id and _says("exit", text)):
        closed, state.active_case_id = state.active_case_id, None
        reply = _list(claims, request_id, state)
        if closed:
            reply["answer"] = _label("closed", case_id=closed) + "\n\n" + reply["answer"]
        return done(reply)
    if action == "CUSTOM_QUERY" and _says("other_case", text) and _asks_more_than_the_switch(text):
        # "dusre case ka details do" (FOS plan 1.3): the question is about ANOTHER of the caller's cases --
        # one other case: switch to it and answer there; several: ask which (never the open case's answer)
        others = [a for a in my_cases(claims) if a.case_id != state.active_case_id]
        if len(others) == 1:
            state.active_case_id = others[0].case_id
            history = [c for c in (state.case_history or []) if c != others[0].case_id]
            state.case_history = (history + [others[0].case_id])[-10:]
            _save(state)
            return Turn(case_id=others[0].case_id, applicant_id=others[0].applicant_id, in_case=others[0].case_id)
        if others:
            return done(which_view(others, request_id, state))
        return done(_base(request_id, "CASE_SELECTION", _label("no_other_case")))
    from app.agents.applicant.copilot.capabilities import case_list as _case_list
    from app.agents.applicant.copilot.capabilities import faq as _faq

    from app.agents.applicant.copilot.answering import contract as _contract
    from app.agents.applicant.copilot.answering import realtime as _realtime

    if action == "CUSTOM_QUERY" and _contract.enabled() and not state.active_case_id and _realtime.is_greeting(text):
        # MASTER SPEC section 11: a short greeting with what needs attention, then the FAQ (section 7)
        from app.agents.applicant.copilot.answering import language_lock

        lang = language_lock.current() or "en"
        reply = _base(request_id, "GREETING", _realtime.greeting(claims, lang, seed=state.turn_id or 0),
                      query_type="CONVERSATION")
        if _faq.enabled():
            shown = _faq.render(_faq.items(None, lang, int(_faq.cfg().get("max_items_on_greeting", 4))), lang).split("\n")
            reply["faq_block"] = "\n".join(shown[1:])
        return done(reply)
    if action == "CUSTOM_QUERY" and _faq.enabled() and _faq.asks_for_faq(text):
        # MASTER SPEC section 7: "help" / "kya pooch sakta hoon" -- the FAQ for this state (the open case or none)
        from app.agents.applicant.copilot.answering import language_lock

        lang = language_lock.current() or "en"
        shown = _faq.render(_faq.items(state.active_case_id, lang, int(_faq.cfg().get("max_items", 8))), lang,
                            seed=state.turn_id or 0).split("\n")
        head = [_label("in_case", case_id=state.active_case_id)] if state.active_case_id else []
        # the heading is the answer; the links travel apart (faq_block) so no rewrite step touches their labels
        reply = _base(request_id, "FAQ", "\n".join(head + shown[:1]), case_id=state.active_case_id,
                      query_type="CONVERSATION")
        reply["faq_block"] = "\n".join(shown[1:])
        # the voice names the categories ("You can ask me: My cases, Documents.") -- never the links
        categories = re.findall(r"\*\*([^*]+?):\*\*", reply["faq_block"])
        if shown and categories:
            reply["tts_text"] = shown[0].rstrip(":") + ": " + ", ".join(categories) + "."
        return done(reply)
    if action == "CUSTOM_QUERY" and _faq.enabled():
        # MASTER SPEC 15.2 step 2: "How do I upload a document?" -- numbered steps + the source, from faq.yaml
        from app.agents.applicant.copilot.answering import language_lock

        steps = _faq.answer_for(text, language_lock.current() or "en")
        if steps:
            reply = _base(request_id, "FAQ_ANSWER", steps, case_id=state.active_case_id,
                          query_type="PROCESS_KNOWLEDGE", category="KNOWLEDGE_ONLY", response_source="FAQ",
                          tools_invoked=[])
            # the voice reads the first steps (never the source line), from the plain text
            reply["tts_text"] = "\n".join(ln for ln in steps.split("\n") if re.match(r"^\d+\.\s", ln))
            return done(reply)

    # MASTER SPEC section 3: "top 5", "last 3", "sabse purane", "KYC wale" -- a list query (config phrases).
    # With a case open it must also name cases ("KYC wale cases"): "KYC wale" alone is about that case.
    asked_list = None
    if action == "CUSTOM_QUERY" and _case_list.enabled() and not any(p.search(text) for p in _ID.values()):
        wanted = _case_list.understand(text)
        nouns = {_norm(w) for w in (_cfg().get("list_rule") or {}).get("case_nouns") or []}
        if wanted is not None and (not state.active_case_id or nouns & set(_norm(text).split())):
            asked_list = wanted
    if action == "LIST_CASES" or asked_list is not None or (
            action == "CUSTOM_QUERY" and (_says("list", text) or _says("switch", text))):
        # the list (or a switch) closes the open case first: never two cases at once
        closed, state.active_case_id = state.active_case_id, None
        reply = paged_view(claims, request_id, state, asked_list) if _case_list.enabled() \
            else list_view(claims, request_id, state)
        if closed:
            reply["answer"] = _label("closed", case_id=closed) + "\n\n" + reply["answer"]
        return done(reply)
    if action == "CUSTOM_QUERY" and _case_list.enabled() and state.list_query and not state.active_case_id \
            and (_says("more", text) or _case_list._has(text, (_case_list.cfg().get("phrases") or {}).get("next")))\
            and len(_norm(text).split()) <= 3:
        return done(_list(claims, request_id, state, text, more=True))
    # "aur dikhao" pages the LIST only while no case is open: inside a case, "tell me more" is about the last answer
    if action == "CUSTOM_QUERY" and state.listed_case_ids and not state.active_case_id \
            and _says("more", text) and len(_norm(text).split()) <= 3:
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
        from app.agents.applicant.copilot.capabilities import product_flow as _named

        # "Zoravar Khanna ka case": a person named with a case noun -- looked up among the caller's own cases
        person = _named.named_person(text) if not names_a_case else None
        if names_a_case or person or _says("open", text) or picking \
                or (state.active_case_id and _says("this_applicant", text)):
            chosen = select(text, claims, state)
            if chosen.application is not None:
                if state.active_case_id and chosen.application.case_id != state.active_case_id \
                        and not _says("open", text) and names_a_case and _asks_beyond_the_id(text):
                    # a question about ANOTHER case while inside one: answered, never switched silently
                    _save(state)
                    return Turn(case_id=chosen.application.case_id, applicant_id=chosen.application.applicant_id,
                                other_case=chosen.application.case_id)
                return opened(chosen.application)
            if chosen.candidates:
                return done(which_view(chosen.candidates, request_id, state))
            if names_a_case or _says("open", text):
                return done(not_found_view(request_id, chosen.named))
            if person:
                # a named person who is not among the caller's cases: the same neutral line (section 2)
                return done(not_found_view(request_id, "name"))
    if not case_id and state.active_case_id:
        from app.store import get_repository

        application = get_repository().get_application(state.active_case_id)
        if application is not None:
            _save(state)
            return Turn(case_id=application.case_id, applicant_id=application.applicant_id,
                        in_case=application.case_id)
    _save(state)
    return Turn()


def ask_which_case(claims: dict[str, Any], request_id: str, context: dict[str, Any] | None,
                   question: str) -> dict[str, Any] | None:
    """
    No case open and a CASE question: "Which case is this about?" over the officer's recent cases (Open links);
    the question is kept so the pick answers it. None when the officer has no case at all.
    """
    from app.agents.applicant.copilot.answering import language_lock
    from app.agents.applicant.copilot.capabilities import case_list, product_flow

    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    state = _state(_subject(claims), workspace_id)
    lang = language_lock.current() or "en"
    text = product_flow.say((product_flow.cfg().get("which_case") or {}).get("ask"), lang,
                            question=" ".join(str(question).split())[:80])
    reply = paged_view(claims, request_id, state, case_list.ListQuery(), ask=text)
    if not (reply.get("workspace_view") or {}).get("case_list"):
        return None
    reply["intent"], reply["query_type"] = "CASE_SELECTION", "CLARIFICATION"
    # the question is the first line (the direct answer to an ambiguous question is the question back)
    reply["answer"] = text + "\n\n" + str(reply.get("answer") or "")
    reply.pop("faq_block", None)
    state.flow = {**{k: v for k, v in (state.flow or {}).items() if k != "portfolio"}, "after_pick": str(question)}
    _save(state)
    reply["context"] = {**(reply.get("context") or {}), "workspace_id": workspace_id or "default"}
    return reply


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


__all__ = ["FLAG", "Selection", "Turn", "ask_which_case", "decorate", "enabled", "handle", "in_case_header", "list_view", "my_cases",
           "open_view", "select", "workspace_block"]
