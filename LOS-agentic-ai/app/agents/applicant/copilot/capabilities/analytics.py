"""
COPILOT ANALYTICS -- counts, totals, averages, min / max, group-by and period comparisons over the officer's OWN cases
(2026-11-02, master prompt section 7; config app/config/copilot_analytics.yaml).

    "how many applications this month?"            -> COUNT, period this_month
    "average loan amount of home loan cases"       -> AVG(loan_amount), product = HOME_LOAN
    "cases by stage" / "stage-wise count"          -> COUNT grouped by stage
    "compare this month with last month"           -> COUNT, this_month vs last_month
    then "and the total amount?" / "compare with September" / "only home loans"
                                                    -> the last question with ONLY the named part changed

THREE STEPS, each testable on its own:
    understand(message, previous) -> Query | Unsupported | None    the words -> a validated query (no model)
    run(claims, query)            -> Result                        the numbers, from the store, in code
    reply(result)                 -> the chat reply, stating the metric, filters, period and basis

READ-ONLY AND AUTHORISED: the rows are case_insights._rows (the case list's own, ownership re-checked per case). A
metric / dimension / filter outside the config is rejected; a value that is not a plain number is left out and said
so, never guessed. The last QUESTION (never its numbers) is kept in the conversation for follow-ups: a follow-up is
always re-computed from the store.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(__file__).resolve().parents[4] / "config" / "copilot_analytics.yaml"
OPS = ("count", "sum", "avg", "min", "max")


@lru_cache(maxsize=1)
def cfg() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def enabled() -> bool:
    """config `enabled`; COPILOT_ANALYTICS=false switches it off without a deploy (rollback / A-B arm)."""
    import os

    flag = (os.getenv("COPILOT_ANALYTICS") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


# ---------------------------------------------------------------------------------------------------------------------
# the query
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Period:
    key: str                 # this_month / last_month / month:2026-10 / last_n:30 / ...
    start: date
    end: date                # inclusive

    def phrase(self) -> str:
        names = {"today": "today", "yesterday": "yesterday", "this_week": "this week", "last_week": "last week",
                 "this_month": "this month", "last_month": "last month", "this_year": "this year",
                 "last_year": "last year"}
        if self.key in names:
            return names[self.key]
        if self.key.startswith("month:"):
            return "in " + self.start.strftime("%B %Y")
        if self.key.startswith("last_n:"):
            return f"in the last {self.key.split(':')[1]} days"
        return f"from {self.start} to {self.end}"

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Period | None":
        if not d:
            return None
        return cls(str(d["key"]), date.fromisoformat(d["start"]), date.fromisoformat(d["end"]))


@dataclass
class Query:
    op: str = "count"
    metric: str = "cases"
    group_by: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    period: Period | None = None
    compare: Period | None = None          # a second period: period vs compare

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["period"] = self.period.as_dict() if self.period else None
        d["compare"] = self.compare.as_dict() if self.compare else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Query | None":
        if not d:
            return None
        return cls(op=d.get("op", "count"), metric=d.get("metric", "cases"), group_by=d.get("group_by"),
                   filters=dict(d.get("filters") or {}), period=Period.from_dict(d.get("period")),
                   compare=Period.from_dict(d.get("compare")))


@dataclass
class Unsupported:
    topic: str
    text: str


class InvalidQuery(ValueError):
    """A query outside the configured allow-lists (never reaches the store)."""


def validate(q: Query) -> Query:
    """The allow-lists: op, metric, dimension, filter keys; a count is of cases, a value op needs a numeric field."""
    c = cfg()
    if q.op not in OPS:
        raise InvalidQuery(f"operation {q.op!r}")
    if q.metric not in (c.get("metrics") or {}):
        raise InvalidQuery(f"metric {q.metric!r}")
    if q.op == "count" and q.metric != "cases":
        q.metric = "cases"
    if q.op != "count" and q.metric == "cases":
        raise InvalidQuery("a total / average needs a recorded amount")
    dims = c.get("dimensions") or {}
    if q.group_by is not None and q.group_by not in dims:
        raise InvalidQuery(f"dimension {q.group_by!r}")
    for key in q.filters:
        if key not in dims or key == "month":
            raise InvalidQuery(f"filter {key!r}")
    limit = int(c.get("max_period_days", 1100))
    for p in (q.period, q.compare):
        if p is not None and (p.end < p.start or (p.end - p.start).days + 1 > limit):
            raise InvalidQuery(f"period {(p.end - p.start).days + 1} days")
    return q


# ---------------------------------------------------------------------------------------------------------------------
# understanding the words (no model)
# ---------------------------------------------------------------------------------------------------------------------

def _norm(message: str) -> str:
    return " " + " ".join(re.sub(r"[^\w\s:-]", " ", str(message or "").lower().replace("-", " ")).split()) + " "


def _has(text: str, phrase: str) -> bool:
    return f" {' '.join(str(phrase).lower().replace('-', ' ').split())} " in text


def _any(text: str, phrases) -> bool:
    return any(_has(text, p) for p in phrases or [])


def _today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(str(cfg().get("timezone") or "UTC"))).date()


def _month_span(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def _named_period(key: str, today: date) -> Period:
    if key == "today":
        return Period(key, today, today)
    if key == "yesterday":
        d = today - timedelta(days=1)
        return Period(key, d, d)
    if key == "this_week":
        return Period(key, today - timedelta(days=today.weekday()), today)
    if key == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return Period(key, start, start + timedelta(days=6))
    if key == "this_month":
        return Period(key, today.replace(day=1), today)
    if key == "last_month":
        prev = today.replace(day=1) - timedelta(days=1)
        return Period(key, *_month_span(prev.year, prev.month))
    if key == "this_year":
        return Period(key, date(today.year, 1, 1), today)
    if key == "last_year":
        return Period(key, date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    raise KeyError(key)


def periods_in(message: str, today: date | None = None) -> list[Period]:
    """Every period the message names, in the order written ("this month vs last month" -> two)."""
    today = today or _today()
    text = _norm(message)
    found: list[tuple[int, Period]] = []
    for key, phrases in (cfg().get("periods") or {}).items():
        for p in phrases or []:
            needle = f" {' '.join(str(p).lower().split())} "
            pos = text.find(needle)
            if pos >= 0:
                found.append((pos, _named_period(key, today)))
                break
    for pattern in cfg().get("last_n_days") or []:
        for m in re.finditer(pattern, text):
            n = int(m.group(1))
            found.append((m.start(), Period(f"last_n:{n}", today - timedelta(days=n - 1), today)))
    months = {str(k).lower(): int(v) for k, v in (cfg().get("months") or {}).items()}
    for m in re.finditer(r" (" + "|".join(sorted(map(re.escape, months), key=len, reverse=True)) + r")(?: (\d{4}))? ",
                         text):
        # "may" as a verb ("may I ...") is not a month: a month needs a year, or a period word around it
        name, year = m.group(1), m.group(2)
        if name == "may" and not year:
            continue
        month = months[name]
        y = int(year) if year else (today.year if month <= today.month else today.year - 1)
        start, end = _month_span(y, month)
        found.append((m.start(), Period(f"month:{y}-{month:02d}", start, min(end, today) if y == today.year
                                        and month == today.month else end)))
    found.sort(key=lambda t: t[0])
    out: list[Period] = []
    for _, p in found:
        if all(p.key != q.key for q in out):
            out.append(p)
    return out


def _strip_periods(text: str) -> str:
    for phrases in (cfg().get("periods") or {}).values():
        for p in phrases or []:
            text = text.replace(f" {' '.join(str(p).lower().split())} ", " ")
    return text


def _op_in(text: str) -> str | None:
    ops = cfg().get("operations") or {}
    for op in ("avg", "min", "max", "count", "sum"):          # "total number of cases" is a count, below
        if _any(text, ops.get(op)):
            return op
    return None


def _metric_in(text: str) -> str | None:
    best, best_len = None, 0
    for name, spec in (cfg().get("metrics") or {}).items():
        for w in spec.get("words") or []:
            if _has(text, w) and len(w) > best_len:
                best, best_len = name, len(w)
    return best


def _dimension_in(text: str) -> str | None:
    """A dimension named as the GROUPING ("by stage", "stage wise", "per product", "distribution ... stage")."""
    c = cfg()
    cues = c.get("group_cues") or []
    text = _strip_periods(text)
    for name, spec in (c.get("dimensions") or {}).items():
        for w in spec.get("words") or []:
            w = " ".join(str(w).lower().replace("-", " ").split())
            if f" {w} " not in text:
                continue
            for cue in cues:
                # "by stage", "by workflow stage", "stage wise"
                if re.search(rf" {re.escape(cue)} (?:\w+ ){{0,2}}{re.escape(w)} ", text) or f" {w} {cue} " in text:
                    return name
            if _any(text, ["distribution", "breakdown", "split"]):
                return name
    return None


def _filters_in(text: str, rows: list[dict[str, Any]] | None) -> dict[str, str]:
    """Dimension VALUES named in the message ("home loan cases", "cases in review"), from the config aliases and the
    values the officer's own cases carry -- never a free-text filter."""
    out: dict[str, str] = {}
    for dim, aliases in (cfg().get("value_aliases") or {}).items():
        for value, words in (aliases or {}).items():
            if _any(text, words) or _has(text, str(value).lower().replace("_", " ")):
                out[dim] = str(value)
    for dim in ("stage", "status"):
        for value in sorted({str(r.get(dim) or "") for r in rows or []} - {"", "--"}, key=len, reverse=True):
            if len(value) >= 3 and _has(text, value.lower().replace("_", " ")):
                out[dim] = value
                break
    situation = (cfg().get("dimensions") or {}).get("situation") or {}
    try:
        from app.agents.applicant.copilot.semantics import task as _task

        key = _task.filter_in(text.strip())
    except Exception:  # noqa: BLE001 - no task vocabulary: no situation filter
        key = None
    kinds = {"kyc_issue": "KYC", "documents_pending": "DOCS", "ready_for_cpa": "READY"}
    if key in kinds and kinds[key] in (situation.get("values") or {}):
        out["situation"] = kinds[key]
    return out


