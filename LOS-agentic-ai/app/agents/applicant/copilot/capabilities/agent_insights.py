"""
COPILOT AGENT INSIGHTS -- what the AGENTS found, for one case or across the officer's own cases (2026-11-04; config
app/config/copilot_agent_insights.yaml).

    "what do the agents say about LOS-DUMMY-0005?"      CASE_360    every agent's latest result on one case
    "fraud risk of LOS-DUMMY-0025"                      ONE_AGENT   one agent on one case
    "is case CASE-SEED-LOS-DUMMY-0007 ready for credit?" READINESS   the agents a case needs: run, and clear?
    "which agents have not run on this case?"           NOT_RUN
    "how many cases are high fraud risk?"               PORTFOLIO   count / list over the officer's own cases
    "which ones?" / "and the valuation details?"        a follow-up of the last one

THE CONCEPT: PLANNER SMALL, TOOLS FIXED, ANSWER CHECKED.
    understand(message, previous, open_case) -> Ask | None     the words -> a validated ask (code, no model)
    gather(ask, owned)                       -> facts          one generated read-only query per agent
    render(ask, facts)                       -> text           a template filled with the facts
    verify(text, facts)                      -> bool           every number in the text is one of the facts
A CPU laptop answers this as accurately as a GPU host: nothing here waits on a model, so nothing can be invented.

AUTHORISED: only the officer's own cases (the case list's grants, each re-checked); a case named in the message that is
not theirs is answered "not found among your cases" -- the same words as a case that does not exist.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
CONFIG = Path(__file__).resolve().parents[4] / "config" / "copilot_agent_insights.yaml"
INTENT = "AGENT_INSIGHTS"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


@lru_cache(maxsize=1)
def cfg() -> dict[str, Any]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    for key, spec in (data.get("agents") or {}).items():
        for col in [spec.get("table"), spec.get("key_column"), spec.get("verdict"), spec.get("time"),
                    *(spec.get("details") or {})]:
            if col is not None and not _IDENT.match(str(col)):
                raise ValueError(f"copilot_agent_insights.yaml: agent {key}: {col!r} is not a plain identifier")
        if spec.get("link") not in ("case_id", "application_number", "application_id"):
            raise ValueError(f"copilot_agent_insights.yaml: agent {key}: link must be case_id / application_number / "
                             "application_id")
    links = data.get("links") or {}
    for col in (links.get("application_table"), links.get("application_number_column"),
                links.get("application_id_column")):
        if col is not None and not _IDENT.match(str(col)):
            raise ValueError(f"copilot_agent_insights.yaml: links: {col!r} is not a plain identifier")
    return data


def enabled() -> bool:
    import os

    flag = (os.getenv("COPILOT_AGENT_INSIGHTS") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def agents() -> dict[str, dict[str, Any]]:
    return dict(cfg().get("agents") or {})


def _words(name: str) -> list[str]:
    return [str(w).lower() for w in ((cfg().get("words") or {}).get(name) or [])]


def _has(text: str, phrases: list[str]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text) for p in phrases)


# ---------------------------------------------------------------------------------------------------------------------
# 1. UNDERSTAND
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Ask:
    kind: str                                   # CASE_360 | ONE_AGENT | READINESS | NOT_RUN | PORTFOLIO
    case_id: str | None = None
    agents: list[str] = field(default_factory=list)
    mode: str = "list"                          # PORTFOLIO: count | list
    filter: str = "attention"                   # PORTFOLIO: attention | high | ran

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "case_id": self.case_id, "agents": list(self.agents), "mode": self.mode,
                "filter": self.filter}


_CASE_ID = re.compile(r"\bCASE-[A-Z0-9][A-Z0-9-]*[A-Z0-9]\b", re.I)
_APP_NO = re.compile(r"\b(LOS-DUMMY-\d{3,5})\b", re.I)


def norm(message: str) -> str:
    return " ".join(str(message or "").lower().replace("’", "'").split())


def _near(a: str, b: str) -> bool:
    """One letter added, dropped or changed (a typo), for words of 5+ letters -- "frad" / "fruad" for "fraud"."""
    if a == b or min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 1 or max(len(a), len(b)) < 5:
        return a == b
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 and a[0] == b[0]


def named_agents(text: str) -> list[str]:
    """The agents a message names (longest phrase first, so "property valuation" never also reads "office"); a
    one-word name also matches with one letter wrong."""
    found: list[tuple[int, str]] = []
    tokens = [(m.start(), m.group(0)) for m in re.finditer(r"[a-z]+", text)]
    for key, spec in agents().items():
        for w in sorted((str(x).lower() for x in spec.get("words") or []), key=len, reverse=True):
            m = re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text)
            if m:
                found.append((m.start(), key))
                break
            if " " not in w and len(w) >= 5:
                hit = next((pos for pos, tok in tokens if _near(tok, w)), None)
                if hit is not None:
                    found.append((hit, key))
                    break
    return [k for _, k in sorted(found)]


def case_named(message: str) -> str | None:
    """A case the message names: a LOS case id, or a seed application number (LOS-DUMMY-0005 -> its case id)."""
    m = _CASE_ID.search(message or "")
    if m:
        return m.group(0).upper()
    m = _APP_NO.search(message or "")
    if m:
        return str((cfg().get("links") or {}).get("case_prefix") or "") + m.group(1).upper()
    return None


def prev_is_portfolio_list(previous: dict[str, Any], text: str) -> bool:
    """ "which ones?" / "list them" right after a portfolio count: the same question, listed."""
    return (previous.get("kind") == "PORTFOLIO" and len(text.split()) <= 5 and _has(text, _words("followup_list"))
            and not re.search(r"\bcases\b", text))


def understand(message: str, previous: dict[str, Any] | None = None, open_case: str | None = None) -> Ask | None:
    """The ask, or None when the message is not about the agents' results (another layer answers it).

    ONE CASE when the message names a case, or points at the open one ("this case", "sab agents ka result", an agent
    named with no count / list word). THE PORTFOLIO when it counts or lists cases ("how many / which cases ...") over
    an agent, over attention, or over every agent. A short follow-up reuses the last ask."""
    text = norm(message)
    if not text:
        return None
    named = named_agents(text)
    case = case_named(message)
    all_phrase = _has(text, _words("all_agents"))
    all_agents = all_phrase or bool(re.search(r"\bagents\b", text)) or \
        (case is not None and bool(re.search(r"\bagent\b", text)))
    asks_whether = any(text.startswith(w) for w in _words("asks_whether")) and _has(text, _words("readiness"))
    if _has(text, _words("actions")) and not asks_whether:
        return None                              # an action ("upload the valuation report"), not a question
    if (any(text.startswith(d) for d in _words("definition")) and not case and not _has(text, _words("result"))
            and not _has(text, _words("count"))):
        return None                              # "what is collateral verification?": the handbook's, not a result
    readiness = _has(text, _words("readiness"))
    not_run = _has(text, _words("not_run"))
    count = _has(text, _words("count"))
    listing = _has(text, _words("list"))
    attention = _has(text, _words("attention"))
    this_case = _has(text, _words("this_case"))
    runs = _has(text, _words("runs"))

    # A FOLLOW-UP only when the message asks nothing of its own: "which ones?" after a count; "and the valuation
    # details?" after a case. "How many cases are high fraud risk?" right after a case is a NEW question.
    own_question = (count or bool(re.search(r"\bcases\b", text)) or readiness or not_run or this_case
                    or all_phrase)
    if previous and len(text.split()) <= 8 and not case and not own_question:
        prev = dict(previous)
        if prev.get("kind") == "PORTFOLIO" and _has(text, _words("followup_list")):
            return Ask("PORTFOLIO", agents=list(prev.get("agents") or []), mode="list",
                       filter=str(prev.get("filter") or "attention"))
        if prev.get("case_id") and named and prev.get("kind") in ("CASE_360", "ONE_AGENT", "READINESS", "NOT_RUN"):
            return Ask("ONE_AGENT", case_id=prev["case_id"], agents=named)
    elif previous and prev_is_portfolio_list(previous, text):
        return Ask("PORTFOLIO", agents=list(previous.get("agents") or []), mode="list",
                   filter=str(previous.get("filter") or "attention"))

    if not (named or all_agents or readiness or not_run):
        return None
    # "how many / which cases": the portfolio -- unless a case is named or "this case" is pointed at
    about_cases = bool(re.search(r"\bcases\b", text)) or attention or (count and runs)
    portfolio_ask = (count or listing) and about_cases and not case and not this_case and not not_run
    # "cases jinko attention chahiye agents ke hisaab se": cases + attention over the agents is a list too
    if not portfolio_ask and not case and not this_case and attention and re.search(r"\bcases\b", text) \
            and (named or all_agents):
        portfolio_ask = True
    target = case or (open_case if not portfolio_ask else None)
    if target and not portfolio_ask and (case or this_case or all_agents or readiness or not_run or named):
        if readiness:
            return Ask("READINESS", case_id=target, agents=named)
        if not_run:
            return Ask("NOT_RUN", case_id=target)
        if named and not _has(text, _words("all_agents")):
            # one agent on a case: only when the agent's RESULT is asked ("what collateral documents are needed?" is
            # a checklist question for another layer)
            if not case and not _has(text, _words("result")):
                return None
            return Ask("ONE_AGENT", case_id=target, agents=named)
        # every agent on a case: a named case + "agents", or the open case + "sab agents" / "agents" + a result word
        if not ((case and all_agents) or all_phrase or (all_agents and _has(text, _words("result")))):
            return None
        return Ask("CASE_360", case_id=target)
    if portfolio_ask or (named and attention):
        if _has(text, _words("high")) or "potential fraud" in text:
            wanted = "high"
        elif attention or not (count and runs):
            wanted = "attention"
        else:
            wanted = "ran"                       # "how many dedupe checks have been done?"
        return Ask("PORTFOLIO", agents=named, mode="count" if count else "list", filter=wanted)
    return None


# ---------------------------------------------------------------------------------------------------------------------
# 2. GATHER -- the officer's own cases, then one read-only query per agent
# ---------------------------------------------------------------------------------------------------------------------

_OWNED: dict[str, tuple[float, list[str]]] = {}
_OWNED_LOCK = threading.Lock()


def owned_cases(claims: dict[str, Any]) -> list[str]:
    """The officer's own case ids (the case list's grants, each re-checked by the ownership rule), cached briefly."""
    from app.agents.applicant.copilot.capabilities import case_list
    from app.store import get_repository

    subject = case_list._subject(claims)
    ttl = float(cfg().get("cache_seconds", 30))
    with _OWNED_LOCK:
        hit = _OWNED.get(subject)
        if hit and time.time() - hit[0] < ttl:
            return list(hit[1])
    limit = int(case_list.cfg().get("scan_limit", 2000))
    apps = case_list._owned(claims, get_repository().list_granted_cases(subject, limit=limit))
    ids = sorted({a.case_id for a in apps})
    with _OWNED_LOCK:
        _OWNED[subject] = (time.time(), ids)
    return ids


def forget_cache() -> None:
    with _OWNED_LOCK:
        _OWNED.clear()


def _connect():
    """The one shared database every agent schema is in (LOS_AGENTIC_DSN), read-only."""
    import psycopg

    from app.agent_db import core

    dsn = core.shared_dsn()
    if not dsn:
        raise RuntimeError("no shared agent database (LOS_AGENTIC_DSN)")
    conn = psycopg.connect(dsn, connect_timeout=5, options="-c default_transaction_read_only=on "
                                                          "-c statement_timeout=5000")
    return conn


def _keys(case_ids: list[str], link: str, conn) -> dict[str, str]:
    """agent key -> LOS case id, for the given cases and the agent's link kind."""
    links = cfg().get("links") or {}
    prefix = str(links.get("case_prefix") or "")
    if link == "case_id":
        return {c: c for c in case_ids}
    numbers = {c[len(prefix):]: c for c in case_ids if prefix and c.startswith(prefix)}
    if link == "application_number":
        return numbers
    if not numbers:
        return {}
    table, no_col, id_col = (links["application_table"], links["application_number_column"],
                             links["application_id_column"])
    rows = conn.execute(f"SELECT {no_col}, {id_col}::text FROM {table} WHERE {no_col} = ANY(%s)",
                        (list(numbers),)).fetchall()
    return {str(r[1]): numbers[r[0]] for r in rows}


