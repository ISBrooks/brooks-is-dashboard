"""Team Metrics tab: Team Overview, Phone (conversion, audit, leaked leads), Form Conversion, date-range tables and exports.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .. import cache
from .common import _settled_forms
from ..report import Bucket, aggregate, week_to_date_label
from ..store import connect

from .common import (
    _EXCLUDED_SOURCES, _RENEWAL_SOURCES, _audit_end_utc, _get_form_events, _own_inbound_subs,
    _perms, _settings, _sts, app,
)
from .agent_metrics import api_quality


def _rows_for(table: dict[str, dict[str, Bucket]]) -> list[dict]:
    rows: list[dict] = []
    for label in sorted(table):
        total = Bucket()
        for agent in sorted(table[label]):
            b = table[label][agent]
            rows.append({"period": label, "agent": agent, **b.row()})
            total.merge_counts(b)
        rows.append({"period": label, "agent": "__TOTAL__", **total.row()})
    return rows


@cache.cached(copy_result=False)     # ~650 KB, read-only: every caller shares one object
def _build_payload() -> dict:
    from ..adjust import adjustment_stats
    from ..periods import PACIFIC, UTC
    conn = connect(_settings.db_path)
    try:
        agg = aggregate(conn)
        # honest-conversion inputs: audit sums + hidden/leaked per period
        for r in conn.execute(
            "SELECT date, agent, qualified, sales FROM audit_daily"):
            dt = datetime.fromisoformat(r["date"] + "T12:00:00+00:00")
            from ..periods import month_label, week_label
            for label, table in ((week_label(dt), agg.weekly),
                                 (month_label(dt), agg.monthly)):
                if label in table and r["agent"] in table[label]:
                    table[label][r["agent"]].audit_qual += r["qualified"]
                    table[label][r["agent"]].audit_sales += r["sales"]
        # bound every period to the days the audit sheet covers, so the
        # honest numerator and denominator describe the SAME days — the
        # current week otherwise shows conversions the audit team hasn't
        # reviewed yet (that's how >100% happened)
        audit_bound = _audit_end_utc(conn)
        for label, agents in agg.weekly.items():
            if not any(b.audit_qual for b in agents.values()):
                continue
            a, b_ = _week_utc_bounds(label)
            b_ = min(b_, audit_bound) if audit_bound else b_
            if b_ <= a:
                continue
            for agent, st in adjustment_stats(conn, a, b_).items():
                if agent in agents:
                    agents[agent].hidden = st["hidden"]
                    agents[agent].leaked = st["leaked"]
                    agents[agent].honest_conv = st["converted"]
        for label, agents in agg.monthly.items():
            if not any(b.audit_qual for b in agents.values()):
                continue
            y, m = int(label[:4]), int(label[5:7])
            start_l = datetime(y, m, 1, tzinfo=PACIFIC)
            end_l = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=PACIFIC)
            a = start_l.astimezone(UTC).isoformat()
            b_ = end_l.astimezone(UTC).isoformat()
            b_ = min(b_, audit_bound) if audit_bound else b_
            if b_ <= a:
                continue
            for agent, st in adjustment_stats(conn, a, b_).items():
                if agent in agents:
                    agents[agent].hidden = st["hidden"]
                    agents[agent].leaked = st["leaked"]
                    agents[agent].honest_conv = st["converted"]
        cov = conn.execute(
            "SELECT COUNT(*), MIN(start_utc), MAX(start_utc) FROM calls"
        ).fetchone()
        subs = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "wtd_label": week_to_date_label(now),
        "conversion_window_days": _settings.conversion_window_days,
        "coverage": {
            "calls": cov[0], "subscriptions": subs,
            "first_call": cov[1], "last_call": cov[2],
        },
        "weekly": _rows_for(agg.weekly),
        "monthly": _rows_for(agg.monthly),
    }


# /api/data as ready-to-send JSON, rebuilt only when the payload object changes
_data_json: dict = {"src": None, "full": b"", "lite": b""}


def _json_bytes(obj) -> bytes:
    # the same encoding FastAPI's JSONResponse uses
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


@app.get("/api/data")
def api_data(request: Request) -> Response:
    payload = _build_payload()
    if _data_json["src"] is not payload:
        lite = {k: payload[k] for k in ("coverage", "generated_at") if k in payload}
        _data_json.update(src=payload, full=_json_bytes(payload), lite=_json_bytes(lite))
    # everyone gets the header's coverage line; the per-agent weekly/monthly
    # rows behind Phone Conversion + Trends need one of those permissions
    have = _perms(request)
    full = bool({"metrics.phone.conv", "metrics.phone.trends"} & have)
    return Response(_data_json["full" if full else "lite"], media_type="application/json")


@app.get("/api/formleads")
@cache.cached()
def api_formleads() -> dict:
    _, payload = _get_form_events()
    # per-source economics live behind the marketing password (/api/sources);
    # this endpoint stays team-level only
    return {k: v for k, v in payload.items() if k != "sources"}


@app.get("/api/formleads_export")
def api_formleads_export(start_day: str = "", end_day: str = ""):
    """CSV of NET form leads (deduped, already-customers excluded) whose first
    touch falls in the Pacific date range — the same rows behind the Form
    Conversion tab's Net Leads number. Sold rows are enriched with the matched
    subscription + the customer's name from FieldRoutes. Lead names for UNSOLD
    rows come from the touch feed IF the sourcing app provides them (auto-
    detected); it doesn't capture names yet, so they're blank until it does."""
    import csv as _csv
    import io
    import sqlite3 as _sq
    from datetime import timedelta

    from fastapi.responses import PlainTextResponse

    from ..periods import PACIFIC
    from ..rangestats import utc_bounds
    if not (start_day and end_day):
        return JSONResponse({"error": "start_day + end_day required"}, status_code=400)
    a_iso, b_iso = utc_bounds(start_day, end_day)
    a_dt = datetime.fromisoformat(a_iso); b_dt = datetime.fromisoformat(b_iso)

    events, _ = _get_form_events()
    net = [e for e in (events or [])
           if a_dt <= e.first_touch_utc < b_dt and not e.already_customer]

    # lead names + pest issue from the touch feed (sourcing app captures them
    # at ingest; columns auto-detected so this degrades gracefully)
    import re as _re

    def _clean_name(v: str) -> str:
        # some gmail-parsed names carry the whole email body — keep line 1 only
        v = (v or "").splitlines()[0].strip()
        return v[:60]

    def _clean_pest(v: str) -> str:
        # strip form-field labels like "what_bugs_do_you_want_to_get_rid_of?: "
        v = (v or "").splitlines()[0].strip()
        v = _re.sub(r"^[\w\s_]+\?:\s*", "", v)
        return v[:80]

    touch_names: dict[str, str] = {}
    touch_pest: dict[str, str] = {}
    try:
        src = _sq.connect(f"file:{_settings.formleads_db}?mode=ro", uri=True)
        cols = {r[1] for r in src.execute("PRAGMA table_info(touches)")}
        name_col = next((c for c in ("name", "lead_name", "full_name") if c in cols), None)
        pest_col = next((c for c in ("pest_issue", "issue", "pest") if c in cols), None)
        sel = [c for c in (name_col, pest_col) if c]
        if sel:
            for row in src.execute(
                f"SELECT phone_e164, email, {', '.join(sel)} FROM touches "
                f"WHERE ({' OR '.join(c + ' IS NOT NULL' for c in sel)}) "
                f"AND occurred_at >= ?",
                ((a_dt - timedelta(days=1)).isoformat(),)):
                ph, em = row[0], row[1]
                vals = dict(zip(sel, row[2:]))
                for k in (ph, em):
                    if not k:
                        continue
                    if name_col and vals.get(name_col):
                        touch_names.setdefault(k, _clean_name(vals[name_col]))
                    if pest_col and vals.get(pest_col):
                        touch_pest.setdefault(k, _clean_pest(vals[pest_col]))
        src.close()
    except Exception:
        pass

    # bulk sold-sub matching: identity key -> [(added_dt, sub row)]
    conn = connect(_settings.db_path)
    try:
        lo = (a_dt - timedelta(hours=2)).isoformat()
        hi = (b_dt + timedelta(days=_settings.form_conversion_window_days)).isoformat()
        by_key: dict[str, list] = {}
        for r in conn.execute(
            """SELECT s.subscription_id, s.customer_id, s.date_added_utc,
                      s.sold_by_name, s.contract_value,
                      s.phone1_e164, s.phone2_e164, c.email
               FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
               WHERE s.initial_status IN ('Completed','Pending')
                 AND s.date_added_utc >= ? AND s.date_added_utc <= ?""", (lo, hi)):
            rec = (datetime.fromisoformat(r["date_added_utc"]), r)
            for k in (r["phone1_e164"], r["phone2_e164"],
                      (r["email"] or "").strip().lower() or None):
                if k:
                    by_key.setdefault(k, []).append(rec)
    finally:
        conn.close()

    window = timedelta(days=_settings.form_conversion_window_days)
    grace = timedelta(hours=2)
    rows = []
    sold_cust_ids = set()
    for e in sorted(net, key=lambda x: x.first_touch_utc):
        keys = list(e.phones) + [m.strip().lower() for m in e.emails]
        sub = None
        for k in keys:
            for added, r in by_key.get(k, ()):
                if (e.first_touch_utc - grace) <= added <= (e.first_touch_utc + window):
                    if sub is None or added < sub[0]:
                        sub = (added, r)
        if sub:
            sold_cust_ids.add(sub[1]["customer_id"])
        local = e.first_touch_utc.astimezone(PACIFIC)
        rows.append({
            "lead_date": local.date().isoformat(),
            "lead_time": local.strftime("%H:%M"),
            "channel": e.channel,
            "sources": " | ".join(sorted(str(x) for x in e.sources)),
            "phones": " | ".join(sorted(e.phones)),
            "email": " | ".join(sorted(e.emails)),
            "submissions": e.raw_count,
            "lead_name": next((touch_names[k] for k in keys if k in touch_names), ""),
            "pest_issue": next((touch_pest[k] for k in keys if k in touch_pest), ""),
            "sold": "YES" if sub else "",
            "sold_date": sub[1]["date_added_utc"][:10] if sub else "",
            "sold_rep": (sub[1]["sold_by_name"] or "") if sub else "",
            "contract_value": sub[1]["contract_value"] if sub else "",
            "customer_id": sub[1]["customer_id"] if sub else "",
        })

    # customer names for sold rows, live from FieldRoutes
    names: dict[str, tuple] = {}
    if sold_cust_ids:
        try:
            from ..fieldroutes import FieldRoutesClient
            ids = sorted(sold_cust_ids)
            with FieldRoutesClient(_settings.pestroutes_base_url,
                                   _settings.pestroutes_auth_key,
                                   _settings.pestroutes_auth_token) as fr:
                for i in range(0, len(ids), 200):
                    got = fr.request("customer", "get", {
                        "customerIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
                    for c in got.get("customers") or []:
                        names[str(c.get("customerID"))] = (
                            (c.get("fname") or "").strip(), (c.get("lname") or "").strip())
        except Exception:
            pass  # export still works without names

    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["lead_date", "lead_time", "first_name", "last_name", "phone",
                "email", "pest_issue", "channel", "sold", "sold_date",
                "sold_rep", "contract_value", "fr_customer_id"])
    for r in rows:
        fn, ln = names.get(str(r["customer_id"]), ("", ""))
        if not (fn or ln) and r["lead_name"]:
            parts = r["lead_name"].split(None, 1)
            fn, ln = parts[0], (parts[1] if len(parts) > 1 else "")
        w.writerow([r["lead_date"], r["lead_time"], fn, ln, r["phones"],
                    r["email"], r["pest_issue"], r["channel"], r["sold"],
                    r["sold_date"], r["sold_rep"], r["contract_value"],
                    r["customer_id"]])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="net_form_leads_{start_day}_to_{end_day}.csv"'})


@app.get("/api/range")
@cache.cached()
def api_range(start: str, end: str) -> dict:
    from ..rangestats import range_stats, utc_bounds
    conn = connect(_settings.db_path)
    try:
        payload = range_stats(conn, start, end, _EXCLUDED_SOURCES,
                              _audit_end_utc(conn))
    finally:
        conn.close()
    # team-level form-lead funnel for the same range
    events, _ = _get_form_events()
    a, b = utc_bounds(start, end)
    a_dt = datetime.fromisoformat(a)
    b_dt = datetime.fromisoformat(b)
    f = {"forms": 0, "deduped": 0, "existing": 0, "net": 0, "converted": 0}
    for e in (events or []):
        if not (a_dt <= e.first_touch_utc < b_dt):
            continue
        f["forms"] += e.raw_count       # raw submissions
        f["deduped"] += 1               # unique lead events
        if e.already_customer:
            f["existing"] += 1
            continue
        f["net"] += 1
        if e.converted:
            f["converted"] += 1
    f["conv_pct"] = round(100.0 * f["converted"] / f["net"], 1) if f["net"] else 0.0
    payload["forms"] = f
    return payload


@app.get("/api/export")
def api_export(dataset: str, start: str, end: str):
    """CSV download of the RECORDS behind a metric for a date range.
    dataset: leads | accounts | adjustments"""
    import csv
    import io

    from fastapi.responses import Response

    from ..rangestats import export_accounts, export_adjustments, export_leads
    conn = connect(_settings.db_path)
    try:
        if dataset == "leads":
            rows = export_leads(conn, start, end)
        elif dataset == "accounts":
            rows = export_accounts(conn, start, end, _EXCLUDED_SOURCES)
        elif dataset == "adjustments":
            rows = export_adjustments(conn, start, end, _audit_end_utc(conn))
        else:
            return Response("unknown dataset", status_code=400)
    finally:
        conn.close()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    else:
        buf.write("no records in range\n")
    fname = f"{dataset}_{start}_to_{end}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/leaked")
@cache.cached(ttl=lambda a: None if (a.get("start_day") and a.get("end_day")) else 300)
def api_leaked(days: int = 30, start_day: str = "", end_day: str = "") -> dict:
    """Leaked-leads report: callers an agent didn't pitch (marked unqualified
    or other) who bought from a different seller within 14 days."""
    from datetime import timedelta

    from ..adjust import adjustment_stats
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from ..rangestats import utc_bounds
        start, end = utc_bounds(start_day, end_day)
    else:
        days = max(1, min(days, 400))
        start, end = (now - timedelta(days=days)).isoformat(), now.isoformat()
    conn = connect(_settings.db_path)
    try:
        adj = adjustment_stats(conn, start, end, detail=True)
    finally:
        conn.close()
    summary = [{"agent": a, "leaked": s["leaked"], "cc_leaked": s["cc_leaked"],
                "hidden": s["hidden"]}
               for a, s in sorted(adj.items(), key=lambda kv: -kv[1]["leaked"])
               if a != "__TEAM__"]
    rows = [r for s in adj.values() for r in s["rows"]]
    rows.sort(key=lambda r: r["call_date"], reverse=True)
    return {"days": days, "summary": summary, "rows": rows}


def _phone_queue_stats(start_day: str, end_day: str) -> dict:
    """The Inside Sales queues' Genesys figures for a range: asked at most every
    five minutes, and an older answer comes back at once while it is refreshed."""
    from ..rangestats import utc_bounds
    a, b = utc_bounds(start_day, end_day)

    def fetch():
        from ..genesys import GenesysClient
        with GenesysClient(_settings.genesys_region, _settings.genesys_client_id,
                           _settings.genesys_client_secret) as g:
            return g.queue_stats(datetime.fromisoformat(a), datetime.fromisoformat(b),
                                 _settings.genesys_queue_ids)
    try:
        return cache.keep_fresh(("queue_stats", start_day, end_day), fetch, 300)
    except Exception as e:  # don't fail the whole tab if Genesys hiccups
        return {"error": str(e)[:120]}


@app.get("/api/teammetrics")
def api_teammetrics(start_day: str, end_day: str, mode: str = "sold",
                    phone: bool = True) -> dict:
    """Total / Josh / Kai team metrics for a date range. Sales metrics per
    team; form + phone-queue metrics are team-total only. The database part is
    saved (_teammetrics_db); the Genesys queue figures are added on top."""
    out = _teammetrics_db(start_day, end_day, mode)          # a private copy: safe to add to
    if phone:
        out["total"]["phone_metrics"] = _phone_queue_stats(start_day, end_day)
    return out


@cache.cached()
def _teammetrics_db(start_day: str, end_day: str, mode: str = "sold") -> dict:
    """Total / Josh / Kai team metrics for a date range, from the database:
    sales metrics per team; form metrics team-total only."""
    from ..rangestats import utc_bounds
    a, b = utc_bounds(start_day, end_day)
    serviced_mode = mode == "serviced"

    conn = connect(_settings.db_path)
    try:
        # key agents -> team; FR id -> team; and per-agent audit lookup
        name_team: dict[str, str] = {}
        fr_team: dict[str, str] = {}
        fr_gid: dict[str, str] = {}          # FR employee id -> the agent's Genesys id
        for r in conn.execute(
            "SELECT genesys_name, genesys_user_id, fr_employee_id, team FROM agent_map "
            "WHERE scorecard = 1"):
            t = r["team"] or "none"
            name_team[r["genesys_name"]] = t
            for fid in (r["fr_employee_id"] or "").split(","):
                if fid:
                    fr_team[fid] = t
                    fr_gid[fid] = r["genesys_user_id"] or ""
        own_inbound = _own_inbound_subs(conn)   # sales tied to the seller's own inbound call

        def blank():
            return {"subs": 0, "m18": 0, "m24": 0, "cv": 0.0, "cv_n": 0,
                    "rev_cv": 0.0, "rev_n": 0, "inb_n": 0,
                    "ip": 0.0, "ip_n": 0, "apay": 0, "apay_n": 0,
                    "recurring": 0, "monthly": 0, "signed": 0,
                    "aud_q": 0, "aud_s": 0}
        buckets = {"total": blank(), "josh": blank(), "kai": blank()}

        def add(team, fn):
            fn(buckets["total"])
            if team in ("josh", "kai"):
                fn(buckets[team])

        # subscription metrics. REVENUE counts mosquito/upsells but not renewals;
        # everything else excludes mosquito (current rule).
        mosq = {s.upper() for s in _EXCLUDED_SOURCES}
        date_clause = ("s.initial_service_date >= ? AND s.initial_service_date <= ?"
                       if serviced_mode else
                       "s.date_added_utc >= ? AND s.date_added_utc < ?")
        date_args = (start_day, end_day) if serviced_mode else (a, b)
        for r in conn.execute(
            f"""SELECT s.subscription_id, s.sold_by, s.agreement_length, s.contract_value,
                       s.initial_price, s.billing_frequency, s.service_frequency,
                       s.signed_agreement, s.source, c.card_on_file
                FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
                WHERE {date_clause} AND s.initial_status IN ('Completed','Pending')""",
            date_args):
            team = fr_team.get(r["sold_by"])
            if team is None:
                continue
            inbound = str(r["subscription_id"]) in own_inbound.get(fr_gid.get(r["sold_by"], ""), ())

            def upd(k, r=r, inbound=inbound):
                src = r["source"] or ""
                cv = r["contract_value"] or 0
                # HEADLINE Total Sales / Total Revenue: mosquito + upsells IN,
                # renewals out (matches the manager's with-mosquito Sales Report).
                # Everything below the mosquito skip keeps excluding them.
                if src not in _RENEWAL_SOURCES:
                    k["rev_n"] += 1
                    if inbound:
                        k["inb_n"] += 1
                    if cv:
                        k["rev_cv"] += cv
                if src.upper() in mosq:
                    return                   # everything below excludes mosquito
                k["subs"] += 1
                months = r["agreement_length"] or 0
                if months >= 24:
                    k["m24"] += 1
                elif months >= 18:
                    k["m18"] += 1
                if cv:
                    k["cv"] += cv; k["cv_n"] += 1
                if r["initial_price"]:
                    k["ip"] += r["initial_price"]; k["ip_n"] += 1
                if r["card_on_file"] is not None:
                    k["apay_n"] += 1
                    if r["card_on_file"]:
                        k["apay"] += 1
                sf = r["service_frequency"]
                if sf is not None and sf != -1:  # recurring (not one-time)
                    k["recurring"] += 1
                    bf = r["billing_frequency"]
                    if bf == 30 or (bf == -1 and sf == 30):
                        k["monthly"] += 1
                if r["signed_agreement"]:  # real COMPLETED contract, not a draft
                    k["signed"] += 1
            add(team, upd)

        # sold -> serviced per team: every first-service status (the ratio is
        # serviced / (serviced + cancelled + no-show)), same dates and upsell
        # exclusions as the agent sheet
        sts_rows: dict[str, list] = {"total": [], "josh": [], "kai": []}
        for r in conn.execute(
            f"""SELECT s.sold_by, s.initial_status, s.source FROM subscriptions s
                WHERE {date_clause}""", date_args):
            team = fr_team.get(r["sold_by"])
            if team is None or (r["source"] or "").upper() in mosq:
                continue
            sts_rows["total"].append(r["initial_status"])
            if team in ("josh", "kai"):
                sts_rows[team].append(r["initial_status"])

        # audit sheet conversion per team
        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) s FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent", (start_day, end_day)):
            team = name_team.get(r["agent"])
            if team is None:
                continue
            add(team, lambda k, r=r: (k.__setitem__("aud_q", k["aud_q"] + r["q"]),
                                      k.__setitem__("aud_s", k["aud_s"] + r["s"])))

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        def finish(k):
            return {
                "phone_conversion": pct(k["aud_s"], k["aud_q"]),
                # Total Sales / Total Revenue are the HEADLINE pair: mosquito +
                # upsells INCLUDED, renewals excluded (rev_n/rev_cv). All quality
                # metrics below still run on k["subs"]/k["cv"] (upsells excluded),
                # so their denominators are untouched by this pair.
                "total_sales": k["rev_n"], "audit_sales": k["aud_s"],
                # of Total Sales, the ones not tied to the seller's own inbound call
                "outbound_sales": k["rev_n"] - k["inb_n"],
                "total_revenue": round(k["rev_cv"], 2),
                "multiyear_pct": pct(k["m18"] + k["m24"], k["subs"]),
                "multi24_pct": pct(k["m24"], k["subs"]),
                "multi18_pct": pct(k["m18"], k["subs"]),
                "acv": round(k["cv"] / k["cv_n"], 2) if k["cv_n"] else None,
                "avg_initial": round(k["ip"] / k["ip_n"], 2) if k["ip_n"] else None,
                "apay_pct": pct(k["apay"], k["apay_n"]),
                "monthly_billing_pct": pct(k["monthly"], k["recurring"]),
                "signed_doc_pct": pct(k["signed"], k["subs"]),
            }
        out = {t: finish(buckets[t]) for t in buckets}
        for t in out:
            out[t]["sts_pct"] = _sts(sts_rows[t])["sts_pct"]

        # --- team-total only: form conversion + all leads ---
        events, _ = _get_form_events()
        a_dt = datetime.fromisoformat(a)
        b_dt = datetime.fromisoformat(b)
        form_net = form_conv = 0
        for e in (events or []):
            if not (a_dt <= e.first_touch_utc < b_dt) or e.already_customer:
                continue
            form_net += 1
            if e.converted:
                form_conv += 1
        tot = buckets["total"]
        out["total"]["form_conversion"] = pct(form_conv, form_net)
        out["total"]["form_net"] = form_net
        out["total"]["form_converted"] = form_conv
        # total leads = (sheet sales + form conv) / (sheet qual + form net)
        denom = tot["aud_q"] + form_net
        out["total"]["total_leads_conversion"] = \
            pct(tot["aud_s"] + form_conv, denom)
    finally:
        conn.close()

    out["start"] = start_day
    out["end"] = end_day
    return out


_TEAM_OV_METRICS = ("total_revenue", "total_sales", "outbound_sales", "total_leads_conversion",
                    "phone_conversion", "form_conversion", "acv", "multiyear_pct",
                    "multi24_pct", "multi18_pct", "sts_pct", "avg_initial", "apay_pct",
                    "monthly_billing_pct", "signed_doc_pct", "quality_score")


_TEAM_OV_CALLS = ("calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s")


@cache.cached()
@_settled_forms
def _team_overview_db() -> dict:
    """The database part of Team Overview (everything except the Genesys
    call-handling cards, which api_team_overview adds per request).

    Team Metrics -> Team Overview cards: every team metric for the whole
    team, Josh's team and Kai's team over the last 30 and 7 COMPLETE days
    (each against the period before it) plus a 13-week trend. Sold between,
    the same as Home. Built from the same per-range function the any-dates
    table uses, so the two can't disagree. Cached until the day or data changes."""
    from datetime import timedelta

    from ..periods import PACIFIC
    from ..voiceops import pacific_weeks
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    end = today - timedelta(days=1)

    def win(days: int, back: int) -> tuple[str, str]:
        e = end - timedelta(days=days * back)
        return (e - timedelta(days=days - 1)).isoformat(), e.isoformat()
    ranges = {"d30": win(30, 0), "p30": win(30, 1), "d7": win(7, 0), "p7": win(7, 1)}
    weeks = pacific_weeks(13)

    def pull(s: str, e: str) -> dict:
        d = api_teammetrics(start_day=s, end_day=e, mode="sold", phone=False)
        try:                           # VoiceOps: cached scores only, never a live pull here
            q = api_quality(start_day=s, end_day=e, fetch=0).get("teams") or {}
        except Exception:
            q = {}
        for t in ("total", "josh", "kai"):
            d[t]["quality_score"] = q.get(t)
        return d
    w = {k: pull(*v) for k, v in ranges.items()}
    series = [pull(s, e) for s, e in weeks]
    teams = {}
    for t in ("total", "josh", "kai"):
        teams[t] = {m: {"d30": w["d30"][t].get(m), "p30": w["p30"][t].get(m),
                        "d7": w["d7"][t].get(m), "p7": w["p7"][t].get(m),
                        "series": [x[t].get(m) for x in series]}
                    for m in _TEAM_OV_METRICS}
        teams[t]["_sales"] = {"d30": w["d30"][t].get("total_sales"),
                              "d7": w["d7"][t].get("total_sales")}
    return {"ranges": ranges, "weeks": weeks, "teams": teams, "end": end.isoformat()}