def unsupported_in(message: str) -> Unsupported | None:
    text = _norm(message)
    for topic, spec in (cfg().get("unsupported") or {}).items():
        if _any(text, spec.get("words")):
            return Unsupported(topic, " ".join(str(spec.get("text") or "").split()))
    return None


def _is_analytic(text: str, op: str | None, metric: str | None, group: str | None, periods: list[Period]) -> bool:
    c = cfg()
    noun = _any(text, c.get("case_nouns"))
    if group and (noun or metric or op):
        return True
    # a VALUE over the portfolio needs a portfolio word, a period or a grouping: "maximum loan amount for personal
    # loan" is a policy question, "maximum loan amount of my cases this month" is analytics
    if op in ("sum", "avg", "min", "max") and metric and (_any(text, c.get("portfolio_words")) or periods):
        return True
    if op == "count" and noun:
        return True
    if _any(text, c.get("compare_cues")) and len(periods) >= 2 and (noun or metric):
        return True
    return False


def _followup(text: str, previous: Query) -> bool:
    """A short message that REFERS BACK to the last analytics question -- never any short message: "what is my loan
    amount?" after a count is the open case's amount, not a total over the portfolio."""
    spec = cfg().get("followup") or {}
    words = text.split()
    if not words or len(words) > int(spec.get("max_words", 9)):
        return False
    if any(text.startswith(f" {' '.join(str(c).lower().split())} ") for c in spec.get("starts") or []):
        return True
    if _any(text, spec.get("refers")):
        return True
    return len(words) <= int(spec.get("period_only_words", 4)) and bool(periods_in(text))


