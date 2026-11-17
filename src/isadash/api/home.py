"""Home tab: the metric cards, trends, per-day figures, Service Level, Quality and year-to-date.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from .. import cache
from .common import _settled_forms
from ..store import connect

from .common import _audit_end_utc, _get_form_events, _roster_sub_rows, _settings, _sts, app
from .revenue import api_revenue


def _phone_window_stats(conn, start_utc: str, end_utc: str) -> dict:
    """Unique qualified leads + unique converted leads on ISA queues,
    roster agents, within [start, end). Converted = own-name sale OR
    pitched + bought from any seller."""
    cf, own, anyc = set(), set(), set()
    for r in conn.execute(
        """SELECT c.ani_e164, c.dispo_group, c.converted_sub_id,
                  c.converted_any_sub_id
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
             AND c.start_utc >= ? AND c.start_utc < ?""",
        (start_utc, end_utc),
    ):
        if r["dispo_group"] in ("closed", "followup"):
            cf.add(r["ani_e164"])
        if r["converted_sub_id"]:
            own.add(r["ani_e164"])
        if r["converted_any_sub_id"]:
            anyc.add(r["ani_e164"])
    conv = own | (cf & anyc)
    qual = cf | conv
    pct = round(100.0 * len(conv) / len(qual), 1) if qual else 0.0
    return {"qual": len(qual), "conv": len(conv), "pct": pct}


@app.get("/api/home")
@cache.cached()
@_settled_forms
def api_home() -> dict:
    """Home cards. Every window is WHOLE Pacific calendar days ending
    YESTERDAY (today is never included — no partial-day noise), and the
    comparison delta is the same number of whole days immediately before.
    Audit-sheet metrics (Phone / All Leads) end at the last AUDITED day if
    the sheet lags behind yesterday. Each card carries its date range."""
    from datetime import timedelta

    from ..periods import PACIFIC
    now = datetime.now(timezone.utc)
    # anchor = start of today (Pacific) = end of the last complete day
    today_pac = now.astimezone(PACIFIC).date()
    anchor = datetime(today_pac.year, today_pac.month, today_pac.day, tzinfo=PACIFIC)
    anchor_utc = anchor.astimezone(timezone.utc)

    def rng(end_dt, days: int) -> dict:
        """Inclusive Pacific day range for the `days` whole days ending before end_dt."""
        last = (end_dt.astimezone(PACIFIC) - timedelta(days=1)).date()
        first = (end_dt.astimezone(PACIFIC) - timedelta(days=days)).date()
        return {"start": first.isoformat(), "end": last.isoformat()}

    events, _ = _get_form_events()
    conn = connect(_settings.db_path)
    try:
        out: dict = {"generated_at": now.isoformat(),
                     "anchor_end": (today_pac - timedelta(days=1)).isoformat()}
        for days in (30, 7):
            cur_start = anchor_utc - timedelta(days=days)
            prev_start = anchor_utc - timedelta(days=2 * days)
            # forms
            def form_stats(a, b):
                evs = [e for e in (events or [])
                       if a <= e.first_touch_utc < b and not e.already_customer]
                conv = sum(1 for e in evs if e.converted)
                return {"net": len(evs), "conv": conv,
                        "pct": round(100.0 * conv / len(evs), 1) if evs else 0.0}
            cur, prev = form_stats(cur_start, anchor_utc), form_stats(prev_start, cur_start)
            out[f"form{days}"] = {**cur, "prev_pct": prev["pct"],
                                  "range": rng(anchor_utc, days),
                                  "prev_range": rng(cur_start, days)}
            # phone
            cur = _phone_window_stats(conn, cur_start.isoformat(), anchor_utc.isoformat())
            prev = _phone_window_stats(conn, prev_start.isoformat(), cur_start.isoformat())
            out[f"phone{days}"] = {**cur, "prev_pct": prev["pct"],
                                   "range": rng(anchor_utc, days),
                                   "prev_range": rng(cur_start, days)}
        # windows for sheet/all/honest metrics are anchored at the last
        # audited day (never later than yesterday's end) so every part
        # describes the same whole days
        ab = _audit_end_utc(conn)
        bound = min(ab, anchor_utc.isoformat()) if ab else anchor_utc.isoformat()

        def audit_window(days_back_hi: int, days_back_lo: int) -> dict:
            """Audit-sheet sales/qualified over whole audited Pacific days."""
            from ..periods import PACIFIC
            bound_dt = datetime.fromisoformat(bound)
            a_day = (bound_dt - timedelta(days=days_back_hi)).astimezone(PACIFIC).date().isoformat()
            b_day = (bound_dt - timedelta(days=days_back_lo)).astimezone(PACIFIC).date().isoformat()
            r = conn.execute(
                "SELECT COALESCE(SUM(qualified),0), COALESCE(SUM(sales),0) "
                "FROM audit_daily WHERE date >= ? AND date < ?",
                (a_day, b_day)).fetchone()
            q, s = r[0], r[1]
            return {"qual": q, "sales": s,
                    "pct": round(100.0 * s / q, 1) if q else None}

        # SHEET CONVERSION — the default conversion metric (audit team's own
        # numbers, exactly as kept on the master sheet)
        bound_dt0 = datetime.fromisoformat(bound)
        for days in (30, 7):
            cur = audit_window(days, 0)
            prev = audit_window(2 * days, days)
            out[f"sheet{days}"] = {**cur, "prev_pct": prev["pct"],
                                   "range": rng(bound_dt0, days),
                                   "prev_range": rng(bound_dt0 - timedelta(days=days), days)}

        # all-leads conversion: SHEET numbers + FORM numbers, added together
        # (per ops decision 2026-07-13 — no cross-channel modeling, just the
        # audit team's qualified/sales plus the form funnel's net/converted,
        # over the same audited-day window)
        bound_dt_all = datetime.fromisoformat(bound)
        for days in (30, 7):
            a = bound_dt_all - timedelta(days=days)
            aw = audit_window(days, 0)
            sheet_q, sheet_s = aw["qual"], aw["sales"]
            form_net = form_conv = 0
            for e in (events or []):
                if not (a <= e.first_touch_utc < bound_dt_all) or e.already_customer:
                    continue
                form_net += 1
                if e.converted:
                    form_conv += 1
            total = sheet_q + form_net
            conv = sheet_s + form_conv
            out[f"all{days}"] = {"total": total, "conv": conv,
                                 "sheet_q": sheet_q, "sheet_s": sheet_s,
                                 "form_net": form_net, "form_conv": form_conv,
                                 "pct": round(100.0 * conv / total, 1) if total else 0.0,
                                 "range": rng(bound_dt_all, days),
                                 "prev_range": rng(bound_dt_all - timedelta(days=days), days)}

        # honest conversion 30d + 7d: team terms are PHONE-DEDUPED
        # (__TEAM__), windows BOUNDED to audited days, prior-window deltas
        from ..adjust import adjustment_stats

        def honest_window(days_back_hi: int, days_back_lo: int) -> dict | None:
            """Whole Pacific audited days, anchored at the audit bound —
            'trailing 7 days' means the LAST 7 AUDITED DAYS, so the call
            window and the audit-sheet days always line up exactly."""
            from ..periods import PACIFIC
            bound_dt = datetime.fromisoformat(bound)
            a_dt = bound_dt - timedelta(days=days_back_hi)
            b_dt = bound_dt - timedelta(days=days_back_lo)
            if b_dt <= a_dt:
                return None
            a_day = a_dt.astimezone(PACIFIC).date().isoformat()
            b_day = b_dt.astimezone(PACIFIC).date().isoformat()
            q = conn.execute(
                "SELECT COALESCE(SUM(qualified),0) FROM audit_daily "
                "WHERE date >= ? AND date < ?", (a_day, b_day)).fetchone()[0]
            adj = adjustment_stats(conn, a_dt.isoformat(), b_dt.isoformat())
            t = adj.get("__TEAM__", {"hidden": 0, "leaked": 0, "converted": 0})
            denom = max(q + t["hidden"] + t["leaked"], t["converted"])
            return {"audit_qual": q, "sys_conv": t["converted"],
                    "hidden": t["hidden"], "leaked": t["leaked"],
                    "pct": round(100.0 * t["converted"] / denom, 1) if q else None}

        for days in (30, 7):
            cur = honest_window(days, 0)
            prev = honest_window(2 * days, days)
            if cur:
                cur["prev_pct"] = prev["pct"] if prev else None
            out[f"true{days}"] = cur

        # sold-to-serviced, trailing 30d vs the 30d before (roster-sold,
        # all statuses — pending sits outside the denominator)
        def sts_window(a: str, b: str) -> dict:
            sts_rows = [r[0] for r in conn.execute(
                """SELECT s.initial_status FROM subscriptions s
                   WHERE s.date_added_utc >= ? AND s.date_added_utc < ?
                     AND s.sold_by IN (SELECT TRIM(value) FROM agent_map,
                        json_each('["'||REPLACE(fr_employee_id,',','","')||'"]')
                        WHERE on_roster=1 AND fr_employee_id IS NOT NULL)""",
                (a, b))]
            return _sts(sts_rows)
        for days in (30, 7):
            cs = anchor_utc - timedelta(days=days)
            cur = sts_window(cs.isoformat(), anchor_utc.isoformat())
            prev = sts_window((anchor_utc - timedelta(days=2 * days)).isoformat(), cs.isoformat())
            out[f"sts{days}"] = {**cur, "prev_pct": prev["sts_pct"],
                                 "range": rng(anchor_utc, days), "prev_range": rng(cs, days)}
        # team multiyear % + ACV, trailing windows (roster-sold subscriptions)
        subs = _roster_sub_rows(conn)

        def sub_window(a: str, b: str) -> dict:
            win = [(m, cv, ip, sg) for _, added, m, cv, _st, ip, sg in subs if a <= added < b]
            n = len(win)
            multi = sum(1 for m, _cv, _ip, _sg in win if m >= 18)
            cvs = [cv for _m, cv, _ip, _sg in win if cv > 0]
            ips = [ip for _m, _cv, ip, _sg in win if ip > 0]
            sgs = [sg for _m, _cv, _ip, sg in win if sg is not None]
            return {"subs": n,
                    "multiyear_pct": round(100.0 * multi / n, 1) if n else 0.0,
                    "acv": round(sum(cvs) / len(cvs), 2) if cvs else 0.0,
                    "init_avg": round(sum(ips) / len(ips), 2) if ips else 0.0,
                    "signed_pct": round(100.0 * sum(sgs) / len(sgs), 1) if sgs else 0.0}

        for days in (30, 7):
            cs_dt = anchor_utc - timedelta(days=days)
            cur_s = cs_dt.isoformat()
            prev_s = (anchor_utc - timedelta(days=2 * days)).isoformat()
            cur = sub_window(cur_s, anchor_utc.isoformat())
            prev = sub_window(prev_s, cur_s)
            out[f"team{days}"] = {**cur, "prev_multiyear_pct": prev["multiyear_pct"],
                                  "prev_acv": prev["acv"],
                                  "prev_init_avg": prev["init_avg"],
                                  "prev_signed_pct": prev["signed_pct"],
                                  "range": rng(anchor_utc, days), "prev_range": rng(cs_dt, days)}
        return out
    finally:
        conn.close()


@app.get("/api/home_ytd")
def api_home_ytd() -> dict:
    """Home's year-to-date counters. Taken straight from the Revenue tab's own
    feed so the two always show the same total (every office-staff seller,
    upsells in, renewals out) — only the two totals are returned, so Home
    doesn't need the Revenue permission."""
    from ..periods import PACIFIC
    year = datetime.now(timezone.utc).astimezone(PACIFIC).year
    d = api_revenue(year=year)
    return {"year": year, "revenue": d.get("team_total"), "sales": d.get("team_sales"),
            "sellers": d.get("seller_count")}


