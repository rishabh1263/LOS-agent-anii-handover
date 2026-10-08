"""
THE APPROVED PRODUCT FLOW (MASTER SPEC section 15; config app/config/product_flow.yaml).

    STATUSES     a case's stage -> its business status (Created / Review / Disbursal) -> its group (Pending / Done).
                 One mapping for the home table, the chat lists, filters, counts and the follow-up.
    FOLLOW-UP    after a case-portfolio answer: "Do you want to know about a particular case?" [Yes] [No]
                 Yes -> "Pending or Done?" -> that list (paged) -> the officer picks -> the case opens -> "What do you
                 want to know?" with the options. No -> the chat goes on. Anything else typed skips the flow.
    QUICK        the always-available buttons (My pending cases, Create New Case, How to upload, FAQ).
    CASE LINKS   every case answer: Download Excel / Doc / PDF and Show in UI (registered action links).

The state lives in the workspace's own conversation record (`state.flow`); nothing here reads a case except
through the ordinary list (scope enforced there).
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

FLAG = "COPILOT_PRODUCT_FLOW"
_PATH = Path(__file__).resolve().parents[4] / "config" / "product_flow.yaml"
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_LOCK = threading.RLock()


def cfg() -> dict[str, Any]:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CFG["mtime"] != mtime:
            _CFG["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
            _CFG["mtime"] = mtime
        return _CFG["data"]


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool((cfg().get("portfolio_flow") or {}).get("enabled", False))


def _lang() -> str:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.current() or "en"


def say(value: Any, lang: str | None = None, seed: int = 0, **values: Any) -> str:
    """A configured text in the language (a list = variants, picked by `seed`)."""
    from app.agents.applicant.copilot.answering import language_lock

    picked = language_lock.pick(value, lang or _lang()) if isinstance(value, dict) else value
    if isinstance(picked, list):
        picked = picked[seed % len(picked)] if picked else ""
    return str(picked or "").format(**values)


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ-]", " ", str(text or "").lower())).strip() + " "


def _said(text: str, phrases: list[str] | None) -> str | None:
    said = _norm(text)
    for p in sorted(phrases or [], key=len, reverse=True):
        if _norm(p) in said:
            return p
    return None


def _only(text: str, phrases: list[str] | None) -> bool:
    """The whole message is one of the phrases (a button answer): "Yes", "haan", "Pending"."""
    return _norm(text).strip() in {_norm(p).strip() for p in phrases or []}


# --------------------------------------------------------------------------
# statuses: stage -> Created / Review / Disbursal -> Pending / Done
# --------------------------------------------------------------------------

def status_of(stage: str | None) -> str:
    c = cfg().get("case_status") or {}
    return str((c.get("by_stage") or {}).get(str(stage or "").upper()) or c.get("default_status") or "Created")


def group_of(status: str) -> str:
    for group, members in ((cfg().get("case_status") or {}).get("groups") or {}).items():
        if status in (members or []):
            return str(group)
    return "pending"


def status_label(status: str, lang: str | None = None) -> str:
    return say(((cfg().get("case_status") or {}).get("labels") or {}).get(status) or status, lang)


def group_label(group: str, lang: str | None = None) -> str:
    return say(((cfg().get("case_status") or {}).get("group_labels") or {}).get(group) or group.title(), lang)


def statuses_in(group: str) -> list[str]:
    return list((((cfg().get("case_status") or {}).get("groups") or {}).get(group)) or [])


def understand(text: str) -> dict[str, str] | None:
    """{"group": "pending"} / {"status": "Review"} when the message asks for that list ("review wale"), else None."""
    phrases = (cfg().get("case_status") or {}).get("phrases") or {}
    best: tuple[int, dict[str, str]] | None = None
    for kind in ("group", "status"):
        for key, words in (phrases.get(kind) or {}).items():
            hit = _said(text, words)
            if hit and (best is None or len(hit) > best[0]):
                best = (len(hit), {kind: str(key)})
    return best[1] if best else None


# --------------------------------------------------------------------------
# the follow-up flow (15.2 step 4)
# --------------------------------------------------------------------------

def _flow_cfg() -> dict[str, Any]:
    return cfg().get("portfolio_flow") or {}


def _texts() -> dict[str, Any]:
    return _flow_cfg().get("texts") or {}


def _get(state) -> dict[str, Any]:
    return dict(((getattr(state, "flow", None) or {}).get("portfolio")) or {})


def _set(state, value: dict[str, Any]) -> None:
    """Only the portfolio question's own key: a case draft / edit in `state.flow` is never touched here."""
    flow = dict(getattr(state, "flow", None) or {})
    if value:
        flow["portfolio"] = value
    else:
        flow.pop("portfolio", None)
    state.flow = flow


