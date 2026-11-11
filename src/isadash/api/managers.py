"""Schedule tab (IS Managers): schedule builder, templates, Saturdays, timesheets, adherence, time off, PTO, agent records.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from ..store import connect

from .common import _payroll_periods, _roster_names, _settings, _user, _valid_range, app


# ---- schedule builder (IS Managers -> Schedule) ----
@app.get("/api/managers/schedule")
def api_managers_schedule(monday: str = ""):
    from datetime import date

    from .. import shiftplan
    try:
        from ..periods import CLOCK_TZ
        m = date.fromisoformat(monday) if monday else shiftplan.monday_of(datetime.now(timezone.utc).astimezone(CLOCK_TZ).date())
    except ValueError:
        return JSONResponse({"error": "bad monday"}, status_code=400)
    m = shiftplan.monday_of(m)
    conn = connect(_settings.db_path)
    try:
        from .. import timeoff
        d = shiftplan.week(conn, m, _roster_names(conn), timeoff.approved_days(conn, m, m + __import__("datetime").timedelta(days=6)))
    finally:
        conn.close()
    # does the sheet have this week (so "Start from the sheet" can be offered)?
    try:
        from .. import schedule
        d["sheet_has_week"] = m.isoformat() in schedule.website_schedule(_settings)["weeks"]
    except Exception:
        d["sheet_has_week"] = False
    return d


@app.post("/api/managers/schedule/shift")
def api_managers_schedule_shift(request: Request, body: dict):
    from datetime import date

    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        from .. import timeoff
        day = date.fromisoformat(body["day"])
        r = shiftplan.set_shift(conn, _who(request), str(body.get("agent") or ""), day, body.get("cell"),
                                timeoff.approved_days(conn, day, day))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/move")
def api_managers_schedule_move(request: Request, body: dict):
    from datetime import date

    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        from .. import timeoff
        to_day = date.fromisoformat(body["to_day"])
        r = shiftplan.move_shift(conn, _who(request), body["agent"], date.fromisoformat(body["day"]),
                                 body["to_agent"], to_day, bool(body.get("copy")), timeoff.approved_days(conn, to_day, to_day))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/week")
def api_managers_schedule_week(request: Request, body: dict):
    """action: create (source blank|last|template|sheet) | delete"""
    from datetime import date

    from .. import schedule, shiftplan
    m = shiftplan.monday_of(date.fromisoformat(body["monday"]))
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "delete":
            r = shiftplan.delete_week(conn, m)
        else:
            src = body.get("source") or "blank"
            sheet_rows = None
            if src == "sheet":
                sheet_rows = schedule.website_schedule(_settings)["weeks"].get(m.isoformat(), {})
            from datetime import timedelta

            from .. import timeoff
            r = shiftplan.create_week(conn, _who(request), m, src, body.get("template_id"), _roster_names(conn), sheet_rows,
                                      timeoff.approved_days(conn, m, m + timedelta(days=6)))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/targets")
def api_managers_schedule_targets(request: Request, body: dict):
    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.set_coverage_targets(conn, body.get("targets") or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/publish")
def api_managers_schedule_publish(request: Request, body: dict):
    from datetime import date

    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.publish(conn, _who(request), shiftplan.monday_of(date.fromisoformat(body["monday"])), bool(body.get("on")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/template")
def api_managers_schedule_template(request: Request, body: dict):
    """action: save (name, monday) | delete (id)"""
    from datetime import date

    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "delete":
            r = shiftplan.delete_template(conn, int(body["id"]))
        else:
            r = shiftplan.save_template(conn, _who(request), body.get("name") or "", shiftplan.monday_of(date.fromisoformat(body["monday"])))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- who's on the clock right now (Schedule tab side box) ----
@app.get("/api/managers/live")
def api_managers_live():
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        return timeclock.live_now(conn, _settings, _roster_names(conn))
    finally:
        conn.close()


# ---- templates screen (IS Managers -> Templates) ----
@app.get("/api/managers/templates")
def api_managers_templates():
    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        return {"templates": shiftplan.template_list(conn)}
    finally:
        conn.close()


@app.get("/api/managers/templates/one")
def api_managers_template_one(id: int = 0):
    from .. import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.template_get(conn, id, _roster_names(conn))
        return JSONResponse(r, status_code=404 if r.get("error") else 200)
    finally:
        conn.close()


@app.post("/api/managers/templates")
def api_managers_templates_post(request: Request, body: dict):
    """action: create (name, source blank|week|copy, monday, from_id) | rename (id, name) | delete (id)
    | set (id, agent, dows, cell) | move (id, agent, dow, to_agent, to_dow, copy) | clear_agent (id, agent)"""
    from datetime import date

    from .. import shiftplan
    who = _who(request)
    act = body.get("action")
    conn = connect(_settings.db_path)
    try:
        if act == "create":
            m = None
            if body.get("monday"):
                try:
                    m = date.fromisoformat(body["monday"])
                except ValueError:
                    return JSONResponse({"error": "Bad date."}, status_code=400)
            r = shiftplan.template_create(conn, who, body.get("name") or "", body.get("source") or "blank", m, body.get("from_id"))
        elif act == "rename":
            r = shiftplan.template_rename(conn, who, body.get("id"), body.get("name") or "")
        elif act == "delete":
            r = shiftplan.delete_template(conn, int(body.get("id") or 0))
        elif act == "set":
            dows = body.get("dows")
            if dows is None:
                dows = [body.get("dow")]
            try:
                dows = [int(k) for k in dows]
            except (TypeError, ValueError):
                return JSONResponse({"error": "Bad day."}, status_code=400)
            r = shiftplan.template_set(conn, who, body.get("id"), str(body.get("agent") or ""), dows, body.get("cell"))
        elif act == "move":
            r = shiftplan.template_move(conn, who, body.get("id"), str(body.get("agent") or ""), int(body.get("dow") or 0),
                                        str(body.get("to_agent") or ""), int(body.get("to_dow") or 0), bool(body.get("copy")))
        elif act == "clear_agent":
            r = shiftplan.template_clear_agent(conn, who, body.get("id"), str(body.get("agent") or ""))
        else:
            r = {"error": "Unknown action."}
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- Saturday rotation tracker (Schedule tab, below the grid) ----
@app.get("/api/managers/saturdays")
def api_managers_saturdays():
    from .. import saturdays
    conn = connect(_settings.db_path)
    try:
        return saturdays.tracker(conn, _roster_names(conn))
    finally:
        conn.close()


@app.post("/api/managers/saturdays")
def api_managers_saturdays_post(request: Request, body: dict):
    """action: mark (agent, day, worked true|false|null) | regular (agent, on)"""
    from datetime import date

    from .. import saturdays
    agent = str(body.get("agent") or "").strip()
    if not agent:
        return JSONResponse({"error": "No agent given."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "regular":
            r = saturdays.set_regular(conn, agent, bool(body.get("on")))
        elif body.get("action") == "mark":
            try:
                day = date.fromisoformat(body.get("day") or "")
            except ValueError:
                return JSONResponse({"error": "Bad date."}, status_code=400)
            w = body.get("worked")
            r = saturdays.mark(conn, _who(request), agent, day, None if w is None else bool(w))
        else:
            r = {"error": "Unknown action."}
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/timeoff")
def api_managers_timeoff(agent: str = ""):
    """The queue, the roster (for "Add time off"), and — when an agent is
    picked — every request that agent has ever had (to edit older ones)."""
    from .. import pto, timeoff
    conn = connect(_settings.db_path)
    try:
        d = timeoff.queue(conn)
        d["agents"] = sorted(_roster_names(conn))
        if agent:
            d["history"] = timeoff.mine(conn, agent, 200)
            d["history_agent"] = agent
            d["history_balance"] = pto.balance(conn, agent)
        return d
    finally:
        conn.close()


@app.get("/api/managers/timeoff/preview")
def api_managers_timeoff_preview(agent: str = "", start: str = "", end: str = ""):
    """The add/edit form's helper line: scheduled hours on those days, the
    projected PTO balance on the first day, and who else is off."""
    from datetime import date

    from .. import shiftplan, timeoff
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end or start)
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    if not agent:
        return JSONResponse({"error": "Pick the agent."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.preview(conn, _settings, agent, _roster_names(conn) + [agent], a, b, shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if not r.get("error") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/save")
def api_managers_timeoff_save(request: Request, body: dict):
    """Add (no id) or change (id) a time-off request, any status — see timeoff.manager_save."""
    from datetime import date

    from .. import shiftplan, timeoff
    try:
        a, b = date.fromisoformat(body.get("start") or ""), date.fromisoformat(body.get("end") or body.get("start") or "")
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    try:
        req_id = int(body["id"]) if body.get("id") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "Bad request id."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        r = timeoff.manager_save(conn, _settings, _who(request), req_id, agent, _roster_names(conn) + ([agent] if agent else []),
                                 a, b, body.get("kind") or "pto", body.get("hours"), body.get("unpaid_hours"),
                                 body.get("note") or "", body.get("status") or "approved", body.get("decision_note"),
                                 shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/decide")
def api_managers_timeoff_decide(request: Request, body: dict):
    from .. import shiftplan, timeoff
    conn = connect(_settings.db_path)
    try:
        r = timeoff.decide(conn, _settings, _who(request), int(body.get("id") or 0), bool(body.get("approve")),
                           body.get("note") or "", _roster_names(conn), shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/revoke")
def api_managers_timeoff_revoke(request: Request, body: dict):
    from .. import timeoff
    conn = connect(_settings.db_path)
    try:
        r = timeoff.revoke(conn, _who(request), int(body.get("id") or 0), body.get("note") or "")
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/pto")
def api_managers_pto():
    from .. import pto
    conn = connect(_settings.db_path)
    try:
        names = _roster_names(conn)
        pto.accrue(conn, names)
        return {"agents": [pto.summary(conn, a) for a in names]}
    finally:
        conn.close()


@app.get("/api/managers/pto/ledger")
def api_managers_pto_ledger(agent: str = ""):
    from .. import pto
    conn = connect(_settings.db_path)
    try:
        return {"agent": agent, "rows": pto.ledger(conn, agent, 500)}
    finally:
        conn.close()


@app.post("/api/managers/pto/adjust")
def api_managers_pto_adjust(request: Request, body: dict):
    from datetime import date

    from .. import pto
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        try:
            hours = float(body.get("hours"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Hours must be a number (negative to take hours away)."}, status_code=400)
        day = date.fromisoformat(body["day"]) if body.get("day") else None
        r = pto.adjust(conn, _who(request), agent, hours, body.get("note") or "", day, body.get("kind") or "adjust")
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


def _who(request: Request) -> str:
    u = _user(request)
    return (u["email"] if u else None) or "admin"


@app.get("/api/managers/timesheet")
def api_managers_timesheet(start_day: str = "", end_day: str = ""):
    from datetime import date

    from .. import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.timesheet(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                   _roster_names(conn))
    finally:
        conn.close()


@app.get("/api/managers/timesheet.csv")
def api_managers_timesheet_csv(start_day: str = "", end_day: str = ""):
    from datetime import date

    from fastapi.responses import Response

    from .. import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        ts = timeclock.timesheet(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                 _roster_names(conn))
    finally:
        conn.close()
    return Response(timeclock.timesheet_csv(ts), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="timesheet_{start_day}_{end_day}.csv"'})


def _hours_report(start_day: str, end_day: str):
    """(report, None) or (None, error response). No dates = the latest closed pay period."""
    from datetime import date
    from .. import timeclock
    if not start_day and not end_day:
        closed_end, closed_start, _run = _payroll_periods()
        start_day, end_day = closed_start.isoformat(), closed_end.isoformat()
    if not _valid_range(start_day, end_day) or (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days > 92:
        return None, JSONResponse({"error": "Pick a date range of at most 93 days."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.hours_report(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                      _roster_names(conn)), None
    finally:
        conn.close()


@app.get("/api/managers/hours_report")
def api_managers_hours_report(start_day: str = "", end_day: str = ""):
    """Regular / daily OT / weekly OT / PTO hours per agent for a date range (payroll)."""
    rep, err = _hours_report(start_day, end_day)
    return err or rep


@app.get("/api/managers/hours_report.csv")
def api_managers_hours_report_csv(start_day: str = "", end_day: str = ""):
    from fastapi.responses import Response

    from .. import timeclock
    rep, err = _hours_report(start_day, end_day)
    if err:
        return err
    return Response(timeclock.hours_report_csv(rep), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="hours_report_{rep["start"]}_{rep["end"]}.csv"'})


@app.post("/api/managers/punch")
def api_managers_punch(request: Request, body: dict):
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.edit_punch(conn, _who(request), body.get("id"), body.get("agent"), body.get("in_at"),
                                 body.get("out_at"), body.get("reason") or "", bool(body.get("delete")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/adherence")
def api_managers_adherence(start_day: str = "", end_day: str = ""):
    from datetime import date

    from .. import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.adherence(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                   _roster_names(conn))
    finally:
        conn.close()


@app.get("/api/managers/clock_rules")
def api_managers_clock_rules():
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"rules": timeclock.clock_rules(conn), "auto": timeclock.auto_rules(conn),
                "overrides": timeclock.overrides(conn), "agents": sorted(_roster_names(conn))}
    finally:
        conn.close()


@app.post("/api/managers/clock_rules")
def api_managers_clock_rules_save(request: Request, body: dict):
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.set_clock_rules(conn, _who(request), body or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/auto_clockout")
def api_managers_auto_clockout(request: Request, body: dict):
    """The auto clock-out setting: enabled, grace_min, stamp (shift_end|run), no_shift_h."""
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.set_auto_rules(conn, _who(request), body or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/clock_override")
def api_managers_clock_override(request: Request, body: dict):
    from datetime import date

    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        try:
            day = date.fromisoformat(str(body.get("day") or ""))
        except ValueError:
            return JSONResponse({"error": "Pick a day."}, status_code=400)
        r = timeclock.set_override(conn, _who(request), agent, day, body.get("note") or "", bool(body.get("remove")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/punch_history")
def api_managers_punch_history(id: int = 0):
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"history": timeclock.punch_history(conn, id)}
    finally:
        conn.close()


@app.get("/api/managers/agents")
def api_managers_agents():
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"agents": timeclock.hr_list(conn, _roster_names(conn))}
    finally:
        conn.close()


@app.post("/api/managers/agents")
def api_managers_agents_save(request: Request, body: dict):
    from .. import timeclock
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        r = timeclock.hr_save(conn, _who(request), agent, body)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()
