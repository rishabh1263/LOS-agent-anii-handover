"""Dates as the document pipeline records them -- parsed strictly; anything else is unreadable (None), never guessed."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.upper(): i for i, m in enumerate(calendar.month_name) if m})
_DAY_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y")
_MONTH_YEAR = re.compile(r"^([A-Za-z]+)[\s\-/,']+(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse(value: Any) -> date | None:
    """A calendar date. A month without a day ("MAY 2026", "2026-05") is that month's LAST day."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", text) else text
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = _MONTH_YEAR.match(text)
    if m and m.group(1).upper() in _MONTHS:
        return month_end(int(m.group(2)), _MONTHS[m.group(1).upper()])
    m = _YEAR_MONTH.match(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return month_end(int(m.group(1)), int(m.group(2)))
    return None


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def covers_months(start: date, end: date, months: int) -> bool:
    """Whether [start, end] spans `months` calendar months: 01-07 .. 30-09 covers 3."""
    from datetime import timedelta

    return end >= add_months(start, months) - timedelta(days=1)


__all__ = ["add_months", "covers_months", "month_end", "parse"]