# Genesys is asked at most every 30 minutes (the windows are whole days ending
# yesterday, so the answer barely moves); an error answer is never saved.
@app.get("/api/home_servicelevel")
@cache.cached(ttl=1800, keep=lambda v: isinstance(v, dict))
def api_home_servicelevel() -> dict:
    """Genesys service level (30d + 7d + weekly trend) for the home row. Kept in
    its own endpoint so the DB-driven home renders instantly and this fills in
    when Genesys responds."""
    from datetime import timedelta

    from ..genesys import GenesysClient
    from ..periods import PACIFIC, week_start
    now = datetime.now(timezone.utc)
    qids = _settings.genesys_queue_ids
    # whole Pacific days ending YESTERDAY, like every other home card
    today_pac = now.astimezone(PACIFIC).date()
    anchor = datetime(today_pac.year, today_pac.month, today_pac.day,
                      tzinfo=PACIFIC).astimezone(timezone.utc)

    def rng(end_dt, days: int) -> dict:
        last = (end_dt.astimezone(PACIFIC) - timedelta(days=1)).date()
        first = (end_dt.astimezone(PACIFIC) - timedelta(days=days)).date()
        return {"start": first.isoformat(), "end": last.isoformat()}

    this_week = week_start(now)  # Sunday 00:00 Pacific — align the trend to full weeks
    wk_start = (this_week - timedelta(weeks=13)).astimezone(timezone.utc)
    wk_end = this_week.astimezone(timezone.utc)
    g = GenesysClient(_settings.genesys_region, _settings.genesys_client_id,
                      _settings.genesys_client_secret)
    try:
        sl30 = g.queue_stats(anchor - timedelta(days=30), anchor, qids)
        sl7 = g.queue_stats(anchor - timedelta(days=7), anchor, qids)
        series = g.service_level_series(wk_start, wk_end, qids, "P7D")
    except Exception as e:  # never break the home page on a Genesys hiccup
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    finally:
        g.close()
    return {
        "sl30": {"pct": sl30.get("service_level"), "offered": sl30.get("offered"),
                 "range": rng(anchor, 30)},
        "sl7": {"pct": sl7.get("service_level"), "offered": sl7.get("offered"),
                "range": rng(anchor, 7)},
        "weeks": [s["start"] for s in series],
        "values": [s["service_level"] for s in series],
    }


