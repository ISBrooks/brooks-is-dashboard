"""PTO balances and accrual (Brooks policy 4-09, docs/TIMESHEET_PLAN.md).

Rules, as encoded:
  * accrual = hours worked x rate; rate = 2/80 (1 per 40) for under two years
    of service, 3/80 (1.5 per 40) from the second anniversary (years from
    agent_hr.start_date)
  * hours worked = hours actually worked: the Homebase upload through
    2026-10-25, then the dashboard time clock (agent_hours source 'clock').
    The frozen SCHEDULE rows (before 2026-10-05) never earn PTO
  * nothing accrues before start_date + 180 days (the probation); balances
    that were granted earlier by mistake are kept (Dallin, 2026-10-09)
  * the 80-hour cap starts 2027-01-01 (CAP_EFFECTIVE). Until then there is
    no cap: balances above 80 keep them and keep accruing. On Jan 1, 2027 a
    balance above 80 (everything dated on or before Dec 31, 2026) is cut to
    80 by one 'cap:2027' ledger row, kept correct if December hours or edits
    arrive later. From then on accrual stops at 80 and resumes when PTO is
    used; carry-over needs no action
  * at most 40 worked hours a week earn PTO (WEEKLY_CAP_HOURS; Dallin,
    2026-11-23): overtime never earns PTO. Weeks run Sunday-Saturday, the
    same week overtime uses; the first 40 hours of the week earn, anything
    after does not. Applies from the week of Sunday 2026-11-22
    (WEEKLY_CAP_FROM) on; days before it are never re-figured
  * full-time test: agent_hr.employment_type when set, else an average of 30+
    hours a week over the trailing eight weeks of agent_hours

Everything is a row in pto_ledger (opening | accrual | use | adjust), so a
balance is always the sum of its rows and can be explained line by line.
Accrual rows are keyed (agent, day) and only ever added, never recomputed.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .periods import PACIFIC

PROBATION_DAYS = 180
CAP_HOURS = 80.0
CAP_EFFECTIVE = date(2027, 1, 1)   # the cap applies from here; balances above 80 are cut to 80 that day
RATE_UNDER_2Y = 2 / 80
RATE_2Y_PLUS = 3 / 80
WEEKLY_CAP_HOURS = 40.0           # only the first 40 worked hours of a Sun-Sat week earn PTO (no PTO on overtime)
WEEKLY_CAP_FROM = date(2026, 11, 22)   # a Sunday: the cap applies to this week and after, never to earlier days
FULL_TIME_WEEKLY = 30.0            # the policy's definition of full time...
FULL_TIME_TEST_WEEKLY = 28.0       # ...tested with two hours of grace, so being let off early doesn't flip anyone

SCHEMA = """
CREATE TABLE IF NOT EXISTS pto_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent      TEXT NOT NULL,
    day        TEXT NOT NULL,              -- the day the entry belongs to
    kind       TEXT NOT NULL,              -- opening | accrual | use | adjust
    hours      REAL NOT NULL,              -- + adds to the balance, - uses it
    note       TEXT,
    ref        TEXT,                       -- e.g. 'hours:2026-10-07' for an accrual, a request id for a use
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pto_accrual_day ON pto_ledger(agent, ref) WHERE kind = 'accrual';
CREATE INDEX IF NOT EXISTS idx_pto_agent ON pto_ledger(agent, day);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def years_of_service(start: date, on: date) -> float:
    return (on - start).days / 365.25


def rate_for(start: date | None, on: date) -> float:
    if not start:
        return 0.0
    return RATE_2Y_PLUS if years_of_service(start, on) >= 2 else RATE_UNDER_2Y


def eligible_from(start: date | None) -> date | None:
    return start + timedelta(days=PROBATION_DAYS) if start else None


def balance(conn, agent: str, through: date | None = None) -> float:
    ensure(conn)
    q = "SELECT COALESCE(SUM(hours), 0) FROM pto_ledger WHERE agent = ?"
    args: tuple = (agent,)
    if through:
        q += " AND day <= ?"
        args += (through.isoformat(),)
    return round(conn.execute(q, args).fetchone()[0], 2)


def hr_row(conn, agent: str) -> dict:
    r = conn.execute("SELECT * FROM agent_hr WHERE agent = ?", (agent,)).fetchone()
    return dict(r) if r else {}


