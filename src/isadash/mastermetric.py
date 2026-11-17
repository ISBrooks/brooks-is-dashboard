"""Master Metric (Agent Metrics -> Master Metric): agent productivity and
availability for a Pacific day range.

Three sources, combined per agent:
  * Genesys   - inbound calls answered / alerted-but-not-answered, talk, ACW
  * Hours     - hours worked (hours.py): frozen scheduled hours before
                2026-10-05, frozen Homebase hours 10-05..10-25, then the
                dashboard time clock (today live); outbound reps 8h/weekday
  * Podium    - outbound conversations (collected each morning) + calls made

Formulas mirror the manual "Daily Tracker - MM" sheet:
  Ans:Missed            = Answered / Alert-No Answer
  Calls/Hr              = Answered / Hours
  OB/Hr                 = Total OBs / Hours           (Total OBs = Podium)
  MM                    = Calls/Hr + OB/Hr
  Inbound Utilization   = Total Talk / Hours
  Available Hours       = Hours - Total Talk
  Available Outbound Rate = Total OBs / Available Hours
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from . import cache
from . import hours as hours_mod
from . import podium
from .genesys import GenesysClient
from .periods import PACIFIC

log = logging.getLogger("isadash.mastermetric")
# MM is only trusted from here on (Podium conversations + Homebase hours both
# complete); a range starting earlier shows MM blank (Dallin, 2026-10-13)
MM_RELIABLE_FROM = date(2026, 9, 1)

# --- Genesys --------------------------------------------------------------
_GEN_TTL_S = 300


def _iv(a: datetime, b: datetime) -> str:
    u = timezone.utc
    return f"{a.astimezone(u):%Y-%m-%dT%H:%M:%S.000Z}/{b.astimezone(u):%Y-%m-%dT%H:%M:%S.000Z}"


def genesys_by_user(settings, start: date, end: date) -> dict[str, dict]:
    """_genesys_by_user_now, asked of Genesys at most every five minutes; an
    older answer is handed back at once and refreshed in the background."""
    return cache.keep_fresh(("genesys_by_user", start, end),
                            lambda: _genesys_by_user_now(settings, start, end), _GEN_TTL_S)


def _genesys_by_user_now(settings, start: date, end: date) -> dict[str, dict]:
    """userId -> {answered, missed, talk_ms, talk_n, acw_ms, acw_n} for whole
    Pacific days start..end inclusive. Inbound voice supplies answered /
    missed / talk; after-call work is taken across all voice interactions,
    which is how the manual report counted it."""
    out: dict[str, dict] = {}

    def add(uid, field, v):
        out.setdefault(uid, {"answered": 0, "missed": 0, "talk_ms": 0, "talk_n": 0,
                             "acw_ms": 0, "acw_n": 0})[field] += v

    voice = {"dimension": "mediaType", "value": "voice"}
    with GenesysClient(settings.genesys_region, settings.genesys_client_id,
                       settings.genesys_client_secret) as g:
        a = datetime(start.year, start.month, start.day, tzinfo=PACIFIC)
        nxt = end + timedelta(days=1)
        b = datetime(nxt.year, nxt.month, nxt.day, tzinfo=PACIFIC)
        for preds, metrics in (
            ([voice, {"dimension": "direction", "value": "inbound"}],
             ["tAnswered", "tNotResponding", "tTalkComplete"]),
            ([voice], ["tAcw"]),
        ):
            r = g._request("POST", "/api/v2/analytics/conversations/aggregates/query",
                           json={"interval": _iv(a, b), "groupBy": ["userId"],
                                 "metrics": metrics,
                                 "filter": {"type": "and", "predicates": preds}})
            for res in r.get("results", []) or []:
                uid = (res.get("group") or {}).get("userId")
                if not uid:
                    continue
                for d in res.get("data", []) or []:
                    for x in d.get("metrics", []) or []:
                        st = x.get("stats") or {}
                        n, sm = st.get("count", 0) or 0, st.get("sum", 0) or 0
                        m = x.get("metric")
                        if m == "tAnswered":
                            add(uid, "answered", n)
                        elif m == "tNotResponding":
                            add(uid, "missed", n)
                        elif m == "tTalkComplete":
                            add(uid, "talk_ms", sm); add(uid, "talk_n", n)
                        elif m == "tAcw":
                            add(uid, "acw_ms", sm); add(uid, "acw_n", n)
    return out


# --- the table ------------------------------------------------------------
def _div(n, d):
    return n / d if n is not None and d else None


def _row(name, answered, missed, hours, talk_ms, talk_n, acw_ms, acw_n, convs, calls):
    talk_h = talk_ms / 3.6e6
    # Total OBs needs BOTH Podium numbers; a half total would understate MM
    obs = None if convs is None or calls is None else convs + calls
    calls_hr = _div(answered, hours)
    ob_hr = _div(obs, hours)
    avail = (hours - talk_h) if hours else None
    r2 = lambda v: None if v is None else round(v, 2)
    return {
        "agent": name,
        "avail_ob_rate": r2(_div(obs, avail) if avail and avail > 0 else None),
        "mm": r2(calls_hr + ob_hr if calls_hr is not None and ob_hr is not None else None),
        "ans_missed": r2(_div(answered, missed)),
        "inbound_util": r2(100 * talk_h / hours) if hours else None,
        "answered": answered, "missed": missed,
        "hours": r2(hours) if hours else None,
        "avail_hours": r2(avail),
        "total_obs": obs,
        "calls_hr": r2(calls_hr), "ob_hr": r2(ob_hr),
        "conversations": convs, "calls_made": calls,
        "avg_talk_s": round(talk_ms / talk_n / 1000) if talk_n else None,
        "avg_acw_s": round(acw_ms / acw_n / 1000) if acw_n else None,
        "total_talk_h": r2(talk_h) if talk_n else None,
    }


def build(conn, settings, start: date, end: date) -> dict:
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    agents = {r["genesys_name"]: r["genesys_user_id"] for r in conn.execute(
        "SELECT genesys_name, genesys_user_id FROM agent_map "
        "WHERE scorecard = 1 AND genesys_name IS NOT NULL")}

    # hours per agent: frozen schedule / Homebase rows through Oct 25, then the
    # time clock (rebuilt from punches on every read), outbound defaults
    imp = hours_mod.import_sheet(conn, settings, list(agents))   # frozen: a no-op
    hours, notes = hours_mod.hours_for(conn, settings, agents, start, end, today)
    if imp.get("error"):
        notes.append(f"Couldn't re-read the Homebase hours sheet just now ({imp['error']}); "
                     "showing the last imported hours.")
    if imp.get("unmatched"):
        notes.append("On the hours sheet but not a tracked agent: " + ", ".join(imp["unmatched"]) + ".")

    gen = genesys_by_user(settings, start, min(end, today))

    # Podium: outbound calls made (live) + conversations messaged (collected
    # by the morning sync for the day before — see podium.collect_conversations)
    convs: dict[str, int] | None = None
    calls: dict[str, int] | None = None
    if podium.configured(settings):
        try:
            st = podium.sync(conn, settings)
            last = min(end, today) + timedelta(days=1)
            a_utc = datetime(start.year, start.month, start.day, tzinfo=PACIFIC).astimezone(timezone.utc)
            b_utc = datetime(last.year, last.month, last.day, tzinfo=PACIFIC).astimezone(timezone.utc)
            by_name = podium.calls_made_by_name(conn, f"{a_utc:%Y-%m-%dT%H:%M:%SZ}",
                                                f"{b_utc:%Y-%m-%dT%H:%M:%SZ}")
            mapping = podium.match_names(by_name, list(agents))
            calls = {}
            for pn, n in by_name.items():
                if pn in mapping:
                    calls[mapping[pn]] = calls.get(mapping[pn], 0) + n
            if not st.get("complete"):
                notes.append("Podium calls are still catching up — Total Calls Made may be low "
                             "for a few minutes. Reload shortly.")
            oldest = conn.execute("SELECT MIN(created_at) FROM podium_calls").fetchone()[0]
            if oldest and oldest[:10] > start.isoformat():
                notes.append(f"Podium calls are only loaded back to {oldest[:10]}, so Total Calls "
                             "Made is incomplete for this range.")
        except Exception as e:
            log.exception("podium calls failed")
            calls = None
            notes.append(f"Couldn't read Podium calls ({type(e).__name__}).")
        try:
            upto = min(end, today)
            have = podium.conversation_days(conn, start, upto)
            if have:
                by_name = podium.conversations_by_name(conn, start, upto)
                mapping = podium.match_names(by_name, list(agents))
                convs = {}
                for pn, n in by_name.items():
                    if pn in mapping:
                        convs[mapping[pn]] = convs.get(mapping[pn], 0) + n
                gaps = []
                d = start
                while d <= upto:
                    if d.isoformat() not in have:
                        gaps.append(d)
                    d += timedelta(days=1)
                if gaps == [today]:
                    notes.append("Podium conversations are collected each morning for the day "
                                 "before, so today's aren't in yet — Total Conversations, Total "
                                 "OBs and MM read low for today.")
                elif gaps:
                    notes.append("Podium conversations aren't loaded for " + _day_list(gaps)
                                 + ", so Total Conversations, Total OBs and MM are understated "
                                 "for this range.")
        except Exception as e:
            log.exception("podium conversations failed")
            convs = None
            notes.append(f"Couldn't read Podium conversations ({type(e).__name__}).")

    rows = []
    tot = {"answered": 0, "missed": 0, "hours": 0.0, "talk_ms": 0, "talk_n": 0,
           "acw_ms": 0, "acw_n": 0}
    for name in sorted(agents):
        gdat = gen.get(agents[name] or "", {})
        h = hours.get(name, 0.0)
        made = None if calls is None else calls.get(name, 0)
        talked = None if convs is None else convs.get(name, 0)
        if talked:
            tot["convs"] = tot.get("convs", 0) + talked
        if not gdat.get("answered") and not gdat.get("missed") and not h and not made and not talked:
            continue                                  # not scheduled, no calls either way
        rows.append(_row(name, gdat.get("answered", 0), gdat.get("missed", 0), h,
                         gdat.get("talk_ms", 0), gdat.get("talk_n", 0),
                         gdat.get("acw_ms", 0), gdat.get("acw_n", 0), talked, made))
        if made:
            tot["calls_made"] = tot.get("calls_made", 0) + made
        tot["answered"] += gdat.get("answered", 0); tot["missed"] += gdat.get("missed", 0)
        tot["hours"] += h
        for k in ("talk_ms", "talk_n", "acw_ms", "acw_n"):
            tot[k] += gdat.get(k, 0)
    team = _row("Team", tot["answered"], tot["missed"], tot["hours"], tot["talk_ms"],
                tot["talk_n"], tot["acw_ms"], tot["acw_n"],
                None if convs is None else tot.get("convs", 0),
                None if calls is None else tot.get("calls_made", 0)) if rows else None
    pending = []
    if convs is None:
        pending.append("conversations")
    if calls is None:
        pending.append("calls_made")
    if pending:
        pending += ["total_obs", "ob_hr", "mm", "avail_ob_rate"]
    if start < MM_RELIABLE_FROM:
        for r in rows + ([team] if team else []):
            r["mm"] = None
    no_hours = [r["agent"] for r in rows if not r["hours"]]
    if no_hours:
        notes.append("Took calls but has no hours in this range: " + ", ".join(no_hours) + ".")
    return {"start": start.isoformat(), "end": end.isoformat(),
            "through": min(end, today).isoformat(),
            "rows": rows, "team": team, "notes": notes,
            "pending": pending, "podium_calls": calls is not None,
            "hours_source": "homebase"}


def mm_only(conn, settings, start: date, end: date) -> dict:
    """{'agents': {name: mm}, 'team': mm} for the IS #'s sheet and the agent
    page — the same numbers as the Master Metric tab's MM column."""
    d = build(conn, settings, start, end)
    return {"agents": {r["agent"]: r["mm"] for r in d["rows"]},
            "team": (d["team"] or {}).get("mm"), "start": d["start"], "end": d["end"]}


