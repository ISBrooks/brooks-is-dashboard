"""Saturday rotation tracker (IS Managers -> Schedule, below the week grid).

Replaces the "SATURDAYS" tab of the IS Scheduling sheet. A few agents work
every Saturday ("regulars"); 2-4 weekday agents are pulled in each week, and
managers pick whoever has gone longest without one. Nothing is auto-assigned.

Where a Saturday comes from:
  * schedule  — a Saturday shift in a PUBLISHED builder week (derived live, so
                editing or unpublishing the week changes the tracker too);
                approved time off on that Saturday cancels it
  * sheet     — history imported once from the old sheet (scripts/import_saturdays.py)
  * manual    — a manager's correction on the tracker: worked = 1 adds a
                Saturday, worked = 0 removes one the schedule or import shows

Table:
  saturday_log  (agent, day) -> worked 0/1, source sheet|manual
Regulars live in app_config 'saturday_regulars' (a JSON list of names).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import shiftplan
from . import timeoff
from .periods import CLOCK_TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS saturday_log (
    agent      TEXT NOT NULL,
    day        TEXT NOT NULL,          -- the Saturday (YYYY-MM-DD)
    worked     INTEGER NOT NULL,       -- 1 = worked, 0 = did not (overrides the schedule)
    source     TEXT NOT NULL,          -- sheet | manual
    note       TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent, day)
);
"""
COLUMNS_BACK = 12          # Saturdays shown on the grid (past, including today if it's Saturday)
WINDOW_WEEKS = 12          # the "in the last N Saturdays" count


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(CLOCK_TZ).date()


def last_saturday(d: date) -> date:
    """The Saturday on or before d."""
    return d - timedelta(days=(d.weekday() - 5) % 7)


def regulars(conn) -> list[str]:
    row = conn.execute("SELECT value FROM app_config WHERE key = 'saturday_regulars'").fetchone()
    try:
        return sorted(json.loads(row["value"])) if row else []
    except ValueError:
        return []


def set_regular(conn, agent: str, on: bool) -> dict:
    cur = set(regulars(conn))
    (cur.add if on else cur.discard)(agent)
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('saturday_regulars', ?)", (json.dumps(sorted(cur)),))
    return {"ok": True}


def scheduled(conn) -> dict[tuple[str, str], str]:
    """{(agent, saturday_iso): cell} from every published builder week, minus approved time off."""
    shiftplan.ensure(conn)
    sats = [(date.fromisoformat(r["monday"]) + timedelta(days=5)).isoformat()
            for r in conn.execute("SELECT monday FROM schedule_weeks WHERE published = 1")]
    if not sats:
        return {}
    out = {}
    q = ",".join("?" * len(sats))
    for r in conn.execute(f"SELECT * FROM shifts WHERE day IN ({q})", sats):
        out[(r["agent"], r["day"])] = shiftplan.fmt_cell(r["start_min"], r["end_min"], r["start2_min"], r["end2_min"])
    if out:
        lo, hi = min(sats), max(sats)
        off = timeoff.approved_days(conn, date.fromisoformat(lo), date.fromisoformat(hi))
        out = {k: v for k, v in out.items() if not off.get(k[0], {}).get(k[1])}
    return out


def _marks(conn) -> dict[str, dict[str, dict]]:
    """{agent: {saturday_iso: {"worked": bool, "source": str, "cell": str|None}}} — everything combined."""
    ensure(conn)
    out: dict[str, dict[str, dict]] = {}
    for (agent, day), cell in scheduled(conn).items():
        out.setdefault(agent, {})[day] = {"worked": True, "source": "schedule", "cell": cell}
    for r in conn.execute("SELECT * FROM saturday_log"):
        cur = out.setdefault(r["agent"], {}).get(r["day"])
        if r["source"] == "manual" or not cur:      # a manual mark always wins; the import fills gaps
            out[r["agent"]][r["day"]] = {"worked": bool(r["worked"]), "source": r["source"],
                                         "cell": cur["cell"] if cur else None, "scheduled": bool(cur)}
    return out


def tracker(conn, agents: list[str]) -> dict:
    today = _today()
    this_sat = last_saturday(today)                  # most recent Saturday on or before today
    cols = [this_sat - timedelta(days=7 * k) for k in range(COLUMNS_BACK - 1, -1, -1)]
    marks = _marks(conn)
    # upcoming Saturdays that are already in a published week
    future = sorted({d for m in marks.values() for d, v in m.items() if v["worked"] and v["source"] == "schedule"
                     and date.fromisoformat(d) > today})
    cols_iso = [d.isoformat() for d in cols] + future
    regs = set(regulars(conn))
    window_lo = this_sat - timedelta(days=7 * (WINDOW_WEEKS - 1))
    rows = []
    for a in sorted(set(agents)):
        m = marks.get(a, {})
        worked = sorted(d for d, v in m.items() if v["worked"] and date.fromisoformat(d) <= today)
        nxt = sorted(d for d, v in m.items() if v["worked"] and date.fromisoformat(d) > today)
        last = worked[-1] if worked else None
        rows.append({
            "agent": a, "regular": a in regs,
            "last": last,
            "weeks_since": ((this_sat - date.fromisoformat(last)).days // 7) if last else None,
            "next": nxt[0] if nxt else None,
            "recent": sum(1 for d in worked if date.fromisoformat(d) >= window_lo),
            "total": len(worked),
            "cells": [m.get(d) for d in cols_iso],
        })
    # longest since a Saturday first (never = longest), then fewest recent, then name
    rows.sort(key=lambda r: (r["regular"], r["next"] is not None, r["last"] or "", r["recent"], r["agent"]))
    first = conn.execute("SELECT MIN(day) AS d FROM saturday_log WHERE source = 'sheet'").fetchone()["d"]
    return {"today": today.isoformat(), "columns": cols_iso, "future_from": len(cols),
            "rows": rows, "window_weeks": WINDOW_WEEKS, "history_from": first}


def mark(conn, who: str, agent: str, day: date, worked: bool | None) -> dict:
    """A manager's correction. worked=None removes the manual mark (back to what
    the schedule / import says)."""
    ensure(conn)
    if day.weekday() != 5:
        return {"error": "That date isn't a Saturday."}
    if day > _today():
        return {"error": "Upcoming Saturdays come from the published schedule — change the week instead."}
    with conn:
        if worked is None:
            conn.execute("DELETE FROM saturday_log WHERE agent = ? AND day = ? AND source = 'manual'", (agent, day.isoformat()))
        else:
            conn.execute("INSERT OR REPLACE INTO saturday_log (agent, day, worked, source, created_by, created_at) "
                         "VALUES (?,?,?,?,?,?)", (agent, day.isoformat(), 1 if worked else 0, "manual", who, _now()))
    return {"ok": True}


def import_history(conn, who: str, entries: list[tuple[str, str]]) -> int:
    """Load (agent, saturday_iso) pairs from the old sheet. Never overwrites a
    manual mark; re-running is harmless."""
    ensure(conn)
    n = 0
    with conn:
        for agent, day in entries:
            cur = conn.execute("INSERT OR IGNORE INTO saturday_log (agent, day, worked, source, created_by, created_at) "
                               "VALUES (?,?,1,'sheet',?,?)", (agent, day, who, _now()))
            n += cur.rowcount
    return n
