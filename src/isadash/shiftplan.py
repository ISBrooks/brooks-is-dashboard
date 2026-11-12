"""The schedule builder (IS Managers -> Schedule): the sheet's replacement.

Weeks run Monday–Sunday. A shift is one or two segments in minutes after
midnight (Mountain). Cells are rendered in the sheet's own notation ("9-5",
"9-4:30", "8-11, 1:30-7") so the existing parsers (schedule.shift_hours /
shift_segments) read them unchanged, and the agent page and the time clock
use a PUBLISHED week from here before they fall back to the sheet.

Tables:
  shifts          one row per agent per day with a shift (no row = not scheduled)
  schedule_weeks  one row per week that has been created here; published flag
  shift_templates named week layouts (agent, weekday -> shift) to build from
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import schedule as sched

SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
    agent      TEXT NOT NULL,
    day        TEXT NOT NULL,
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL,
    start2_min INTEGER,
    end2_min   INTEGER,
    note       TEXT,
    updated_by TEXT,
    updated_at TEXT,
    PRIMARY KEY (agent, day)
);
CREATE TABLE IF NOT EXISTS schedule_weeks (
    monday       TEXT PRIMARY KEY,
    published    INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    published_by TEXT,
    created_from TEXT,
    created_by   TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS shift_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    rows       TEXT NOT NULL,          -- JSON [{agent, dow, start, end, start2, end2}]
    created_by TEXT,
    created_at TEXT
);
"""
PRESETS = ["8-3", "8-4", "9-3", "9-4", "9-4:30", "9-5", "9-6", "10-5", "10-6", "11-7", "12-8",
           "1-6", "3-7", "3-8", "4-8", "5-8"]
COVER_FROM, COVER_TO = 7, 21            # coverage strip: 7:00 .. 20:00 hour rows


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shift_templates)")}
    for c in ("updated_by", "updated_at"):            # added with the Templates screen
        if c not in cols:
            conn.execute(f"ALTER TABLE shift_templates ADD COLUMN {c} TEXT")
            conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --- cell text <-> minutes ----------------------------------------------------
def fmt_time(m: int) -> str:
    h, mm = divmod(m, 60)
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d}" if mm else f"{h12}"


def fmt_cell(start: int, end: int, start2: int | None = None, end2: int | None = None) -> str:
    s = f"{fmt_time(start)}-{fmt_time(end)}"
    if start2 is not None and end2 is not None:
        s += f", {fmt_time(start2)}-{fmt_time(end2)}"
    return s


def parse_cell(cell: str) -> tuple | None:
    """'9-5' -> (540, 1020, None, None); '8-11, 1:30-7' -> (480, 660, 810, 1140); OFF -> None"""
    segs = sched.shift_segments(cell)
    if not segs:
        return None
    (a, b) = segs[0]
    (c, d) = segs[1] if len(segs) > 1 else (None, None)
    return a, b, c, d


# --- reading ------------------------------------------------------------------
def week_rows(conn, monday: date) -> dict[str, list]:
    """{agent: [cell text or '' for Mon..Sun]} for one week."""
    ensure(conn)
    sunday = monday + timedelta(days=6)
    out: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM shifts WHERE day BETWEEN ? AND ?", (monday.isoformat(), sunday.isoformat())):
        k = (date.fromisoformat(r["day"]) - monday).days
        out.setdefault(r["agent"], [""] * 7)[k] = fmt_cell(r["start_min"], r["end_min"], r["start2_min"], r["end2_min"])
    return out


def shift_stamps(conn, monday: date) -> dict[str, list]:
    """{agent: [{"by", "at"} or None for Mon..Sun]}: who last set each shift in
    the week and when (UTC). Only shifts that exist have one — a removed shift
    leaves nothing behind, and there is no history of earlier values."""
    sunday = monday + timedelta(days=6)
    out: dict[str, list] = {}
    for r in conn.execute("SELECT agent, day, updated_by, updated_at FROM shifts WHERE day BETWEEN ? AND ?",
                          (monday.isoformat(), sunday.isoformat())):
        if r["updated_at"]:
            k = (date.fromisoformat(r["day"]) - monday).days
            out.setdefault(r["agent"], [None] * 7)[k] = {"by": r["updated_by"], "at": r["updated_at"]}
    return out