def _latest(conn, key: str, spec: dict[str, Any], case_ids: list[str]) -> dict[str, dict[str, Any]]:
    """LOS case id -> this agent's latest result {verdict, at, details}; a case it never ran on is absent."""
    keys = _keys(case_ids, str(spec["link"]), conn)
    if not keys:
        return {}
    details = list((spec.get("details") or {}).keys())
    cols = ", ".join([f"t.{spec['verdict']}::text", f"t.{spec['time']}"] + [f"t.{d}::text" for d in details])
    k = f"t.{spec['key_column']}::text"
    sql = (f"SELECT DISTINCT ON ({k}) {k}, {cols} FROM {spec['table']} t WHERE {k} = ANY(%s) "
           f"ORDER BY {k}, t.{spec['time']} DESC")
    try:
        rows = conn.execute(sql, (list(keys),)).fetchall()
    except Exception as exc:  # noqa: BLE001 - an agent's table missing / unreadable: "not available", never a pass
        conn.rollback()
        logger.warning("agent insights: %s unreadable (%s)", key, type(exc).__name__)
        return {"__unavailable__": {}}
    out = {}
    for r in rows:
        case_id = keys.get(str(r[0]))
        if case_id:
            out[case_id] = {"verdict": (r[1] or "").upper() or None, "at": r[2],
                            "details": {d: v for d, v in zip(details, r[3:]) if v not in (None, "")}}
    return out


