"""Agent hours for the Master Metric, one row per agent per day in `agent_hours`.

Where the rows come from:
  * source 'schedule' — scheduled hours FROZEN from the old shift-schedule
    sheet for every day up to and including 2026-10-04. Written once by
    scripts/freeze_schedule_hours.py and never touched again (the schedule
    sheet is no longer a data connection).
  * source 'homebase' — hours worked, from the daily Homebase upload sheet
    (settings.hours_sheet_url: Date, Agent Name, Regular Hours, OT Hours,
    Total Hours). Imported for days from settings.hours_sheet_from onward by
    the morning sync and, lightly throttled, whenever the tab loads, so a
    row corrected on the sheet flows through. Days before the cutover are
    never changed by the sheet.
  * source 'clock' — from 2026-10-26 (CLOCK_FROM, the time clock's go-live)
    every row is the hours actually worked on the dashboard's own time clock
    (punches, by Mountain day; an open punch counts up to now). Rebuilt from
    the punches whenever hours are read, so a manager's punch fix flows
    through. The Homebase sheet is frozen: its rows end 2026-10-25
    (HOMEBASE_THROUGH) and it is no longer read.
  * the outbound reps in settings.mm_default_hours_agents always count
    settings.mm_default_hours_per_day per weekday — they are on neither sheet.

Homebase hours ran through yesterday; clock hours include today so far.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger("isadash.hours")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_hours (
    day       TEXT NOT NULL,              -- Pacific day, YYYY-MM-DD
    agent     TEXT NOT NULL,              -- agent_map.genesys_name
    hours     REAL NOT NULL,              -- the number the Master Metric divides by
    regular   REAL,
    ot        REAL,
    source    TEXT NOT NULL,              -- 'schedule' (frozen) | 'homebase' (frozen) | 'clock'
    loaded_at TEXT NOT NULL,
    PRIMARY KEY (day, agent)
);
"""
_IMPORT_TTL_S = 600                       # tab loads re-read the sheet at most every 10 min
HOMEBASE_THROUGH = date(2026, 10, 25)     # last day of Homebase hours; the sheet is frozen after this
CLOCK_FROM = date(2026, 10, 26)           # from here, hours come from the dashboard time clock
_state = {"at": 0.0, "result": None}
_lock = threading.Lock()


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _parse_day(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _num(s) -> float | None:
    try:
        return float(str(s).strip() or 0)
    except ValueError:
        return None


def match_name(name: str, agents: list[str]) -> str | None:
    """Sheet name -> tracked agent: exact (case-insensitive) first, else the
    one agent sharing first and last word (FieldRoutes-style spacing quirks)."""
    n = " ".join((name or "").split()).lower()
    if not n:
        return None
    for a in agents:
        if a.lower() == n:
            return a
    parts = n.split()
    if len(parts) >= 2:
        hits = [a for a in agents
                if a.lower().split()[:1] == parts[:1] and a.lower().split()[-1:] == parts[-1:]]
        if len(hits) == 1:
            return hits[0]
    return None


def import_sheet(conn, settings, agents: list[str], force: bool = False) -> dict:
    """FROZEN since the time clock went live (2026-10-26): the Homebase rows
    through HOMEBASE_THROUGH stay exactly as last imported and the sheet is not
    read again. Kept as a no-op so callers don't change."""
    return {"ok": True, "rows": 0, "days": [], "unmatched": [], "error": None, "frozen": True}


def _import_sheet_live(conn, settings, agents: list[str], force: bool = False) -> dict:
    """(The old importer, unused.) Pull the Homebase hours sheet and upsert
    rows for days on/after the cutover, never past HOMEBASE_THROUGH."""
    with _lock:
        if not force and _state["result"] is not None and time.monotonic() - _state["at"] < _IMPORT_TTL_S:
            return _state["result"]
        res = _import(conn, settings, agents)
        _state.update(at=time.monotonic(), result=res)
        return res


def _import(conn, settings, agents: list[str]) -> dict:
    ensure(conn)
    out = {"ok": False, "rows": 0, "days": [], "unmatched": [], "error": None}
    if not settings.hours_sheet_url:
        out["error"] = "no hours sheet configured"
        return out
    try:
        r = httpx.get(settings.hours_sheet_url, follow_redirects=True, timeout=45)
        r.raise_for_status()
        if "text/csv" not in r.headers.get("content-type", ""):
            raise RuntimeError("the hours sheet did not return a CSV — is link sharing still on?")
        text = r.content.decode("utf-8-sig", "replace")
    except Exception as e:                           # noqa: BLE001 — reported, never fatal
        log.warning("hours sheet unreachable: %s", e)
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out
    cutover = date.fromisoformat(settings.hours_sheet_from)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, days, unmatched = [], set(), set()
    for rec in csv.DictReader(io.StringIO(text)):
        d = _parse_day(rec.get("Date", ""))
        name = (rec.get("Agent Name") or "").strip()
        if not d or not name or d < cutover or d > HOMEBASE_THROUGH:
            continue
        total = _num(rec.get("Total Hours"))
        reg, ot = _num(rec.get("Regular Hours")), _num(rec.get("OT Hours"))
        if total is None:
            total = (reg or 0.0) + (ot or 0.0)
        agent = match_name(name, agents)
        if not agent:
            unmatched.add(name)
            continue
        rows.append((d.isoformat(), agent, round(total, 2), reg, ot, "homebase", now))
        days.add(d.isoformat())
    with conn:
        conn.executemany(
            "INSERT INTO agent_hours (day, agent, hours, regular, ot, source, loaded_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(day, agent) DO UPDATE SET hours=excluded.hours, "
            "regular=excluded.regular, ot=excluded.ot, source=excluded.source, loaded_at=excluded.loaded_at",
            rows)
        conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('hours_sheet_last_import', ?)",
                     (now,))
    out.update(ok=True, rows=len(rows), days=sorted(days), unmatched=sorted(unmatched))
    return out


