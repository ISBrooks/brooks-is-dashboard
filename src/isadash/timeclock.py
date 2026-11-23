"""Time clock + timesheets (docs/TIMESHEET_PLAN.md, segments 1–2).

  * punches     — one row per clock-in; the server stamps every time (UTC).
                  out_kind: manual | auto (closed at the scheduled shift end)
                  | edited (a manager changed it) | added (a manager entered it)
  * punch_audit — every manager change: who, when, field, old -> new, reason
  * agent_hr    — per-agent HR facts the PTO engine will need (start date,
                  employment type, weekly hours, work state); separate from
                  agent_map so the roster table stays untouched

Shifts come from the schedule sheet for now (schedule.py); when the schedule
builder lands they come from the shifts table instead — only shift_for()
changes. Auto clock-out: an open punch whose scheduled shift ended more than
the grace window ago is closed AT the shift end and flagged "auto"; with no
scheduled shift, a punch open longer than MAX_OPEN_H is closed at that cap.
All of that is a manager setting (IS Managers -> Timesheets): auto_rules() —
on/off, the minutes after the shift end, whether the clock-out is recorded at
the shift end or when it runs, and the no-shift cap in hours (0 = never).
A background thread in the web process runs the sweep every minute.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import schedule as sched
from . import shiftplan
from . import timeoff
from .periods import CLOCK_TZ

log = logging.getLogger("isadash.timeclock")
GRACE_MIN = 10            # default: minutes after the scheduled end before auto clock-out
MAX_OPEN_H = 14           # default: with no scheduled shift, an open punch is closed after this long
AUTO_DEFAULTS = {"enabled": True, "grace_min": GRACE_MIN, "stamp": "shift_end", "no_shift_h": MAX_OPEN_H}
LATE_MIN = 5              # clocked in this many minutes after the shift start = "late"
EARLY_OUT_MIN = 5         # clocked out this many minutes before the shift end (by hand) = "early out"
DEFAULT_RULES = {"early_min": 0, "block_unscheduled": False}   # clock-in guards, off until a manager turns them on

SCHEMA = """
CREATE TABLE IF NOT EXISTS punches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    in_at       TEXT NOT NULL,
    out_at      TEXT,
    out_kind    TEXT,
    shift       TEXT,                 -- the schedule cell for that day, as text ("9-5")
    shift_start TEXT,                 -- scheduled start/end (UTC) when known
    shift_end   TEXT,
    ip          TEXT,
    ua          TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punches_agent ON punches(agent, in_at);
