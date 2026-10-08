"""
THE CASE LIST AT SCALE (MASTER SPEC section 3; config app/config/case_list.yaml).

ONE QUERY for the chat ("my cases", "top 5", "KYC wale", "aur dikhao") and for the full-list panel
(GET /api/v1/fos/cases): the caller's OWN cases (live grants, every row re-checked by the ownership rule),
filtered, searched, sorted and paged on the server. A chat reply never shows more than one page.

COST. A plain order (newest / oldest) is a SQL page plus a separate count: only the page's rows get a status.
A status filter or the default "needs action first" needs every case's status: computed once and cached per
user for `cache_ttl_seconds` (30 s), so paging through 200 cases computes them once.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).resolve().parents[4] / "config" / "case_list.yaml"
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_ROWS: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_LOCK = threading.Lock()


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


FLAG = "COPILOT_CASE_LIST_PAGING"


def enabled() -> bool:
    import os

    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def clear_cache() -> None:
    with _LOCK:
        _ROWS.clear()


# --------------------------------------------------------------------------
# the query
# --------------------------------------------------------------------------

@dataclass
class ListQuery:
    filter: str | None = None            # a key of case_list.yaml `filters`
    search: str | None = None            # applicant name (fuzzy), case / applicant / co-applicant id
    sort: str | None = None              # a key of `sorts` (None = default_sort)
    page: int = 0
    size: int | None = None              # None = page_size
    stage: str | None = None
    product: str | None = None
    created_from: str | None = None      # ISO date
    created_to: str | None = None
    group: str | None = None             # product_flow.yaml case_status.groups: pending / done (section 15.5)
    status: str | None = None            # Created / Review / Disbursal
    with_counts: bool = False            # the counts by status over ALL the caller's cases (summary / greeting)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", False)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ListQuery":
        return cls(**{k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__})


@dataclass
class ListPage:
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0                       # cases matching the query
    all_total: int = 0                   # every case of the caller
    counts: dict[str, int] = field(default_factory=dict)    # by status kind, over all cases (when computed)
    page: int = 0
    size: int = 5
    has_more: bool = False
    query: ListQuery = field(default_factory=ListQuery)


def _size(query: ListQuery) -> int:
    c = cfg()
    wanted = int(query.size or c.get("page_size", 5))
    return max(1, min(wanted, int(c.get("max_page_size", 20))))


def _subject(claims: dict[str, Any]) -> str:
    from app.security.auth import get_subject

    return str(get_subject(claims) or "")


def _owned(claims: dict[str, Any], applications: list[Any]) -> list[Any]:
    """Every row re-checked by the ownership rule (a revoked grant, a moved case -- never listed)."""
    from app.agents.applicant.copilot.capabilities import workspace
    from app.security import access

    out = []
    for application in applications:
        try:
            workspace.authorize(claims, application.case_id)
        except access.AccessDenied:
            continue
        out.append(application)
    return out


def _row(application: Any) -> dict[str, Any]:
    """One row, exactly as the case itself reports it (the same status words as the workspace list)."""
    from app.agents.applicant.copilot.capabilities import workspace

    status, kind = workspace._row_status(application)
    if kind != "KYC":
        # a recorded KYC failure / review is a KYC issue even while the FOS->CPA KYC rule is off (the gate then
        # does not block on it, but the officer still has to fix it) -- the same rule as the KYC A / B verdict
        from app.agents.applicant.copilot.capabilities import faq

        if faq._kyc_open(application.case_id):
            status, kind = workspace._label("row_kyc"), "KYC"
    days, over = None, False
    try:
        from app.agents.applicant.copilot.capabilities import timeline

        t = timeline.build(application.case_id)
        days, over = t.get("days_in_stage"), bool(t.get("over_target"))
    except Exception:  # noqa: BLE001 - a row is a summary; the case itself has the detail
        pass
    from app.agents.applicant.copilot.capabilities import product_flow

    stage = workspace._stage(application.case_id) or "--"
    business = product_flow.status_of(stage)
    return {"case_id": application.case_id, "applicant_id": application.applicant_id,
            "applicant_name": workspace._name(application.applicant_id, short=True),
            "applicant_full_name": workspace._name(application.applicant_id, short=False),
            "stage": stage, "status": business, "group": product_flow.group_of(business),
            "status_label": status, "status_kind": kind,
            "product": getattr(application, "product", None), "days_in_stage": days, "over_target": over,
            "created_at": str(getattr(application, "created_at", "") or ""),
            "updated_at": str(getattr(application, "updated_at", "") or "")}


def _rows_for(subject: str, applications: list[Any]) -> dict[str, dict[str, Any]]:
    """The rows of these cases, from the per-user cache when fresh (case_list.yaml cache_ttl_seconds)."""
    ttl = float(cfg().get("cache_ttl_seconds", 30))
    now = time.time()
    with _LOCK:
        cached = _ROWS.get(subject)
        rows = dict(cached[1]) if cached and cached[0] > now else {}
    missing = [a for a in applications if a.case_id not in rows]
    for application in missing:
        rows[application.case_id] = _row(application)
    if missing:
        with _LOCK:
            _ROWS[subject] = (now + ttl, rows)
    return {a.case_id: rows[a.case_id] for a in applications}


def _matches_search(row: dict[str, Any], term: str, co_cases: set[str]) -> bool:
    from app.agents.applicant.copilot.capabilities import workspace

    t = term.strip().upper()
    if not t:
        return True
    if row["case_id"].upper().startswith(t) or str(row["applicant_id"]).upper().startswith(t):
        return True
    if row["case_id"].upper() in co_cases:
        return True
    name = workspace._name(row["applicant_id"], short=False).lower().split()
    words = [w for w in t.lower().split() if len(w) >= 3]
    threshold = float((workspace._cfg() or {}).get("name_match_threshold", 0.84))
    return bool(words) and all(max((difflib.SequenceMatcher(None, w, n).ratio() for n in name), default=0) >= threshold
                               for w in words)


def _sort_key(sort: str):
    spec = (cfg().get("sorts") or {}).get(sort) or {}
    if spec.get("order"):
        rank = {k: i for i, k in enumerate(spec["order"])}
        return lambda r: (rank.get(r["status_kind"], len(rank)), _neg_time(r["updated_at"]))
    by, desc = spec.get("by", "updated_at"), bool(spec.get("desc", True))
    if by == "days_in_stage":
        return lambda r: -(r.get("days_in_stage") or 0) if desc else (r.get("days_in_stage") or 0)
    return lambda r: _neg_time(r.get(by) or "") if desc else str(r.get(by) or "")


def _neg_time(value: str) -> tuple:
    # newest first without parsing: invert every character of the ISO string
    return tuple(-ord(ch) for ch in str(value))


def run(claims: dict[str, Any], query: ListQuery) -> ListPage:
    from app.store import get_repository

    repository = get_repository()
    subject = _subject(claims)
    size, c = _size(query), cfg()
    sort = query.sort if query.sort in (c.get("sorts") or {}) else c.get("default_sort", "needs_action")
    spec = (c.get("sorts") or {}).get(sort) or {}
    computed = bool(query.filter or query.search or query.stage or query.group or query.status or query.with_counts
                    or spec.get("order")
                    or spec.get("by") == "days_in_stage")
    page_no = max(0, int(query.page or 0))
    if not computed and hasattr(repository, "count_granted_cases"):
        # THE CHEAP PATH: a SQL page + a separate count; statuses only for the page's rows
        total = int(repository.count_granted_cases(subject, product=query.product, created_from=query.created_from,
                                                   created_to=query.created_to))
        apps = _owned(claims, repository.list_granted_cases(
            subject, limit=size, offset=page_no * size, order=spec.get("by", "updated_at"),
            descending=bool(spec.get("desc", True)), product=query.product,
            created_from=query.created_from, created_to=query.created_to))
        rows = list(_rows_for(subject, apps).values())
        return ListPage(rows=_number(rows, page_no, size), total=total, all_total=total, page=page_no, size=size,
                        has_more=(page_no + 1) * size < total, query=query)
    apps = _owned(claims, repository.list_granted_cases(subject, limit=int(c.get("scan_limit", 2000))))
    rows_by_case = _rows_for(subject, apps)
    every = list(rows_by_case.values())
    counts: dict[str, int] = {}
    for r in every:
        counts[r["status_kind"]] = counts.get(r["status_kind"], 0) + 1
    chosen = every
    if query.filter:
        rule = (c.get("filters") or {}).get(query.filter) or {}
        if rule.get("kinds"):
            chosen = [r for r in chosen if r["status_kind"] in rule["kinds"]]
        if rule.get("over_target"):
            chosen = [r for r in chosen if r.get("over_target")]
    if query.stage:
        chosen = [r for r in chosen if str(r["stage"]).upper() == str(query.stage).upper()]
    if query.group:
        chosen = [r for r in chosen if r["group"] == str(query.group).lower()]
    if query.status:
        chosen = [r for r in chosen if str(r["status"]).lower() == str(query.status).lower()]
    if query.product:
        chosen = [r for r in chosen if str(r.get("product") or "").upper() == str(query.product).upper()]
    if query.created_from:
        chosen = [r for r in chosen if r["created_at"][:10] >= query.created_from[:10]]
    if query.created_to:
        chosen = [r for r in chosen if r["created_at"][:10] <= query.created_to[:10]]
    if query.search:
        co_cases: set[str] = set()
        if re.match(r"(?i)^COAPP-", query.search.strip()):
            try:
                from app.agents.los import co_applicants

                co_cases = {x.upper() for x in co_applicants.cases_for(query.search.strip().upper())}
            except Exception:  # noqa: BLE001
                co_cases = set()
        words = [w for w in query.search.lower().split() if len(w) >= 3]
        from app.agents.applicant.copilot.capabilities import workspace as _ws

        exact = [r for r in chosen if words and set(words) <= set(_ws._name(r["applicant_id"], short=False)
                                                                 .lower().split())]
        # an exact name wins; fuzzy (typos) only when nothing matches exactly
        chosen = exact or [r for r in chosen if _matches_search(r, query.search, co_cases)]
    chosen = sorted(chosen, key=_sort_key(sort))
    window = chosen[page_no * size:(page_no + 1) * size]
    return ListPage(rows=_number(window, page_no, size), total=len(chosen), all_total=len(every), counts=counts,
                    page=page_no, size=size, has_more=(page_no + 1) * size < len(chosen), query=query)


def _number(rows: list[dict[str, Any]], page: int, size: int) -> list[dict[str, Any]]:
    return [{**r, "number": i} for i, r in enumerate(rows, page * size + 1)]


# --------------------------------------------------------------------------
# natural language -> the query
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ-]", " ", str(text or "").lower())).strip() + " "


def _has(text: str, phrases: list[str]) -> str | None:
    said = _norm(text)
    for p in sorted(phrases or [], key=len, reverse=True):
        if _norm(p) in said:
            return p
    return None


def _number_in(text: str) -> int | None:
    found = re.search(r"\b(\d{1,2})\b", text)
    if found:
        return int(found.group(1))
    words = (cfg().get("phrases") or {}).get("numbers") or {}
    for w in _norm(text).split():
        if w in words:
            return int(words[w])
    return None


def names_other_people(text: str) -> bool:
    """Section 17.2: the message asks about other officers' / users' cases (case_list.yaml other_people)."""
    return bool(_has(text, cfg().get("other_people") or []))


def understand(text: str, previous: dict[str, Any] | None = None) -> ListQuery | None:
    """
    The list query a message asks for, or None when it asks for no list. `previous` is the last list's query
    (state): "aur dikhao" pages it; a new filter / order starts a fresh first page.
    """
    p = cfg().get("phrases") or {}
    query = ListQuery()
    asked = False
    n = _number_in(text)
    for key, phrases in (p.get("filter") or {}).items():
        if _has(text, phrases):
            query.filter, asked = key, True
            break
    if not asked:
        # section 15.5: "my pending cases", "review wale", "disbursal wale" (product_flow.yaml case_status.phrases)
        from app.agents.applicant.copilot.capabilities import product_flow

        wanted = product_flow.understand(text)
        if wanted:
            query.group, query.status, asked = wanted.get("group"), wanted.get("status"), True
    if _has(text, p.get("oldest")):
        query.sort, asked = "oldest", True
    elif _has(text, p.get("longest_in_stage")):
        query.sort, asked = "longest_in_stage", True
    elif _has(text, p.get("last_n")):
        query.sort, asked = "recent", True
        query.size = n
    elif _has(text, p.get("first_n")) and n:
        query.size, asked = n, True
    if n and asked and not query.size:
        query.size = n
    if not asked and previous is not None and _has(text, p.get("next")):
        prev = ListQuery.from_dict(previous)
        prev.page += 1
        if n:
            prev.size = n
        return prev
    return query if asked else None


# --------------------------------------------------------------------------
# the chat reply (markdown table + links)
# --------------------------------------------------------------------------

def _say(key: str, lang: str, seed: int = 0, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    value = (cfg().get("labels") or {}).get(key, key)
    picked = language_lock.pick(value, lang) if isinstance(value, dict) else value
    if isinstance(picked, list):
        picked = picked[seed % len(picked)] if picked else ""
    return str(picked).format(**values)


def _name_of(group: str, key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    value = ((cfg().get("labels") or {}).get(group) or {}).get(key, key)
    return str(language_lock.pick(value, lang) if isinstance(value, dict) else value).format(**values)


def render(page: ListPage, lang: str, seed: int = 0, closing: list[str] | None = None) -> str:
    """
    Summary line, ONE page as a table with Open links, "Showing x-y of N", the options as links. `closing`: the
    product flow's question (section 15.2) in place of the list's own follow-up line -- one question per reply.
    """
    from app.agents.applicant.copilot.answering import contract

    q = page.query
    from app.agents.applicant.copilot.capabilities import product_flow

    filter_name = None
    if q.filter:
        filter_name = _name_of("filter_names", q.filter, lang)
    elif q.group:
        filter_name = product_flow.group_label(q.group, lang)
    elif q.status:
        filter_name = product_flow.status_label(q.status, lang)
    elif q.search:
        filter_name = _name_of("filter_names", "search", lang, q=q.search)
    if page.all_total == 0:
        return _say("none", lang, seed)
    if page.total == 0:
        return _say("none_match", lang, seed, filter=filter_name or "")
    if filter_name:
        lines = [_say("filtered", lang, seed, n=page.total, filter=filter_name)]
    elif page.counts:
        order = ((cfg().get("sorts") or {}).get("needs_action") or {}).get("order") or list(page.counts)
        parts = [_name_of("part", k, lang, n=page.counts[k]) for k in order if page.counts.get(k)]
        lines = [_say("summary", lang, seed, total=page.all_total, parts=", ".join(parts))]
    else:
        lines = [_say("summary", lang, seed, total=page.all_total, parts=_name_of("sorted", q.sort or "recent", lang))]
    head = contract.cfg().get("markdown", {}).get("case_table_headers") or {}
    from app.agents.applicant.copilot.answering import language_lock

    cols = language_lock.pick(head, lang) if isinstance(head, dict) else head
    cols = cols or ["#", "Case", "Applicant", "Stage", "Status", ""]
    table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    from app.agents.applicant.copilot.answering import professional

    for r in page.rows:
        status = product_flow.status_label(r["status"], lang) + " · " \
            + professional.strip_emojis(str(r["status_label"])).strip()
        if r.get("over_target") and r.get("days_in_stage") is not None:
            status += f" ({r['days_in_stage']}d)"
        cells = [str(r["number"]), r["case_id"], f"{r['applicant_name']} ({r['applicant_id']})", str(r["stage"]),
                 status, contract.link("open_case", lang, id=r["case_id"])]
        table.append("| " + " | ".join(str(x).replace("|", "/") for x in cells) + " |")
    first = page.page * page.size + 1
    sort_name = _name_of("sorted", q.sort or cfg().get("default_sort", "needs_action"), lang)
    lines += ["", *table, "", _say("showing", lang, seed, first=first, last=first + len(page.rows) - 1,
                                   total=page.total, sort=sort_name)]
    options = [o for o in (language_lock.pick((cfg().get("labels") or {}).get("options") or {}, lang) or [])]
    links = []
    if page.has_more:
        links.append(contract.link("list_more", lang))
    else:
        lines.append(_say("end", lang, seed))
    links += [contract.ask(o) for o in options[1:]]
    # a `closing` (even empty) replaces the follow-up line; its own lines are placed by the caller
    lines += ["", " · ".join(links)] if closing is not None else ["", _say("follow_up", lang, seed), " · ".join(links)]
    if closing:
        lines += [""] + list(closing)
    return "\n".join(lines)


__all__ = ["ListPage", "ListQuery", "cfg", "clear_cache", "render", "run", "understand"]