def closing_for_list(state, query: dict[str, Any] | None) -> list[str] | None:
    """
    The closing lines of a case-portfolio answer: "Do you want to know about a particular case?" [Yes] [No] -- or,
    for the list the flow itself asked for, "Pick a case". None when the flow is off (the list keeps its own line).
    """
    from app.agents.applicant.copilot.answering import contract

    if not enabled():
        return None
    seed = int(getattr(state, "turn_id", 0) or 0)
    if _get(state).get("pending") == "pick":
        return [say(_texts().get("pick"), seed=seed)]
    if query and int(query.get("page") or 0) > 0:
        return None                                   # paging on: the question was asked on the first page
    _set(state, {"pending": "particular"})
    t = _texts()
    return [say(t.get("ask_particular"), seed=seed),
            contract.ask(say(t.get("yes_label"))) + " · " + contract.ask(say(t.get("no_label")))]


def step(text: str, state) -> dict[str, Any] | None:
    """
    The answer to the flow's pending question, or None (no question pending, or the officer typed something else
    -- the flow is then dropped and the message handled as usual).
    Returns {"answer": ...} for a finished reply, or {"list": {"group": ...}} for the list to show.
    """
    from app.agents.applicant.copilot.answering import contract

    pending = _get(state).get("pending")
    if not enabled() or not pending:
        return None
    f, t = _flow_cfg(), _texts()
    seed = int(getattr(state, "turn_id", 0) or 0)
    if pending == "particular":
        if _only(text, f.get("yes_words")) or _only(text, [say(t.get("yes_label"))]):
            _set(state, {"pending": "group"})
            groups = list(((cfg().get("case_status") or {}).get("groups") or {}))
            return {"answer": say(t.get("ask_group"), seed=seed),
                    "block": " · ".join(contract.ask(group_label(g)) for g in groups)}
        if _only(text, f.get("no_words")) or _only(text, [say(t.get("no_label"))]):
            _set(state, {})
            return {"answer": say(t.get("no_reply"), seed=seed)}
    elif pending == "group":
        for group, words in (("pending", f.get("pending_words")), ("done", f.get("done_words"))):
            if _only(text, words) or _only(text, [group_label(group)]):
                _set(state, {"pending": "pick", "group": group})
                return {"list": {"group": group}}
    elif pending == "pick":
        _set(state, {})
        state.picking = True                 # not persisted: this turn may open the picked case
        return None
    _set(state, {})
    return None


def after_open(state) -> str | None:
    """'What do you want to know?' + the options, when the case was picked from the flow's list."""
    from app.agents.applicant.copilot.answering import contract, language_lock

    if not getattr(state, "picking", False):
        return None
    state.picking = False
    lang = _lang()
    options = _flow_cfg().get("what_options") or []
    items = language_lock.pick(options, lang) if isinstance(options, dict) else options
    seed = int(getattr(state, "turn_id", 0) or 0)
    return say(_texts().get("what_to_know"), lang, seed) + "\n" + "\n".join(
        f"- {contract.ask(q)}" for q in (items if isinstance(items, list) else [items]))


# --------------------------------------------------------------------------
# quick buttons and case links
# --------------------------------------------------------------------------

def quick_buttons(lang: str | None = None) -> list[dict[str, str]]:
    """The always-available buttons: {id, label, send} in the language (GET /api/v1/fos/quick-actions)."""
    return [{"id": str(b.get("id")), "label": say(b.get("label"), lang), "send": say(b.get("send"), lang)}
            for b in cfg().get("quick_buttons") or [] if isinstance(b, dict)]


def case_links(case_id: str | None, lang: str, *, list_query: dict[str, Any] | None = None) -> list[str]:
    """Download Excel / Doc / PDF + Show in UI, for a case answer (or a list answer: the filtered list)."""
    from app.agents.applicant.copilot.answering import contract

    formats = list((cfg().get("case_links") or {}).get("downloads") or [])
    out: list[str] = []
    if case_id:
        out += [contract.link("download", lang, format=f, case=case_id, fmt_label=_fmt(f, lang)) for f in formats]
        out.append(contract.link("show_in_ui", lang, case=case_id))
    elif list_query is not None and (cfg().get("case_links") or {}).get("on_lists", True):
        filters = {k: v for k, v in list_query.items() if k in LIST_KEYS and v not in (None, "")}
        query = ",".join(f"{k}:{str(v).replace(',', ' ').replace(':', ' ')}" for k, v in filters.items())
        out += [contract.link("download_list", lang, format=f, q=query or "all", fmt_label=_fmt(f, lang))
                for f in formats]
        out.append(contract.link("show_list_in_ui", lang, q=query or "all"))
    return out