CREATE TABLE IF NOT EXISTS punch_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    punch_id   INTEGER NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL,
    old        TEXT,
    new        TEXT,
    reason     TEXT
);
CREATE TABLE IF NOT EXISTS clock_overrides (
    agent      TEXT NOT NULL,
    day        TEXT NOT NULL,              -- the local day the override applies to
    note       TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent, day)
);
CREATE TABLE IF NOT EXISTS agent_hr (
    agent           TEXT PRIMARY KEY,
    start_date      TEXT,
    employment_type TEXT,             -- full_time | part_time
    weekly_hours    REAL,
    work_state      TEXT,
    notes           TEXT,
    updated_by      TEXT,
    updated_at      TEXT
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _local_day(dt: datetime) -> date:
    return dt.astimezone(CLOCK_TZ).date()


# --- the scheduled shift for a day ------------------------------------------
def shift_lookup(settings, agent: str, agents: list[str], day: date, db_weeks: dict | None = None) -> tuple[dict | None, bool]:
    """(shift, posted): shift = {'cell': '9-5', 'start': dt, 'end': dt,
    'segments': [(dt, dt)...]} or None when the agent isn't scheduled that
    day; posted = False when no schedule exists for that week at all (or the
    schedule can't be read), so callers can tell "off" from "unknown"."""
    try:
        roster = sched.day_roster(settings, day, agents, db_weeks, db_weeks.get("_time_off") if db_weeks else None)
    except Exception as e:                        # noqa: BLE001 — sheet down: no shift known
        log.warning("schedule unavailable for %s: %s", day, e)
        return None, False
    posted = bool(roster.get("posted"))
    cell = None
    for r in roster.get("on", []):
        if r["name"] == agent:
            cell = r["shift"]
            break
    if not cell:
        return None, posted
    segs = sched.shift_segments(cell)
    if not segs:
        return None, posted
    midnight = datetime(day.year, day.month, day.day, tzinfo=CLOCK_TZ)
    dts = [(midnight + timedelta(minutes=a), midnight + timedelta(minutes=b)) for a, b in segs]
    return {"cell": cell, "start": dts[0][0], "end": dts[-1][1], "segments": dts}, posted


def shift_for(settings, agent: str, agents: list[str], day: date, db_weeks: dict | None = None) -> dict | None:
    return shift_lookup(settings, agent, agents, day, db_weeks)[0]


def _t12(dt: datetime) -> str:
    """9:05 AM style, Mountain (portable: no %-I)."""
    d = dt.astimezone(CLOCK_TZ)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


# --- clock-in guards (IS Managers -> Timesheets -> Clock-in rules) --------------
def clock_rules(conn) -> dict:
    ensure(conn)
    row = conn.execute("SELECT value FROM app_config WHERE key = 'clock_rules'").fetchone()
    try:
        saved = json.loads(row["value"]) if row else {}
    except ValueError:
        saved = {}
    return {"early_min": int(saved.get("early_min", DEFAULT_RULES["early_min"]) or 0),
            "block_unscheduled": bool(saved.get("block_unscheduled", DEFAULT_RULES["block_unscheduled"]))}


def set_clock_rules(conn, who: str, rules: dict) -> dict:
    try:
        early = int(float(rules.get("early_min") or 0))
    except (TypeError, ValueError):
        return {"error": "Early minutes must be a whole number."}
    if not 0 <= early <= 240:
        return {"error": "Early minutes must be between 0 (off) and 240."}
    clean = {"early_min": early, "block_unscheduled": bool(rules.get("block_unscheduled")), "updated_by": who, "updated_at": _iso(_now())}
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('clock_rules', ?)", (json.dumps(clean),))
    return {"ok": True, "rules": clock_rules(conn)}


def auto_rules(conn) -> dict:
    """The auto clock-out setting: enabled, grace_min (minutes after the shift
    ends), stamp ('shift_end' = record the clock-out at the scheduled end,
    'run' = record it when the auto clock-out happens), no_shift_h (close a
    punch with no scheduled shift after this many hours; 0 = never)."""
    row = conn.execute("SELECT value FROM app_config WHERE key = 'auto_clockout'").fetchone()
    try:
        saved = json.loads(row["value"]) if row else {}
    except ValueError:
        saved = {}
    out = dict(AUTO_DEFAULTS)
    for k in AUTO_DEFAULTS:
        if k in saved:
            out[k] = saved[k]
    out["enabled"] = bool(out["enabled"])
    out["grace_min"] = int(out["grace_min"])
    out["no_shift_h"] = float(out["no_shift_h"])
    out["stamp"] = out["stamp"] if out["stamp"] in ("shift_end", "run") else "shift_end"
    for k in ("updated_by", "updated_at"):
        if k in saved:
            out[k] = saved[k]
    return out


def set_auto_rules(conn, who: str, body: dict) -> dict:
    try:
        grace = int(float(body.get("grace_min", GRACE_MIN)))
        cap = float(body.get("no_shift_h", MAX_OPEN_H) or 0)
    except (TypeError, ValueError):
        return {"error": "Minutes and hours must be numbers."}
    if not 0 <= grace <= 600:
        return {"error": "Minutes after the shift must be between 0 and 600."}
    if not 0 <= cap <= 24:
        return {"error": "The no-shift limit must be between 1 and 24 hours (0 = never)."}
    stamp = body.get("stamp") or "shift_end"
    if stamp not in ("shift_end", "run"):
        return {"error": "Pick when the clock-out is recorded."}
    clean = {"enabled": bool(body.get("enabled")), "grace_min": grace, "stamp": stamp, "no_shift_h": round(cap, 2),
             "updated_by": who, "updated_at": _iso(_now())}
    with conn:
        conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('auto_clockout', ?)", (json.dumps(clean),))
    return {"ok": True, "auto": auto_rules(conn)}


def set_override(conn, who: str, agent: str, day: date, note: str, remove: bool = False) -> dict:
    """A manager lets one agent clock in on one day regardless of the rules."""
    ensure(conn)
    with conn:
        if remove:
            conn.execute("DELETE FROM clock_overrides WHERE agent = ? AND day = ?", (agent, day.isoformat()))
        else:
            conn.execute("INSERT OR REPLACE INTO clock_overrides (agent, day, note, created_by, created_at) VALUES (?,?,?,?,?)",
                         (agent, day.isoformat(), (note or "").strip() or None, who, _iso(_now())))
    return {"ok": True}


def overrides(conn, from_day: date | None = None) -> list[dict]:
    ensure(conn)
    lo = (from_day or _local_day(_now())).isoformat()
    return [dict(r) for r in conn.execute("SELECT * FROM clock_overrides WHERE day >= ? ORDER BY day, agent", (lo,))]


def guard(conn, agent: str, now: datetime, shift: dict | None, posted: bool, rules: dict | None = None) -> dict | None:
    """None when the agent may clock in now; otherwise {'blocked': why,
    'allowed_from': iso|None}. An override for (agent, today) lifts both rules;
    a week that isn't posted never blocks anyone."""
    rules = rules or clock_rules(conn)
    today = _local_day(now)
    if conn.execute("SELECT 1 FROM clock_overrides WHERE agent = ? AND day = ?", (agent, today.isoformat())).fetchone():
        return None
    if shift and rules["early_min"] > 0:
        allowed = shift["start"] - timedelta(minutes=rules["early_min"])
        if now < allowed:
            return {"blocked": f"Your shift starts at {_t12(shift['start'])}. You can clock in from "
                               f"{_t12(allowed)} ({rules['early_min']} minutes before).",
                    "allowed_from": _iso(allowed), "kind": "early"}
    if not shift and posted and rules["block_unscheduled"]:
        return {"blocked": "You're not on today's schedule. Ask a manager to allow a clock-in for today.", "allowed_from": None,
                "kind": "unscheduled"}
    return None


# --- clock -------------------------------------------------------------------
def open_punch(conn, agent: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM punches WHERE agent = ? AND out_at IS NULL ORDER BY in_at DESC LIMIT 1",
                        (agent,)).fetchone()


def status(conn, settings, agent: str, agents: list[str]) -> dict:
    ensure(conn)
    sweep(conn, settings, agents)                 # lazy auto clock-out, in case the thread is asleep
    now = _now()
    today = _local_day(now)
    op = open_punch(conn, agent)
    shift, posted = shift_lookup(settings, agent, agents, today, shiftplan.published_weeks(conn))
    rules = clock_rules(conn)
    auto = auto_rules(conn)
    g = None if op else guard(conn, agent, now, shift, posted, rules)
    day_start = datetime(today.year, today.month, today.day, tzinfo=CLOCK_TZ).astimezone(timezone.utc)
    rows = conn.execute("SELECT * FROM punches WHERE agent = ? AND in_at >= ? ORDER BY in_at",
                        (agent, day_start.isoformat())).fetchall()
    worked = 0.0
    for r in rows:
        a, b = _parse(r["in_at"]), _parse(r["out_at"]) or now
        worked += (b - a).total_seconds() / 3600
    return {
        "agent": agent, "now": _iso(now), "today": today.isoformat(),
        "open": _punch_json(op) if op else None,
        "shift": ({"cell": shift["cell"], "start": _iso(shift["start"]), "end": _iso(shift["end"])} if shift else None),
        "today_punches": [_punch_json(r) for r in rows],
        "worked_today_h": round(worked, 2),
        "grace_min": auto["grace_min"], "auto": auto,
        "guard": g, "rules": rules, "posted": posted,
    }


def _punch_json(r: sqlite3.Row) -> dict:
    d = dict(r)
    a, b = _parse(d["in_at"]), _parse(d["out_at"])
    d["hours"] = round((b - a).total_seconds() / 3600, 2) if (a and b) else None
    return d


def clock_in(conn, settings, agent: str, agents: list[str], ip: str | None, ua: str | None) -> dict:
    ensure(conn)
    if open_punch(conn, agent):
        return {"error": "You're already clocked in."}
    now = _now()
    shift, posted = shift_lookup(settings, agent, agents, _local_day(now), shiftplan.published_weeks(conn))
    g = guard(conn, agent, now, shift, posted)
    if g:
        return {"error": g["blocked"], "guard": g}
    with conn:
        conn.execute(
            "INSERT INTO punches (agent, in_at, shift, shift_start, shift_end, ip, ua, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (agent, _iso(now), shift["cell"] if shift else None,
             _iso(shift["start"]) if shift else None, _iso(shift["end"]) if shift else None,
             ip, (ua or "")[:200], _iso(now)))
    return {"ok": True}


