"""The agent page (/api/my/...): an agent's own numbers, schedule, clock, PTO, time off, renewal calculator and payroll.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import auth
from ..store import connect

from .common import (
    _client_ip, _comm_db, _comm_reports_dir, _inbound_split, _is_pre_go_live, _live_from,
    _load_config, _lookup_why, _mm_only, _perms, _revenue_data, _roster_names, _scorecard_calls,
    _settings, _user, _valid_range, app,
)
from .agent_metrics import api_quality, api_scorecard


# ---- time clock (agent) + timesheets / agent records (IS Managers) ----------
@app.get("/api/my/clock")
def api_my_clock(request: Request, as_agent: str = ""):
    from .. import timeclock
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        return timeclock.status(conn, _settings, name, _roster_names(conn) + [name])
    finally:
        conn.close()


@app.post("/api/my/clock/in")
def api_my_clock_in(request: Request):
    from .. import timeclock
    u = _user(request)
    name = u["agent"] if (u and u["kind"] == "agent") else None   # only the agent themself, never a preview
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeclock.clock_in(conn, _settings, name, _roster_names(conn) + [name],
                               _client_ip(request), request.headers.get("user-agent"))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/my/clock/out")
def api_my_clock_out(request: Request):
    from .. import timeclock
    u = _user(request)
    name = u["agent"] if (u and u["kind"] == "agent") else None
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeclock.clock_out(conn, name)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- time off requests ----
def _agent_self(request: Request) -> str | None:
    u = _user(request)
    return (u["agent"] or None) if (u and u["kind"] == "agent") else None


@app.get("/api/my/timeoff")
def api_my_timeoff(request: Request, as_agent: str = ""):
    from .. import pto, timeoff
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        return {"agent": name, "requests": timeoff.mine(conn, name), "pto": pto.summary(conn, name)}
    finally:
        conn.close()


@app.post("/api/my/timeoff")
def api_my_timeoff_create(request: Request, body: dict):
    from datetime import date

    from .. import shiftplan, timeoff
    name = _agent_self(request)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        try:
            a, b = date.fromisoformat(body["start"]), date.fromisoformat(body["end"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
        hours = body.get("hours")
        r = timeoff.create(conn, _settings, name, _roster_names(conn) + [name], a, b, body.get("kind") or "pto",
                           float(hours) if hours not in (None, "") else None, body.get("note") or "",
                           shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/my/timeoff/preview")
def api_my_timeoff_preview(request: Request, start: str = "", end: str = "", as_agent: str = ""):
    from datetime import date

    from .. import shiftplan, timeoff
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end or start)
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.preview(conn, _settings, name, _roster_names(conn) + [name], a, b, shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if not r.get("error") else 400)
    finally:
        conn.close()


@app.post("/api/my/timeoff/cancel")
def api_my_timeoff_cancel(request: Request, body: dict):
    from .. import timeoff
    name = _agent_self(request)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.cancel(conn, name, int(body.get("id") or 0))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- PTO ----
@app.get("/api/my/pto")
def api_my_pto(request: Request, as_agent: str = ""):
    from .. import pto
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        pto.accrue(conn, [name])
        d = pto.summary(conn, name)
        d["recent"] = pto.ledger(conn, name, 12)
        return d
    finally:
        conn.close()


@app.get("/api/my/tabs")
def api_my_tabs(request: Request, as_agent: str = ""):
    """Which of the agent page's sub-tabs this agent may see. For a
    user-manager previewing /?as=Name it is that agent's login's setting
    (all three when they have no login yet)."""
    u = _user(request)
    if u and u["kind"] == "agent":
        return {"agent": u["agent"], "tabs": [k for k in auth.AGENT_TAB_KEYS if k in u["permissions"]]}
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        rows = conn.execute("SELECT permissions FROM users WHERE kind = 'agent' AND agent = ? AND status != 'disabled'", (name,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"agent": name, "tabs": list(auth.AGENT_TAB_KEYS), "login": False}
    have = set()
    for r in rows:
        have |= set(auth.clean_agent_tabs(json.loads(r["permissions"] or "[]")))
    return {"agent": name, "tabs": [k for k in auth.AGENT_TAB_KEYS if k in have], "login": True}


@app.get("/api/my/renewal")
def api_my_renewal():
    """The Renewal Calculator's rules and pricing tables, read from the
    IS Renewal Calculator sheet (ten-minute cache, last good copy kept)."""
    from .. import renewal
    conn = connect(_settings.db_path)
    try:
        d = renewal.pricing(conn, _settings)
    finally:
        conn.close()
    return JSONResponse(d, status_code=503 if "error" in d else 200)


@app.get("/api/my/inbound")
def api_my_inbound(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own Inbound vs Other row plus the team row."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    d = _inbound_split(start_day, end_day)
    return {"start": d["start"], "end": d["end"], "agent": name,
            "rows": [r for r in d["rows"] if r["agent"] == name], "team": d["team"],
            "notes": d["notes"]}


@app.get("/api/my/schedule")
def api_my_schedule(request: Request, as_agent: str = ""):
    """The signed-in agent's shifts: last week, this week and every week the
    schedule sheet has after that."""
    from .. import schedule
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        # match the sheet's short names against the tracked roster only (every
        # Genesys user would make "Aaron" or "Alex" ambiguous), plus whoever is asking
        agents = [r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL "
            "AND (scorecard = 1 OR genesys_name = ?)", (name,))]
    finally:
        conn.close()
    try:
        from .. import shiftplan, timeoff
        conn = connect(_settings.db_path)
        try:
            dbw = shiftplan.published_weeks(conn)
            off = timeoff.approved_days(conn)
        finally:
            conn.close()
        return schedule.my_schedule(_settings, name, agents, db_weeks=dbw, time_off=off)
    except Exception as e:
        logging.getLogger("isadash.schedule").exception("schedule failed")
        return JSONResponse({"error": f"Couldn't read the schedule sheet: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/my/schedule_day")
def api_my_schedule_day(request: Request, day: str = "", as_agent: str = ""):
    """Who is on (and off) on one day — for coverage and shift swaps. Any
    signed-in agent may look; names are the tracked roster's."""
    from datetime import date

    from .. import schedule
    if not _my_agent(request, as_agent):
        return JSONResponse(_NO_AGENT, status_code=403)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "bad day"}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        agents = [r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL AND scorecard = 1")]
    finally:
        conn.close()
    try:
        from .. import shiftplan, timeoff
        conn = connect(_settings.db_path)
        try:
            dbw = shiftplan.published_weeks(conn)
            off = timeoff.approved_days(conn, d, d)
        finally:
            conn.close()
        return schedule.day_roster(_settings, d, agents, dbw, off)
    except Exception as e:
        logging.getLogger("isadash.schedule").exception("day roster failed")
        return JSONResponse({"error": f"Couldn't read the schedule sheet: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/my/ytd")
def api_my_ytd(request: Request, as_agent: str = ""):
    """The signed-in agent's own year so far: revenue and sales to date and
    their best day, week and month — the same numbers the Revenue tab shows
    for them (upsells in, renewals out)."""
    from ..periods import PACIFIC
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        row = conn.execute("SELECT fr_name FROM agent_map WHERE genesys_name = ?",
                           (name,)).fetchone()
    finally:
        conn.close()
    year = datetime.now(timezone.utc).astimezone(PACIFIC).year
    people = _revenue_data(year).get("_people") or {}
    me = people.get((row["fr_name"] if row and row["fr_name"] else name)) or people.get(name)
    if not me:
        # FieldRoutes sometimes spells a name differently ("Micah Guzman" for
        # "Micah De Guzman"): same first name + same final surname word, if unique
        parts = name.lower().split()
        close = [v for k, v in people.items()
                 if len(parts) >= 2 and k.lower().split()[:1] == parts[:1]
                 and k.lower().split()[-1:] == parts[-1:]]
        me = close[0] if len(close) == 1 else {}
    return {"year": year, "revenue": me.get("revenue", 0), "sales": me.get("sales", 0),
            "best_day": me.get("best_day"), "best_week": me.get("best_week"),
            "best_month": me.get("best_month")}


@app.get("/api/my/calls")
def api_my_calls(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own Genesys call handling, plus the team's."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        d = _scorecard_calls(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.scorecard").exception("my calls failed")
        return JSONResponse({"error": f"Genesys didn't answer ({type(e).__name__})."}, status_code=502)
    return {"me": d["agents"].get(name), "team": d["team"]}


@app.get("/api/my/mm")
def api_my_mm(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own MM, plus the team's."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        d = _mm_only(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.mastermetric").exception("my mm failed")
        return JSONResponse({"error": f"Couldn't build MM ({type(e).__name__})."}, status_code=502)
    return {"me": d["agents"].get(name), "team": d["team"]}


# ---- the agent page (ACCOUNTS_PLAN segment 4) -----------------------------
# Every /api/my/* feed works out WHO from the signed-in session, never from
# the request, so an agent login can only ever get its own numbers. The one
# exception is a user-manager previewing a page with ?as=Agent Name.
def _my_agent(request: Request, as_agent: str = "") -> str | None:
    u = _user(request)
    if u and u["kind"] == "agent":
        return u["agent"] or None
    if as_agent and "settings.users" in _perms(request):
        return as_agent.strip() or None
    return None


def _pay_period(which: str):
    """(start Sunday, end Saturday, through) for the 'current' or 'last' pay
    period, Pacific. `through` is the end, capped at today — the current
    period is measured as if it ended today."""
    from datetime import date, timedelta

    from ..periods import PACIFIC
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    end0 = date.fromisoformat(_settings.payroll_anchor_monday) - timedelta(days=2)
    n = ((today - end0).days + 13) // 14          # smallest n with end0 + 14n >= today
    end = end0 + timedelta(days=14 * n)
    if which == "last":
        end -= timedelta(days=14)
    return end - timedelta(days=13), end, min(end, today)


_NO_AGENT = {"error": "This login isn't linked to an agent yet."}


@app.get("/api/my/scorecard")
def api_my_scorecard(request: Request, start_day: str = "", end_day: str = "",
                     mode: str = "sold", as_agent: str = ""):
    """The signed-in agent's own IS #'s row, plus the pooled team row."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    d = api_scorecard(start_day=start_day, end_day=end_day,
                      mode="serviced" if mode == "serviced" else "sold")
    mine = [r for r in d["rows"] if r["agent"] == name]
    # everyone's sheet metrics too (the Genesys call columns and the quality
    # score stay the agent's own + the team's), for the expandable team row
    return {"start": d["start"], "end": d["end"], "agent": name,
            "rows": mine, "team": d["team"], "tiers": d["tiers"],
            "everyone": sorted(d["rows"], key=lambda r: -(r.get("rev") or 0)),
            "updated": _data_updated()}


def _data_updated() -> str | None:
    """When the data was last refreshed: the end of the last sync, falling
    back to the database file's own timestamp."""
    import json as _j

    from ..store import get_config
    try:
        conn = connect(_settings.db_path)
        try:
            raw = get_config(conn, "last_sync_steps")
        finally:
            conn.close()
        if raw:
            fin = (_j.loads(raw) or {}).get("finished")
            if fin:
                return fin
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(_settings.db_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


@app.get("/api/my/quality")
def api_my_quality(request: Request, start_day: str = "", end_day: str = "",
                   as_agent: str = ""):
    """VoiceOps quality for the signed-in agent and the whole team."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    q = api_quality(start_day=start_day, end_day=end_day)
    return {"me": (q["agents"].get(name) or {}).get("quality"),
            "team": q["teams"].get("total")}


@app.get("/api/my/power")
def api_my_power(request: Request, period: str = "current", as_agent: str = "",
                 dates_only: int = 0):
    """The Power Score ranking for the current or last pay period — serviced
    between, the same inputs the managers' Power Score tab uses. Only the
    columns the Power Score shows are sent."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    which = "last" if period == "last" else "current"
    start, end, _through = _pay_period(which)
    # the whole pay period, future days included: appointments still booked
    # inside it count as they will once serviced (projected), like the
    # managers' Power Score tab (Dallin, 2026-10-16)
    if dates_only:      # the page asks for the period first, to set its date picker
        return {"period": which, "start": start.isoformat(), "end": end.isoformat(),
                "period_end": end.isoformat(), "agent": name}
    d = api_scorecard(start_day=start.isoformat(), end_day=end.isoformat(),
                      mode="serviced")
    q = api_quality(start_day=start.isoformat(), end_day=end.isoformat())
    # only the metrics the Power Score uses (its rules are editable in Settings)
    conn = connect(_settings.db_path)
    try:
        ps = _load_config(conn)["power_score"]
    finally:
        conn.close()
    used = {x["metric"] for g in ("points", "penalties", "bonuses") for x in ps.get(g, [])}
    calls = mm = None
    if used & set(_PS_CALL_KEYS):
        try:
            calls = _scorecard_calls(start.isoformat(), end.isoformat())["agents"]
        except Exception:                       # noqa: BLE001 — Genesys down: those columns blank
            logging.getLogger("isadash.scorecard").exception("power score calls failed")
    if "mm" in used:
        try:
            mm = _mm_only(start.isoformat(), end.isoformat())["agents"]
        except Exception:                       # noqa: BLE001
            logging.getLogger("isadash.mastermetric").exception("power score mm failed")
    rows = []
    for r in d["rows"]:
        if not r["powerscore"]:
            continue
        row = {k: r.get(k) for k in ({"agent", "powerscore", "rev"} | used)}
        row["quality_score"] = (q["agents"].get(r["agent"]) or {}).get("quality")
        for k in _PS_CALL_KEYS:
            if k in used:
                row[k] = ((calls or {}).get(r["agent"]) or {}).get(k)
        if "mm" in used:
            row["mm"] = (mm or {}).get(r["agent"])
        rows.append(row)
    return {"period": which, "start": start.isoformat(), "end": end.isoformat(),
            "period_end": end.isoformat(), "agent": name, "rows": rows}


_PS_CALL_KEYS = ("calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s")


# --------------- Agent page -> Payroll (own report + own lookup) -------------
# Everything here is for ONE agent: `_my_agent` works out who from the
# signed-in session (never from the request), and every query is filtered to
# that name. An agent only ever sees FINAL pay periods — a draft run can still
# be re-run on its payroll Monday, so it isn't shown until that day passes.
_AGENT_TYPE_LABEL = {
    "initial": "New sale", "upsell": "Upsell", "restore": "Deduction given back",
    "trueup": "Contract value change", "transfer": "Account reassigned",
    "feeback": "Cancel fee paid - half back", "halfkeep": "Cancelled - half kept",
    "clawback": "Cancelled - deducted", "vest": "Vested",
    "baseline": "Manual payroll sheet",
}


_AGENT_STATE_LABEL = {
    "paid": "Paid - not vested yet", "vested": "Vested (locked)",
    "pending": "Not paid yet", "not_started": "Not earned yet",
    "clawed_back": "Deducted", "cancelled_unpaid": "Cancelled before first payment",
    "half_kept_fee_paid": "Cancelled - fee paid, half kept",
    "excluded_renewal": "Cancelled as renewal - kept",
    "excluded_moved": "Cancelled as moved - kept",
    "excluded_resigned": "Cancelled then re-signed - kept",
    "excluded_prestart": "Before your ISA start date",
}


_stmt_cache: dict = {}


def _us_date(v) -> str:
    """'2026-10-14' -> '10/14/2026' (blank stays blank)."""
    from datetime import date as _date
    s = str(v or "")[:10]
    try:
        return _date.fromisoformat(s).strftime("%m/%d/%Y")
    except ValueError:
        return s


def _agent_line_text(kind: str, amount: float, note: str = "", rate=None,
                     cancel_date: str = "", cancel_reason: str = "",
                     first_paid: str = "") -> str:
    """Plain-English explanation of one pay line. Built from the line's own
    fields; the engine's raw note is only mined for dates and numbers — it is
    never shown, since it carries manager emails, other reps' names (split
    changes) and internal state strings."""
    import re as _re
    note = note or ""
    if rate is None:
        m = _re.search(r"rate=([\d.]+)%|\(([\d.]+)%\)", note)
        if m:
            rate = float(m.group(1) or m.group(2)) / 100
    pct = f" at {float(rate) * 100:g}%" if rate not in (None, "") else ""
    if not cancel_date:
        m = _re.search(r"cancelled (\d{4}-\d{2}-\d{2})(?: \(([^)]*)\))?", note)
        if m:
            cancel_date, cancel_reason = m.group(1), cancel_reason or (m.group(2) or "")
    cxl = _us_date(cancel_date) or "(date not recorded)"
    why = cancel_reason if cancel_reason and cancel_reason != "(none)" else "no reason recorded"
    if kind == "initial":
        return ("First service completed and paid" +
                (f" {_us_date(first_paid)}" if first_paid else "") + f" - commission{pct}")
    if kind == "upsell":
        return f"Upsell credit{pct and ' -' + pct.replace(' at', '')} of the contract value increase"
    if kind == "restore":
        if "renewal" in note.lower():
            return "Cancel reason changed to Renewal - earlier deduction given back"
        return "Account reinstated - earlier deduction given back"
    if kind == "trueup":
        m = _re.search(r"contract value ([\d,.]+) -> ([\d,.]+)", note)
        if m:
            a, b = (float(x.replace(",", "")) for x in m.groups())
            return f"Contract value changed ${a:,.2f} -> ${b:,.2f} - commission adjusted{pct}"
        return f"Contract value changed - commission adjusted{pct}"
    if kind == "transfer":
        return ("Account reassigned to you - commission moved to you" if amount > 0
                else "Account reassigned to another rep - commission moved off")
    if kind == "feeback":
        m = _re.search(r"fee paid (\d{4}-\d{2}-\d{2})", note)
        when = f" {_us_date(m.group(1))}" if m else ""
        return f"Cancellation fee paid{when} after the deduction - half given back"
    if kind == "halfkeep":
        return f"Cancelled {cxl} and the cancellation fee was paid - you keep half"
    if kind == "clawback":
        return f"Cancelled {cxl} ({why}) before vesting - commission taken back"
    if kind == "vest":
        return "Vested - locked permanently, can never be deducted"
    if kind == "baseline":
        return "What the manual payroll sheet paid you, net of any deductions"
    return "Adjustment"


def _agent_final_periods() -> list[str]:
    """Statement periods an agent may see, newest first: paid from the app
    (on/after go-live), with a statement file, and FINAL — marked final, or
    its payroll Monday (period end + 2 days) has passed (Pacific)."""
    import re as _re
    from datetime import date as _date, timedelta as _td

    from ..periods import PACIFIC
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    final = set()
    conn = _comm_db()
    if conn:
        try:
            final = {r[0] for r in conn.execute(
                "SELECT period_end FROM payroll_runs WHERE status = 'final'")}
        except Exception:                        # older engine DB: no table yet
            final = set()
        finally:
            conn.close()
    out = []
    if _comm_reports_dir().is_dir():
        for f in _comm_reports_dir().glob("commission_statement_*.xlsx"):
            m = _re.fullmatch(r"commission_statement_(\d{4}-\d{2}-\d{2})\.xlsx", f.name)
            if not m or _is_pre_go_live(m.group(1)):
                continue
            p = m.group(1)
            if p in final or today > _date.fromisoformat(p) + _td(days=2):
                out.append(p)
    return sorted(set(out), reverse=True)


def _stmt_lines(period: str):
    """(commission rows, deduction rows) of a statement, all reps; cached by
    file mtime so the periods list doesn't re-read every workbook."""
    from openpyxl import load_workbook
    src = _comm_reports_dir() / f"commission_statement_{period}.xlsx"
    if not src.is_file():
        return None
    mt = src.stat().st_mtime
    hit = _stmt_cache.get(period)
    if hit and hit[0] == mt:
        return hit[1]
    wb = load_workbook(src, read_only=True, data_only=True)
    try:
        out = []
        for tab in ("Commissions", "Deductions"):
            it = wb[tab].iter_rows(values_only=True)
            next(it, None)
            out.append([tuple(r) for r in it if r and r[0] and r[4] is not None])
    finally:
        wb.close()
    if len(_stmt_cache) > 12:
        _stmt_cache.clear()
    _stmt_cache[period] = (mt, tuple(out))
    return _stmt_cache[period][1]


def _agent_totals(rep: str, period: str) -> dict | None:
    lines = _stmt_lines(period)
    if lines is None:
        return None
    comm = [r for r in lines[0] if r[0] == rep]
    ded = [r for r in lines[1] if r[0] == rep]
    tc = round(sum(float(r[4]) for r in comm), 2)
    td = round(sum(float(r[4]) for r in ded), 2)
    return {"commission": tc, "deductions": td, "payout": round(tc + td, 2),
            "commission_lines": len(comm), "deduction_lines": len(ded)}


@app.get("/api/my/payroll/periods")
def api_my_payroll_periods(request: Request, as_agent: str = ""):
    """The agent's final pay periods, newest first, with their own totals."""
    from datetime import date as _date, timedelta as _td
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    out = []
    for p in _agent_final_periods():
        t = _agent_totals(name, p)
        if t is None:
            continue
        end = _date.fromisoformat(p)
        out.append({"period_end": p, "start": (end - _td(days=13)).isoformat(), **t})
    return {"agent": name, "periods": out}


@app.get("/api/my/payroll/report.csv")
def api_my_payroll_report_csv(request: Request, period: str = "", as_agent: str = ""):
    """The agent's own commission report for one final pay period: totals,
    then every commission and deduction line, from the locked statement."""
    import csv
    import io
    import re as _re
    from datetime import date as _date, timedelta as _td

    from fastapi.responses import Response
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", period or ""):
        return JSONResponse({"error": "period must be YYYY-MM-DD"}, status_code=400)
    if period not in _agent_final_periods():
        return JSONResponse({"error": "That pay period isn't available yet."}, status_code=404)
    lines = _stmt_lines(period)
    comm = [r for r in lines[0] if r[0] == name]
    ded = [r for r in lines[1] if r[0] == name]
    t = _agent_totals(name, period)
    end = _date.fromisoformat(period)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Inside Sales Commission Report"])
    w.writerow(["Agent", name])
    w.writerow(["Pay period", f"{end - _td(days=13):%m/%d/%Y} - {end:%m/%d/%Y}"])
    w.writerow([])
    w.writerow(["Total commission", f"{t['commission']:.2f}", f"{t['commission_lines']} lines"])
    w.writerow(["Total deductions", f"{t['deductions']:.2f}", f"{t['deduction_lines']} lines"])
    w.writerow(["Commission payout", f"{t['payout']:.2f}"])
    w.writerow([])
    w.writerow(["Section", "Account #", "Customer", "Subscription #", "Plan",
                "Sold", "First Paid", "Contract Value", "Rate", "Amount",
                "Line Type", "Explanation", "Account Status Now",
                "Services Completed", "Coverage Paid", "Coverage Needed to Vest",
                "Cancel Date", "Cancel Reason"])
    num = lambda v: "" if v in (None, "") else f"{float(v):.2f}"
    for section, rows in (("Commission", comm), ("Deduction", ded)):
        for r in rows:
            amt = float(r[4])
            w.writerow([
                section, r[2] or "", r[3] or "", r[1] or "", r[9] or "",
                _us_date(r[10]), _us_date(r[11]), num(r[7]),
                "" if r[6] in (None, "") else f"{float(r[6]) * 100:g}%",
                f"{amt:.2f}", _AGENT_TYPE_LABEL.get(r[5], r[5] or ""),
                _agent_line_text(r[5], amt, r[20] or "", r[6], r[17] or "",
                                 r[18] or "", r[11] or ""),
                _AGENT_STATE_LABEL.get(r[16], r[16] or ""),
                "" if r[12] is None else r[12], num(r[13]), num(r[14]),
                _us_date(r[17]), "" if r[18] in (None, "(none)") else r[18]])
    u = _user(request)
    logging.getLogger("isadash.commissions").info(
        "agent report %s downloaded for %s by %s", period, name,
        (u or {}).get("email") or "?")
    safe = _re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),     # BOM: Excel opens it cleanly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="commission_report_{safe}_{period}.csv"',
                 "Cache-Control": "no-store"})


@app.get("/api/my/payroll/lookup")
def api_my_payroll_lookup(request: Request, q: str = "", as_agent: str = ""):
    """The agent's Commission Lookup — ONLY accounts tied to them: they sold
    it, are on a split/assignment, hold an upsell credit on it, or have ever
    had a pay line on it. Anything else simply isn't found. Shows their own
    share and their own pay history (final periods only); other reps on the
    account are never named."""
    import sqlite3 as _sq
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"results": []}
    conn = _comm_db()
    if conn is None:
        return {"error": "Commission data isn't available right now."}
    final = _agent_final_periods()
    # anything on/before this date is already on a finished payroll (or the
    # manual sheet, before go-live)
    from datetime import timedelta as _td
    last_final = final[0] if final else (_live_from() - _td(days=1)).isoformat()
    try:
        mine = """
            WITH mine AS (
              SELECT subscription_id FROM sub_status WHERE rep = :me
              UNION SELECT subscription_id FROM assignments
                    WHERE rep = :me AND removed_at IS NULL
              UNION SELECT subscription_id FROM upsells
                    WHERE rep = :me AND removed_at IS NULL
              UNION SELECT subscription_id FROM ledger
                    WHERE rep = :me AND kind != 'baseline')"""
        args = {"me": name}
        if q.isdigit():
            where = "(sub.customer_id = :q OR sub.subscription_id = :q)"
            args["q"] = q
        else:
            where = "cu.name LIKE :q ESCAPE '\\'"
            args["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        subs = conn.execute(mine + f"""
            SELECT sub.subscription_id, sub.customer_id, cu.name AS customer,
                   sub.service_type, sub.contract_value, sub.date_added,
                   sub.active, sub.date_cancelled AS cancel_date, sub.cancel_reason,
                   st.rep AS seller, st.state, st.rate, st.expected,
                   st.first_paid_date AS first_paid,
                   st.qualifying_completed AS qualifying, st.coverage_paid,
                   st.coverage_target, st.vested_date AS vested
            FROM subscriptions sub
            JOIN mine USING (subscription_id)
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            LEFT JOIN sub_status st USING (subscription_id)
            WHERE {where}
            ORDER BY sub.date_added DESC LIMIT 40""", args).fetchall()
        out = []
        for r in subs:
            d = dict(r)
            sid = d["subscription_id"]
            assigns = conn.execute(
                "SELECT rep, share, type FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL", (sid,)).fetchall()
            my_assign = [a for a in assigns if a["rep"] == name]
            ups = conn.execute(
                "SELECT amount, date FROM upsells WHERE subscription_id = ? "
                "AND rep = ? AND removed_at IS NULL", (sid, name)).fetchall()
            if my_assign:
                share = min(1.0, sum(float(a["share"]) for a in my_assign))
                kind = my_assign[0]["type"]
                pct = f"{share * 100:.0f}%"
                role = {"split": f"Split - your share {pct}",
                        "outbound": f"Shared with outbound - your share {pct}",
                        "rescue": "Rescued by you",
                        "reassign": "Reassigned to you"}.get(kind, f"Your share {pct}")
                if kind in ("rescue", "reassign") and share < 1:
                    role += f" - your share {pct}"
            elif d["seller"] == name and not assigns:
                share, role = 1.0, "You sold it"
            elif ups:
                share, role = 0.0, "Upsell credit"
            else:
                share, role = 0.0, "No longer on your account"
            # their own pay history: app payroll lines in final periods only.
            # Manual-sheet (pre-go-live) amounts are never shown (Dallin,
            # 2026-10-26) — only the fact that it was paid that way.
            hist, manual, pending = [], False, False
            for l in conn.execute(
                    "SELECT period_end, amount, kind, COALESCE(note,'') AS note "
                    "FROM ledger WHERE subscription_id = ? AND rep = ? "
                    "ORDER BY period_end, id", (sid, name)):
                if l["kind"] == "baseline":
                    manual = manual or float(l["amount"]) != 0
                    continue
                if l["period_end"] not in final:
                    pending = True
                    continue
                hist.append({"period_end": l["period_end"],
                             "label": _AGENT_TYPE_LABEL.get(l["kind"], "Adjustment"),
                             "amount": round(float(l["amount"]), 2),
                             "text": _agent_line_text(
                                 l["kind"], float(l["amount"]), l["note"],
                                 cancel_date=(d["cancel_date"] or "")[:10]
                                 if l["kind"] in ("clawback", "halfkeep") else "",
                                 cancel_reason=d["cancel_reason"] or "",
                                 first_paid=d["first_paid"] or "")})
            state = d["state"] or "not_started"
            why = _lookup_why(d)
            got = sum(h["amount"] for h in hist)
            fp = (d["first_paid"] or "")[:10]
            cx = (d["cancel_date"] or "")[:10]
            if role == "No longer on your account":
                why = ("This account is no longer credited to you; your pay "
                       "history on it is below.")
            elif (pending or (state in ("paid", "vested") and share > 0 and got <= 0
                              and fp and fp > last_final)
                  or (state in ("clawed_back", "half_kept_fee_paid")
                      and cx and cx > last_final)):
                why += " Not on a finished payroll yet - it will show on an upcoming one."
            mine_exp = (round(float(d["expected"]) * share, 2)
                        if d["expected"] is not None and share else None)
            out.append({
                "subscription_id": sid, "customer_id": d["customer_id"],
                "customer": d["customer"], "service_type": d["service_type"],
                "contract_value": d["contract_value"], "rate": d["rate"],
                "your_commission": mine_exp, "role": role,
                "upsell_credit": round(sum(float(u["amount"]) for u in ups), 2) if ups else None,
                "date_added": (d["date_added"] or "")[:10], "first_paid": fp,
                "qualifying": d["qualifying"], "coverage_paid": d["coverage_paid"],
                "coverage_target": d["coverage_target"],
                "vested": (d["vested"] or "")[:10], "active": bool(d["active"]),
                "cancel_date": cx, "cancel_reason": d["cancel_reason"],
                "state": state, "state_label": _AGENT_STATE_LABEL.get(state, state),
                "why": why, "history": hist, "manual_paid": manual,
                "paid_to_you": round(got, 2) if hist else None})
        synced = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"results": out, "data_as_of": synced["value"] if synced else None}
    except _sq.OperationalError:
        logging.getLogger("isadash.commissions").exception("agent lookup failed")
        return {"error": "Lookup failed - try again in a minute."}
    finally:
        conn.close()