def published_weeks(conn) -> dict[str, dict[str, list] | None]:
    """{monday_iso: {agent: [7 cells]}} for every PUBLISHED week, and
    {monday_iso: None} for every DRAFT week. The agent page, the day roster and
    the time clock take published weeks over the sheet; a draft week hides that
    week entirely — agents never see a week until it is published (Dallin,
    2026-10-21), not even the sheet's old copy of it."""
    ensure(conn)
    out: dict[str, dict[str, list] | None] = {}
    for r in conn.execute("SELECT monday, published FROM schedule_weeks"):
        out[r["monday"]] = week_rows(conn, date.fromisoformat(r["monday"])) if r["published"] else None
    return out


def coverage(rows: dict[str, list]) -> list[dict]:
    """Headcount per hour per day (half hours count .5), like the sheet's matrix."""
    out = []
    for h in range(COVER_FROM, COVER_TO):
        counts = []
        for k in range(7):
            n = 0.0
            for cells in rows.values():
                for a, b in sched.shift_segments(cells[k] if k < len(cells) else ""):
                    lo, hi = max(a, h * 60), min(b, (h + 1) * 60)
                    if hi > lo:
                        n += (hi - lo) / 60
            counts.append(round(n, 1))
        out.append({"hour": h, "label": fmt_time(h * 60), "counts": counts})
    return out


DEFAULT_TARGETS = {"yellow": 8, "green": 12}      # below yellow = red


def coverage_targets(conn) -> dict:
    """{hour(str): {"yellow": n, "green": n}} for 7..20 (a global default plus per-hour overrides)."""
    row = conn.execute("SELECT value FROM app_config WHERE key = 'coverage_targets'").fetchone()
    saved = json.loads(row["value"]) if row else {}
    out = {}
    for h in range(COVER_FROM, COVER_TO):
        t = saved.get(str(h)) or saved.get("default") or DEFAULT_TARGETS
        out[str(h)] = {"yellow": float(t.get("yellow", DEFAULT_TARGETS["yellow"])), "green": float(t.get("green", DEFAULT_TARGETS["green"]))}
    return out


def set_coverage_targets(conn, targets: dict) -> dict:
    clean = {}
    for h, t in (targets or {}).items():
        try:
            y, g = float(t["yellow"]), float(t["green"])
        except (KeyError, TypeError, ValueError):
            return {"error": f"Bad numbers for {h}:00."}
        if g < y or y < 0:
            return {"error": f"{h}:00 — green must be at least yellow, and both at least 0."}
        clean[str(h)] = {"yellow": y, "green": g}
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('coverage_targets', ?)", (json.dumps(clean),))
    return {"ok": True}


def week(conn, monday: date, agents: list[str], time_off: dict | None = None) -> dict:
    ensure(conn)
    meta = conn.execute("SELECT * FROM schedule_weeks WHERE monday = ?", (monday.isoformat(),)).fetchone()
    rows = week_rows(conn, monday)
    names = sorted(set(agents) | set(rows))
    stamps = shift_stamps(conn, monday)
    grid = []
    working: dict[str, list] = {}       # the week as it will actually be worked: time-off days blanked
    for a in names:
        cells = list(rows.get(a, [""] * 7))
        off = []
        for k in range(7):
            kind = (time_off or {}).get(a, {}).get((monday + timedelta(days=k)).isoformat())
            off.append("PTO" if kind == "pto" else "Unpaid" if kind else "")
        # A shift saved before the time off was approved stays in the table (it
        # splits the PTO hours by day, and comes back if the approval is revoked),
        # but that person isn't working it: it must not count toward coverage,
        # the Hrs total or the day's sort order.
        on = ["" if off[k] else c for k, c in enumerate(cells)]
        working[a] = on
        segs = [sched.shift_segments(c) for c in on]
        grid.append({"agent": a, "cells": cells, "off": off, "changed": stamps.get(a, [None] * 7),
                     "starts": [s[0][0] if s else None for s in segs],
                     "lengths": [sum(b - x for x, b in s) for s in segs],
                     "hours": round(sum((sched.shift_hours(c) or 0) for c in on), 2)})
    prev = week_rows(conn, monday - timedelta(days=7))
    return {"monday": monday.isoformat(), "sunday": (monday + timedelta(days=6)).isoformat(),
            "exists": bool(meta), "published": bool(meta and meta["published"]),
            "published_at": meta["published_at"] if meta else None, "published_by": meta["published_by"] if meta else None,
            "created_from": meta["created_from"] if meta else None,
            "created_by": meta["created_by"] if meta else None, "created_at": meta["created_at"] if meta else None,
            "agents": grid, "coverage": coverage(working), "presets": PRESETS, "targets": coverage_targets(conn),
            "prev_week_exists": bool(prev),
            "templates": [{"id": r["id"], "name": r["name"]} for r in conn.execute("SELECT id, name FROM shift_templates ORDER BY name")]}


