"""The agents' shift schedule, for the agent page's Schedule tab.

Read through the Google service account from the "IS Scheduling" sheet's
"Website Schedule" tab (the one that used to be posted to the old website):
blocks of two Monday–Sunday weeks side by side, a "m/d-m/d" label above each
block, names down the left, one shift cell per day ("9-5", "8-11, 1:30-7",
"OFF", "-" or blank = not scheduled). Cached ten minutes, like the other
sheets; edits on the sheet show up on the next load.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from .marketing import _cached, _sheet_get
from .periods import CLOCK_TZ

TAB = "Website Schedule"
_WEEK = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s*$")
_SEG = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")
_NICKNAMES = {"dave": "david", "ben": "benjamin", "zach": "zachary", "nate": "nathan",
              "will": "william", "alex": "alexander", "andy": "andrew", "jake": "jacob",
              "gabe": "gabriel"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def shift_hours(cell: str) -> float | None:
    """'9-5' -> 8, '9-4:30' -> 7.5, '8-11, 1:30-7' -> 8.5, OFF / '-' / blank -> 0,
    anything unreadable -> None."""
    s = (cell or "").strip()
    if not s or s in ("-", "–", "—") or s.upper().startswith("OFF"):
        return 0.0
    s = re.sub(r"\([^)]*\)", "", s)
    total, any_seg = 0.0, False
    for seg in re.split(r"[,;.&]|\band\b", s):
        if not seg.strip():
            continue
        m = _SEG.match(seg)
        if not m:
            return None
        a = int(m.group(1)) + int(m.group(2) or 0) / 60
        b = int(m.group(3)) + int(m.group(4) or 0) / 60
        if a < 7:
            a += 12
            b += 12
        if b <= a:
            b += 12
        if not 0 < b - a <= 16:
            return None
        total += b - a
        any_seg = True
    return total if any_seg else None


def _date_for(month: int, day: int, today: date) -> date:
    """The sheet's labels carry no year: pick the candidate nearest today."""
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(date(y, month, day))
        except ValueError:
            pass
    return min(cands, key=lambda d: abs((d - today).days))


def parse_website_schedule(rows: list[list[str]], today: date) -> dict[str, dict[str, list[str]]]:
    """{monday_iso: {short_name: [7 shift cells Mon..Sun]}}"""
    out: dict[str, dict[str, list[str]]] = {}
    i = 0
    while i < len(rows):
        r = rows[i]
        starts = []                                   # (col, monday) for each block on this label row
        for c, cell in enumerate(r):
            m = _WEEK.match(cell or "")
            if m:
                starts.append((c, _date_for(int(m.group(1)), int(m.group(2)), today)))
        if not starts:
            i += 1
            continue
        # the day-name header sits 2 rows below the label; names follow until a blank-name row
        j = i + 3
        while j < len(rows):
            rr = rows[j]
            any_name = False
            for c, monday in starts:
                name = (rr[c - 1].strip() if c - 1 < len(rr) and c - 1 >= 0 else "")
                if not name:
                    continue
                any_name = True
                cells = [(rr[c + k].strip() if c + k < len(rr) else "") for k in range(7)]
                out.setdefault(monday.isoformat(), {})[name] = cells
            if not any_name:
                break
            j += 1
        i = j + 1
    return out


def website_schedule(settings) -> dict:
    return _cached(("website_schedule", settings.schedule_sheet_id), lambda: _website_schedule(settings))


