"""Date helpers. All dates are ISO "YYYY-MM-DD" strings in the configured timezone."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Matches Intl en-GB short months (note "Sept").
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
_LONG_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_LONG_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def now_ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_iso(now: datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def _to_date(iso: str) -> date:
    return date(int(iso[0:4]), int(iso[5:7]), int(iso[8:10]))


def days_between(a: str, b: str) -> int:
    return (_to_date(b) - _to_date(a)).days


def is_real_date(iso: object) -> bool:
    if not isinstance(iso, str) or not _ISO.fullmatch(iso):
        return False
    try:
        return _to_date(iso).isoformat() == iso
    except ValueError:
        return False


def label(iso: str) -> str:
    """'Tue 3 Dec'"""
    d = _to_date(iso)
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def long_label(now: datetime, tz: str) -> str:
    """'Thursday, 3 September 2026' (en-GB full date style)."""
    d = now.astimezone(ZoneInfo(tz)).date()
    return f"{_LONG_WEEKDAYS[d.weekday()]}, {d.day} {_LONG_MONTHS[d.month - 1]} {d.year}"


def add_days(iso: str, n: int) -> str:
    return (_to_date(iso) + timedelta(days=n)).isoformat()


_WD_INDEX = {n.lower(): i for i, n in enumerate(_LONG_WEEKDAYS)} | {n.lower(): i for i, n in enumerate(_WEEKDAYS)}
_NUM = re.compile(r"in\s+(\d+)\s+(day|week|month)s?")


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def resolve_relative(phrase: str, today: str) -> tuple[str, str] | None:
    """Turn 'tomorrow', 'next week', 'next month', 'in 10 days', 'next friday'... into (ISO date, how it was chosen)."""
    p, t = phrase.lower().strip(), _to_date(today)
    if p in ("today", "now"):
        return today, "today"
    if p == "tomorrow":
        return add_days(today, 1), "tomorrow"
    if p == "next week":
        return (t + timedelta(days=7 - t.weekday())).isoformat(), "Monday of next week"
    if p == "next month":
        y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
        return _first_monday(y, m).isoformat(), "first Monday of next month (a typical business-trip start)"
    if p in ("end of month", "end of the month"):
        y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
        return (date(y, m, 1) - timedelta(days=1)).isoformat(), "last day of this month"
    m = _NUM.fullmatch(p)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "month":
            idx = t.year * 12 + t.month - 1 + n
            y, mo = divmod(idx, 12)
            last = (date(y + (mo + 1) // 12, (mo + 1) % 12 + 1, 1) - timedelta(days=1)).day
            return date(y, mo + 1, min(t.day, last)).isoformat(), f"{n} month(s) from today"
        return add_days(today, n * (7 if unit == "week" else 1)), f"{n} {unit}(s) from today"
    m2 = re.fullmatch(r"(next|this)?\s*([a-z]+)", p)
    if m2 and m2.group(2) in _WD_INDEX:
        ahead = (_WD_INDEX[m2.group(2)] - t.weekday()) % 7
        if ahead == 0:
            ahead = 7
        return add_days(today, ahead), "next occurrence of that weekday"
    return None