# --- writing ------------------------------------------------------------------
def _touch_week(conn, monday: date, who: str, created_from: str | None = None) -> None:
    conn.execute("INSERT INTO schedule_weeks (monday, published, created_from, created_by, created_at) VALUES (?,0,?,?,?) "
                 "ON CONFLICT(monday) DO NOTHING", (monday.isoformat(), created_from, who, _now()))


def _off_kind(time_off: dict | None, agent: str, day: date) -> str | None:
    return (time_off or {}).get(agent, {}).get(day.isoformat())


def set_shift(conn, who: str, agent: str, day: date, cell: str | None, time_off: dict | None = None) -> dict:
    """cell = '9-5' style text (or minutes via fmt_cell upstream); empty/OFF clears."""
    ensure(conn)
    parsed = parse_cell(cell or "")
    if parsed and _off_kind(time_off, agent, day):
        return {"error": f"{agent} has approved time off on {day:%b} {day.day} — that day can't be scheduled."}
    if cell and cell.strip() and cell.strip().upper() not in ("OFF", "-") and not parsed:
        return {"error": f"Couldn't read “{cell}” as a shift. Use the sheet's notation, e.g. 9-5 or 8-11, 1:30-7."}
    with conn:
        _touch_week(conn, monday_of(day), who)
        if not parsed:
            conn.execute("DELETE FROM shifts WHERE agent = ? AND day = ?", (agent, day.isoformat()))
        else:
            a, b, c, d = parsed
            conn.execute("INSERT INTO shifts (agent, day, start_min, end_min, start2_min, end2_min, updated_by, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent, day) DO UPDATE SET start_min=excluded.start_min, "
                         "end_min=excluded.end_min, start2_min=excluded.start2_min, end2_min=excluded.end2_min, "
                         "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (agent, day.isoformat(), a, b, c, d, who, _now()))
    return {"ok": True, "cell": fmt_cell(*parsed) if parsed else ""}


def move_shift(conn, who: str, agent: str, day: date, to_agent: str, to_day: date, copy: bool = False,
               time_off: dict | None = None) -> dict:
    ensure(conn)
    r = conn.execute("SELECT * FROM shifts WHERE agent = ? AND day = ?", (agent, day.isoformat())).fetchone()
    if not r:
        return {"error": "Nothing to move."}
    cell = fmt_cell(r["start_min"], r["end_min"], r["start2_min"], r["end2_min"])
    res = set_shift(conn, who, to_agent, to_day, cell, time_off)
    if res.get("ok") and not copy:
        with conn:
            conn.execute("DELETE FROM shifts WHERE agent = ? AND day = ?", (agent, day.isoformat()))
    return res


def create_week(conn, who: str, monday: date, source: str, template_id: int | None, agents: list[str],
                sheet_rows: dict[str, list] | None = None, time_off: dict | None = None) -> dict:
    """source: blank | last (copy the previous week) | template | sheet (the
    Website Schedule tab's block for this week, names matched to the roster)."""
    ensure(conn)
    if conn.execute("SELECT 1 FROM schedule_weeks WHERE monday = ?", (monday.isoformat(),)).fetchone():
        return {"error": "That week already exists. Delete it first if you want to rebuild it."}
    rows: dict[str, list] = {}
    if source == "last":
        rows = week_rows(conn, monday - timedelta(days=7))
    elif source == "template":
        t = conn.execute("SELECT rows FROM shift_templates WHERE id = ?", (template_id,)).fetchone()
        if not t:
            return {"error": "No such template."}
        for x in json.loads(t["rows"]):
            rows.setdefault(x["agent"], [""] * 7)[int(x["dow"])] = fmt_cell(x["start"], x["end"], x.get("start2"), x.get("end2"))
    elif source == "sheet":
        for short, cells in (sheet_rows or {}).items():
            full = sched.match_name(short, agents)
            if full:
                rows[full] = cells
    n = skipped = 0
    with conn:
        _touch_week(conn, monday, who, source)
        for agent, cells in rows.items():
            for k, cell in enumerate(cells):
                p = parse_cell(cell)
                if not p:
                    continue
                d = monday + timedelta(days=k)
                if _off_kind(time_off, agent, d):          # approved time off wins over the source
                    skipped += 1
                    continue
                conn.execute("INSERT OR REPLACE INTO shifts (agent, day, start_min, end_min, start2_min, end2_min, updated_by, updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", (agent, d.isoformat(), *p, who, _now()))
                n += 1
    return {"ok": True, "shifts": n, "skipped_time_off": skipped}


def delete_week(conn, monday: date) -> dict:
    ensure(conn)
    with conn:
        conn.execute("DELETE FROM shifts WHERE day BETWEEN ? AND ?", (monday.isoformat(), (monday + timedelta(days=6)).isoformat()))
        conn.execute("DELETE FROM schedule_weeks WHERE monday = ?", (monday.isoformat(),))
    return {"ok": True}


def publish(conn, who: str, monday: date, on: bool) -> dict:
    ensure(conn)
    with conn:
        _touch_week(conn, monday, who)
        conn.execute("UPDATE schedule_weeks SET published = ?, published_at = ?, published_by = ? WHERE monday = ?",
                     (1 if on else 0, _now() if on else None, who if on else None, monday.isoformat()))
    return {"ok": True, "published": on}


def save_template(conn, who: str, name: str, monday: date) -> dict:
    ensure(conn)
    name = (name or "").strip()
    if not name:
        return {"error": "Give the template a name."}
    rows = []
    for r in conn.execute("SELECT * FROM shifts WHERE day BETWEEN ? AND ?", (monday.isoformat(), (monday + timedelta(days=6)).isoformat())):
        rows.append({"agent": r["agent"], "dow": (date.fromisoformat(r["day"]) - monday).days, "start": r["start_min"],
                     "end": r["end_min"], "start2": r["start2_min"], "end2": r["end2_min"]})
    with conn:
        conn.execute("INSERT INTO shift_templates (name, rows, created_by, created_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET rows=excluded.rows, created_by=excluded.created_by, created_at=excluded.created_at",
                     (name, json.dumps(rows), who, _now()))
    return {"ok": True, "shifts": len(rows)}


def delete_template(conn, template_id: int) -> dict:
    ensure(conn)
    with conn:
        conn.execute("DELETE FROM shift_templates WHERE id = ?", (template_id,))
    return {"ok": True}


# --- templates screen (IS Managers -> Templates) ---------------------------------
# A template is a week with no dates: {agent: [7 cells Mon..Sun]}. Stored as the
# same JSON rows create_week() already reads, so old templates keep working.
def _tpl_grid(rows_json: str) -> dict[str, list]:
    grid: dict[str, list] = {}
    for x in json.loads(rows_json or "[]"):
        grid.setdefault(x["agent"], [""] * 7)[int(x["dow"])] = fmt_cell(x["start"], x["end"], x.get("start2"), x.get("end2"))
    return grid


def _tpl_rows(grid: dict[str, list]) -> list[dict]:
    rows = []
    for agent in sorted(grid):
        for k, cell in enumerate(grid[agent]):
            p = parse_cell(cell)
            if p:
                rows.append({"agent": agent, "dow": k, "start": p[0], "end": p[1], "start2": p[2], "end2": p[3]})
    return rows


def _tpl_row(conn, template_id) -> sqlite3.Row | None:
    ensure(conn)
    try:
        return conn.execute("SELECT * FROM shift_templates WHERE id = ?", (int(template_id),)).fetchone()
    except (TypeError, ValueError):
        return None


def _tpl_write(conn, who: str, template_id: int, grid: dict[str, list]) -> None:
    with conn:
        conn.execute("UPDATE shift_templates SET rows = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(_tpl_rows(grid)), who, _now(), template_id))


def _hours(cells: list) -> float:
    return round(sum((sched.shift_hours(c) or 0) for c in cells), 2)


def template_list(conn) -> list[dict]:
    ensure(conn)
    out = []
    for r in conn.execute("SELECT * FROM shift_templates ORDER BY name COLLATE NOCASE"):
        grid = _tpl_grid(r["rows"])
        out.append({"id": r["id"], "name": r["name"],
                    "shifts": sum(1 for cells in grid.values() for c in cells if c),
                    "agents": sum(1 for cells in grid.values() if any(cells)),
                    "hours": round(sum(_hours(c) for c in grid.values()), 2),
                    "created_by": r["created_by"], "created_at": r["created_at"],
                    "updated_by": r["updated_by"] or r["created_by"], "updated_at": r["updated_at"] or r["created_at"]})
    return out


def template_get(conn, template_id, agents: list[str]) -> dict:
    r = _tpl_row(conn, template_id)
    if not r:
        return {"error": "No such template."}
    grid = _tpl_grid(r["rows"])
    roster = set(agents)
    rows = []
    for a in sorted(roster | set(grid)):
        cells = list(grid.get(a, [""] * 7))
        segs = [sched.shift_segments(c) for c in cells]
        rows.append({"agent": a, "cells": cells, "off": [""] * 7, "on_roster": a in roster,
                     "starts": [s[0][0] if s else None for s in segs],
                     "lengths": [sum(b - x for x, b in s) for s in segs],
                     "hours": _hours(cells)})
    return {"id": r["id"], "name": r["name"], "agents": rows, "coverage": coverage(grid),
            "presets": PRESETS, "targets": coverage_targets(conn),
            "updated_by": r["updated_by"] or r["created_by"], "updated_at": r["updated_at"] or r["created_at"]}


def template_create(conn, who: str, name: str, source: str = "blank", monday: date | None = None,
                    from_id: int | None = None) -> dict:
    """source: blank | week (copy a week's shifts, monday given) | copy (duplicate template from_id)."""
    ensure(conn)
    name = (name or "").strip()
    if not name:
        return {"error": "Give the template a name."}
    if conn.execute("SELECT 1 FROM shift_templates WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        return {"error": f"There's already a template called “{name}”."}
    grid: dict[str, list] = {}
    if source == "week":
        if not monday:
            return {"error": "Pick the week to copy."}
        grid = week_rows(conn, monday_of(monday))
        if not grid:
            return {"error": "That week has no shifts in the builder."}
    elif source == "copy":
        src = _tpl_row(conn, from_id)
        if not src:
            return {"error": "No such template."}
        grid = _tpl_grid(src["rows"])
    now = _now()
    with conn:
        cur = conn.execute("INSERT INTO shift_templates (name, rows, created_by, created_at, updated_by, updated_at) "
                           "VALUES (?,?,?,?,?,?)", (name, json.dumps(_tpl_rows(grid)), who, now, who, now))
    return {"ok": True, "id": cur.lastrowid}


def template_rename(conn, who: str, template_id, name: str) -> dict:
    r = _tpl_row(conn, template_id)
    if not r:
        return {"error": "No such template."}
    name = (name or "").strip()
    if not name:
        return {"error": "Give the template a name."}
    if conn.execute("SELECT 1 FROM shift_templates WHERE name = ? COLLATE NOCASE AND id <> ?", (name, r["id"])).fetchone():
        return {"error": f"There's already a template called “{name}”."}
    with conn:
        conn.execute("UPDATE shift_templates SET name = ?, updated_by = ?, updated_at = ? WHERE id = ?", (name, who, _now(), r["id"]))
    return {"ok": True}


def template_set(conn, who: str, template_id, agent: str, dows: list[int], cell: str | None) -> dict:
    """Set (or clear) one agent's shift on one or more weekdays (0 = Mon)."""
    r = _tpl_row(conn, template_id)
    if not r:
        return {"error": "No such template."}
    agent = (agent or "").strip()
    if not agent:
        return {"error": "No agent given."}
    parsed = parse_cell(cell or "")
    if cell and cell.strip() and cell.strip().upper() not in ("OFF", "-") and not parsed:
        return {"error": f"Couldn't read “{cell}” as a shift. Use the sheet's notation, e.g. 9-5 or 8-11, 1:30-7."}
    if not dows or any(not (0 <= int(k) <= 6) for k in dows):
        return {"error": "Bad day."}
    grid = _tpl_grid(r["rows"])
    cells = grid.setdefault(agent, [""] * 7)
    for k in dows:
        cells[int(k)] = fmt_cell(*parsed) if parsed else ""
    _tpl_write(conn, who, r["id"], grid)
    return {"ok": True, "cell": fmt_cell(*parsed) if parsed else ""}


def template_move(conn, who: str, template_id, agent: str, dow: int, to_agent: str, to_dow: int, copy: bool = False) -> dict:
    r = _tpl_row(conn, template_id)
    if not r:
        return {"error": "No such template."}
    grid = _tpl_grid(r["rows"])
    cell = (grid.get(agent) or [""] * 7)[int(dow)]
    if not cell:
        return {"error": "Nothing to move."}
    grid.setdefault(to_agent, [""] * 7)[int(to_dow)] = cell
    if not copy and (agent, int(dow)) != (to_agent, int(to_dow)):
        grid[agent][int(dow)] = ""
    _tpl_write(conn, who, r["id"], grid)
    return {"ok": True}


def template_clear_agent(conn, who: str, template_id, agent: str) -> dict:
    """Take an agent out of a template entirely (e.g. someone who has left)."""
    r = _tpl_row(conn, template_id)
    if not r:
        return {"error": "No such template."}
    grid = _tpl_grid(r["rows"])
    grid.pop(agent, None)
    _tpl_write(conn, who, r["id"], grid)
    return {"ok": True}