@app.get("/api/team_overview")
def api_team_overview() -> dict:
    """Team Overview cards. The database part is saved until its data changes
    (_team_overview_db, ~5 s to build); the Genesys call-handling cards are
    added here on every request from Genesys's own short-lived cache, so a
    Genesys hiccup is never saved."""
    from datetime import date as _date
    core = _team_overview_db()           # a private copy: safe to add to
    ranges, weeks, teams = core["ranges"], core["weeks"], core["teams"]
    end = _date.fromisoformat(core["end"])
    # Genesys call handling per team (calls taken / missed, talk, after-call
    # work): one per-day pull covers every window, summed over each team's reps
    try:

        from .. import mastermetric
        first = min(ranges["p30"][0], weeks[0][0] if weeks else ranges["p30"][0])
        daily = mastermetric.genesys_daily(_settings, _date.fromisoformat(first), end)
        conn = connect(_settings.db_path)
        try:
            uid_team = {r["genesys_user_id"]: (r["team"] or "none") for r in conn.execute(
                "SELECT genesys_user_id, team FROM agent_map "
                "WHERE scorecard = 1 AND genesys_user_id IS NOT NULL")}
        finally:
            conn.close()

        def calls(team: str, s: str, e: str) -> dict:
            tot = {"answered": 0, "missed": 0, "talk_ms": 0, "talk_n": 0, "acw_ms": 0, "acw_n": 0}
            for uid, t in uid_team.items():
                if team != "total" and t != team:
                    continue
                for day, g in (daily.get(uid) or {}).items():
                    if s <= day <= e:
                        for k in tot:
                            tot[k] += g[k]
            return {"calls_taken": tot["answered"], "calls_missed": tot["missed"],
                    "ans_missed": round(tot["answered"] / tot["missed"], 2) if tot["missed"] else None,
                    "avg_talk_s": round(tot["talk_ms"] / tot["talk_n"] / 1000) if tot["talk_n"] else None,
                    "avg_acw_s": round(tot["acw_ms"] / tot["acw_n"] / 1000) if tot["acw_n"] else None}
        for t in ("total", "josh", "kai"):
            cw = {k: calls(t, *v) for k, v in ranges.items()}
            cs = [calls(t, s, e) for s, e in weeks]
            for m in _TEAM_OV_CALLS:
                teams[t][m] = {"d30": cw["d30"][m], "p30": cw["p30"][m], "d7": cw["d7"][m],
                               "p7": cw["p7"][m], "series": [x[m] for x in cs]}
    except Exception:      # Genesys down: those cards show a dash, the rest still load
        logging.getLogger("isadash.teamoverview").exception("team overview: Genesys part failed")
    payload = {"ranges": {k: {"start": v[0], "end": v[1]} for k, v in ranges.items()},
               "weeks": [s for s, _ in weeks], "teams": teams}
    return payload


