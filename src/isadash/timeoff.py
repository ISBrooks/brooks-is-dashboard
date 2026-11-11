"""Time-off requests and approvals (docs/TIMESHEET_PLAN.md segment 3).

An agent asks for a date range as PTO or unpaid time. A manager approves or
denies. Approving a PTO request writes one 'use' row per day to pto_ledger
(negative hours, ref 'req:<id>') — split by the scheduled hours of each day
when a schedule is posted, otherwise evenly — and the days show as PTO on
the agent's schedule, the day roster and the builder; the time clock treats
them as unscheduled. Revoking an approval removes those ledger rows again.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import pto
from . import schedule as sched

SCHEMA = """
CREATE TABLE IF NOT EXISTS time_off_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL,
    start_day     TEXT NOT NULL,
    end_day       TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- pto | unpaid | mixed
    hours         REAL,                     -- PTO hours (the paid part)
    unpaid_hours  REAL,                     -- the unpaid part of a mixed / unpaid request
    note          TEXT,
    status        TEXT NOT NULL,            -- pending | approved | denied | cancelled
    requested_at  TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    decision_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_tor_agent ON time_off_requests(agent, start_day);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    pto.ensure(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(time_off_requests)")}
    if "unpaid_hours" not in cols:
        conn.execute("ALTER TABLE time_off_requests ADD COLUMN unpaid_hours REAL")
    for c in ("entered_by", "edited_by", "edited_at"):     # manager-entered / manager-edited requests
        if c not in cols:
            conn.execute(f"ALTER TABLE time_off_requests ADD COLUMN {c} TEXT")
            conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def scheduled_hours_by_day(settings, agent: str, agents: list[str], days: list[date], db_weeks: dict | None) -> dict[str, float]:
    """Hours on the posted schedule for each day (0 when not scheduled / not posted)."""
    out: dict[str, float] = {}
    for d in days:
        try:
            roster = sched.day_roster(settings, d, agents, db_weeks)
        except Exception:                      # noqa: BLE001 — no schedule reachable: treat as 0
            roster = {"on": []}
        h = 0.0
        for r in roster.get("on", []):
            if r["name"] == agent:
                h = float(r["hours"] or 0)
        out[d.isoformat()] = h
    return out


def create(conn, settings, agent: str, agents: list[str], start: date, end: date, kind: str, hours: float | None,
           note: str, db_weeks: dict | None) -> dict:
    ensure(conn)
    if end < start:
        return {"error": "The end date is before the start date."}
    if (end - start).days > 60:
        return {"error": "One request can cover at most 60 days."}
    if kind not in ("pto", "unpaid"):
        return {"error": "Kind must be PTO or unpaid."}
    by_day = scheduled_hours_by_day(settings, agent, agents, _days(start, end), db_weeks)
    asked = float(hours) if hours not in (None, "") else (sum(by_day.values()) or 8.0 * sum(1 for d in _days(start, end) if d.weekday() < 5))
    asked = round(asked, 2)
    if asked <= 0:
        return {"error": "Hours must be more than zero."}
    unpaid = None
    message = None
    if kind == "pto":
        # what they'll have by the first day off: today's balance less pending requests, plus what they earn until then
        proj = pto.projected(conn, agent, start)
        avail = max(0.0, proj["projected"])
        if asked > avail + 0.005:
            # not enough PTO: the request goes in as a mix, paid up to what's available
            paid = round(avail, 2)
            unpaid = round(asked - paid, 2)
            kind = "mixed" if paid > 0 else "unpaid"
            hours = paid if paid > 0 else None
            earn = f" (counting the {proj['accrual_ahead']:g} hours you should earn by then)" if proj["accrual_ahead"] > 0 else ""
            if proj.get("cap_cut"):
                earn += " (after the Jan 1, 2027 cut to the 80-hour cap)"
            message = (f"You should have {paid:g} hours of PTO by {start:%b} {start.day}{earn}, so {paid:g} hours of this request will be PTO and the other "
                       f"{unpaid:g} hours unpaid." if paid > 0 else
                       f"You have no PTO available for {start:%b} {start.day}, so this request was submitted as {unpaid:g} hours of unpaid time off.")
        else:
            hours = asked
    else:
        hours, unpaid = None, asked
    overlap = conn.execute(
        "SELECT 1 FROM time_off_requests WHERE agent = ? AND status IN ('pending','approved') AND NOT (end_day < ? OR start_day > ?)",
        (agent, start.isoformat(), end.isoformat())).fetchone()
    if overlap:
        return {"error": "You already have a request covering some of those days."}
    with conn:
        cur = conn.execute(
            "INSERT INTO time_off_requests (agent, start_day, end_day, kind, hours, unpaid_hours, note, status, requested_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (agent, start.isoformat(), end.isoformat(), kind, hours, unpaid, (note or "").strip() or None, "pending", _now()))
    return {"ok": True, "id": cur.lastrowid, "kind": kind, "hours": hours, "unpaid_hours": unpaid, "message": message}


def preview(conn, settings, agent: str, agents: list[str], start: date, end: date, db_weeks: dict | None) -> dict:
    """What the request form shows before sending: scheduled hours per day,
    the projected PTO balance on the start date, and who else is already off
    (approved) or waiting (pending) on each of those days."""
    ensure(conn)
    if end < start or (end - start).days > 60:
        return {"error": "Pick a start and end date (at most 60 days apart)."}
    days = _days(start, end)
    by_day = scheduled_hours_by_day(settings, agent, agents, days, db_weeks)
    sched = round(sum(by_day.values()), 2)
    default_h = sched or round(8.0 * sum(1 for d in days if d.weekday() < 5), 2)
    others: dict[str, list] = {d.isoformat(): [] for d in days}
    for r in conn.execute(
            "SELECT agent, start_day, end_day, kind, status FROM time_off_requests WHERE agent != ? AND status IN ('pending','approved') "
            "AND NOT (end_day < ? OR start_day > ?) ORDER BY agent", (agent, start.isoformat(), end.isoformat())):
        for d in _days(max(start, date.fromisoformat(r["start_day"])), min(end, date.fromisoformat(r["end_day"]))):
            others[d.isoformat()].append({"name": r["agent"], "status": r["status"], "kind": r["kind"]})
    proj = pto.projected(conn, agent, start)
    return {"start": start.isoformat(), "end": end.isoformat(), "scheduled_h": sched, "default_h": default_h,
            "days": [{"day": d.isoformat(), "hours": by_day[d.isoformat()], "others": others[d.isoformat()]} for d in days],
            "projected": proj, "posted": any(v > 0 for v in by_day.values())}


def cancel(conn, agent: str, req_id: int) -> dict:
    ensure(conn)
    r = conn.execute("SELECT * FROM time_off_requests WHERE id = ? AND agent = ?", (req_id, agent)).fetchone()
    if not r:
        return {"error": "No such request."}
    if r["status"] != "pending":
        return {"error": "Only a pending request can be cancelled — ask a manager to revoke an approved one."}
    with conn:
        conn.execute("UPDATE time_off_requests SET status = 'cancelled', decided_at = ? WHERE id = ?", (_now(), req_id))
    return {"ok": True}


def decide(conn, settings, who: str, req_id: int, approve: bool, note: str, agents: list[str], db_weeks: dict | None) -> dict:
    ensure(conn)
    r = conn.execute("SELECT * FROM time_off_requests WHERE id = ?", (req_id,)).fetchone()
    if not r:
        return {"error": "No such request."}
    if r["status"] not in ("pending", "approved", "denied"):
        return {"error": "That request was cancelled."}
    status = "approved" if approve else "denied"
    with conn:
        # any earlier approval's ledger rows go first (re-deciding is allowed)
        conn.execute("DELETE FROM pto_ledger WHERE kind = 'use' AND ref = ?", (f"req:{req_id}",))
        conn.execute("UPDATE time_off_requests SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                     (status, who, _now(), (note or "").strip() or None, req_id))
        if approve and r["kind"] in ("pto", "mixed") and r["hours"]:
            days = _days(date.fromisoformat(r["start_day"]), date.fromisoformat(r["end_day"]))
            by_day = scheduled_hours_by_day(settings, r["agent"], agents, days, db_weeks)
            total_sched = sum(by_day.values())
            if total_sched > 0:
                weights = {d: by_day[d] / total_sched for d in by_day}
            else:
                wk = [d for d in days if d.weekday() < 5] or days
                weights = {d.isoformat(): (1 / len(wk) if d in wk else 0.0) for d in days}
            for d in days:
                h = round(r["hours"] * weights.get(d.isoformat(), 0.0), 2)
                if h > 0:
                    conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (r["agent"], d.isoformat(), "use", -h, f"PTO {r['start_day']} – {r['end_day']}"
                                  + (f" ({r['unpaid_hours']:g} h of it unpaid)" if r["unpaid_hours"] else ""), f"req:{req_id}", who, _now()))
    return {"ok": True, "status": status, "balance": pto.balance(conn, r["agent"])}


def revoke(conn, who: str, req_id: int, note: str) -> dict:
    """Undo an approval (the agent didn't take the time after all)."""
    ensure(conn)
    with conn:
        conn.execute("DELETE FROM pto_ledger WHERE kind = 'use' AND ref = ?", (f"req:{req_id}",))
        conn.execute("UPDATE time_off_requests SET status = 'cancelled', decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                     (who, _now(), (note or "").strip() or "approval revoked", req_id))
    return {"ok": True}


def manager_save(conn, settings, who: str, req_id: int | None, agent: str, agents: list[str], start: date, end: date,
                 kind: str, hours, unpaid_hours, note: str, status: str, decision_note: str | None,
                 db_weeks: dict | None) -> dict:
    """A manager enters or changes a request with full control: dates, kind
    (pto | unpaid | mixed), the exact PTO / unpaid hours, the agent's note and
    the status (approved | pending | denied). Unlike an agent's request there
    is no automatic PTO/unpaid split — PTO may take a balance below zero (the
    result says so). Approved PTO is re-written to the ledger from the new
    values; pending / denied takes it off again."""
    ensure(conn)
    if end < start:
        return {"error": "The end date is before the start date."}
    if (end - start).days > 60:
        return {"error": "One request can cover at most 60 days."}
    if kind not in ("pto", "unpaid", "mixed"):
        return {"error": "Type must be PTO, unpaid, or PTO + unpaid."}
    if status not in ("approved", "pending", "denied"):
        return {"error": "Status must be approved, pending or denied."}
    old = None
    if req_id:
        old = conn.execute("SELECT * FROM time_off_requests WHERE id = ?", (req_id,)).fetchone()
        if not old:
            return {"error": "No such request."}
        agent = old["agent"]                      # the agent never changes on an edit
    agent = (agent or "").strip()
    if not agent:
        return {"error": "Pick the agent."}

    def num(v):
        if v in (None, ""):
            return None
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return "bad"
    h, u = num(hours), num(unpaid_hours)
    if "bad" in (h, u):
        return {"error": "Hours must be numbers."}
    if kind != "mixed" and h is None and u is None:          # default: the scheduled hours, else 8 per weekday
        by_day = scheduled_hours_by_day(settings, agent, agents, _days(start, end), db_weeks)
        dflt = round(sum(by_day.values()) or 8.0 * sum(1 for d in _days(start, end) if d.weekday() < 5), 2)
        h = u = dflt
    if kind == "pto":
        h, u = (h if h is not None else u), None
    elif kind == "unpaid":
        h, u = None, (u if u is not None else h)
    else:
        if not h or not u:
            return {"error": "For PTO + unpaid, give both the PTO hours and the unpaid hours."}
    if (h is not None and h <= 0) or (u is not None and u <= 0):
        return {"error": "Hours must be more than zero."}
    if status in ("approved", "pending"):
        overlap = conn.execute(
            "SELECT start_day, end_day FROM time_off_requests WHERE agent = ? AND id != ? AND status IN ('pending','approved') "
            "AND NOT (end_day < ? OR start_day > ?)", (agent, req_id or 0, start.isoformat(), end.isoformat())).fetchone()
        if overlap:
            return {"error": f"{agent} already has a request covering some of those days ({overlap['start_day']} – {overlap['end_day']}). "
                             "Edit that one instead."}
    now = _now()
    note = (note or "").strip() or None
    with conn:
        if old:
            conn.execute("UPDATE time_off_requests SET start_day = ?, end_day = ?, kind = ?, hours = ?, unpaid_hours = ?, note = ?, "
                         "status = 'pending', edited_by = ?, edited_at = ? WHERE id = ?",
                         (start.isoformat(), end.isoformat(), kind, h, u, note, who, now, req_id))
        else:
            req_id = conn.execute(
                "INSERT INTO time_off_requests (agent, start_day, end_day, kind, hours, unpaid_hours, note, status, requested_at, entered_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (agent, start.isoformat(), end.isoformat(), kind, h, u, note, "pending", now, who)).lastrowid
        if status == "pending":                   # no decision: nothing on the ledger
            conn.execute("DELETE FROM pto_ledger WHERE kind = 'use' AND ref = ?", (f"req:{req_id}",))
            conn.execute("UPDATE time_off_requests SET decided_by = NULL, decided_at = NULL, decision_note = ? WHERE id = ?",
                         ((decision_note or "").strip() or None, req_id))
    if status != "pending":
        dn = decision_note if decision_note is not None else (old["decision_note"] if old else None)
        r = decide(conn, settings, who, req_id, status == "approved", dn or "", agents, db_weeks)
        if not r.get("ok"):
            return r
    bal = pto.balance(conn, agent)
    out = {"ok": True, "id": req_id, "balance": bal}
    if bal < 0:
        out["warning"] = f"{agent}'s PTO balance is now {bal:.2f} h."
    return out


def mine(conn, agent: str, limit: int = 50) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM time_off_requests WHERE agent = ? ORDER BY start_day DESC, id DESC LIMIT ?", (agent, limit))]


def queue(conn, from_day: date | None = None) -> dict:
    """Pending requests, plus approved time off from `from_day` on (default: 2 weeks back)."""
    ensure(conn)
    lo = (from_day or (date.today() - timedelta(days=14))).isoformat()
    pend = [dict(r) for r in conn.execute("SELECT * FROM time_off_requests WHERE status = 'pending' ORDER BY start_day, id")]
    upcoming = [dict(r) for r in conn.execute(
        "SELECT * FROM time_off_requests WHERE status = 'approved' AND end_day >= ? ORDER BY start_day, agent", (lo,))]
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM time_off_requests WHERE status IN ('denied','cancelled') AND decided_at >= ? ORDER BY decided_at DESC LIMIT 30",
        ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),))]
    for lst in (pend, upcoming):
        for r in lst:
            r["balance"] = pto.balance(conn, r["agent"])
    return {"pending": pend, "approved": upcoming, "recent": recent}


def approved_days(conn, lo: date | None = None, hi: date | None = None) -> dict[str, dict[str, str]]:
    """{agent: {day_iso: kind}} for approved time off — laid over the schedule."""
    ensure(conn)
    out: dict[str, dict[str, str]] = {}
    q = "SELECT agent, start_day, end_day, kind FROM time_off_requests WHERE status = 'approved'"
    args: tuple = ()
    if lo and hi:
        q += " AND NOT (end_day < ? OR start_day > ?)"
        args = (lo.isoformat(), hi.isoformat())
    for r in conn.execute(q, args):
        for d in _days(date.fromisoformat(r["start_day"]), date.fromisoformat(r["end_day"])):
            out.setdefault(r["agent"], {})[d.isoformat()] = r["kind"]
    return out