def clock_out(conn, agent: str) -> dict:
    ensure(conn)
    op = open_punch(conn, agent)
    if not op:
        return {"error": "You're not clocked in."}
    with conn:
        conn.execute("UPDATE punches SET out_at = ?, out_kind = 'manual' WHERE id = ?", (_iso(_now()), op["id"]))
    return {"ok": True}


def sweep(conn, settings, agents: list[str] | None = None) -> int:
    """Auto clock-out (per auto_rules): every open punch past its shift end
    (+ the grace minutes), or open longer than the no-shift limit with no
    shift. Does nothing while the setting is off. Returns how many were closed."""
    ensure(conn)
    auto = auto_rules(conn)
    if not auto["enabled"]:
        return 0
    grace = timedelta(minutes=auto["grace_min"])
    cap = timedelta(hours=auto["no_shift_h"]) if auto["no_shift_h"] > 0 else None
    now = _now()
    closed = 0
    db_weeks = shiftplan.published_weeks(conn)
    for r in conn.execute("SELECT * FROM punches WHERE out_at IS NULL").fetchall():
        in_at = _parse(r["in_at"])
        shift_end = _parse(r["shift_end"])
        if shift_end is None and agents is not None:
            # the shift may have been posted after the clock-in: look it up now
            s = shift_for(settings, r["agent"], agents, _local_day(in_at), db_weeks)
            if s:
                shift_end = s["end"]
                with conn:
                    conn.execute("UPDATE punches SET shift = ?, shift_start = ?, shift_end = ? WHERE id = ?",
                                 (s["cell"], _iso(s["start"]), _iso(s["end"]), r["id"]))
        if shift_end and now >= shift_end + grace:
            # recorded at the shift end, or at the moment it runs (shift end + grace), never before the clock-in
            out = max(shift_end if auto["stamp"] == "shift_end" else min(now, shift_end + grace), in_at)
            with conn:
                conn.execute("UPDATE punches SET out_at = ?, out_kind = 'auto' WHERE id = ?", (_iso(out), r["id"]))
            closed += 1
        elif not shift_end and cap and now - in_at >= cap:
            with conn:
                conn.execute("UPDATE punches SET out_at = ?, out_kind = 'auto', note = COALESCE(note, '') || ? WHERE id = ?",
                             (_iso(in_at + cap), f" [no scheduled shift: closed after {auto['no_shift_h']:g}h]", r["id"]))
            closed += 1
    return closed


