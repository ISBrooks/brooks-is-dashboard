"""Overtime is weekly only.

Hours past 40 in a Sunday–Saturday week are overtime. A long day on its own is
not: daily overtime (hours over 8 in a day) was dropped on 2026-11-23, and
nothing the time clock hands out — the timesheet, the hours report or either
CSV — may carry it again.
"""
from __future__ import annotations

from isadash import timeclock

SUNDAY = "2026-11-08"          # a Sunday; the week runs through Saturday 2026-11-14


def _agent(hours_by_day: dict[str, float]) -> dict:
    days = [{"date": d, "shift": None, "scheduled_h": 0.0, "worked_h": h, "punches": [], "flags": []}
            for d, h in hours_by_day.items()]
    ag = {"agent": "Avery Stone", "scheduled_h": 0.0, "worked_h": sum(hours_by_day.values()), "days": days, "flags": 0}
    timeclock._overtime(ag)
    return ag


def test_a_long_day_in_a_short_week_is_not_overtime():
    ag = _agent({"2026-11-09": 12.0, "2026-11-10": 10.0, "2026-11-11": 8.0})        # 30 hours
    assert ag["weekly_ot_h"] == 0 and ag["weeks_over_40"] == 0
    assert all(d["weekly_ot_h"] == 0 and d["flags"] == [] for d in ag["days"])


def test_only_hours_past_40_in_the_week_are_overtime():
    ag = _agent({"2026-11-09": 10.0, "2026-11-10": 10.0, "2026-11-11": 10.0, "2026-11-12": 10.0, "2026-11-13": 6.5})
    assert ag["weekly_ot_h"] == 6.5 and ag["weeks_over_40"] == 1
    assert [d["weekly_ot_h"] for d in ag["days"]] == [0, 0, 0, 0, 6.5]


def test_the_week_starts_over_on_sunday():
    ag = _agent({"2026-11-12": 12.0, "2026-11-13": 12.0, "2026-11-14": 12.0,         # Thu–Sat: 36 hours
                 "2026-11-15": 12.0})                                                # Sunday: a new week
    assert ag["weekly_ot_h"] == 0 and ag["weeks_over_40"] == 0


def test_nothing_reports_daily_overtime():
    ag = _agent({SUNDAY: 14.0, "2026-11-09": 14.0, "2026-11-10": 14.0})              # 42 hours, every day over 8
    assert ag["weekly_ot_h"] == 2.0
    carried = set(ag) | {k for d in ag["days"] for k in d}
    assert not {k for k in carried if "daily" in k or "over_8" in k}
    sheet = timeclock.timesheet_csv({"agents": [ag]})
    report = timeclock.hours_report_csv({
        "agents": [{"agent": "Avery Stone", "regular_h": 40.0, "weekly_ot_h": 2.0, "pto_h": 0.0, "worked_h": 42.0}],
        "totals": {"regular_h": 40.0, "weekly_ot_h": 2.0, "pto_h": 0.0, "worked_h": 42.0}})
    assert report.splitlines()[0] == "Agent,Regular Hours,Weekly OT,PTO"
    assert report.splitlines()[1] == "Avery Stone,40.00,2.00,0.00"
    for text in (sheet, report):
        assert "daily" not in text.lower() and "over 8" not in text.lower()