def notifications(claims: dict[str, Any], lang: str | None = None) -> list[dict[str, Any]]:
    """
    15.4: the caller's Pending cases in their stage for `notifications.pending_days` or more, longest first --
    {case_id, days, text}. The same list feeds the home screen and the chat greeting.
    """
    from app.agents.applicant.copilot.capabilities import case_list

    n = cfg().get("notifications") or {}
    days = int(n.get("pending_days", 5))
    page = case_list.run(claims, case_list.ListQuery(group="pending", sort="longest_in_stage",
                                                     size=int(case_list.cfg().get("max_page_size", 20))))
    out = []
    for r in page.rows:
        stuck = r.get("days_in_stage")
        if stuck is not None and int(stuck) >= days:
            out.append({"case_id": r["case_id"], "days": int(stuck),
                        "text": say(n.get("text"), lang, case_id=r["case_id"], days=int(stuck))})
    return out[: int(n.get("max_items", 10))]


def named_person(text: str) -> str | None:
    """'Zoravar Khanna ka case' / "Priya's case" / 'cases of Anil' -> the name asked for, else None."""
    spec = cfg().get("name_lookup") or {}
    blocked = {_norm(w).strip() for w in spec.get("not_names") or []}
    for pattern in spec.get("patterns") or []:
        found = re.search(pattern, str(text or "").strip(), re.I)
        if found:
            name = " ".join(found.group("name").split())
            if name and not any(w in blocked for w in _norm(name).split()):
                return name
    return None


def out_of_scope(text: str) -> bool:
    """A message clearly outside the product (product_flow.yaml out_of_scope.off_topic_words; whole phrases)."""
    return bool(_said(text, (cfg().get("out_of_scope") or {}).get("off_topic_words")))


def out_of_scope_reply(lang: str | None = None, seed: int = 0) -> tuple[str, str]:
    """(the polite line, the next option as an ask: link)."""
    from app.agents.applicant.copilot.answering import contract

    spec = cfg().get("out_of_scope") or {}
    return say(spec.get("text"), lang, seed), contract.ask(say(spec.get("next"), lang))


def stage_move_note(published: Any, message: str) -> Any:
    """
    Golden rule 4, said plainly: a request to move / approve / change a stage is never done from chat -- the reply
    opens with that line (both endpoints), then shows where the case stands.
    """
    if not isinstance(published, dict) or not message:
        return published
    spec = cfg().get("stage_move") or {}
    if not _said(message, spec.get("phrases")) or str(published.get("response_source") or "") in ("SAFETY",
                                                                                                   "GUARDRAIL"):
        return published
    note = say(spec.get("text"))
    answer = str(published.get("answer") or "")
    if note and note not in answer:
        # the direct answer stays first (section 8: "Not ready for CPA ..."); the note follows it -- after the
        # active-case line too ("📍 CASE-...")
        lines = answer.split("\n")
        at = 1 if lines and re.fullmatch(r"\W*\**CASE-[0-9A-Z]+\**", lines[0].strip()) else 0
        at += 1 if len(lines) > at and lines[at].strip() else 0
        published["answer"] = "\n".join(lines[:at] + ["", note, ""] + lines[at:]).strip()
        published.pop("answer_markdown", None)
    return published


def asks_download(text: str) -> bool:
    """'excel download karo', 'pdf chahiye' -- a short request whose words include a download word."""
    words = _norm(text).split()
    return len(words) <= 6 and bool(_said(text, (cfg().get("case_links") or {}).get("ask_words")))


def downloads_reply(case_id: str | None, list_query: dict[str, Any] | None, lang: str | None = None) -> str:
    """The heading + the download links (the open case, else the last list / all cases)."""
    lang = lang or _lang()
    here = (cfg().get("case_links") or {}).get("here") or {}
    if case_id:
        return say(here.get("case"), lang, case_id=case_id) + "\n" + " · ".join(case_links(case_id, lang))
    return say(here.get("list"), lang) + "\n" + " · ".join(case_links(None, lang, list_query=list_query or {}))


LIST_KEYS = ("filter", "group", "status", "search", "sort", "stage")


def parse_list_q(q: str | None) -> dict[str, str]:
    """The list filters a download_list link carries ("group:pending,search:rahul"; "all" = none)."""
    out: dict[str, str] = {}
    for part in str(q or "").split(","):
        key, _, value = part.partition(":")
        if key.strip() in LIST_KEYS and value.strip():
            out[key.strip()] = value.strip()[:80]
    return out


def _fmt(fmt: str, lang: str) -> str:
    from app.agents.applicant.copilot.answering import contract

    names = ((contract.cfg().get("markdown") or {}).get("format_names") or {}).get(fmt)
    return say(names, lang) if names else fmt.upper()


__all__ = ["FLAG", "after_open", "case_links", "cfg", "closing_for_list", "enabled", "group_label", "group_of",
           "quick_buttons", "say", "status_label", "status_of", "statuses_in", "step", "understand"]