def understand(message: str, previous: dict[str, Any] | None = None,
               rows: list[dict[str, Any]] | None = None, today: date | None = None):
    """The analytics question in the message (a validated Query), an Unsupported topic, or None (not analytics).
    `previous`: the last analytics question of this conversation -- a short follow-up changes only what it names."""
    if not enabled():
        return None
    text = _norm(message)
    today = today or _today()
    periods = periods_in(message, today)
    op, metric, group = _op_in(text), _metric_in(text), _dimension_in(text)
    if op == "sum" and not metric:
        op = "count"                                    # "total cases" / "total applications" is a count
    prev = Query.from_dict(previous) if previous else None
    compare = _any(text, cfg().get("compare_cues"))
    analytic = _is_analytic(text, op, metric, group, periods)
    unsupported = unsupported_in(message)
    # "how many were approved", "average sanctioned amount", "which branch has most cases": data FOS does not record
    noun_or_metric = bool(metric) or _any(text, cfg().get("case_nouns"))
    if unsupported and (analytic or ((op or compare) and noun_or_metric) or (prev and _followup(text, prev))):
        return unsupported
    if not analytic:
        if prev is None or not _followup(text, prev):
            return None
        # A FOLLOW-UP of the last analytics question: change only what this message names
        changed = bool(periods or op or metric or group or compare or _filters_in(text, rows))
        if not changed:
            return None
        q = Query.from_dict(prev.as_dict())
        if compare and periods:
            base = q.period or _named_period("this_month", today)
            q.period, q.compare = base, periods[0] if periods[0].key != base.key else None
            if len(periods) >= 2:
                q.period, q.compare = periods[0], periods[1]
        elif periods:
            q.period, q.compare = periods[0], (periods[1] if len(periods) > 1 else None)
        if metric and metric != "cases":
            q.metric, q.op = metric, (op if op and op != "count" else (q.op if q.op != "count" else "sum"))
        elif op:
            q.op = op
            if op == "count":
                q.metric = "cases"
        if group:
            q.group_by = group
        q.filters.update(_filters_in(text, rows))
        return validate(q)
    q = Query(op=op or "count", metric=metric if (metric and (op or "count") != "count") else "cases",
              group_by=group, filters=_filters_in(text, rows))
    if q.op != "count" and q.metric == "cases":
        q.metric = metric or "loan_amount"
    if periods:
        q.period = periods[0]
        if len(periods) > 1 and compare:
            q.compare = periods[1]
    elif compare and prev is not None and prev.period is not None:
        q.period = prev.period
    return validate(q)