def _website_schedule(settings) -> dict:
    today = datetime.now(timezone.utc).astimezone(CLOCK_TZ).date()
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{settings.schedule_sheet_id}/values/"
           + quote(f"{TAB}!A1:AZ700", safe="!:"))
    rows = _sheet_get(settings, url).get("values", [])
    return {"weeks": parse_website_schedule(rows, today),
            "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def match_name(short: str, agents: list[str]) -> str | None:
    """'Tyler G' / 'Dave S' / 'Aaron' -> the one agent whose first name (or its
    formal form) and, if given, last initial fit."""
    parts = short.split()
    if not parts:
        return None
    first = parts[0].lower()
    formal = _NICKNAMES.get(first, first)
    initial = parts[1][0].lower() if len(parts) > 1 and parts[1] else ""
    hits = []
    for a in agents:
        ap = a.lower().split()
        if not ap:
            continue
        af = ap[0]
        if af not in (first, formal) and _NICKNAMES.get(af, af) != formal:
            continue
        if initial and not ap[-1].startswith(initial):
            continue
        hits.append(a)
    return hits[0] if len(hits) == 1 else None


def _overlay_off(weeks: dict, agents: list[str], time_off: dict | None) -> dict:
    """Approved time off wins over whatever the week says for that day. The
    week's rows are keyed by the sheet's short names, so the agent's full
    name is matched to the row; an agent with no row gets one."""
    if not time_off:
        return weeks
    out = {m: {n: list(c) for n, c in rows.items()} for m, rows in weeks.items()}
    for agent, days in time_off.items():
        for day_iso, kind in days.items():
            d = date.fromisoformat(day_iso)
            monday = (d - timedelta(days=d.weekday())).isoformat()
            if monday not in out:
                continue          # time off never makes an unposted week appear
            rows = out[monday]
            key = next((n for n in rows if n == agent or match_name(n, agents) == agent), agent)
            cells = rows.setdefault(key, [""] * 7)
            cells[d.weekday()] = "PTO" if kind == "pto" else "Unpaid"
    return out


def _weeks(settings, db_weeks: dict | None) -> tuple[dict, dict]:
    """The sheet's weeks with any PUBLISHED builder weeks laid over them; a
    DRAFT builder week (value None) removes that week altogether."""
    try:
        sheet = website_schedule(settings)
    except Exception as e:                            # noqa: BLE001 — sheet down: builder weeks still work
        import logging
        logging.getLogger("isadash.schedule").warning("schedule sheet unavailable: %s", e)
        sheet = {"weeks": {}, "read_at": None}
    weeks = dict(sheet["weeks"])
    for monday, rows in (db_weeks or {}).items():
        if rows is None:
            weeks.pop(monday, None)
        else:
            weeks[monday] = rows
    return weeks, sheet


def my_schedule(settings, agent: str, agents: list[str], weeks_back: int = 1,
                db_weeks: dict | None = None, time_off: dict | None = None) -> dict:
    """The signed-in agent's weeks from last week onward, oldest first."""
    all_weeks, sched = _weeks(settings, db_weeks)
    all_weeks = _overlay_off(all_weeks, agents, {agent: time_off.get(agent, {})} if time_off and agent in time_off else None)
    sched = {"weeks": all_weeks, "read_at": sched["read_at"]}
    today = datetime.now(timezone.utc).astimezone(CLOCK_TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    first = this_monday - timedelta(days=7 * weeks_back)
    weeks = []
    found_name = None
    for monday_iso in sorted(sched["weeks"]):
        monday = date.fromisoformat(monday_iso)
        if monday < first:
            continue
        people = sched["weeks"][monday_iso]
        mine = None
        for short, cells in people.items():
            if match_name(short, agents) == agent:
                mine, found_name = cells, short
                break
        days = []
        total = 0.0
        for k in range(7):
            d = monday + timedelta(days=k)
            cell = mine[k] if mine else ""
            h = shift_hours(cell)
            if h:
                total += h
            days.append({"date": d.isoformat(), "dow": DAYS[k], "shift": cell,
                         "hours": round(h, 2) if h else 0, "today": d == today,
                         "past": d < today, "off": cell in ("PTO", "Unpaid")})
        weeks.append({"start": monday_iso, "end": (monday + timedelta(days=6)).isoformat(),
                      "current": monday == this_monday, "on_sheet": mine is not None,
                      "from_builder": monday_iso in (db_weeks or {}),
                      "days": days, "hours": round(total, 2)})
    return {"agent": agent, "sheet_name": found_name, "weeks": weeks,
            "read_at": sched["read_at"], "today": today.isoformat()}


def _start_minutes(cell: str) -> int:
    """Minutes after midnight the shift starts (for sorting); 24h*60 when none."""
    s = (cell or "").strip()
    m = _SEG.match(re.split(r"[,;.&]|\band\b", s)[0]) if s else None
    if not m:
        return 24 * 60
    h = int(m.group(1)) + (12 if int(m.group(1)) < 7 else 0)
    return h * 60 + int(m.group(2) or 0)


def day_roster(settings, day: date, agents: list[str], db_weeks: dict | None = None,
               time_off: dict | None = None) -> dict:
    """Everyone's shift on one day: {'on': [...earliest first], 'off': [...]}.
    Names are the roster's full names where the sheet's short name matches,
    else the short name as written on the sheet. A published builder week
    replaces the sheet's block for that week."""
    all_weeks, sched = _weeks(settings, db_weeks)
    all_weeks = _overlay_off(all_weeks, agents, time_off)
    monday = day - timedelta(days=day.weekday())
    people = all_weeks.get(monday.isoformat())
    if people is None:
        return {"date": day.isoformat(), "on": [], "off": [], "posted": False}
    k = day.weekday()
    on, off = [], []
    for short, cells in people.items():
        cell = cells[k] if k < len(cells) else ""
        full = match_name(short, agents) or short
        h = shift_hours(cell)
        if h:
            on.append({"name": full, "shift": cell, "hours": round(h, 2), "start": _start_minutes(cell)})
        else:
            off.append({"name": full, "shift": cell})
    on.sort(key=lambda r: (r["start"], r["name"]))
    off.sort(key=lambda r: r["name"])
    return {"date": day.isoformat(), "dow": DAYS[k], "on": on, "off": off, "posted": True,
            "read_at": sched["read_at"]}


def shift_segments(cell: str) -> list[tuple[int, int]]:
    """'9-5' -> [(540, 1020)], '8-11, 1:30-7' -> [(480, 660), (810, 1140)];
    OFF / '-' / blank / unreadable -> []. Minutes after midnight, same
    AM/PM rules as shift_hours."""
    s = (cell or "").strip()
    if not s or s in ("-", "–", "—") or s.upper().startswith("OFF"):
        return []
    s = re.sub(r"\([^)]*\)", "", s)
    out = []
    for seg in re.split(r"[,;.&]|\band\b", s):
        if not seg.strip():
            continue
        m = _SEG.match(seg)
        if not m:
            return []
        a = int(m.group(1)) * 60 + int(m.group(2) or 0)
        b = int(m.group(3)) * 60 + int(m.group(4) or 0)
        if a < 7 * 60:
            a += 12 * 60
            b += 12 * 60
        if b <= a:
            b += 12 * 60
        if not 0 < b - a <= 16 * 60:
            return []
        out.append((a, b))
    return out