@app.get("/api/home_trends")
@cache.cached()
@_settled_forms
def api_home_trends() -> dict:
    """13 weekly points for each home metric — the last-90-days sparklines.
    Same definitions as the home cards, bucketed into full Sunday–Saturday
    Pacific weeks (last point = the last COMPLETE week)."""
    from datetime import timedelta

    from ..periods import week_start
    now = datetime.now(timezone.utc)
    events, _ = _get_form_events()
    conn = connect(_settings.db_path)
    try:
        this_week = week_start(now)  # Sunday 00:00 Pacific of the current week
        N = 13
        wins = [(this_week - timedelta(weeks=N - i),
                 this_week - timedelta(weeks=N - 1 - i)) for i in range(N)]
        weeks = [a.date().isoformat() for a, _b in wins]  # Sunday start dates

        subs = _roster_sub_rows(conn)  # (id, added_iso, months, cv, status, ip)
        sts_rows = conn.execute(
            """SELECT s.date_added_utc, s.initial_status FROM subscriptions s
               WHERE s.date_added_utc >= ? AND s.sold_by IN (
                 SELECT TRIM(value) FROM agent_map,
                   json_each('["'||REPLACE(fr_employee_id,',','","')||'"]')
                 WHERE on_roster=1 AND fr_employee_id IS NOT NULL)""",
            (wins[0][0].astimezone(timezone.utc).isoformat(),)).fetchall()

        keys = ("all", "phone", "form", "multiyear", "acv", "aip", "sts", "signed")
        series: dict[str, list] = {k: [] for k in keys}
        for a, b in wins:
            # subs are stored as UTC instants; audit_daily is keyed by Pacific day
            a_iso = a.astimezone(timezone.utc).isoformat()
            b_iso = b.astimezone(timezone.utc).isoformat()
            a_day = a.date().isoformat()
            b_day = b.date().isoformat()
            q, s = conn.execute(
                "SELECT COALESCE(SUM(qualified),0), COALESCE(SUM(sales),0) "
                "FROM audit_daily WHERE date >= ? AND date < ?", (a_day, b_day)).fetchone()
            series["phone"].append(round(100.0 * s / q, 1) if q else None)
            fn = fc = 0
            for e in (events or []):
                if a <= e.first_touch_utc < b and not e.already_customer:
                    fn += 1
                    if e.converted:
                        fc += 1
            series["form"].append(round(100.0 * fc / fn, 1) if fn else None)
            tot, cnv = q + fn, s + fc
            series["all"].append(round(100.0 * cnv / tot, 1) if tot else None)
            win = [(m, cv, ip, sg) for _id, added, m, cv, _st, ip, sg in subs if a_iso <= added < b_iso]
            n = len(win)
            cvs = [cv for _m, cv, _ip, _sg in win if cv and cv > 0]
            ips = [ip for _m, _cv, ip, _sg in win if ip and ip > 0]
            sgs = [sg for _m, _cv, _ip, sg in win if sg is not None]
            series["multiyear"].append(round(100.0 * sum(1 for m, _cv, _ip, _sg in win if m >= 18) / n, 1) if n else None)
            series["acv"].append(round(sum(cvs) / len(cvs)) if cvs else None)
            # avg initial-service price — same counted subs as ACV
            series["aip"].append(round(sum(ips) / len(ips)) if ips else None)
            # signed-agreement % — signed ÷ contract-checked (NULL = not yet checked)
            series["signed"].append(round(100.0 * sum(sgs) / len(sgs), 1) if sgs else None)
            win_sts = [st for (added, st) in sts_rows if added and a_iso <= added < b_iso]
            series["sts"].append(_sts(win_sts).get("sts_pct"))
        return {"weeks": weeks, "series": series}
    finally:
        conn.close()