def sync_clock(conn, through: date | None = None) -> int:
    """Rebuild the 'clock' rows (CLOCK_FROM .. today) from the time clock's
    punches: hours per agent per Mountain day, by the day each punch started;
    an open punch counts up to now. Rows for a day whose punches were all
    removed are deleted. Days before CLOCK_FROM are never touched. Returns the
    number of agent-days on file."""
    from . import timeclock
    from .periods import CLOCK_TZ
    ensure(conn)
    timeclock.ensure(conn)
    now = datetime.now(timezone.utc)
    today = through or now.astimezone(CLOCK_TZ).date()
    if today < CLOCK_FROM:
        return 0
    lo = datetime(CLOCK_FROM.year, CLOCK_FROM.month, CLOCK_FROM.day, tzinfo=CLOCK_TZ).astimezone(timezone.utc)
    hi = (datetime(today.year, today.month, today.day, tzinfo=CLOCK_TZ) + timedelta(days=1)).astimezone(timezone.utc)
    want: dict[tuple[str, str], float] = {}
    for r in conn.execute("SELECT agent, in_at, out_at FROM punches WHERE in_at >= ? AND in_at < ?",
                          (lo.isoformat(timespec="seconds"), hi.isoformat(timespec="seconds"))):
        a = datetime.fromisoformat(r["in_at"])
        b = datetime.fromisoformat(r["out_at"]) if r["out_at"] else now
        h = max(0.0, (b - a).total_seconds() / 3600)
        k = (a.astimezone(CLOCK_TZ).date().isoformat(), r["agent"])
        want[k] = want.get(k, 0.0) + h
    stamp = now.isoformat(timespec="seconds")
    have = {(r["day"], r["agent"]): r["hours"] for r in conn.execute(
        "SELECT day, agent, hours FROM agent_hours WHERE source = 'clock' AND day >= ? AND day <= ?",
        (CLOCK_FROM.isoformat(), today.isoformat()))}
    with conn:
        for k, h in want.items():
            h = round(h, 2)
            if have.get(k) != h:
                conn.execute("INSERT INTO agent_hours (day, agent, hours, regular, ot, source, loaded_at) VALUES (?,?,?,?,?,?,?) "
                             "ON CONFLICT(day, agent) DO UPDATE SET hours=excluded.hours, regular=excluded.regular, "
                             "ot=excluded.ot, source=excluded.source, loaded_at=excluded.loaded_at",
                             (k[0], k[1], h, None, None, "clock", stamp))
        for k in set(have) - set(want):
            conn.execute("DELETE FROM agent_hours WHERE day = ? AND agent = ? AND source = 'clock'", k)
    return len(want)


def hours_for(conn, settings, agents: dict, start: date, end: date, today: date) -> tuple[dict, list[str]]:
    """{agent: hours} over start..end plus notes for the tab. Frozen schedule
    and Homebase rows through Oct 25, then the time clock — today included,
    so far. The outbound reps' default is applied through yesterday."""
    ensure(conn)
    sync_clock(conn)
    last = min(end, today)
    last_def = min(end, today - timedelta(days=1))
    hours: dict[str, float] = {}
    notes: list[str] = []
    defaults = [a for a in settings.mm_default_hours_agents if a in agents]
    if last >= start:
        for r in conn.execute(
                "SELECT agent, SUM(hours) h FROM agent_hours WHERE day BETWEEN ? AND ? GROUP BY agent",
                (start.isoformat(), last.isoformat())):
            if r["agent"] in agents and r["agent"] not in defaults:
                hours[r["agent"]] = float(r["h"] or 0.0)
        d = start
        wd = 0
        while d <= last_def:
            if d.weekday() < 5:
                wd += 1
            d += timedelta(days=1)
        for a in defaults:
            if wd:
                hours[a] = wd * settings.mm_default_hours_per_day
        if defaults and wd:
            notes.append(f"Outbound team counted as {settings.mm_default_hours_per_day:g}h Mon–Fri: "
                         + ", ".join(defaults) + ".")
        # which Homebase days (cutover .. Oct 25) have nothing on file?
        cutover = date.fromisoformat(settings.hours_sheet_from)
        hb_last = min(last, HOMEBASE_THROUGH)
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM agent_hours WHERE day BETWEEN ? AND ? AND source = 'homebase'",
            (start.isoformat(), hb_last.isoformat()))}
        missing = []
        d = max(start, cutover)
        while d <= hb_last:
            if d.isoformat() not in have:
                missing.append(d)
            d += timedelta(days=1)
        if missing:
            notes.append("No hours uploaded yet for " + _day_list(missing)
                         + " — those days count as 0 hours until the sheet has them.")
    if end >= today and today >= CLOCK_FROM:
        notes.append("Today's hours come live from the time clock (anyone still clocked in counts up to now).")
    return hours, notes


def _day_list(days: list[date]) -> str:
    if len(days) == 1:
        return f"{days[0]:%b} {days[0].day}"
    return f"{days[0]:%b} {days[0].day} – {days[-1]:%b} {days[-1].day} ({len(days)} days)"