def _week_utc_bounds(label: str) -> tuple[str, str]:
    """'YYYY-MM-DD..YYYY-MM-DD' (Pacific Sun–Sat) -> [start, end) in UTC ISO."""
    from datetime import timedelta

    from ..periods import PACIFIC, UTC
    start_local = datetime.fromisoformat(label[:10] + "T00:00:00").replace(tzinfo=PACIFIC)
    end_local = start_local + timedelta(days=7)
    return (start_local.astimezone(UTC).isoformat(),
            end_local.astimezone(UTC).isoformat())


@app.get("/api/audit")
@cache.cached(copy_result=False)     # large and read-only: shared, as it always was
def api_audit() -> dict:
    """Audited (human) qualified/sales vs system dispositions/conversions,
    weekly Sun–Sat per agent, plus hidden/leaked adjustment terms."""
    from datetime import timezone as _tz

    from ..adjust import adjustment_stats
    from ..periods import week_label
    conn = connect(_settings.db_path)
    try:
        audit = conn.execute("SELECT date, agent, qualified, sales FROM audit_daily").fetchall()
        weeks: dict[str, dict[str, dict]] = {}
        for r in audit:
            # audit dates are Pacific calendar days; anchor at noon UTC so the
            # Sun–Sat week bucket is unambiguous
            dt = datetime.fromisoformat(r["date"] + "T12:00:00+00:00")
            w = week_label(dt)
            b = weeks.setdefault(w, {}).setdefault(r["agent"], {
                "audit_qualified": 0, "audit_sales": 0,
                "sys_qualified": 0, "sys_conversions": 0, "sys_unqualified": 0,
                "hidden": 0, "leaked": 0, "adj_conversions": 0,
            })
            b["audit_qualified"] += r["qualified"]
            b["audit_sales"] += r["sales"]
        # Only compare weeks where system call data exists — audit history
        # reaches back to 2023, Genesys pull starts 2025-06-29.
        first_call = conn.execute("SELECT MIN(start_utc) FROM calls").fetchone()[0]
        if first_call:
            dt = datetime.fromisoformat(first_call)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            min_week = week_label(dt)
            weeks = {w: v for w, v in weeks.items() if w >= min_week}
        if weeks:
            q = """
                SELECT c.start_utc, c.dispo_group, c.converted_sub_id,
                       c.converted_any_sub_id, c.ani_e164,
                       m.genesys_name AS agent
                FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
                WHERE m.on_roster = 1
            """
            cf_p: dict[tuple, set] = {}
            own_p: dict[tuple, set] = {}
            any_p: dict[tuple, set] = {}
            unq: dict[tuple, int] = {}
            for r in conn.execute(q):
                dt = datetime.fromisoformat(r["start_utc"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                w = week_label(dt)
                if w not in weeks:
                    continue
                k = (w, r["agent"])
                ph = r["ani_e164"]
                if r["dispo_group"] in ("closed", "followup") and ph:
                    cf_p.setdefault(k, set()).add(ph)
                elif r["dispo_group"] == "unqualified":
                    unq[k] = unq.get(k, 0) + 1
                if ph and r["converted_sub_id"]:
                    own_p.setdefault(k, set()).add(ph)
                if ph and r["converted_any_sub_id"]:
                    any_p.setdefault(k, set()).add(ph)
            for k in set(cf_p) | set(own_p) | set(any_p):
                w, agent = k
                if agent not in weeks.get(w, {}):
                    continue
                cf = cf_p.get(k, set())
                conv = own_p.get(k, set()) | (cf & any_p.get(k, set()))
                weeks[w][agent]["sys_qualified"] = len(cf | conv)
                weeks[w][agent]["sys_conversions"] = len(conv)
            for (w, agent), n in unq.items():
                if agent in weeks.get(w, {}):
                    weeks[w][agent]["sys_unqualified"] = n
        # hidden / leaked / bounded numerator, per week (never past the
        # last audited day)
        audit_bound = _audit_end_utc(conn)
        for w in weeks:
            a, b_ = _week_utc_bounds(w)
            if audit_bound:
                b_ = min(b_, audit_bound)
            if b_ <= a:
                continue
            adj = adjustment_stats(conn, a, b_)
            for agent, st in adj.items():
                if agent != "__TEAM__" and agent in weeks[w]:
                    weeks[w][agent]["hidden"] = st["hidden"]
                    weeks[w][agent]["leaked"] = st["leaked"]
                    weeks[w][agent]["adj_conversions"] = st["converted"]
        rows = []
        for w in sorted(weeks):
            for agent in sorted(weeks[w]):
                rows.append({"period": w, "agent": agent, **weeks[w][agent]})
        return {"rows": rows, "has_data": bool(rows)}
    finally:
        conn.close()