def is_full_time(conn, agent: str, on: date, hr: dict | None = None) -> tuple[bool, str]:
    hr = hr if hr is not None else hr_row(conn, agent)
    if hr.get("employment_type") == "full_time":
        return True, "set to full time in Agent records"
    if hr.get("employment_type") == "part_time":
        return False, "set to part time in Agent records"
    lo = on - timedelta(weeks=8)
    h = conn.execute("SELECT COALESCE(SUM(hours), 0) FROM agent_hours WHERE agent = ? AND day > ? AND day <= ?",
                     (agent, lo.isoformat(), on.isoformat())).fetchone()[0]
    avg = h / 8
    return avg >= FULL_TIME_TEST_WEEKLY, f"{avg:.1f} h/week over the last 8 weeks"


CLOCK_RECALC_DAYS = 60     # time-clock days this far back are re-checked each run (a manager may fix a punch)


def week_sunday(d: date) -> date:
    """The Sunday that starts d's Sun-Sat week (the overtime week)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def earning_hours(d: date, worked: float, earlier_in_week: float) -> float:
    """The part of a day's worked hours that earns PTO: from WEEKLY_CAP_FROM,
    only what still fits inside the week's first 40 hours (the same split the
    overtime rule makes, so overtime hours never earn). Before then, all of it."""
    if d < WEEKLY_CAP_FROM:
        return worked
    return max(0.0, min(worked, WEEKLY_CAP_HOURS - earlier_in_week))


def accrue(conn, agents: list[str], through: date | None = None, since: date | None = None) -> dict:
    """Bring accrual rows up to date, honouring probation and the cap. Only
    hours actually WORKED earn PTO: Homebase hours through Oct 25, 2026, then
    the dashboard time clock (complete days only — today is earned tomorrow).
    Scheduled hours never earn PTO. Homebase days are added once; time-clock
    days in the last CLOCK_RECALC_DAYS are recalculated, so a fixed punch
    corrects that day's accrual. Nothing is earned on or before an agent's
    latest 'opening' row (the go-live balance) or before probation ends.
    Returns {agent: net hours added}."""
    from . import hours as hours_mod
    from .periods import CLOCK_TZ
    ensure(conn)
    today = through or datetime.now(timezone.utc).astimezone(CLOCK_TZ).date()
    clock_last = today if through else today - timedelta(days=1)   # live runs: the clock day must be over
    hours_mod.sync_clock(conn, today)
    added: dict[str, float] = {}
    for agent in agents:
        hr = hr_row(conn, agent)
        start = date.fromisoformat(hr["start_date"]) if hr.get("start_date") else None
        if not start:
            continue
        elig = eligible_from(start)
        last_open = conn.execute("SELECT MAX(day) FROM pto_ledger WHERE agent = ? AND kind = 'opening'", (agent,)).fetchone()[0]
        after_open = date.fromisoformat(last_open) + timedelta(days=1) if last_open else None
        total = 0.0
        # (1) Homebase days, through Oct 25, 2026: added once, never recomputed
        last = conn.execute("SELECT MAX(day) FROM pto_ledger WHERE agent = ? AND kind IN ('opening','accrual')", (agent,)).fetchone()[0]
        lo = max([d for d in (elig, since, (date.fromisoformat(last) + timedelta(days=1)) if last else None) if d])
        hb_hi = min(today, hours_mod.HOMEBASE_THROUGH)
        have = {r[0] for r in conn.execute("SELECT ref FROM pto_ledger WHERE agent = ? AND kind = 'accrual'", (agent,))}
        rows = conn.execute("SELECT day, hours FROM agent_hours WHERE agent = ? AND source = 'homebase' AND day >= ? AND day <= ? "
                            "AND hours > 0 ORDER BY day", (agent, lo.isoformat(), hb_hi.isoformat())).fetchall() if lo <= hb_hi else []
        cut_checked = False
        with conn:
            for r in rows:
                ref = f"hours:{r['day']}"
                if ref in have:
                    continue
                d = date.fromisoformat(r["day"])
                ft, _why = is_full_time(conn, agent, d, hr)
                if not ft:
                    continue
                earn = round(r["hours"] * rate_for(start, d), 4)
                if d >= CAP_EFFECTIVE:                   # the cap only applies from 2027
                    if not cut_checked:                  # December's rows are all in by now: settle the Jan 1 cut first
                        _settle_cut(conn, agent)
                        cut_checked = True
                    # the balance as of that day (PTO approved for later days isn't taken yet)
                    earn = min(earn, max(0.0, CAP_HOURS - balance(conn, agent, d)))
                if earn <= 0:
                    continue
                conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (agent, r["day"], "accrual", earn, f"{r['hours']:g} h worked x {rate_for(start, d) * 80:g}/80", ref, "system", _now()))
                total += earn
        # (2) time-clock days: recalculated over a rolling window, so punch fixes flow through
        c_lo = max([d for d in (hours_mod.CLOCK_FROM, elig, since, after_open, clock_last - timedelta(days=CLOCK_RECALC_DAYS)) if d])
        if c_lo <= clock_last:
            worked = {r["day"]: r["hours"] for r in conn.execute(
                "SELECT day, hours FROM agent_hours WHERE agent = ? AND source = 'clock' AND day >= ? AND day <= ? AND hours > 0",
                (agent, c_lo.isoformat(), clock_last.isoformat()))}
            # every clock day of the weeks touched, so a day knows what came earlier
            # in its Sun-Sat week even when that falls before the recalculation window
            wk = {r["day"]: r["hours"] for r in conn.execute(
                "SELECT day, hours FROM agent_hours WHERE agent = ? AND source = 'clock' AND day >= ? AND day <= ? AND hours > 0",
                (agent, week_sunday(c_lo).isoformat(), clock_last.isoformat()))}
            booked = {r["day"]: (r["id"], r["hours"]) for r in conn.execute(
                "SELECT id, day, hours FROM pto_ledger WHERE agent = ? AND kind = 'accrual' AND day >= ? AND day <= ?",
                (agent, c_lo.isoformat(), clock_last.isoformat()))}
            with conn:
                for iso in sorted(set(worked) | set(booked)):
                    d = date.fromisoformat(iso)
                    h = worked.get(iso, 0.0)
                    old_id, old = booked.get(iso, (None, 0.0))
                    earn = 0.0
                    sun = week_sunday(d).isoformat()
                    before = sum(v for k, v in wk.items() if sun <= k < iso)
                    counted = earning_hours(d, h, before)
                    if counted > 0 and is_full_time(conn, agent, d, hr)[0]:
                        earn = round(counted * rate_for(start, d), 4)
                        if d >= CAP_EFFECTIVE:
                            if not cut_checked:
                                _settle_cut(conn, agent)
                                cut_checked = True
                            earn = min(earn, max(0.0, CAP_HOURS - (balance(conn, agent, d) - old)))
                    earn = round(earn, 4)
                    if abs(earn - old) < 0.00005:
                        continue
                    note = f"{h:g} h worked (time clock) x {rate_for(start, d) * 80:g}/80"
                    if counted < h - 0.00005:
                        note = (f"{counted:g} of {h:g} h worked (time clock) x {rate_for(start, d) * 80:g}/80"
                                f" - only the first {WEEKLY_CAP_HOURS:g} h of the week earn PTO")
                    if old_id and earn > 0:
                        conn.execute("UPDATE pto_ledger SET hours = ?, note = ?, created_at = ? WHERE id = ?", (earn, note, _now(), old_id))
                    elif old_id:
                        conn.execute("DELETE FROM pto_ledger WHERE id = ?", (old_id,))
                    else:
                        conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (agent, iso, "accrual", earn, note, f"hours:{iso}", "system", _now()))
                    total += earn - old
        if abs(total) > 0.005:
            added[agent] = round(total, 2)
    if today >= CAP_EFFECTIVE:                   # everyone, accruing or not (part time, no start date…)
        with conn:
            for agent in agents:
                _settle_cut(conn, agent)
    return added


def _settle_cut(conn, agent: str) -> None:
    """The one-time Jan 1, 2027 cut. Its size is worked out from everything
    dated on or before Dec 31, 2026 (so PTO approved for January doesn't
    count yet, and December hours that arrive after New Year's are included):
    a balance above 80 then is brought down to 80 by a single 'cap:2027'
    adjust row dated 2027-01-01. Re-run safely: the row is corrected (or
    removed) if late December rows or edits change the year-end balance."""
    year_end = CAP_EFFECTIVE - timedelta(days=1)
    over = balance(conn, agent, year_end) - CAP_HOURS
    want = round(-over, 2) if over > 0.005 else 0.0
    row = conn.execute("SELECT id, hours FROM pto_ledger WHERE agent = ? AND ref = 'cap:2027'", (agent,)).fetchone()
    note = "Policy 4-09: balance cut to the 80-hour cap on Jan 1, 2027"
    if row and want == 0:
        conn.execute("DELETE FROM pto_ledger WHERE id = ?", (row["id"],))
    elif row and abs(row["hours"] - want) > 0.005:
        conn.execute("UPDATE pto_ledger SET hours = ?, created_at = ? WHERE id = ?", (want, _now(), row["id"]))
    elif not row and want < 0:
        conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (agent, CAP_EFFECTIVE.isoformat(), "adjust", want, note, "cap:2027", "system", _now()))


def summary(conn, agent: str, on: date | None = None) -> dict:
    """What the agent page and the Agents tab show."""
    ensure(conn)
    on = on or datetime.now(timezone.utc).astimezone(PACIFIC).date()
    hr = hr_row(conn, agent)
    start = date.fromisoformat(hr["start_date"]) if hr.get("start_date") else None
    elig = eligible_from(start)
    ft, why = is_full_time(conn, agent, on, hr)
    bal = balance(conn, agent)
    # hours spoken for: approved PTO still ahead plus requests waiting on a manager
    pending = conn.execute("SELECT COALESCE(SUM(hours), 0) FROM pto_ledger WHERE agent = ? AND kind = 'use' AND day > ?",
                           (agent, on.isoformat())).fetchone()[0]
    try:
        pending -= conn.execute("SELECT COALESCE(SUM(hours), 0) FROM time_off_requests WHERE agent = ? AND status = 'pending' AND kind IN ('pto','mixed')",
                                (agent,)).fetchone()[0]
    except sqlite3.OperationalError:               # requests table not created yet
        pass
    ytd_used = -conn.execute("SELECT COALESCE(SUM(hours), 0) FROM pto_ledger WHERE agent = ? AND kind = 'use' AND day >= ? AND day <= ?",
                             (agent, date(on.year, 1, 1).isoformat(), on.isoformat())).fetchone()[0]
    ytd_acc = conn.execute("SELECT COALESCE(SUM(hours), 0) FROM pto_ledger WHERE agent = ? AND kind = 'accrual' AND day >= ?",
                           (agent, date(on.year, 1, 1).isoformat())).fetchone()[0]
    if not start:
        status, detail = "no start date", "Accrual starts once a start date is on file."
    elif on < elig:
        status, detail = "probation", f"Accrual starts {elig:%b} {elig.day}, {elig.year} (180 days after {start:%b} {start.day}, {start.year})."
    elif not ft:
        status, detail = "part time", f"Not accruing: {why}; full time is 30+ hours a week."
    elif bal >= CAP_HOURS and on >= CAP_EFFECTIVE:
        status, detail = "at cap", "At the 80-hour cap — accrual resumes when some PTO is used."
    elif bal >= CAP_HOURS:
        status, detail = "accruing", (f"Above 80 h — still accruing until Jan 1, 2027, when balances are cut to 80 and the cap applies. "
                                      f"Earning {rate_for(start, on) * 40:g} hours per 40 worked (up to 40 hours a week; overtime doesn't earn PTO).")
    else:
        status, detail = "accruing", f"Earning {rate_for(start, on) * 40:g} hours per 40 worked, up to 40 hours a week; overtime doesn't earn PTO ({years_of_service(start, on):.1f} years of service)."
    return {"agent": agent, "balance": bal, "available": round(bal - abs(pending), 2), "pending_use": round(abs(pending), 2),
            "cap": CAP_HOURS, "start_date": start.isoformat() if start else None, "eligible_from": elig.isoformat() if elig else None,
            "years": round(years_of_service(start, on), 2) if start else None,
            "rate_per_80": rate_for(start, on) * 80 if start else None, "full_time": ft, "full_time_why": why,
            "status": status, "detail": detail, "ytd_accrued": round(ytd_acc, 2), "ytd_used": round(ytd_used, 2)}


def pending_hours(conn, agent: str) -> float:
    """PTO hours on requests still waiting for a manager (pto + the paid part of mixed)."""
    try:
        return round(conn.execute("SELECT COALESCE(SUM(hours), 0) FROM time_off_requests WHERE agent = ? AND status = 'pending' AND kind IN ('pto','mixed')",
                                  (agent,)).fetchone()[0], 2)
    except sqlite3.OperationalError:
        return 0.0


def projected(conn, agent: str, on: date) -> dict:
    """What the balance should be on `on`: today's balance (approved future
    PTO is already off it), minus requests still pending, plus what the agent
    will earn between now and the day before `on` at their recent pace
    (average daily hours over the last 8 weeks, no more than 40 a week,
    x their rate), honouring probation and, from 2027, the cap. A projection, not a promise."""
    ensure(conn)
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    bal = balance(conn, agent)
    pend = pending_hours(conn, agent)
    hr = hr_row(conn, agent)
    start = date.fromisoformat(hr["start_date"]) if hr.get("start_date") else None
    elig = eligible_from(start)
    ft, _why = is_full_time(conn, agent, today, hr)
    tot = conn.execute("SELECT COALESCE(SUM(hours), 0) FROM agent_hours WHERE agent = ? AND day > ? AND day <= ?",
                       (agent, (today - timedelta(days=56)).isoformat(), today.isoformat())).fetchone()[0]
    daily = tot / 56
    # Walk the days ahead with the balance as it will stand on each one: future
    # ledger rows (approved PTO) and pending requests land on their own days,
    # the Jan 1, 2027 cut happens if the walk crosses it, accrual is capped from
    # 2027. Whatever is dated on/after `on` is still taken off at the end —
    # it's spoken for.
    future: dict[str, float] = {}
    for r in conn.execute("SELECT day, SUM(hours) AS h FROM pto_ledger WHERE agent = ? AND day > ? GROUP BY day",
                          (agent, today.isoformat())):
        future[r["day"]] = r["h"]
    pend_on: dict[str, float] = {}
    try:
        for r in conn.execute("SELECT start_day, hours FROM time_off_requests WHERE agent = ? AND status = 'pending' "
                              "AND kind IN ('pto','mixed') AND hours > 0", (agent,)):
            k = max(r["start_day"], (today + timedelta(days=1)).isoformat())
            pend_on[k] = pend_on.get(k, 0.0) - r["hours"]
    except sqlite3.OperationalError:
        pass
    run = balance(conn, agent, today)
    ahead = cut = 0.0
    crosses = today < CAP_EFFECTIVE <= on

    def apply_cut() -> None:
        nonlocal run, cut
        if run > CAP_HOURS:
            cut = CAP_HOURS - run
            run = CAP_HOURS
    d = today + timedelta(days=1)
    while d < on:
        if d == CAP_EFFECTIVE and crosses:
            apply_cut()
        iso = d.isoformat()
        run += future.pop(iso, 0.0) + pend_on.pop(iso, 0.0)
        if start and ft and elig and d >= elig:
            pace = min(daily, WEEKLY_CAP_HOURS / 7) if d >= WEEKLY_CAP_FROM else daily   # overtime doesn't earn
            earn = pace * rate_for(start, d)
            if d >= CAP_EFFECTIVE:
                earn = min(earn, max(0.0, CAP_HOURS - run))
            earn = max(0.0, earn)
            run += earn
            ahead += earn
        d += timedelta(days=1)
    if crosses and on == CAP_EFFECTIVE:
        apply_cut()
    run += sum(future.values()) + sum(pend_on.values())    # still ahead: approved and pending PTO on/after `on`
    return {"on": on.isoformat(), "balance": bal, "pending": pend, "accrual_ahead": round(ahead, 2),
            "cap_cut": round(cut, 2), "projected": round(run, 2), "avg_weekly_h": round(daily * 7, 1),
            "days_ahead": max(0, (on - today).days)}


def ledger(conn, agent: str, limit: int = 200) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM pto_ledger WHERE agent = ? ORDER BY day DESC, id DESC LIMIT ?", (agent, limit))]


def adjust(conn, who: str, agent: str, hours: float, note: str, day: date | None = None, kind: str = "adjust") -> dict:
    ensure(conn)
    note = (note or "").strip()
    if not note:
        return {"error": "A note is required."}
    if kind not in ("adjust", "use", "opening"):
        return {"error": "bad kind"}
    with conn:
        conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (agent, (day or date.today()).isoformat(), kind, round(float(hours), 2), note, None, who, _now()))
    return {"ok": True, "balance": balance(conn, agent)}
