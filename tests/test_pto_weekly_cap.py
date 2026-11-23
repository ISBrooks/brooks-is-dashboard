"""PTO is earned on at most 40 worked hours a week.

Dallin, 2026-11-23: overtime hours never earn PTO. Only the first 40 hours
worked in a Sunday–Saturday week (the same week overtime uses) count. The rule
starts with the week of Sunday 2026-11-22; earlier days keep what they earned.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from isadash import hours, pto

AGENT = "Avery Stone"


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(hours.SCHEMA)
    c.execute("CREATE TABLE IF NOT EXISTS agent_hr (agent TEXT PRIMARY KEY, start_date TEXT, employment_type TEXT, "
              "weekly_hours REAL, work_state TEXT, notes TEXT, updated_by TEXT, updated_at TEXT)")
    # well past probation, under two years of service (2 h per 80), full time
    c.execute("INSERT INTO agent_hr (agent, start_date, employment_type) VALUES (?, '2025-06-01', 'full_time')", (AGENT,))
    pto.ensure(c)
    monkeypatch.setattr(hours, "sync_clock", lambda *a, **k: 0)     # the rows below stand in for the punches
    return c


def _work(conn, days: dict[str, float]) -> None:
    for d, h in days.items():
        conn.execute("INSERT INTO agent_hours (day, agent, hours, source, loaded_at) VALUES (?,?,?,'clock','t')", (d, AGENT, h))


def _earned(conn) -> dict[str, float]:
    return {r["day"]: round(r["hours"], 4) for r in
            conn.execute("SELECT day, hours FROM pto_ledger WHERE agent = ? AND kind = 'accrual' ORDER BY day", (AGENT,))}


def test_only_the_first_40_hours_of_a_week_earn(conn):
    # Sun Nov 29 – Sat Dec 5: 5 x 10 h = 50 h; only 40 earn (2/80 -> 1.0 h)
    _work(conn, {"2026-11-30": 10, "2026-12-01": 10, "2026-12-02": 10, "2026-12-03": 10, "2026-12-04": 10})
    pto.accrue(conn, [AGENT], through=date(2026, 12, 5))
    got = _earned(conn)
    assert got == {"2026-11-30": 0.25, "2026-12-01": 0.25, "2026-12-02": 0.25, "2026-12-03": 0.25}   # Friday is all overtime
    assert round(sum(got.values()), 4) == 1.0                                                       # 40 h x 2/80


def test_the_day_that_crosses_40_earns_on_its_part_inside_40(conn):
    _work(conn, {"2026-11-30": 12, "2026-12-01": 12, "2026-12-02": 12, "2026-12-03": 12})          # 48 h
    pto.accrue(conn, [AGENT], through=date(2026, 12, 5))
    got = _earned(conn)
    assert got["2026-12-03"] == round(4 * 2 / 80, 4)                                               # 4 of its 12 h
    assert round(sum(got.values()), 4) == 1.0


def test_a_new_week_starts_on_sunday(conn):
    _work(conn, {"2026-12-03": 14, "2026-12-04": 14, "2026-12-05": 14,                            # Thu–Sat: 42 h
                 "2026-12-06": 10})                                                                # Sunday: new week
    pto.accrue(conn, [AGENT], through=date(2026, 12, 7))
    got = _earned(conn)
    assert got["2026-12-05"] == round(12 * 2 / 80, 4)
    assert got["2026-12-06"] == round(10 * 2 / 80, 4)


def test_weeks_before_the_rule_are_not_refigured(conn):
    # Sun Nov 15 – Fri Nov 20: 50 h, before the cap started — every hour still earns
    _work(conn, {"2026-11-16": 10, "2026-11-17": 10, "2026-11-18": 10, "2026-11-19": 10, "2026-11-20": 10})
    pto.accrue(conn, [AGENT], through=date(2026, 11, 22))
    assert round(sum(_earned(conn).values()), 4) == round(50 * 2 / 80, 4)


# --- the rate: 1 per 40 for under two years of service, then 1.5 per 40 ---

def test_step_up_is_at_two_years():
    assert pto.rate_for(date(2025, 6, 1), date(2026, 12, 1)) == 1 / 40     # 1.5 years in
    assert pto.rate_for(date(2024, 6, 1), date(2026, 12, 1)) == 1.5 / 40   # 2.5 years in