# ---------------------------------------------------------------------------------------------------------------------
# computing (the store, in code)
# ---------------------------------------------------------------------------------------------------------------------

def _number(raw: Any) -> Decimal | None:
    s = re.sub(r"[,\s₹]|rs\.?|inr", "", str(raw if raw is not None else ""), flags=re.I)
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _created(row: dict[str, Any]) -> date | None:
    raw = str(row.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo

    return moment.astimezone(ZoneInfo(str(cfg().get("timezone") or "UTC"))).date()


_LIGHT: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def light_rows(claims: dict[str, Any]) -> list[dict[str, Any]]:
    """THE CHEAP ROWS (2026-11-04): the officer's own cases with only what a count / sum / group by stage, status,
    product, month or a metric needs -- stage (one resolve per case) and the application's own fields. The full case
    list row (KYC, documents, timeline: ~33 queries a case, 12-18 s for 500 cases) is built only for a "situation"
    question (rows_for). Same ownership rule; cached per officer as long as the case list's rows."""
    from app.agents.applicant.copilot.capabilities import case_list, product_flow, workspace
    from app.store import get_repository

    subject = case_list._subject(claims)
    ttl = float(case_list.cfg().get("cache_ttl_seconds", 30))
    hit = _LIGHT.get(subject)
    if hit and hit[0] > time.time():
        return [dict(r) for r in hit[1]]
    limit = int(case_list.cfg().get("scan_limit", 2000))
    apps = case_list._owned(claims, get_repository().list_granted_cases(subject, limit=limit))
    fields = {str(s.get("field")) for s in (cfg().get("metrics") or {}).values() if s.get("field")}
    fields |= {str(s.get("field")) for s in (cfg().get("dimensions") or {}).values() if s.get("field")}

    def row(app: Any) -> dict[str, Any]:
        stage = workspace._stage(app.case_id) or "--"
        out = {"case_id": app.case_id, "applicant_id": app.applicant_id, "stage": stage,
               "status": product_flow.status_of(stage), "product": getattr(app, "product", None),
               "created_at": str(getattr(app, "created_at", "") or ""),
               "updated_at": str(getattr(app, "updated_at", "") or ""),
               "loan_amount": getattr(app, "loan_amount", None)}
        for f in fields:
            out[f] = getattr(app, f, None)
        return out

    rows = case_list._parallel(row, apps)
    _LIGHT[subject] = (time.time() + ttl, rows)
    return [dict(r) for r in rows]


def needs_full_rows(q: Any) -> bool:
    """A query that groups or filters by "situation" (KYC issue / documents pending / ready) needs the case list's
    full rows; everything else is answered from light_rows."""
    if q is None or isinstance(q, Unsupported):
        return False
    return getattr(q, "group_by", None) == "situation" or "situation" in (getattr(q, "filters", None) or {})


def rows_for(claims: dict[str, Any]) -> list[dict[str, Any]]:
    """The caller's own case rows (case_insights._rows: the case list's, ownership re-checked) + the metric fields."""
    from app.agents.applicant.copilot.capabilities import case_insights, case_list
    from app.store import get_repository

    rows = case_insights._rows(claims)
    fields = {str(s.get("field")) for s in (cfg().get("metrics") or {}).values() if s.get("field")}
    fields |= {str(s.get("field")) for s in (cfg().get("dimensions") or {}).values() if s.get("field")}
    fields.discard("loan_amount")                       # already on the row
    if fields and rows:
        repository = get_repository()
        for row in rows:
            try:
                app = repository.get_application(row["case_id"])
            except Exception:  # noqa: BLE001 - unreadable: its fields stay unknown (left out, said so)
                app = None
            for f in fields:
                row[f] = getattr(app, f, None) if app is not None else None
    _ = case_list
    return rows


def _value_of(row: dict[str, Any], dim: str) -> str:
    if dim == "month":
        d = _created(row)
        return d.strftime("%Y-%m") if d else "unknown"
    if dim == "situation":
        return str(row.get("status_kind") or "OTHER")
    spec = (cfg().get("dimensions") or {}).get(dim) or {}
    return str(row.get(spec.get("field") or dim) or "unknown")


def _select(rows: list[dict[str, Any]], q: Query, period: Period | None) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if any(_value_of(r, k).upper() != str(v).upper() for k, v in q.filters.items()):
            continue
        if period is not None:
            d = _created(r)
            if d is None or not (period.start <= d <= period.end):
                continue
        out.append(r)
    return out


@dataclass
class Value:
    value: Decimal | int | None
    n: int                 # cases counted / with a number
    missing: int = 0       # cases left out (no number recorded)


def _aggregate(rows: list[dict[str, Any]], q: Query) -> Value:
    if q.op == "count":
        return Value(len(rows), len(rows))
    field_name = str(((cfg().get("metrics") or {}).get(q.metric) or {}).get("field") or q.metric)
    numbers = [n for n in (_number(r.get(field_name)) for r in rows) if n is not None]
    missing = len(rows) - len(numbers)
    if not numbers:
        return Value(None, 0, missing)
    if q.op == "sum":
        v: Decimal = sum(numbers, Decimal(0))
    elif q.op == "avg":
        v = sum(numbers, Decimal(0)) / len(numbers)
    elif q.op == "min":
        v = min(numbers)
    else:
        v = max(numbers)
    return Value(v, len(numbers), missing)


@dataclass
class Result:
    query: Query
    total_cases: int
    value: Value | None = None
    compare_value: Value | None = None
    groups: list[tuple[str, Value]] = field(default_factory=list)


def run(claims: dict[str, Any], q: Query, rows: list[dict[str, Any]] | None = None) -> Result:
    validate(q)
    rows = rows_for(claims) if rows is None else rows
    chosen = _select(rows, q, q.period)
    result = Result(query=q, total_cases=len(rows))
    if q.group_by:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in chosen:
            buckets.setdefault(_value_of(r, q.group_by), []).append(r)
        values = [(k, _aggregate(v, q)) for k, v in buckets.items()]
        values.sort(key=lambda kv: (-(kv[1].value or 0), kv[0]))
        result.groups = values
        result.value = _aggregate(chosen, q)
    else:
        result.value = _aggregate(chosen, q)
    if q.compare is not None:
        result.compare_value = _aggregate(_select(rows, q, q.compare), q)
    return result


# ---------------------------------------------------------------------------------------------------------------------
# the reply
# ---------------------------------------------------------------------------------------------------------------------

def _t(key: str, **values: Any) -> str:
    text = str((cfg().get("texts") or {}).get(key) or "")
    return text.format(**values) if values else text


def _fmt(value: Decimal | int | None, metric: str) -> str:
    if value is None:
        return "--"
    unit = ((cfg().get("metrics") or {}).get(metric) or {}).get("unit")
    if unit == "rupees":
        whole = int(Decimal(value).quantize(Decimal("1")))
        return ("-" if whole < 0 else "") + "₹" + _indian(abs(whole))
    if unit == "months":
        shown = Decimal(value).quantize(Decimal("0.1"))
        return f"{int(shown) if shown == shown.to_integral_value() else shown} months"
    return str(value)


def _indian(n: int) -> str:
    s = str(abs(n))
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return ("-" if n < 0 else "") + (head + "," + tail if head else tail)


def _label(dim: str, value: str) -> str:
    spec = (cfg().get("dimensions") or {}).get(dim) or {}
    if dim == "month" and re.fullmatch(r"\d{4}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m").strftime("%B %Y")
    return str((spec.get("values") or {}).get(value) or value.replace("_", " ").title())


def _filters_text(q: Query) -> str:
    if not q.filters:
        return ""
    parts = [f"{(cfg().get('dimensions') or {}).get(k, {}).get('label', k)} = {_label(k, v)}"
             for k, v in q.filters.items()]
    return " where " + ", ".join(parts)


def _period_text(p: Period | None) -> str:
    if p is None:
        return _t("all_time")
    return _t("period_phrase", period=p.phrase(), start=p.start.strftime("%d %b %Y"), end=p.end.strftime("%d %b %Y"))


def render(result: Result) -> str:
    q, c = result.query, cfg()
    metric = (c.get("metrics") or {}).get(q.metric) or {}
    op_label = (c.get("texts") or {}).get("op_labels", {}).get(q.op, q.op)
    lines: list[str] = []
    if q.compare is not None and result.compare_value is not None:
        a, b = result.value, result.compare_value
        lines.append(_t("compare_heading", metric_label="Cases" if q.op == "count"
                        else f"{op_label} {str(metric.get('label')).lower()}",
                        a_label=q.period.phrase() if q.period else "all", b_label=q.compare.phrase()))
        lines.append(_t("compare_row", label=f"{q.period.phrase()} ({_span(q.period)})" if q.period else "all",
                        value=_fmt(a.value, q.metric)))
        lines.append(_t("compare_row", label=f"{q.compare.phrase()} ({_span(q.compare)})",
                        value=_fmt(b.value, q.metric)))
        if a.value is not None and b.value is not None:
            change = Decimal(a.value) - Decimal(b.value)
            sign = "+" if change > 0 else ""
            shown = (_fmt(change, q.metric) if change < 0 else sign + _fmt(change, q.metric))                 if q.metric != "cases" else f"{sign}{change}"
            if Decimal(b.value) != 0:
                pct = (change / Decimal(b.value) * 100).quantize(Decimal("0.1"))
                lines.append(_t("compare_change_pct", change=shown, pct=f"{sign}{pct}%"))
            else:
                lines.append(_t("compare_change", change=shown))
    elif q.group_by:
        dim_label = ((c.get("dimensions") or {}).get(q.group_by) or {}).get("label", q.group_by)
        head = (f"**{op_label} of cases by {dim_label.lower()}**" if q.op == "count"
                else f"**{op_label} {str(metric.get('label')).lower()} by {dim_label.lower()}**")
        lines.append(head + " " + _period_text(q.period))
        limit = int(c.get("max_groups", 12))
        shown, rest = result.groups[:limit], result.groups[limit:]
        for key, v in shown:
            lines.append(_t("group_row", label=_label(q.group_by, key), value=_fmt(v.value, q.metric)))
        if rest:
            lines.append(_t("group_row", label=_t("others"), value=sum(v.n for _, v in rest)))
        if not result.groups:
            lines.append(_t("none_in_period", period=q.period.phrase()) if q.period else _t("no_cases"))
    else:
        v = result.value
        if q.op == "count":
            lines.append(_t("heading_count", n=v.value, noun="case" if v.value == 1 else "cases",
                            period_phrase=_period_text(q.period)))
        elif v.value is None:
            lines.append(_t("not_numeric", metric_label_lower=str(metric.get("label")).lower()))
        else:
            lines.append(_t("heading_value", op_label=op_label, metric_label=str(metric.get("label")).lower(),
                            value=_fmt(v.value, q.metric), period_phrase=_period_text(q.period)))
    v = result.value
    basis_n = v.n if v is not None else 0
    lines.append("")
    lines.append(_t("basis", n=basis_n if q.op != "count" else (v.value if v else 0), filters=_filters_text(q),
                    definition=metric.get("definition") or ""))
    if v is not None and v.missing:
        lines.append(_t("basis_missing", missing=v.missing, metric_label_lower=str(metric.get("label")).lower()))
    return "\n".join(lines)


def _span(p: Period) -> str:
    return f"{p.start.strftime('%d %b')} to {p.end.strftime('%d %b %Y')}"


# ---------------------------------------------------------------------------------------------------------------------
# the chat turn
# ---------------------------------------------------------------------------------------------------------------------

def _previous(state, conversation_id: str | None = None) -> dict[str, Any] | None:
    """The last analytics QUESTION of this officer's chat -- only while it is the last thing asked (forget() runs on
    any other turn), within the TTL, and never from another conversation."""
    spec = ((getattr(state, "flow", None) or {}).get("analytics") or {})
    ttl = float((cfg().get("followup") or {}).get("ttl_seconds", 1800))
    if not spec or time.time() - float(spec.get("at", 0)) > ttl:
        return None
    if conversation_id and spec.get("conversation_id") and spec["conversation_id"] != conversation_id:
        return None
    return spec.get("query")


def forget(state) -> None:
    """Another kind of turn was asked: "what about tenure?" next is about THAT, not the last count."""
    flow = dict(getattr(state, "flow", None) or {})
    if state is None or "analytics" not in flow:
        return
    flow.pop("analytics", None)
    state.flow = flow
    try:
        from app.agents.applicant.copilot.capabilities import workspace

        workspace._save(state)
    except Exception:  # noqa: BLE001 - the follow-up context is only ever narrower for it
        pass


def _remember(state, q: Query, conversation_id: str | None = None) -> None:
    if state is None:
        return
    from app.agents.applicant.copilot.capabilities import workspace

    flow = dict(getattr(state, "flow", None) or {})
    flow["analytics"] = {"query": q.as_dict(), "at": time.time(), "conversation_id": conversation_id}
    state.flow = flow
    try:
        workspace._save(state)
    except Exception:  # noqa: BLE001 - the answer stands; only the follow-up context is lost
        pass


def answer(message: str, claims: dict[str, Any], request_id: str, *, state=None,
           strong_only: bool = False, conversation_id: str | None = None) -> dict[str, Any] | None:
    """The analytics reply for this message, or None. `strong_only`: claim only a message that names a period, a
    grouping, a value operation or a follow-up of the last analytics question (case insights answers the rest)."""
    if not enabled():
        return None
    from app.agents.applicant.copilot.capabilities import general

    previous = _previous(state, conversation_id)
    text = _norm(message)
    if strong_only and not (periods_in(message) or _dimension_in(text) or previous or unsupported_in(message)
                            or _op_in(text) in ("sum", "avg", "min", "max")
                            or (_op_in(text) == "count" and _any(text, cfg().get("strong_filter_words") or []))):
        return None
    reply = _answer(message, claims, request_id, previous)
    if reply is None:
        forget(state)
    elif (reply.get("analytics") or {}).get("query"):
        _remember(state, Query.from_dict(reply["analytics"]["query"]), conversation_id)
    return reply


def _answer(message: str, claims: dict[str, Any], request_id: str,
            previous: dict[str, Any] | None) -> dict[str, Any] | None:
    from app.agents.applicant.copilot.capabilities import general

    try:
        rows = light_rows(claims)
    except Exception:  # noqa: BLE001 - the store unreadable: not this layer's answer
        return None
    try:
        q = understand(message, previous, rows)
    except InvalidQuery as exc:
        if "period" in str(exc):
            days = re.search(r"(\d+)", str(exc))
            return general._reply(request_id, "CASE_ANALYTICS", _t("too_long", days=days.group(1) if days else "?"),
                                  category="CASE_ONLY")
        return None
    if q is None:
        return None
    if isinstance(q, Unsupported):
        reply = general._reply(request_id, "CASE_ANALYTICS", q.text, category="CASE_ONLY")
        reply.update(query_type="CASE_ANALYTICS", response_source="STRUCTURED",
                     analytics={"unsupported": q.topic})
        return reply
    if not rows:
        return general._reply(request_id, "CASE_ANALYTICS", _t("no_cases"), category="CASE_ONLY")
    if needs_full_rows(q):
        try:
            rows = rows_for(claims)
        except Exception:  # noqa: BLE001
            return None
    result = run(claims, q, rows)
    reply = general._reply(request_id, "CASE_ANALYTICS", render(result), category="CASE_ONLY")
    v = result.value
    reply.update(query_type="CASE_ANALYTICS", response_source="STRUCTURED", tools_invoked=["case.analytics"],
                 analytics={"query": q.as_dict(),
                            "value": None if v is None or v.value is None else str(v.value),
                            "cases": v.n if v else 0, "left_out": v.missing if v else 0,
                            "compare_value": (None if result.compare_value is None
                                              or result.compare_value.value is None
                                              else str(result.compare_value.value)),
                            "groups": [{"key": k, "value": None if g.value is None else str(g.value), "cases": g.n}
                                       for k, g in result.groups]})
    return reply


__all__ = ["InvalidQuery", "Period", "Query", "Result", "Unsupported", "answer", "cfg", "enabled", "periods_in",
           "render", "run", "understand", "unsupported_in", "validate"]
