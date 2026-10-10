"""Resolve spoken dates ("by Friday", "October 15", "Q1") relative to the call date."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MONTHS: dict[str, int] = {}
for _i in range(1, 13):
    MONTHS[calendar.month_name[_i].lower()] = _i
    MONTHS[calendar.month_abbr[_i].lower()] = _i
MONTHS["sept"] = 9

WEEKDAYS = {calendar.day_name[i].lower(): i for i in range(7)}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(WEEKDAYS)

_MONTH_DAY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_WEEKDAY = re.compile(rf"\b(?:(next|this)\s+)?({_WEEKDAY_ALT})\b", re.I)
_RELATIVE = re.compile(r"\b(today|tomorrow|end of (?:the )?week|this week|end of (?:the )?month)\b", re.I)

_QUARTER = re.compile(r"\b(?:Q([1-4])|end of (?:the )?quarter)(?:\s+(20\d\d))?\b", re.I)
_MONTH_ONLY = re.compile(rf"\b(?:end of\s+)?({_MONTH_ALT})\b(?:\s+(20\d\d))?(?!\s+\d)", re.I)


def _next_weekday(ref: date, weekday: int) -> date:
    delta = (weekday - ref.weekday()) % 7
    return ref + timedelta(days=delta or 7)


def _end_of_month(ref: date) -> date:
    return ref.replace(day=calendar.monthrange(ref.year, ref.month)[1])


def find_date(text: str, ref: date) -> tuple[date, str] | None:
    """Return the first concrete calendar date in ``text`` and the phrase it came from.

    Weekdays resolve to the next occurrence strictly after the call date, which is
    how reps use "Friday" on a call. "next Friday" resolves the same way: the
    ambiguity is real and is recorded in the fact's reasons by the caller.
    """
    candidates: list[tuple[int, date, str]] = []
    for m in _MONTH_DAY.finditer(text):
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
        year = ref.year
        try:
            resolved = date(year, month, day)
            if resolved < ref - timedelta(days=30):
                resolved = date(year + 1, month, day)
        except ValueError:
            continue
        candidates.append((m.start(), resolved, m.group(0)))
    for m in _WEEKDAY.finditer(text):
        candidates.append((m.start(), _next_weekday(ref, WEEKDAYS[m.group(2).lower()]), m.group(0)))
    for m in _RELATIVE.finditer(text):
        phrase = m.group(1).lower()
        if phrase == "today":
            resolved = ref
        elif phrase == "tomorrow":
            resolved = ref + timedelta(days=1)
        elif "week" in phrase:
            resolved = ref + timedelta(days=(4 - ref.weekday()) % 7)  # that week's Friday
        else:
            resolved = _end_of_month(ref)
        candidates.append((m.start(), resolved, m.group(0)))
    if not candidates:
        return None
    _, resolved, phrase = min(candidates, key=lambda c: c[0])
    return resolved, phrase


def find_period(text: str, ref: date) -> tuple[str, str] | None:
    """Return a coarse target period ("2027-Q1", "2027-01") for timelines.

    Periods resolve to the next occurrence at or after the call date's period.
    """
    q = _QUARTER.search(text)
    if q:
        current_q = (ref.month - 1) // 3 + 1
        if q.group(1):
            quarter = int(q.group(1))
            year = int(q.group(2)) if q.group(2) else ref.year + (1 if quarter < current_q else 0)
        else:
            quarter, year = current_q, ref.year
        return f"{year}-Q{quarter}", q.group(0)
    m = _MONTH_ONLY.search(text)
    if m and m.group(1).lower() not in ("may", "mar"):  # "may" and "mar" are too often verbs
        month = MONTHS[m.group(1).lower()]
        year = int(m.group(2)) if m.group(2) else ref.year + (1 if month < ref.month else 0)
        return f"{year}-{month:02d}", m.group(0)
    return None
