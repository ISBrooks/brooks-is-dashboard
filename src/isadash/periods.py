"""Reporting periods. Weeks are ALWAYS Sunday–Saturday, in Pacific time
(Brooks HQ). Months are calendar months, Pacific."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
# The IS team's own clock: shift times ("9-5"), punches, auto clock-out,
# timesheets, the schedule builder and the Saturday tracker run on Mountain
# time (the team works in Utah). Sales / reporting days above stay Pacific.
CLOCK_TZ = ZoneInfo("America/Denver")
UTC = ZoneInfo("UTC")


def to_pacific(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(PACIFIC)


def week_start(d: datetime) -> datetime:
    """Sunday 00:00 Pacific of the week containing d (d is aware)."""
    local = d.astimezone(PACIFIC)
    days_since_sunday = (local.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun->0
    sunday = (local - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return sunday


def week_label(d: datetime) -> str:
    ws = week_start(d)
    we = ws + timedelta(days=6)
    return f"{ws:%Y-%m-%d}..{we:%Y-%m-%d}"


def month_label(d: datetime) -> str:
    return f"{d.astimezone(PACIFIC):%Y-%m}"


def current_week_start_utc(now_utc: datetime) -> datetime:
    return week_start(now_utc).astimezone(UTC)


def trailing_12mo_start_utc(now_utc: datetime) -> datetime:
    """Sunday 00:00 Pacific on/before the date 12 months back — so the
    backfill starts on a clean week boundary."""
    local = now_utc.astimezone(PACIFIC)
    year, month = local.year - 1, local.month
    approx = local.replace(year=year, month=month, day=1,
                           hour=0, minute=0, second=0, microsecond=0)
    return week_start(approx).astimezone(UTC)