@app.get("/api/home_daily")
@cache.cached()
@_settled_forms
def api_home_daily() -> dict:
    """Per-day breakdown for the Home metric drop-downs: the last 7 COMPLETE
    Pacific days (through yesterday). Forms: net/converted/pct by first-touch
    day. Subs (same counted base as the ACV/Multiyear cards): n, multiyear %,
    ACV, avg initial price by sale day."""
    from datetime import timedelta

    from ..periods import PACIFIC
    today_pac = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    days = [(today_pac - timedelta(days=i)).isoformat() for i in range(7, 0, -1)]
    day_set = set(days)

    events, _ = _get_form_events()
    form = {d: {"net": 0, "conv": 0} for d in days}
    for e in (events or []):
        if e.already_customer:
            continue
        d = e.first_touch_utc.astimezone(PACIFIC).date().isoformat()
        if d in day_set:
            form[d]["net"] += 1
            if e.converted:
                form[d]["conv"] += 1

    conn = connect(_settings.db_path)
    try:
        subs = _roster_sub_rows(conn)
    finally:
        conn.close()
    sub = {d: {"n": 0, "my": 0, "cv_sum": 0.0, "cv_n": 0, "ip_sum": 0.0, "ip_n": 0,
               "sg": 0, "sg_n": 0}
           for d in days}
    for _a, added, months, cv, _st, ip, sg in subs:
        d = datetime.fromisoformat(added).astimezone(PACIFIC).date().isoformat()
        if d not in day_set:
            continue
        k = sub[d]
        k["n"] += 1
        if months >= 18:
            k["my"] += 1
        if cv > 0:
            k["cv_sum"] += cv; k["cv_n"] += 1
        if ip > 0:
            k["ip_sum"] += ip; k["ip_n"] += 1
        if sg is not None:
            k["sg_n"] += 1
            if sg:
                k["sg"] += 1

    # VoiceOps quality per single Pacific day. Cache only — the nightly sync warms
    # exactly these 7 days — so the drop-down never waits on the vendor.
    from ..voiceops import cached_scores, weighted_quality
    qual = {}
    conn = connect(_settings.db_path)
    try:
        for d in days:
            rows = list(cached_scores(conn, None, d, d).values())
            scored = [r for r in rows if r.get("quality_score") is not None]
            qual[d] = {"pct": weighted_quality(rows),
                       "calls": sum(r.get("call_count") or 0 for r in scored),
                       "agents": len(scored)}
    finally:
        conn.close()

    out = []
    for d in days:
        f, k = form[d], sub[d]
        out.append({
            "date": d,
            "form": {"net": f["net"], "conv": f["conv"],
                     "pct": round(100.0 * f["conv"] / f["net"], 1) if f["net"] else None},
            "subs": k["n"],
            "multiyear_pct": round(100.0 * k["my"] / k["n"], 1) if k["n"] else None,
            "acv": round(k["cv_sum"] / k["cv_n"], 2) if k["cv_n"] else None,
            "aip": round(k["ip_sum"] / k["ip_n"], 2) if k["ip_n"] else None,
            "signed_pct": round(100.0 * k["sg"] / k["sg_n"], 1) if k["sg_n"] else None,
            "quality": qual[d],
        })
    return {"days": out}