_sweeper = {"thread": None}


def start_sweeper(settings, connect, agents_fn) -> None:
    """A once-a-minute auto clock-out sweep inside the web process."""
    if _sweeper["thread"]:
        return

    def run() -> None:
        while True:
            time.sleep(60)
            try:
                conn = connect(settings.db_path)
                try:
                    n = sweep(conn, settings, agents_fn(conn))
                    if n:
                        log.info("auto clock-out closed %d punch(es)", n)
                finally:
                    conn.close()
            except Exception:                     # noqa: BLE001 — keep the thread alive
                log.exception("auto clock-out sweep failed")
    t = threading.Thread(target=run, name="timeclock-sweeper", daemon=True)
    t.start()
    _sweeper["thread"] = t


# --- manager: who's on the clock right now (Schedule tab side box) --------------
EARLY_OK_MIN = 15         # clocked in up to this long before the shift starts still counts as "on shift"


def live_now(conn, settings, agents: list[str]) -> dict:
    """Right now: who is clocked in, who is scheduled but not clocked in, and
    who is clocked in outside their scheduled hours. Uses the same schedule
    the time clock uses (a published builder week, else the sheet), with
    approved time off taken out."""
    ensure(conn)
    sweep(conn, settings, agents)
    now = _now()
    today = _local_day(now)
    db_weeks = shiftplan.published_weeks(conn)
    try:
        roster = sched.day_roster(settings, today, agents, db_weeks, timeoff.approved_days(conn, today, today))
    except Exception as e:                        # noqa: BLE001 — sheet down: punches still show
        log.warning("live: schedule unavailable: %s", e)
        roster = {"on": [], "posted": False}
    midnight = datetime(today.year, today.month, today.day, tzinfo=CLOCK_TZ)
    shifts = {}
    for r in roster.get("on", []):
        segs = [(midnight + timedelta(minutes=a), midnight + timedelta(minutes=b)) for a, b in sched.shift_segments(r["shift"])]
        if segs:
            shifts[r["name"]] = {"cell": r["shift"], "segs": segs}
    punches = {r["agent"]: r for r in conn.execute("SELECT agent, in_at FROM punches WHERE out_at IS NULL ORDER BY in_at")}
    auto = auto_rules(conn)
    early = timedelta(minutes=EARLY_OK_MIN)
    grace = timedelta(minutes=auto["grace_min"] if auto["enabled"] else GRACE_MIN)
    on, missing, extra = [], [], []
    for name, p in punches.items():
        s = shifts.get(name)
        row = {"agent": name, "in_at": p["in_at"], "in_label": _t12(_parse(p["in_at"])), "shift": s["cell"] if s else None}
        if s and any(a - early <= now < b + grace for a, b in s["segs"]):
            on.append(row)
        else:
            extra.append(row)
    for name, s in shifts.items():
        if name in punches:
            continue
        cur = next(((a, b) for a, b in s["segs"] if a <= now < b), None)
        if cur:
            missing.append({"agent": name, "shift": s["cell"], "since": _iso(cur[0]), "since_label": _t12(cur[0]),
                            "late_min": int((now - cur[0]).total_seconds() // 60)})
    on.sort(key=lambda r: r["agent"])
    extra.sort(key=lambda r: r["agent"])
    missing.sort(key=lambda r: (-r["late_min"], r["agent"]))
    return {"now": _iso(now), "now_label": _t12(now), "today": today.isoformat(), "posted": bool(roster.get("posted")),
            "on": on, "missing": missing, "extra": extra, "late_after_min": LATE_MIN, "early_ok_min": EARLY_OK_MIN,
            "grace_min": grace.seconds // 60, "auto": auto}


# --- manager: timesheets ------------------------------------------------------
def timesheet(conn, settings, start: date, end: date, agents: list[str]) -> dict:
    """Per agent per day over the range: scheduled hours, worked hours,
    punches and flags (late, missed clock-in, auto clock-out, still open)."""
    ensure(conn)
    sweep(conn, settings, agents)
    now = _now()
    today = _local_day(now)
    a_utc = datetime(start.year, start.month, start.day, tzinfo=CLOCK_TZ).astimezone(timezone.utc)
    b_utc = (datetime(end.year, end.month, end.day, tzinfo=CLOCK_TZ) + timedelta(days=1)).astimezone(timezone.utc)
    rows = conn.execute("SELECT * FROM punches WHERE in_at >= ? AND in_at < ? ORDER BY agent, in_at",
                        (a_utc.isoformat(), b_utc.isoformat())).fetchall()
    by_day: dict[tuple[str, str], list] = {}
    for r in rows:
        by_day.setdefault((r["agent"], _local_day(_parse(r["in_at"])).isoformat()), []).append(r)
    # scheduled hours per agent per day from the posted schedule
    day = start
    sched_h: dict[tuple[str, str], tuple[float, str, datetime | None]] = {}
    db_weeks = shiftplan.published_weeks(conn)
    off = timeoff.approved_days(conn, start, end)
    while day <= min(end, today):
        try:
            roster = sched.day_roster(settings, day, agents, db_weeks, off)
        except Exception:                        # noqa: BLE001
            roster = {"on": []}
        for r in roster.get("on", []):
            segs = sched.shift_segments(r["shift"])
            st = None
            if segs:
                midnight = datetime(day.year, day.month, day.day, tzinfo=CLOCK_TZ)
                st = midnight + timedelta(minutes=segs[0][0])
            sched_h[(r["name"], day.isoformat())] = (r["hours"], r["shift"], st)
        day += timedelta(days=1)
    out: dict[str, dict] = {}
    keys = set(by_day) | set(sched_h)
    for agent, d in sorted(keys):
        if agent not in agents:
            continue
        ps = by_day.get((agent, d), [])
        sh, cell, st = sched_h.get((agent, d), (0.0, None, None))
        worked = 0.0
        flags = []
        for p in ps:
            a, b = _parse(p["in_at"]), _parse(p["out_at"])
            worked += ((b or now) - a).total_seconds() / 3600
            if p["out_kind"] == "auto":
                flags.append("auto clock-out")
            if b is None:
                flags.append("still clocked in")
        if ps and st and (_parse(ps[0]["in_at"]) - st) > timedelta(minutes=LATE_MIN):
            flags.append(f"late {int((_parse(ps[0]['in_at']) - st).total_seconds() // 60)} min")
        if sh and not ps and date.fromisoformat(d) < today:
            flags.append("no clock-in")
        if sh and not ps and date.fromisoformat(d) == today and st and now > st + timedelta(minutes=LATE_MIN):
            flags.append("not clocked in yet")
        ag = out.setdefault(agent, {"agent": agent, "scheduled_h": 0.0, "worked_h": 0.0, "days": [], "flags": 0})
        ag["scheduled_h"] += sh
        ag["worked_h"] += worked
        ag["flags"] += len(flags)
        ag["days"].append({"date": d, "shift": cell, "scheduled_h": round(sh, 2), "worked_h": round(worked, 2),
                           "punches": [_punch_json(p) for p in ps], "flags": flags})
    for ag in out.values():
        ag["scheduled_h"] = round(ag["scheduled_h"], 2)
        ag["worked_h"] = round(ag["worked_h"], 2)
        ag["diff_h"] = round(ag["worked_h"] - ag["scheduled_h"], 2)
        _overtime(ag)
    return {"start": start.isoformat(), "end": end.isoformat(), "through": min(end, today).isoformat(),
            "agents": sorted(out.values(), key=lambda x: x["agent"]),
            "grace_min": auto_rules(conn)["grace_min"], "auto": auto_rules(conn), "generated_at": _iso(now)}


def _overtime(ag: dict) -> None:
    """Overtime is weekly only: hours over 40 in a Sun–Sat week. The first 40
    hours of a week are regular however long any one day ran — there is no
    daily overtime (dropped 2026-11-23). Each day carries the part of it that
    fell past the week's 40th hour."""
    weeks: dict[str, list] = {}
    for d in ag["days"]:
        dd = date.fromisoformat(d["date"])
        sunday = dd - timedelta(days=(dd.weekday() + 1) % 7)
        weeks.setdefault(sunday.isoformat(), []).append(d)
    weekly = 0.0
    weeks_over = 0
    for days in weeks.values():
        run = 0.0
        for d in sorted(days, key=lambda x: x["date"]):
            h = d["worked_h"]
            in_window = max(0.0, min(h, 40.0 - run))          # the part of today still inside the first 40
            w_ot = h - in_window
            d["weekly_ot_h"] = round(w_ot, 2)
            weekly += w_ot
            run += h
        if run > 40:
            weeks_over += 1
    ag["weekly_ot_h"] = round(weekly, 2)
    ag["weeks_over_40"] = weeks_over


# --- manager: hours report (payroll) -------------------------------------------
def hours_report(conn, settings, start: date, end: date, agents: list[str]) -> dict:
    """Per agent over start..end: regular hours, weekly OT and PTO —
    the same overtime rule as the Timesheets tab (_overtime). Overtime is
    worked out on whole Sun–Sat weeks, so a range that starts mid-week still
    knows how many hours came earlier that week; only days inside the range
    are added up. Regular = worked - weekly OT. PTO = approved PTO
    taken on days in the range (the ledger's 'use' rows, incl. the paid part
    of a PTO + unpaid request and manager "PTO used" entries)."""
    from . import pto
    sunday = start - timedelta(days=(start.weekday() + 1) % 7)
    ts = timesheet(conn, settings, sunday, end, agents)
    lo, hi = start.isoformat(), end.isoformat()
    rows: dict[str, dict] = {}
    open_punches = []
    for ag in ts["agents"]:
        r = {"agent": ag["agent"], "regular_h": 0.0, "weekly_ot_h": 0.0, "pto_h": 0.0, "worked_h": 0.0}
        for d in ag["days"]:
            if not lo <= d["date"] <= hi:
                continue
            w, wot = d["worked_h"], d.get("weekly_ot_h", 0.0)
            r["worked_h"] += w
            r["weekly_ot_h"] += wot
            r["regular_h"] += w - wot
            if "still clocked in" in d["flags"]:
                open_punches.append({"agent": ag["agent"], "date": d["date"]})
        rows[ag["agent"]] = r
    pto.ensure(conn)
    for p in conn.execute("SELECT agent, -SUM(hours) AS h FROM pto_ledger WHERE kind = 'use' AND day BETWEEN ? AND ? GROUP BY agent",
                          (lo, hi)):
        if p["agent"] not in agents or not p["h"]:
            continue
        rows.setdefault(p["agent"], {"agent": p["agent"], "regular_h": 0.0, "weekly_ot_h": 0.0,
                                     "pto_h": 0.0, "worked_h": 0.0})["pto_h"] = p["h"]
    out = []
    for r in sorted(rows.values(), key=lambda x: x["agent"]):
        for k in ("regular_h", "weekly_ot_h", "pto_h", "worked_h"):
            r[k] = round(r[k] + 0.0, 2)
        if r["worked_h"] or r["pto_h"]:
            out.append(r)
    tot = {k: round(sum(r[k] for r in out), 2) for k in ("regular_h", "weekly_ot_h", "pto_h", "worked_h")}
    return {"start": lo, "end": hi, "through": ts["through"], "agents": out, "totals": tot,
            "open_punches": open_punches, "generated_at": ts["generated_at"]}


def hours_report_csv(rep: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Agent", "Regular Hours", "Weekly OT", "PTO"])
    for r in rep["agents"]:
        w.writerow([r["agent"], f"{r['regular_h']:.2f}", f"{r['weekly_ot_h']:.2f}", f"{r['pto_h']:.2f}"])
    t = rep["totals"]
    w.writerow(["TOTAL", f"{t['regular_h']:.2f}", f"{t['weekly_ot_h']:.2f}", f"{t['pto_h']:.2f}"])
    return buf.getvalue()


# --- manager: adherence ----------------------------------------------------------
def adherence(conn, settings, start: date, end: date, agents: list[str]) -> dict:
    """Schedule adherence per agent over the range, from the same data as the
    timesheet. Only scheduled days up to today count. Per agent:
      scheduled days / hours, no-shows (scheduled, no punch), late arrivals
      (first clock-in > LATE_MIN after the start) with minutes, early outs
      (last clock-out, unless automatic, > EARLY_OUT_MIN before the end), unscheduled
      days (punches with no shift), hours inside the shift window, and
      adherence % = hours inside the window / scheduled hours; on-time % =
      scheduled days with neither a no-show nor a late arrival."""
    ts = timesheet(conn, settings, start, end, agents)
    now = _now()
    today = _local_day(now)
    out = []
    for ag in ts["agents"]:
        r = {"agent": ag["agent"], "sched_days": 0, "sched_h": 0.0, "worked_days": 0, "no_show": 0, "late": 0, "late_min": 0,
             "early_out": 0, "early_min": 0, "unscheduled_days": 0, "in_window_h": 0.0, "worked_h": 0.0, "exceptions": []}
        for d in ag["days"]:
            dd = date.fromisoformat(d["date"])
            ps = d["punches"]
            segs = sched.shift_segments(d["shift"] or "") if d["shift"] else []
            if ps and not segs:
                r["unscheduled_days"] += 1
                r["worked_h"] += d["worked_h"]
                r["exceptions"].append({"date": d["date"], "kind": "unscheduled", "detail": f"clocked {d['worked_h']:.2f} h with no shift posted"})
                continue
            if not segs or dd > today:
                continue
            midnight = datetime(dd.year, dd.month, dd.day, tzinfo=CLOCK_TZ)
            win = [(midnight + timedelta(minutes=a), midnight + timedelta(minutes=b)) for a, b in segs]
            st, en = win[0][0], win[-1][1]
            r["sched_days"] += 1
            r["sched_h"] += d["scheduled_h"]
            r["worked_h"] += d["worked_h"]
            if not ps:
                if dd < today or now > en:
                    r["no_show"] += 1
                    r["exceptions"].append({"date": d["date"], "kind": "no-show", "detail": f"scheduled {d['shift']}, never clocked in"})
                else:
                    r["sched_days"] -= 1
                    r["sched_h"] -= d["scheduled_h"]        # today's shift still ahead: not judged yet
                continue
            r["worked_days"] += 1
            first_in = _parse(ps[0]["in_at"])
            last = ps[-1]
            last_out = _parse(last["out_at"])
            if first_in - st > timedelta(minutes=LATE_MIN):
                m = int((first_in - st).total_seconds() // 60)
                r["late"] += 1
                r["late_min"] += m
                r["exceptions"].append({"date": d["date"], "kind": "late", "detail": f"in at {_t12(first_in)}, shift {d['shift']} ({m} min late)"})
            if last_out and last["out_kind"] != "auto" and en - last_out > timedelta(minutes=EARLY_OUT_MIN):
                m = int((en - last_out).total_seconds() // 60)
                r["early_out"] += 1
                r["early_min"] += m
                r["exceptions"].append({"date": d["date"], "kind": "early out", "detail": f"out at {_t12(last_out)}, shift {d['shift']} ({m} min early)"})
            inside = 0.0
            for p in ps:
                a, b = _parse(p["in_at"]), _parse(p["out_at"]) or now
                for wa, wb in win:
                    lo, hi = max(a, wa), min(b, wb)
                    if hi > lo:
                        inside += (hi - lo).total_seconds() / 3600
            r["in_window_h"] += inside
        r["sched_h"] = round(r["sched_h"], 2)
        r["worked_h"] = round(r["worked_h"], 2)
        r["in_window_h"] = round(min(r["in_window_h"], r["sched_h"]) if r["sched_h"] else 0.0, 2)
        r["adherence_pct"] = round(100 * r["in_window_h"] / r["sched_h"], 1) if r["sched_h"] else None
        r["on_time_pct"] = round(100 * (r["sched_days"] - r["late"] - r["no_show"]) / r["sched_days"], 1) if r["sched_days"] else None
        r["avg_late_min"] = round(r["late_min"] / r["late"], 1) if r["late"] else 0
        r["exceptions"].sort(key=lambda x: x["date"])
        r["exception_count"] = len(r["exceptions"])
        out.append(r)
    return {"start": ts["start"], "end": ts["end"], "through": ts["through"], "agents": out,
            "late_min": LATE_MIN, "early_out_min": EARLY_OUT_MIN, "generated_at": _iso(now)}


def edit_punch(conn, who: str, punch_id: int | None, agent: str | None, in_at: str | None,
               out_at: str | None, reason: str, delete: bool = False) -> dict:
    """Manager edit: change in/out (ISO local or UTC strings), add a punch
    (punch_id None), or delete one. Everything is written to punch_audit;
    the note is optional."""
    ensure(conn)
    now = _iso(_now())
    reason = (reason or "").strip() or None

    def norm(s: str | None) -> str | None:
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CLOCK_TZ)
        return _iso(dt)
    with conn:
        if punch_id is None:
            if not (agent and in_at):
                return {"error": "Agent and clock-in time are required."}
            cur = conn.execute(
                "INSERT INTO punches (agent, in_at, out_at, out_kind, note, created_at) VALUES (?,?,?,?,?,?)",
                (agent, norm(in_at), norm(out_at), "added" if out_at else None, f"added by {who}", now))
            conn.execute("INSERT INTO punch_audit (punch_id, changed_by, changed_at, field, old, new, reason) VALUES (?,?,?,?,?,?,?)",
                         (cur.lastrowid, who, now, "added", None, f"{norm(in_at)} -> {norm(out_at)}", reason))
            return {"ok": True, "id": cur.lastrowid}
        row = conn.execute("SELECT * FROM punches WHERE id = ?", (punch_id,)).fetchone()
        if not row:
            return {"error": "No such punch."}
        if delete:
            conn.execute("INSERT INTO punch_audit (punch_id, changed_by, changed_at, field, old, new, reason) VALUES (?,?,?,?,?,?,?)",
                         (punch_id, who, now, "deleted", f"{row['in_at']} -> {row['out_at']}", None, reason))
            conn.execute("DELETE FROM punches WHERE id = ?", (punch_id,))
            return {"ok": True}
        for field, val in (("in_at", norm(in_at)), ("out_at", norm(out_at))):
            if val is not None and val != row[field]:
                conn.execute("INSERT INTO punch_audit (punch_id, changed_by, changed_at, field, old, new, reason) VALUES (?,?,?,?,?,?,?)",
                             (punch_id, who, now, field, row[field], val, reason))
                conn.execute(f"UPDATE punches SET {field} = ?, out_kind = CASE WHEN ? = 'out_at' THEN 'edited' ELSE out_kind END WHERE id = ?",
                             (val, field, punch_id))
    return {"ok": True}


def punch_history(conn, punch_id: int) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM punch_audit WHERE punch_id = ? ORDER BY changed_at", (punch_id,))]


def timesheet_csv(ts: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Agent", "Date", "Shift", "Scheduled hours", "Worked hours", "Weekly OT", "Clock in", "Clock out", "Out kind", "Flags"])
    for ag in ts["agents"]:
        for d in ag["days"]:
            if d["punches"]:
                for i, p in enumerate(d["punches"]):
                    w.writerow([ag["agent"], d["date"], d["shift"] or "", d["scheduled_h"], p["hours"] or "",
                                d.get("weekly_ot_h", 0) if i == 0 else "",
                                _fmt_local(p["in_at"]), _fmt_local(p["out_at"]), p["out_kind"] or "", "; ".join(d["flags"])])
            else:
                w.writerow([ag["agent"], d["date"], d["shift"] or "", d["scheduled_h"], 0, 0, "", "", "", "; ".join(d["flags"])])
        w.writerow([ag["agent"], "TOTAL", "", ag["scheduled_h"], ag["worked_h"], ag["weekly_ot_h"], "", "", "",
                    f"{ag['weeks_over_40']} weeks over 40h"])
    return buf.getvalue()


def _fmt_local(s: str | None) -> str:
    dt = _parse(s)
    return dt.astimezone(CLOCK_TZ).strftime("%Y-%m-%d %H:%M") if dt else ""


# --- agent HR facts -----------------------------------------------------------
def hr_list(conn, agents: list[str]) -> list[dict]:
    ensure(conn)
    have = {r["agent"]: dict(r) for r in conn.execute("SELECT * FROM agent_hr")}
    return [have.get(a, {"agent": a, "start_date": None, "employment_type": None, "weekly_hours": None,
                         "work_state": None, "notes": None}) for a in sorted(agents)]


def hr_save(conn, who: str, agent: str, fields: dict) -> dict:
    ensure(conn)
    allowed = {"start_date", "employment_type", "weekly_hours", "work_state", "notes"}
    vals = {k: (fields.get(k) if fields.get(k) not in ("", None) else None) for k in allowed}
    if vals["start_date"]:
        try:
            date.fromisoformat(vals["start_date"])
        except ValueError:
            return {"error": "Start date must be YYYY-MM-DD."}
    if vals["employment_type"] not in (None, "full_time", "part_time"):
        return {"error": "Employment type must be full_time or part_time."}
    if vals["weekly_hours"] is not None:
        try:
            vals["weekly_hours"] = float(vals["weekly_hours"])
        except (TypeError, ValueError):
            return {"error": "Weekly hours must be a number."}
    with conn:
        conn.execute(
            "INSERT INTO agent_hr (agent, start_date, employment_type, weekly_hours, work_state, notes, updated_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET start_date=excluded.start_date, "
            "employment_type=excluded.employment_type, weekly_hours=excluded.weekly_hours, work_state=excluded.work_state, "
            "notes=excluded.notes, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (agent, vals["start_date"], vals["employment_type"], vals["weekly_hours"], vals["work_state"], vals["notes"],
             who, _iso(_now())))
    return {"ok": True}