def _day_list(days: list) -> str:
    """'Sep 27–Sep 30, Oct 2' style summary of runs of consecutive days."""
    out, run = [], [days[0]]
    for d in days[1:]:
        if (d - run[-1]).days == 1:
            run.append(d)
        else:
            out.append(run)
            run = [d]
    out.append(run)
    return ", ".join(f"{r[0]:%b} {r[0].day}" if len(r) == 1 else
                     f"{r[0]:%b} {r[0].day}–{r[-1]:%b} {r[-1].day}" for r in out)


def _in_range_note(note: str, start: date, end: date) -> bool:
    m = re.search(r" (\d{2})/(\d{2}):", note)
    if not m:
        return False
    for y in (start.year, end.year):
        try:
            d = date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if start <= d <= end:
            return True
    return False


# --- Genesys per agent per day (Team Overview cards) -------------------------
def genesys_daily(settings, start: date, end: date) -> dict[str, dict[str, dict]]:
    """_genesys_daily_now, kept for hours when the range is all in the past and
    five minutes otherwise; an older answer is handed back at once and
    refreshed in the background."""
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    ttl = 6 * 3600 if end < today else _GEN_TTL_S
    return cache.keep_fresh(("genesys_daily", start, end),
                            lambda: _genesys_daily_now(settings, start, end), ttl)


