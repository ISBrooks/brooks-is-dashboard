"""The schedule builder's week: what counts as "on".

A shift saved before time off was approved stays in the shifts table under the
PTO / Unpaid label. These check that such a shift is not counted as coverage or
as scheduled hours, and that it comes back when the approval is revoked.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from isadash import shiftplan, timeoff

MONDAY = date(2026, 11, 9)
SUNDAY = date(2026, 11, 15)
AGENTS = ["Avery Stone", "Blake Rivers"]


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    shiftplan.ensure(c)
    timeoff.ensure(c)
    yield c
    c.close()


def _week(conn):
    return shiftplan.week(conn, MONDAY, AGENTS, timeoff.approved_days(conn, MONDAY, SUNDAY))


def _on_at(week, hour, dow):
    return next(r for r in week["coverage"] if r["hour"] == hour)["counts"][dow]


def _time_off(conn, agent, day, kind):
    with conn:
        return conn.execute(
            "INSERT INTO time_off_requests (agent, start_day, end_day, kind, hours, status, requested_at) VALUES (?,?,?,?,?,?,?)",
            (agent, day.isoformat(), day.isoformat(), kind, 8, "approved", "2026-11-01T15:00:00+00:00")).lastrowid


def test_week_says_who_changed_what_and_when(conn):
    assert shiftplan.set_shift(conn, "pat@example.test", "Avery Stone", MONDAY, "9-5").get("ok")
    draft = _week(conn)
    avery = next(r for r in draft["agents"] if r["agent"] == "Avery Stone")
    blake = next(r for r in draft["agents"] if r["agent"] == "Blake Rivers")
    assert avery["changed"][0]["by"] == "pat@example.test" and avery["changed"][0]["at"]
    assert avery["changed"][1:] == [None] * 6 and blake["changed"] == [None] * 7
    assert draft["created_by"] == "pat@example.test" and draft["created_at"] and draft["published_by"] is None

    assert shiftplan.set_shift(conn, "sam@example.test", "Avery Stone", MONDAY, "10-6").get("ok")   # the latest change wins
    assert shiftplan.publish(conn, "sam@example.test", MONDAY, True).get("ok")
    live = _week(conn)
    assert next(r for r in live["agents"] if r["agent"] == "Avery Stone")["changed"][0]["by"] == "sam@example.test"
    assert live["published_by"] == "sam@example.test" and live["published_at"]
    assert live["created_by"] == "pat@example.test"                 # publishing doesn't rewrite who created it


@pytest.mark.parametrize("kind", ["pto", "unpaid", "mixed"])
def test_time_off_is_not_coverage(conn, kind):
    for a in AGENTS:
        assert shiftplan.set_shift(conn, "tester", a, SUNDAY, "9-5").get("ok")
        assert shiftplan.set_shift(conn, "tester", a, MONDAY, "9-5").get("ok")
    before = _week(conn)
    assert _on_at(before, 9, 6) == 2 and _on_at(before, 16, 6) == 2

    req = _time_off(conn, "Avery Stone", SUNDAY, kind)            # approved after the shift was already saved
    after = _week(conn)
    avery = next(r for r in after["agents"] if r["agent"] == "Avery Stone")
    blake = next(r for r in after["agents"] if r["agent"] == "Blake Rivers")
    assert avery["off"][6] == ("PTO" if kind == "pto" else "Unpaid")
    for hour in range(9, 17):
        assert _on_at(after, hour, 6) == 1                        # only Blake is on
        assert _on_at(after, hour, 0) == 2                        # Monday is untouched
    assert avery["hours"] == 8 and blake["hours"] == 16           # the day off isn't scheduled hours
    assert avery["starts"][6] is None and avery["lengths"][6] == 0

    assert timeoff.revoke(conn, "tester", req, "").get("ok")      # the shift underneath comes back
    back = _week(conn)
    assert _on_at(back, 9, 6) == 2
    assert next(r for r in back["agents"] if r["agent"] == "Avery Stone")["hours"] == 16