def run_count(case_ids: list[str], agent_keys: list[str]) -> int:
    """How many results (every run, not only the latest) the agents hold for these cases."""
    total = 0
    with _connect() as conn:
        for key in agent_keys:
            spec = agents()[key]
            keys = _keys(case_ids, str(spec["link"]), conn)
            if keys:
                total += int(conn.execute(f"SELECT count(*) FROM {spec['table']} t "
                                          f"WHERE t.{spec['key_column']}::text = ANY(%s)", (list(keys),)).fetchone()[0])
    return total


def gather(case_ids: list[str], agent_keys: list[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """agent -> case id -> its latest result, for the given cases (every configured agent unless named)."""
    wanted = agent_keys or list(agents())
    out: dict[str, dict[str, dict[str, Any]]] = {}
    with _connect() as conn:
        for key in wanted:
            out[key] = _latest(conn, key, agents()[key], case_ids)
    return out


def state_of(key: str, result: dict[str, Any] | None) -> str:
    """NOT_RUN | OK | ATTENTION | OTHER (a result the config does not classify is shown, never judged)."""
    if not result:
        return "NOT_RUN"
    spec = agents()[key]
    v = str(result.get("verdict") or "").upper()
    if v in {str(x).upper() for x in spec.get("ok") or []}:
        return "OK"
    if v in {str(x).upper() for x in spec.get("attention") or []}:
        return "ATTENTION"
    return "OTHER"


# ---------------------------------------------------------------------------------------------------------------------
# 3. RENDER + 4. VERIFY
# ---------------------------------------------------------------------------------------------------------------------

def _app_no(case_id: str) -> str:
    prefix = str((cfg().get("links") or {}).get("case_prefix") or "")
    return case_id[len(prefix):] if prefix and case_id.startswith(prefix) else case_id


def _when(value: Any) -> str:
    return value.strftime("%d %b %Y") if isinstance(value, datetime) else ""


def _human(v: Any) -> str:
    if v is None:
        return ""
    text = str(v).strip()
    return {"true": "yes", "false": "no"}.get(text.lower(), text.replace("_", " ").lower())


def _line(key: str, result: dict[str, Any] | None) -> str:
    spec = agents()[key]
    label = spec.get("label") or key
    if not result:
        return f"- {label}: not run yet"
    bits = [f"- {label}: **{result['verdict'] or 'no result'}**"]
    names = spec.get("details") or {}
    extra = [f"{names.get(d, d)}: {_human(v)}" for d, v in (result.get("details") or {}).items()]
    if extra:
        bits.append("(" + ", ".join(extra) + ")")
    when = _when(result.get("at"))
    if when:
        bits.append(f"on {when}")
    state = state_of(key, result)
    if state == "ATTENTION":
        bits.append("- needs attention")
    return " ".join(bits)


def _case_block(case_id: str, facts: dict[str, dict[str, dict[str, Any]]], keys: list[str]) -> list[str]:
    head = f"**Agent results for {case_id}**" + (f" ({_app_no(case_id)})" if _app_no(case_id) != case_id else "")
    return [head] + [_line(k, facts.get(k, {}).get(case_id)) for k in keys]


def _readiness(case_id: str, facts: dict[str, dict[str, dict[str, Any]]]) -> tuple[str, list[str], list[str]]:
    needs = [k for k in cfg().get("credit_ready_needs") or [] if k in agents()]
    missing = [k for k in needs if state_of(k, facts.get(k, {}).get(case_id)) == "NOT_RUN"]
    attention = [k for k in needs if state_of(k, facts.get(k, {}).get(case_id)) in ("ATTENTION", "OTHER")]
    verdict = "READY" if not missing and not attention else "NOT_READY"
    return verdict, missing, attention


def render_case(ask: Ask, facts: dict[str, dict[str, dict[str, Any]]]) -> str:
    case_id = str(ask.case_id)
    all_keys = list(agents())
    ran = [k for k in all_keys if facts.get(k, {}).get(case_id)]
    if ask.kind == "ONE_AGENT":
        lines = _case_block(case_id, facts, ask.agents)
        for k in ask.agents:
            if state_of(k, facts.get(k, {}).get(case_id)) == "ATTENTION" and agents()[k].get("next"):
                lines.append(f"Next step ({agents()[k]['label']}): {agents()[k]['next']}")
        return "\n".join(lines)
    if ask.kind == "NOT_RUN":
        not_run = [agents()[k]["label"] for k in all_keys if k not in ran]
        if not not_run:
            return f"Every agent has a result on {case_id}."
        return (f"**Agents not run yet on {case_id}:** " + ", ".join(not_run) + ".\n"
                + ("Results so far: " + ", ".join(f"{agents()[k]['label']} {facts[k][case_id]['verdict']}"
                                                  for k in ran) + "." if ran else "No agent has run on it yet."))
    if not ran and ask.kind == "CASE_360":
        return (f"No agent has run on {case_id} yet: none of the agents ("
                + ", ".join(agents()[k]["label"] for k in all_keys) + ") has a result for it.")
    needs = [k for k in cfg().get("credit_ready_needs") or [] if k in agents()]
    lines = _case_block(case_id, facts, [k for k in all_keys if k in ran] if ask.kind == "CASE_360" else needs)
    if ask.kind == "CASE_360":
        not_run = [agents()[k]["label"] for k in all_keys if k not in ran]
        if not_run:
            lines.append("Not run yet: " + ", ".join(not_run) + ".")
    verdict, missing, attention = _readiness(case_id, facts)
    if verdict == "READY":
        lines.append(f"**Credit readiness: ready** - every agent it needs ({len(cfg()['credit_ready_needs'])}) has "
                     "run and none needs attention.")
    else:
        why = []
        if attention:
            why.append("needs review: " + ", ".join(f"{agents()[k]['label']} ({facts[k][case_id]['verdict']})"
                                                     for k in attention))
        if missing:
            why.append("not run yet: " + ", ".join(agents()[k]["label"] for k in missing))
        lines.append("**Credit readiness: not ready** - " + "; ".join(why) + ".")
        steps = [agents()[k]["next"] for k in attention if agents()[k].get("next")]
        if steps:
            lines.append("Next: " + " ".join(dict.fromkeys(steps)))
    return "\n".join(lines)


def portfolio(ask: Ask, owned: list[str], facts: dict[str, dict[str, dict[str, Any]]]) -> list[tuple[str, list[str]]]:
    """[(case id, [agent label + verdict ...])] of the officer's cases that match the ask's filter."""
    keys = ask.agents or list(agents())
    out = []
    for case_id in owned:
        hits = []
        for k in keys:
            r = facts.get(k, {}).get(case_id)
            st = state_of(k, r)
            if ask.filter == "high" and r and "HIGH" in str(r.get("verdict") or ""):
                hits.append(f"{agents()[k]['label']} {r['verdict']}")
            elif ask.filter == "attention" and st in ("ATTENTION",):
                hits.append(f"{agents()[k]['label']} {r['verdict']}")
            elif ask.filter == "ran" and st != "NOT_RUN":
                hits.append(f"{agents()[k]['label']} {r['verdict']}")
        if hits:
            out.append((case_id, hits))
    return out


def render_portfolio(ask: Ask, owned: list[str], matches: list[tuple[str, list[str]]],
                     runs: int | None = None) -> str:
    keys = ask.agents or list(agents())
    scope = " / ".join(agents()[k]["label"] for k in keys) if ask.agents else "any agent"
    n, total = len(matches), len(owned)
    what = {"high": f"{'is' if n == 1 else 'are'} high risk", "attention": f"need{'s' if n == 1 else ''} attention",
            "ran": f"{'has' if n == 1 else 'have'} a result"}[ask.filter]
    head = f"**{n} of your {total} cases** {what} ({scope})."
    if runs is not None:
        noun = str(agents()[keys[0]].get("run_noun") or "run") if len(keys) == 1 else "run"
        head += f" {runs} {noun}{'' if runs == 1 else 's'} in all."
    if n == 0:
        return head
    if ask.mode == "count":
        return head + " Say \"which ones?\" to see them."
    top = int(cfg().get("max_listed", 12))
    lines = [head] + [f"- {_app_no(c)} ({c}): " + ", ".join(h) for c, h in matches[:top]]
    if n > top:
        lines.append(f"...and {n - top} more.")
    return "\n".join(lines)


_NUM = re.compile(r"(?<![A-Z0-9-])\d+(?![0-9])")


def verify(text: str, allowed: set[str]) -> bool:
    """Every standalone number in the reply is one of the facts' numbers (counts, dates, details). The ids carry
    their digits inside them (LOS-DUMMY-0005), so they are never read as numbers here."""
    scrubbed = _CASE_ID.sub(" ", _APP_NO.sub(" ", text))
    return all(n in allowed for n in _NUM.findall(scrubbed))


def _allowed_numbers(*parts: Any) -> set[str]:
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, datetime):
            out.update({v.strftime("%d"), str(v.day), v.strftime("%Y")})
        elif v is not None:
            out.update(_NUM.findall(str(v)))
    for p in parts:
        walk(p)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# THE TURN
# ---------------------------------------------------------------------------------------------------------------------

def _previous(state, conversation_id: str | None) -> dict[str, Any] | None:
    spec = ((getattr(state, "flow", None) or {}).get("agent_insights") or {})
    if not spec or time.time() - float(spec.get("at", 0)) > float(cfg().get("followup_ttl_seconds", 1800)):
        return None
    if conversation_id and spec.get("conversation_id") and spec["conversation_id"] != conversation_id:
        return None
    return spec.get("ask")


def _remember(state, ask: Ask | None, conversation_id: str | None) -> None:
    if state is None:
        return
    from app.agents.applicant.copilot.capabilities import workspace

    flow = dict(getattr(state, "flow", None) or {})
    if ask is None:
        if "agent_insights" not in flow:
            return
        flow.pop("agent_insights", None)
    else:
        flow["agent_insights"] = {"ask": ask.as_dict(), "at": time.time(), "conversation_id": conversation_id}
    state.flow = flow
    try:
        workspace._save(state)
    except Exception:  # noqa: BLE001 - the answer stands; only the follow-up context is lost
        pass


def answer(message: str, claims: dict[str, Any], request_id: str, *, state=None, open_case: str | None = None,
           conversation_id: str | None = None) -> dict[str, Any] | None:
    """The agent-insights reply for this message, or None (another layer answers it)."""
    if not enabled():
        return None
    from app.agent_db import core
    from app.agents.applicant.copilot.capabilities import general

    if not core.shared_dsn():
        return None                  # no agents' database configured here: the other layers answer as before

    previous = _previous(state, conversation_id)
    ask = understand(message, previous, open_case)
    if ask is None:
        _remember(state, None, conversation_id)
        return None
    started = time.perf_counter()
    try:
        owned = owned_cases(claims)
    except Exception as exc:  # noqa: BLE001 - the case store unreadable: not this layer's answer
        logger.warning("agent insights: own cases unreadable (%s)", type(exc).__name__)
        return None
    if ask.case_id and ask.case_id not in set(owned):
        text = (f"I couldn't find {ask.case_id} among your cases. Check the case id, or open the case and ask again.")
        reply = general._reply(request_id, INTENT, text, category="CASE_ONLY")
        reply.update(query_type=INTENT, response_source="STRUCTURED", agent_insights={"ask": ask.as_dict(),
                                                                                     "found": False})
        return reply
    try:
        if ask.kind == "PORTFOLIO":
            facts = gather(owned, ask.agents or None)
            matches = portfolio(ask, owned, facts)
            runs = run_count(owned, ask.agents) if ask.filter == "ran" and ask.agents else None
            text = render_portfolio(ask, owned, matches, runs)
            allowed = _allowed_numbers(len(matches), len(owned), int(cfg().get("max_listed", 12)),
                                       len(matches) - int(cfg().get("max_listed", 12)), runs, facts)
            extra = {"count": len(matches), "of": len(owned), "cases": [c for c, _ in matches]}
        else:
            facts = gather([str(ask.case_id)])
            text = render_case(ask, facts)
            allowed = _allowed_numbers(facts, len(cfg().get("credit_ready_needs") or []))
            verdict, missing, attention = _readiness(str(ask.case_id), facts)
            extra = {"readiness": verdict, "not_run": missing, "attention": attention,
                     "results": {k: (v.get(ask.case_id) or {}).get("verdict") for k, v in facts.items()}}
    except Exception as exc:  # noqa: BLE001 - the agents' database unreadable: said plainly, never guessed
        logger.warning("agent insights: unavailable (%s)", type(exc).__name__)
        text, allowed, extra = ("The agents' results can't be read right now. Please try again in a moment.",
                                set(), {"unavailable": True})
    if not verify(text, allowed):                      # a number no fact supports never reaches the officer
        logger.error("agent insights: reply failed its number check; the plain fallback is sent")
        text = "I can't state these results reliably right now. Please open the case's agent screens."
        extra["verify_failed"] = True
    reply = general._reply(request_id, INTENT, text, case_id=ask.case_id, category="CASE_ONLY")
    reply.update(query_type=INTENT, response_source="STRUCTURED", tools_invoked=["agents.results"],
                 agent_insights={"ask": ask.as_dict(), **extra,
                                 "ms": round((time.perf_counter() - started) * 1000, 1)})
    _remember(state, ask, conversation_id)
    return reply


__all__ = ["Ask", "INTENT", "agents", "answer", "case_named", "cfg", "enabled", "gather", "named_agents",
           "owned_cases", "portfolio", "render_case", "render_portfolio", "understand", "verify"]