@app.get("/api/home_quality")
@cache.cached()
def api_home_quality() -> dict:
    """VoiceOps quality score for the Home row: 30d + 7d bubbles and the 13-week
    trend. Served entirely from the voiceops_scores cache (the nightly sync warms
    exactly these periods), so it is fast and never blocks on the vendor — the
    row simply reads blank until the first sync populates it.

    Weeks are strict Sunday-Saturday Pacific, and the bubbles are whole Pacific
    days ending yesterday, matching every other Home card.
    """
    from datetime import timedelta

    from ..periods import PACIFIC
    from ..voiceops import cached_scores, pacific_weeks, weighted_quality

    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    yday = today - timedelta(days=1)

    def window(days: int, offset: int = 0):
        end = yday - timedelta(days=offset)
        start = end - timedelta(days=days - 1)
        return start.isoformat(), end.isoformat()

    conn = connect(_settings.db_path)
    try:
        def bucket(days: int, offset: int = 0):
            sd, ed = window(days, offset)
            rows = list(cached_scores(conn, None, sd, ed).values())
            calls = sum(r.get("call_count") or 0 for r in rows
                        if r.get("quality_score") is not None)
            return {"pct": weighted_quality(rows), "calls": calls,
                    "range": {"start": sd, "end": ed}}

        q30, q7 = bucket(30), bucket(7)
        q30["prev_pct"] = bucket(30, 30)["pct"]
        q30["prev_range"] = bucket(30, 30)["range"]
        q7["prev_pct"] = bucket(7, 7)["pct"]
        q7["prev_range"] = bucket(7, 7)["range"]

        weeks, values = [], []
        for sd, ed in pacific_weeks(13, today):
            weeks.append(sd)
            values.append(weighted_quality(list(cached_scores(conn, None, sd, ed).values())))
        return {"q30": q30, "q7": q7, "weeks": weeks, "values": values}
    finally:
        conn.close()