def _genesys_daily_now(settings, start: date, end: date) -> dict[str, dict[str, dict]]:
    """userId -> {Pacific day -> {answered, missed, talk_ms, talk_n, acw_ms,
    acw_n}} for start..end inclusive, in two Genesys queries (one-day buckets),
    so any number of windows can be summed without asking Genesys again.
    Same definitions as genesys_by_user."""
    out: dict[str, dict[str, dict]] = {}

    def add(uid, day, field, v):
        d = out.setdefault(uid, {}).setdefault(day, {
            "answered": 0, "missed": 0, "talk_ms": 0, "talk_n": 0, "acw_ms": 0, "acw_n": 0})
        d[field] += v

    voice = {"dimension": "mediaType", "value": "voice"}
    a = datetime(start.year, start.month, start.day, tzinfo=PACIFIC)
    nxt = end + timedelta(days=1)
    b = datetime(nxt.year, nxt.month, nxt.day, tzinfo=PACIFIC)
    with GenesysClient(settings.genesys_region, settings.genesys_client_id,
                       settings.genesys_client_secret) as g:
        for preds, metrics in (
            ([voice, {"dimension": "direction", "value": "inbound"}],
             ["tAnswered", "tNotResponding", "tTalkComplete"]),
            ([voice], ["tAcw"]),
        ):
            r = g._request("POST", "/api/v2/analytics/conversations/aggregates/query",
                           json={"interval": _iv(a, b), "granularity": "P1D",
                                 "timeZone": "America/Los_Angeles", "groupBy": ["userId"],
                                 "metrics": metrics,
                                 "filter": {"type": "and", "predicates": preds}})
            for res in r.get("results", []) or []:
                uid = (res.get("group") or {}).get("userId")
                if not uid:
                    continue
                for d in res.get("data", []) or []:
                    day = (d.get("interval") or "")[:10]      # bucket start, Pacific
                    for x in d.get("metrics", []) or []:
                        st = x.get("stats") or {}
                        n, sm = st.get("count", 0) or 0, st.get("sum", 0) or 0
                        m = x.get("metric")
                        if m == "tAnswered":
                            add(uid, day, "answered", n)
                        elif m == "tNotResponding":
                            add(uid, day, "missed", n)
                        elif m == "tTalkComplete":
                            add(uid, day, "talk_ms", sm); add(uid, day, "talk_n", n)
                        elif m == "tAcw":
                            add(uid, day, "acw_ms", sm); add(uid, day, "acw_n", n)
    return out
