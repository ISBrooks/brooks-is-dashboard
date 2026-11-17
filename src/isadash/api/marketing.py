"""Marketing tab: Spend & ROAS, Lead Volume, Volume By Source, Sales Data and the source detail overlay.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import cache
from ..store import connect
from .common import _settled_forms

from .common import (
    _RENEWAL_SOURCES, _allowed, _channel_for, _get_form_events, _load_source_channels,
    _load_source_maps, _settings, _valid_range, app,
)


# ---- Marketing tab: Call Volume / Form Leads / By Source (marketing.py) ----
@app.get("/api/marketing/volume")
def api_marketing_volume(start_day: str = "", end_day: str = ""):
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return marketing.queue_volume(_settings, date.fromisoformat(start_day), date.fromisoformat(end_day))
    except Exception as e:                         # Genesys down: say so
        logging.getLogger("isadash.marketing").exception("queue volume failed")
        return JSONResponse({"error": f"Couldn't read Genesys: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/forms")
def api_marketing_forms(start_day: str = "", end_day: str = ""):
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, payload = _get_form_events()
    if events is None:
        return JSONResponse({"error": (payload or {}).get("error", "form leads unavailable")}, status_code=502)
    return marketing.forms_weekly(events, date.fromisoformat(start_day), date.fromisoformat(end_day))


@app.get("/api/marketing/bysource")
@cache.cached(keep=lambda v: isinstance(v, dict))
def api_marketing_bysource(start_day: str = "", end_day: str = ""):
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.by_source(conn, events or [], date.fromisoformat(start_day),
                                   date.fromisoformat(end_day), maps,
                                   lambda label, prov, grp: _channel_for(chan, label, prov, grp))
    finally:
        conn.close()


@app.get("/api/marketing/volume_history")
def api_marketing_volume_history():
    """Year-over-year calls offered per week: Genesys (from April 2023) beside
    the team's manual tracker sheet."""
    from .. import marketing
    try:
        return marketing.volume_history(_settings)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("volume history failed")
        return JSONResponse({"error": f"Couldn't build the history: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/forms_history")
def api_marketing_forms_history():
    """Year-over-year net new form leads per week: dashboard beside the tracker."""
    from .. import marketing
    events, _ = _get_form_events()
    try:
        return marketing.forms_history(_settings, events or [])
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("forms history failed")
        return JSONResponse({"error": f"Couldn't build the history: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/hourly")
def api_marketing_hourly(start_day: str = "", end_day: str = ""):
    """Lead Volume -> Leads by hour: calls offered + net new form leads per
    Mountain-time hour of the day, against the same number of days before."""
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    try:
        return marketing.hourly(_settings, events or [], date.fromisoformat(start_day), date.fromisoformat(end_day))
    except Exception as e:                         # Genesys down: say so
        logging.getLogger("isadash.marketing").exception("hourly volume failed")
        return JSONResponse({"error": f"Couldn't read Genesys: {type(e).__name__}: {e}"[:300]}, status_code=502)


# Spend & ROAS took ~5.5 s of calculation on every open. Its windows are complete
# weeks, so it is saved until the database, the source files or the spend sheet
# change (the sheet is re-read in the background every ten minutes).
@app.get("/api/marketing/spend_windows")
@cache.cached(keep=lambda v: isinstance(v, dict))
@_settled_forms
def api_marketing_spend_windows():
    """Marketing -> Spend & ROAS: per source, last week / trailing 4 / trailing 12."""
    from .. import marketing
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_windows(conn, _settings, events or [], maps,
                                       lambda label, prov, grp: _channel_for(chan, label, prov, grp),
                                       exclude_sources=_RENEWAL_SOURCES)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("spend windows failed")
        return JSONResponse({"error": f"Couldn't build Spend & ROAS: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


@app.get("/api/marketing/spend_chart")
@cache.cached(keep=lambda v: isinstance(v, dict))
def api_marketing_spend_chart(start_day: str = "", end_day: str = ""):
    """Marketing -> Spend & ROAS: the weekly spend / revenue / CPA % chart for
    a chosen date range (the page shows year to date until one is picked)."""
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_chart(conn, _settings, events or [], date.fromisoformat(start_day),
                                     date.fromisoformat(end_day), maps,
                                     lambda label, prov, grp: _channel_for(chan, label, prov, grp),
                                     exclude_sources=_RENEWAL_SOURCES)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("spend chart failed")
        return JSONResponse({"error": f"Couldn't build the chart: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


@app.get("/api/marketing/spend")
@cache.cached(keep=lambda v: isinstance(v, dict))
def api_marketing_spend(start_day: str = "", end_day: str = ""):
    """Marketing -> Spend & ROAS: the spend sheet's weekly spend per source
    joined to the dashboard's leads, sales and revenue by source group."""
    from datetime import date

    from .. import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_roas(conn, _settings, events or [], date.fromisoformat(start_day),
                                    date.fromisoformat(end_day), maps,
                                    lambda label, prov, grp: _channel_for(chan, label, prov, grp))
    except Exception as e:                         # sheet unreachable etc.
        logging.getLogger("isadash.marketing").exception("spend & roas failed")
        return JSONResponse({"error": f"Couldn't build Spend & ROAS: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


@app.get("/api/sources")
def api_sources(request: Request, days: int = 30, grouped: int = 1,
                start_day: str = "", end_day: str = "") -> dict:
    """Marketing view: every metric by lead source. Requires the admin cookie.
    Always returns GROUP rows (all Elocal offices -> Elocal, etc., per the
    sourcing app's dispute_groups.csv + our own overrides). A group made of 2+
    providers carries them as a `children` list for inline expand/collapse.
    (`grouped` is accepted for backward-compat but no longer changes anything.)"""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    import re
    from datetime import timedelta

    from ..formleads import load_source_names
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from ..rangestats import utc_bounds
        start, range_end = utc_bounds(start_day, end_day)
    else:
        days = max(1, min(days, 400))
        start, range_end = (now - timedelta(days=days)).isoformat(), now.isoformat()

    # grouping + attribution maps (base sourcing-app file + our overrides)
    group_of, label_to_provider, dnis_to_provider = _load_source_maps()

    def provider_for_label(label: str | None) -> str:
        if not label:
            return "(no source)"
        return label_to_provider.get(label.strip(), label.strip())

    rows: dict[str, dict] = {}

    def fresh(prov: str) -> dict:
        return {
            # 📞 Genesys — phone dispositions only (unique callers)
            "source": prov, "leads": set(), "qual_cf": set(), "closed_set": set(),
            # 📝 forms
            "form_net": 0, "form_conv": 0,
            # 🗂 PestRoutes — Pending + Serviced accounts only
            "sold": 0, "pending": 0, "my": 0, "cv_sum": 0.0, "cv_n": 0,
            "init_sum": 0.0, "init_n": 0, "apay": 0, "apay_n": 0,
            # Sold→Serviced: serviced / (serviced + cancelled + no-show)
            "svc": 0, "svc_denom": 0,
        }

    def bucket(prov: str) -> dict:
        return rows.setdefault(prov, fresh(prov))

    # --- phone leads by DNIS ---
    conn = connect(_settings.db_path)
    try:
        for r in conn.execute(
            """SELECT c.ani_e164 ph, c.dnis, c.dispo_group
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
                 AND c.start_utc >= ? AND c.start_utc < ?""", (start, range_end)):
            digits = re.sub(r"\D", "", r["dnis"] or "")
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            prov = dnis_to_provider.get(digits)
            if not prov:
                continue
            b = bucket(prov)
            # Genesys dispositions ONLY — no PestRoutes converted_sub_id lookup,
            # so Ph Leads / Qualified / Closed are a pure phone-disposition funnel.
            if r["dispo_group"] in ("closed", "followup", "unqualified"):
                b["leads"].add(r["ph"])
                if r["dispo_group"] != "unqualified":
                    b["qual_cf"].add(r["ph"])
            if r["dispo_group"] == "closed":
                b["closed_set"].add(r["ph"])

        # --- form leads by source ---
        events, _ = _get_form_events()
        a_dt = datetime.fromisoformat(start)
        b_dt = datetime.fromisoformat(range_end)
        for e in (events or []):
            if not (a_dt <= e.first_touch_utc < b_dt) or e.already_customer:
                continue
            provs = {provider_for_label(s) for s in e.sources} or {"(no source)"}
            for prov in provs:
                b = bucket(prov)
                b["form_net"] += 1
                if e.converted:
                    b["form_conv"] += 1

        # --- subscriptions by source (all sellers — marketing view) ---
        # Count ONLY Pending + Serviced(Completed) by subscription status. Never-
        # scheduled ('No Appointment') and failed (No Show / Cancelled / Not
        # Serviced) don't count anywhere in the PestRoutes section.
        for r in conn.execute(
            """SELECT s.source, s.initial_status, s.agreement_length,
                      s.contract_value, s.initial_price, c.card_on_file
               FROM subscriptions s
               LEFT JOIN customers c ON c.customer_id = s.customer_id
               WHERE s.date_added_utc >= ? AND s.date_added_utc < ?""",
            (start, range_end)):
            st = r["initial_status"]
            # 'No Appointment' (never scheduled) and anything undecided beyond the
            # ones below never count on this table
            if st not in ("Completed", "Pending", "Cancelled", "No Show"):
                continue
            b = bucket(provider_for_label(r["source"]))
            # Sold→Serviced: of subs whose first service was DECIDED (serviced,
            # cancelled, or no-show), the share that got serviced
            if st in ("Completed", "Cancelled", "No Show"):
                b["svc_denom"] += 1
                if st == "Completed":
                    b["svc"] += 1
            # every other PestRoutes metric is Pending + Serviced only
            if st not in ("Completed", "Pending"):
                continue
            b["sold"] += 1
            if st == "Pending":
                b["pending"] += 1
            if (r["agreement_length"] or 0) >= 18:
                b["my"] += 1
            if r["contract_value"]:
                b["cv_sum"] += r["contract_value"]
                b["cv_n"] += 1
            if r["initial_price"]:
                b["init_sum"] += r["initial_price"]
                b["init_n"] += 1
            if r["card_on_file"] is not None:
                b["apay_n"] += 1
                if r["card_on_file"]:
                    b["apay"] += 1
    finally:
        conn.close()

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    def finalize(name: str, b: dict) -> dict:
        leads = len(b["leads"]); qual = len(b["qual_cf"]); closed = len(b["closed_set"])
        return {
            "source": name,
            # 📞 Genesys — phone dispositions only
            "leads": leads, "qual": qual, "qual_pct": pct(qual, leads),
            "closed": closed, "close_pct": pct(closed, qual),
            # 📝 Forms
            "form_net": b["form_net"], "form_conv": b["form_conv"],
            "form_pct": pct(b["form_conv"], b["form_net"]),
            # 🗂 PestRoutes — Pending + Serviced accounts only
            "subs_sold": b["sold"], "sts_pct": pct(b["svc"], b["svc_denom"]),
            "pending": b["pending"], "multiyear_pct": pct(b["my"], b["sold"]),
            "acv": round(b["cv_sum"] / b["cv_n"], 2) if b["cv_n"] else None,
            "init_avg": round(b["init_sum"] / b["init_n"], 2) if b["init_n"] else None,
            "apay_pct": pct(b["apay"], b["apay_n"]),
        }

    # roll the per-provider buckets up into their group, keeping each group's
    # providers as expandable children (a group with 2+ providers gets an arrow).
    _SETS = ("leads", "qual_cf", "closed_set")
    _NUMS = ("form_net", "form_conv", "sold", "pending", "my", "cv_sum", "cv_n",
             "init_sum", "init_n", "apay", "apay_n", "svc", "svc_denom")
    from collections import defaultdict as _dd
    by_group: dict[str, list] = _dd(list)
    for prov, b in rows.items():
        by_group[group_of.get(prov.lower(), prov)].append((prov, b))
    vol = lambda r: -(r["leads"] + r["form_net"] + r["subs_sold"])

    out = []
    for g, members in by_group.items():
        if len(members) == 1:
            out.append(finalize(g, members[0][1]))
            continue
        comb = fresh(g)
        for _, b in members:
            for k in _SETS:
                comb[k] |= b[k]
            for k in _NUMS:
                comb[k] += b[k]
        row = finalize(g, comb)
        row["children"] = sorted((finalize(p, b) for p, b in members), key=vol)
        out.append(row)
    out.sort(key=vol)
    return {"days": days, "rows": out}


def _norm_dnis(raw) -> str:
    import re
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _source_dnis(source: str) -> set:
    """DNIS digits whose provider belongs to `source` (a group or a provider)."""
    group_of, _lbl, dnis_to_provider = _load_source_maps()
    return {d for d, prov in dnis_to_provider.items()
            if prov and (prov == source or group_of.get(prov.lower(), prov) == source)}


def _dispo_counts(conn, src_dnis: set, a_iso: str, b_iso: str) -> dict:
    """{closed|followup|unqualified: {total, items:[{name,count}]}} for a source's
    calls in [a_iso, b_iso), broken down by wrap-up disposition."""
    from collections import defaultdict as _dd
    dispo = {g: _dd(int) for g in ("closed", "followup", "unqualified")}
    for r in conn.execute(
        """SELECT c.dnis, c.dispo_group, c.wrapup_name
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
        (a_iso, b_iso)):
        if r["dispo_group"] not in dispo or _norm_dnis(r["dnis"]) not in src_dnis:
            continue
        dispo[r["dispo_group"]][r["wrapup_name"] or "(no disposition)"] += 1
    return {
        g: {"total": sum(w.values()),
            "items": sorted(({"name": k, "count": v} for k, v in w.items()),
                            key=lambda x: -x["count"])}
        for g, w in dispo.items()
    }


@app.get("/api/source_dispositions")
def api_source_dispositions(request: Request, source: str = "",
                            start_day: str = "", end_day: str = "") -> dict:
    """Disposition breakdown for ONE source over a chosen Pacific date range —
    powers the range picker on the detail overlay. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    if not (source and start_day and end_day):
        return JSONResponse({"error": "source + range required"}, status_code=400)
    from ..rangestats import utc_bounds
    a_iso, b_iso = utc_bounds(start_day, end_day)
    conn = connect(_settings.db_path)
    try:
        dispositions = _dispo_counts(conn, _source_dnis(source), a_iso, b_iso)
    finally:
        conn.close()
    return {"source": source, "start": start_day, "end": end_day,
            "dispositions": dispositions}


@app.get("/api/source_detail")
def api_source_detail(request: Request, source: str = "") -> dict:
    """Deep-dive for ONE source (a group like 'Elocal' OR a single provider like
    'Elocal San Jose'): last-week Genesys disposition breakdown + 13 weekly
    Sun-Sat series (call/form volume and PestRoutes economics). Admin only. Uses
    fixed windows (last complete week + trailing 13 weeks), not the page range."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    if not source:
        return JSONResponse({"error": "source required"}, status_code=400)

    from datetime import timedelta

    from ..periods import week_start
    group_of, label_to_provider, dnis_to_provider = _load_source_maps()

    # a provider belongs to this view if it IS the requested name (child click)
    # or rolls up into it (group click)
    def prov_matches(prov: str) -> bool:
        return bool(prov) and (prov == source or group_of.get(prov.lower(), prov) == source)

    def label_matches(label) -> bool:
        lbl = (label or "").strip()
        return prov_matches(label_to_provider.get(lbl, lbl) if lbl else "(no source)")

    src_dnis = {d for d, prov in dnis_to_provider.items() if prov_matches(prov)}

    # trailing 13 complete Sun-Sat weeks (current partial week excluded), like Home
    now = datetime.now(timezone.utc)
    this_week = week_start(now)
    N = 13
    wins = [(this_week - timedelta(weeks=N - i),
             this_week - timedelta(weeks=N - 1 - i)) for i in range(N)]
    weeks = [a.date().isoformat() for a, _b in wins]
    win_utc = [(a.astimezone(timezone.utc).isoformat(),
                b.astimezone(timezone.utc).isoformat()) for a, b in wins]
    range_start, range_end = win_utc[0][0], win_utc[-1][1]

    def wk(ts_iso: str) -> int:
        for i, (a, b) in enumerate(win_utc):
            if a <= ts_iso < b:
                return i
        return -1

    callers = [set() for _ in range(N)]       # unique dispositioned callers / week
    qual_callers = [set() for _ in range(N)]  # unique Closed/Follow-Up callers / week
    closed_callers = [set() for _ in range(N)]  # unique Closed callers / week
    forms = [0] * N
    revenue = [0.0] * N; sub_cnt = [0] * N; my_cnt = [0] * N
    cv_sum = [0.0] * N; cv_n = [0] * N; ip_sum = [0.0] * N; ip_n = [0] * N
    sold = [0] * N; serviced = [0] * N
    svc = [0] * N; svc_denom = [0] * N  # Sold->Serviced: serviced / decided outcomes

    conn = connect(_settings.db_path)
    try:
        # UNIQUE CALLERS per week (a caller who calls 3x counts once), matching the
        # Sources table's "Ph Leads"/"Qual" basis — dispositioned callers only.
        for r in conn.execute(
            """SELECT c.ani_e164 ph, c.dnis, c.dispo_group, c.start_utc
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
                 AND c.start_utc >= ? AND c.start_utc < ?""",
            (range_start, range_end)):
            if r["dispo_group"] not in ("closed", "followup", "unqualified"):
                continue
            if _norm_dnis(r["dnis"]) not in src_dnis:
                continue
            i = wk(r["start_utc"])
            if i < 0:
                continue
            callers[i].add(r["ph"])
            if r["dispo_group"] in ("closed", "followup"):
                qual_callers[i].add(r["ph"])
            if r["dispo_group"] == "closed":
                closed_callers[i].add(r["ph"])
        total_calls = [len(s) for s in callers]
        qual_calls = [len(s) for s in qual_callers]

        events, _ = _get_form_events()
        for e in (events or []):
            if e.already_customer or not any(label_matches(s) for s in e.sources):
                continue
            i = wk(e.first_touch_utc.astimezone(timezone.utc).isoformat())
            if i >= 0:
                forms[i] += 1

        for r in conn.execute(
            """SELECT s.source, s.initial_status, s.agreement_length,
                      s.contract_value, s.initial_price, s.date_added_utc
               FROM subscriptions s
               WHERE s.date_added_utc >= ? AND s.date_added_utc < ?""",
            (range_start, range_end)):
            st = r["initial_status"]
            if not label_matches(r["source"]):
                continue
            i = wk(r["date_added_utc"])
            if i < 0:
                continue
            # Sold->Serviced ratio: serviced / (serviced + cancelled + no-show),
            # by sale week — same definition as the table's "Sold->Svc%".
            if st in ("Completed", "Cancelled", "No Show"):
                svc_denom[i] += 1
                if st == "Completed":
                    svc[i] += 1
            if st not in ("Completed", "Pending"):
                continue
            sub_cnt[i] += 1; sold[i] += 1
            if st == "Completed":
                serviced[i] += 1
            if (r["agreement_length"] or 0) >= 18:
                my_cnt[i] += 1
            cv = r["contract_value"] or 0
            if cv:
                revenue[i] += cv; cv_sum[i] += cv; cv_n[i] += 1
            if r["initial_price"]:
                ip_sum[i] += r["initial_price"]; ip_n[i] += 1

        # last-week disposition breakdown (wrap-ups grouped by category)
        dispositions = _dispo_counts(conn, src_dnis, *win_utc[-1])
    finally:
        conn.close()

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    series = {
        "total_calls": total_calls, "qual_calls": qual_calls, "forms": forms,
        "qual_leads": [qual_calls[i] + forms[i] for i in range(N)],
        "qual_pct": [pct(qual_calls[i], total_calls[i]) for i in range(N)],
        "close_pct": [pct(len(closed_callers[i]), qual_calls[i]) for i in range(N)],
        "revenue": [round(x, 2) for x in revenue],
        "multiyear_pct": [pct(my_cnt[i], sub_cnt[i]) for i in range(N)],
        "acv": [round(cv_sum[i] / cv_n[i]) if cv_n[i] else None for i in range(N)],
        "avg_init": [round(ip_sum[i] / ip_n[i]) if ip_n[i] else None for i in range(N)],
        "sold": sold, "serviced": serviced,
        "sts_pct": [pct(svc[i], svc_denom[i]) for i in range(N)],
    }
    return {
        "source": source, "weeks": weeks,
        "last_week": {"start": wins[-1][0].date().isoformat(),
                      "end": (wins[-1][1] - timedelta(days=1)).date().isoformat()},
        "dispositions": dispositions, "series": series,
        "has_calls": sum(total_calls) > 0, "has_forms": sum(forms) > 0,
    }
