"""Agent Metrics tab: IS #'s, Power Score export, quality, Master Metric, Inbound vs Other, Compare Agents.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from .. import cache
from ..store import connect

from .common import (
    _EXCLUDED_SOURCES, _RENEWAL_SOURCES, _audit_end_utc, _inbound_split, _load_config, _mm_only,
    _own_inbound_subs, _perf_log, _roster_sub_rows, _scorecard_calls, _settings, _sts,
    _valid_range, app,
)


def _bucket(months: int) -> str:
    # one-time treatments (agreementLength=0) are "below 12 mo" by policy
    if months < 12:
        return "lt12"
    if months < 18:
        return "m12"
    if months < 24:
        return "m18"
    return "m24"


@app.get("/api/agentsubs")
def api_agentsubs() -> dict:
    """Per-agent per-period subscription metrics: duration buckets, multiyear
    % (18mo+), and average/total contract value."""
    from ..periods import month_label, week_label
    conn = connect(_settings.db_path)
    try:
        subs = _roster_sub_rows(conn)
    finally:
        conn.close()

    def blank() -> dict:
        return {"subs": 0, "pending": 0, "lt12": 0, "m12": 0, "m18": 0,
                "m24": 0, "cv_total": 0.0, "cv_n": 0,
                "ip_total": 0.0, "ip_n": 0}

    weekly: dict[str, dict[str, dict]] = {}
    monthly: dict[str, dict[str, dict]] = {}
    # _roster_sub_rows gained a 7th value (signed agreement) and this feed was
    # never updated, so it had been failing; nothing on the page uses it today
    for agent, added, months, cv, status, ip, *_ in subs:
        dt = datetime.fromisoformat(added)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        for label, table in ((week_label(dt), weekly), (month_label(dt), monthly)):
            b = table.setdefault(label, {}).setdefault(agent, blank())
            b["subs"] += 1
            if status == "Pending":
                b["pending"] += 1
            b[_bucket(months)] += 1
            if cv > 0:
                b["cv_total"] += cv
                b["cv_n"] += 1
            if ip > 0:
                b["ip_total"] += ip
                b["ip_n"] += 1

    def finish(table):
        rows = []
        for label in sorted(table):
            total = blank()
            for agent in sorted(table[label]):
                b = table[label][agent]
                for k in total:
                    total[k] += b[k]
                rows.append({"period": label, "agent": agent, **_derive(b)})
            rows.append({"period": label, "agent": "__TOTAL__", **_derive(total)})
        return rows

    def _derive(b):
        multi = b["m18"] + b["m24"]
        n = b["subs"]
        def pct(x):
            return round(100.0 * x / n, 1) if n else 0.0
        return {**{k: b[k] for k in ("subs", "pending", "lt12", "m12", "m18",
                                     "m24")},
                "lt12_pct": pct(b["lt12"]), "m12_pct": pct(b["m12"]),
                "m18_pct": pct(b["m18"]), "m24_pct": pct(b["m24"]),
                "multiyear_pct": pct(multi),
                "acv": round(b["cv_total"] / b["cv_n"], 2) if b["cv_n"] else 0.0,
                "cv_total": round(b["cv_total"], 2),
                "init_avg": round(b["ip_total"] / b["ip_n"], 2) if b["ip_n"] else 0.0}

    return {"weekly": finish(weekly), "monthly": finish(monthly)}


# With no dates given the window is "the last N days up to this minute", which
# drifts with the clock - keep that form only briefly. Explicit dates are saved
# until the data changes.
@app.get("/api/scorecard")
@cache.cached(ttl=lambda a: None if (a.get("start_day") and a.get("end_day")) else 300)
def api_scorecard(start_day: str = "", end_day: str = "", days: int = 30,
                  mode: str = "sold") -> dict:
    """Per-agent pay-period scorecard (the agents flagged scorecard=1).
    mode='sold'    -> subs whose DATE SOLD is in range (Completed/Pending).
    mode='serviced'-> subs whose INITIAL SERVICE DATE is in range — for past
                     ranges this leaves only serviced accounts (pending ones
                     resolve to serviced or cancel out).
    Conversion is the audit sheet's sales/qualified (always by day range)."""
    from datetime import timedelta

    from ..rangestats import utc_bounds
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        a, b = utc_bounds(start_day, end_day)
        sd, ed = start_day, end_day
    else:
        days = max(1, min(days, 400))
        a, b = (now - timedelta(days=days)).isoformat(), now.isoformat()
        sd, ed = a[:10], now.date().isoformat()
    serviced_mode = mode == "serviced"

    conn = connect(_settings.db_path)
    try:
        # scorecard agents + their FR employee id set; power-score flag too
        agents: dict[str, set] = {}
        powerscore_set: set = set()
        team_of: dict[str, str] = {}
        gid_of: dict[str, str] = {}
        for r in conn.execute(
            "SELECT genesys_name, genesys_user_id, fr_employee_id, powerscore, team FROM agent_map "
            "WHERE scorecard = 1 AND fr_employee_id IS NOT NULL"):
            agents[r["genesys_name"]] = set(r["fr_employee_id"].split(","))
            gid_of[r["genesys_name"]] = r["genesys_user_id"] or ""
            team_of[r["genesys_name"]] = r["team"] or ""
            if r["powerscore"]:
                powerscore_set.add(r["genesys_name"])
        fr_to_agent = {fid: name for name, ids in agents.items() for fid in ids}

        stat = {name: {"subs": 0, "m18": 0, "m24": 0, "cv": 0.0, "cv_n": 0,
                       "rev_cv": 0.0, "ip": 0.0, "ip_n": 0, "apay": 0, "apay_n": 0,
                       "sg": 0, "sg_n": 0, "recurring": 0, "monthly": 0, "inb": 0}
                for name in agents}
        own_inbound = _own_inbound_subs(conn)     # sales tied to the agent's own inbound call

        mosq = {s.upper() for s in _EXCLUDED_SOURCES}
        if serviced_mode:
            date_clause = "s.initial_service_date >= ? AND s.initial_service_date <= ?"
            date_args = (sd, ed)
        else:
            date_clause = "s.date_added_utc >= ? AND s.date_added_utc < ?"
            date_args = (a, b)
        # Pull EVERY source; split per-metric: REVENUE counts mosquito/upsells
        # but not renewals; ACV/multiyear/subs exclude mosquito (current rule).
        for r in conn.execute(
            f"""SELECT s.subscription_id, s.sold_by, s.agreement_length, s.contract_value,
                       s.initial_price, s.source, s.signed_agreement,
                       s.billing_frequency, s.service_frequency, c.card_on_file
                FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
                WHERE {date_clause}
                  AND s.initial_status IN ('Completed','Pending')""",
            date_args):
            name = fr_to_agent.get(r["sold_by"])
            if not name:
                continue
            k = stat[name]
            src = r["source"] or ""
            cv = r["contract_value"] or 0
            if cv and src not in _RENEWAL_SOURCES:
                k["rev_cv"] += cv            # revenue: mosquito in, renewals out
            if src.upper() in mosq:
                continue                     # everything below excludes mosquito
            k["subs"] += 1
            if str(r["subscription_id"]) in own_inbound.get(gid_of.get(name, ""), ()):
                k["inb"] += 1
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
            if r["signed_agreement"] is not None:
                k["sg_n"] += 1
                if r["signed_agreement"]:
                    k["sg"] += 1
            sf = r["service_frequency"]
            if sf is not None and sf != -1:  # recurring (not one-time)
                k["recurring"] += 1
                bf = r["billing_frequency"]
                if bf == 30 or (bf == -1 and sf == 30):
                    k["monthly"] += 1

        # sold -> serviced per agent: same dates and upsell exclusions as the
        # rest of the sheet, but EVERY first-service status, since the ratio
        # is serviced / (serviced + cancelled + no-show)
        sts_by: dict[str, list] = {name: [] for name in agents}
        for r in conn.execute(
            f"""SELECT s.sold_by, s.initial_status, s.source FROM subscriptions s
                WHERE {date_clause}""", date_args):
            name = fr_to_agent.get(r["sold_by"])
            if name and (r["source"] or "").upper() not in mosq:
                sts_by[name].append(r["initial_status"])

        # audit-sheet conversion per agent
        conv = {name: [0, 0] for name in agents}  # [qual, sales]
        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) sa FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent", (sd, ed)):
            if r["agent"] in conv:
                conv[r["agent"]] = [r["q"], r["sa"]]

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        rows = []
        for name in sorted(agents):
            k = stat[name]; q, sa = conv[name]
            n = k["subs"]
            my18 = k["m18"]; my24 = k["m24"]
            rows.append({
                "agent": name,
                "conversion_pct": pct(sa, q),
                "call_grade": None,   # no data source in the dashboard
                "acv": round(k["cv"] / k["cv_n"], 0) if k["cv_n"] else None,
                "multi_total_pct": pct(my18 + my24, n),
                "multi24_pct": pct(my24, n),
                # IS#'s / Power Score revenue: upsells EXCLUDED (renewals kept,
                # matching the manager's report). rev_all keeps upsells in and
                # renewals out — used only by the Home YTD table.
                "rev": round(k["cv"], 2),
                "rev_all": round(k["rev_cv"], 2),
                "apay_pct": pct(k["apay"], k["apay_n"]),
                "multi18_pct": pct(my18, n),
                "aip": round(k["ip"] / k["ip_n"], 2) if k["ip_n"] else None,
                "signed_pct": pct(k["sg"], k["sg_n"]),
                "monthly_pct": pct(k["monthly"], k["recurring"]),
                "subs": n,
                # Total Sales NOT tied to an inbound call this agent took (see
                # _own_inbound_subs) — the outbound / other-channel count
                "outbound_subs": n - k["inb"],
                "sts_pct": _sts(sts_by[name])["sts_pct"],
                "team": team_of.get(name, ""),
                "powerscore": name in powerscore_set,
            })
        # the team as one pooled book of business (every sub / every audited
        # call counted once) — what the agent page compares each rep against.
        # Revenue is the average per rep who had any counted subs.
        tot = {f: sum(k[f] for k in stat.values()) for f in next(iter(stat.values()), {})}
        tq = sum(v[0] or 0 for v in conv.values())
        ts = sum(v[1] or 0 for v in conv.values())
        team = None
        if tot:
            sellers = sum(1 for k in stat.values() if k["subs"])
            tn = tot["subs"]
            team = {
                "agent": "Team average",
                "conversion_pct": pct(ts, tq),
                "call_grade": None,
                "acv": round(tot["cv"] / tot["cv_n"], 0) if tot["cv_n"] else None,
                "multi_total_pct": pct(tot["m18"] + tot["m24"], tn),
                "multi24_pct": pct(tot["m24"], tn),
                "rev": round(tot["cv"] / sellers, 2) if sellers else None,
                "apay_pct": pct(tot["apay"], tot["apay_n"]),
                "multi18_pct": pct(tot["m18"], tn),
                "aip": round(tot["ip"] / tot["ip_n"], 2) if tot["ip_n"] else None,
                "signed_pct": pct(tot["sg"], tot["sg_n"]),
                "monthly_pct": pct(tot["monthly"], tot["recurring"]),
                "subs": round(tn / sellers, 1) if sellers else None,
                "outbound_subs": round((tn - tot["inb"]) / sellers, 1) if sellers else None,
                "sts_pct": _sts([st for v in sts_by.values() for st in v])["sts_pct"],
            }
        return {"start": sd, "end": ed, "rows": rows, "team": team,
                "tiers": _load_config(conn)["scorecard_tiers"]}
    finally:
        conn.close()


@app.get("/api/inbound_split")
@cache.cached(keep=lambda v: isinstance(v, dict))
def api_inbound_split(start_day: str = "", end_day: str = ""):
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    return _inbound_split(start_day, end_day)


@app.get("/api/scorecard_calls")
def api_scorecard_calls(start_day: str = "", end_day: str = ""):
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return _scorecard_calls(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.scorecard").exception("scorecard calls failed")
        return JSONResponse({"error": f"Genesys didn't answer ({type(e).__name__})."}, status_code=502)


@app.get("/api/scorecard_mm")
def api_scorecard_mm(start_day: str = "", end_day: str = ""):
    """IS #'s: each agent's MM (Master Metric) for the range — filled in after the sheet renders."""
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return _mm_only(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.mastermetric").exception("scorecard mm failed")
        return JSONResponse({"error": f"Couldn't build MM ({type(e).__name__})."}, status_code=502)


@app.get("/api/mastermetric")
def api_mastermetric(start_day: str = "", end_day: str = ""):
    """Agent Metrics -> Master Metric: productivity + availability per agent
    (Genesys calls, Homebase hours from the daily upload sheet, Podium
    outbound). See mastermetric.py for the formulas."""
    from datetime import date

    from .. import mastermetric
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return mastermetric.build(conn, _settings, date.fromisoformat(start_day),
                                  date.fromisoformat(end_day))
    except Exception as e:           # Genesys down etc. — say so instead of a blank tab
        logging.getLogger("isadash.mastermetric").exception("master metric failed")
        return JSONResponse({"error": f"Couldn't build the Master Metric: {type(e).__name__}: {e}"[:300]},
                            status_code=502)
    finally:
        conn.close()


@app.post("/api/powerscore_export")
def api_powerscore_export(body: dict):
    """Excel workbook for the Power Score view.

    Tab 1 is the table EXACTLY as the browser rendered it: the browser computes
    the scores (config-driven, client-side), so it sends the finished rows and
    this endpoint writes them verbatim — no second copy of the scoring logic to
    drift. Tab 2 is every PestRoutes account that fed those numbers, selected
    with the same rules as /api/scorecard (power-score agents, Completed/Pending,
    the Sold or Serviced date clause, mosquito/upsell sources excluded) and
    carrying every subscription + customer field we store.
    Values only — no formulas — so nothing needs recalculating on open.
    """
    import io
    import re as _re
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return JSONResponse({"error": "openpyxl is not installed on the server"}, status_code=500)
    from fastapi.responses import Response
    from ..periods import PACIFIC
    from ..rangestats import utc_bounds

    sd, ed = str(body.get("start_day") or ""), str(body.get("end_day") or "")
    mode = "serviced" if body.get("mode") == "serviced" else "sold"
    if not (_re.fullmatch(r"\d{4}-\d{2}-\d{2}", sd) and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", ed)):
        return JSONResponse({"error": "start_day and end_day required (YYYY-MM-DD)"}, status_code=400)
    columns = [str(c) for c in (body.get("columns") or [])]
    formats = [str(f) for f in (body.get("formats") or [])]
    rows = [list(r) for r in (body.get("rows") or []) if isinstance(r, (list, tuple))]
    if not columns:
        return JSONResponse({"error": "columns required"}, status_code=400)

    F = "Arial"
    HEAD, BODY = Font(name=F, bold=True), Font(name=F)
    NOTE, FILL = Font(name=F, italic=True, color="666666"), PatternFill("solid", fgColor="DDE7F0")
    NUMFMT = {"pct": '0.0"%"', "money0": "$#,##0", "money2": "$#,##0.00", "num1": "0.0"}

    def finish(ws, widths, fmts=None):
        for c in ws[1]:
            c.font, c.fill = HEAD, FILL
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for i, c in enumerate(row):
                c.font = BODY
                if fmts and i < len(fmts) and fmts[i] in NUMFMT and isinstance(c.value, (int, float)):
                    c.number_format = NUMFMT[fmts[i]]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb = Workbook()
    # ---- tab 1: the table as shown --------------------------------------
    ws = wb.active
    ws.title = "Power Score"
    ws.append(columns)
    for r in rows:
        ws.append([v if isinstance(v, (int, float, str)) or v is None else str(v) for v in r])
    n1 = ws.max_row
    finish(ws, [5, 22] + [13] * (len(columns) - 2), formats)
    mode_txt = ("subscriptions whose INITIAL SERVICE DATE is in range" if mode == "serviced"
                else "subscriptions whose SALE DATE is in range")
    for i, t in enumerate([
        f"Power Score {sd} to {ed} (Pacific days) · mode: {mode} — {mode_txt}.",
        "Exactly the table shown on the dashboard at export time (scores use the weights saved in Settings). "
        "The Accounts tab lists every PestRoutes account behind these numbers.",
        "Conversion % comes from the audit sheet and Quality Score from VoiceOps — those are not account-based, "
        "so they have no rows on the Accounts tab.",
    ], start=n1 + 2):
        ws.cell(row=i, column=1, value=t).font = NOTE

    # ---- tab 2: every contributing account -------------------------------
    conn = connect(_settings.db_path)
    try:
        fr_to_agent = {}
        for r in conn.execute("SELECT genesys_name, fr_employee_id FROM agent_map "
                              "WHERE scorecard = 1 AND powerscore = 1 AND fr_employee_id IS NOT NULL"):
            for fid in r["fr_employee_id"].split(","):
                fr_to_agent[fid] = r["genesys_name"]
        if mode == "serviced":
            clause, args = "s.initial_service_date >= ? AND s.initial_service_date <= ?", (sd, ed)
        else:
            a, b = utc_bounds(sd, ed)
            clause, args = "s.date_added_utc >= ? AND s.date_added_utc < ?", (a, b)
        mosq = {x.upper() for x in _EXCLUDED_SOURCES}
        # The fields agents and managers actually use — trimmed to this exact
        # list (and these exact header names) at the owner's request, 2026-09-21.
        SUB = ["customer_id", "sold_by_name", "phone1_e164", "phone2_e164", "agreement_length",
               "contract_value", "service_type", "initial_status", "source", "initial_price",
               "active", "date_cancelled_utc", "initial_service_date", "billing_frequency",
               "service_frequency", "contract_added", "signed_agreement"]
        CUST = ["email", "card_on_file"]
        header = ["Agent", "Sale date (PT)"] + SUB + [f"customer_{c}" for c in CUST]
        fmts2 = (["", ""] + ["money2" if c in ("contract_value", "initial_price") else "" for c in SUB]
                 + [""] * len(CUST))
        # sold_by / date_added_utc are needed for the agent match and sale date
        # even though they are not columns on the sheet
        sel = ", ".join([f"s.{c} AS s_{c}" for c in SUB + ["sold_by", "date_added_utc"]]
                        + [f"c.{c} AS c_{c}" for c in CUST])
        acc = wb.create_sheet("Accounts")
        acc.append(header)
        bf_i = 2 + SUB.index("billing_frequency")

        def billing_words(v):
            # FieldRoutes stores -1 for "bill at time of service"
            return "Billed after each completed service" if v in (-1, "-1") else v

        n = 0
        for r in conn.execute(
                f"""SELECT {sel} FROM subscriptions s
                    LEFT JOIN customers c ON c.customer_id = s.customer_id
                    WHERE {clause} AND s.initial_status IN ('Completed','Pending')
                    ORDER BY s.date_added_utc""", args):
            agent = fr_to_agent.get(r["s_sold_by"])
            if not agent or (r["s_source"] or "").upper() in mosq:
                continue
            try:
                sale_pt = datetime.fromisoformat(r["s_date_added_utc"]).astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                sale_pt = r["s_date_added_utc"]
            row = [agent, sale_pt] + [r[f"s_{c}"] for c in SUB] + [r[f"c_{c}"] for c in CUST]
            row[bf_i] = billing_words(row[bf_i])
            acc.append(row)
            n += 1
        finish(acc, [24, 17] + [15] * (len(header) - 2), fmts2)
        acc.cell(row=acc.max_row + 2, column=1,
                 value=f"{n} accounts · power-score agents only · serviced or pending initial · "
                       f"mosquito/upsell sources excluded (they count toward no Power Score metric) · "
                       f"{'initial service date' if mode == 'serviced' else 'sale date'} {sd} to {ed}.").font = NOTE
    finally:
        conn.close()

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="PowerScore_{mode}_{sd}_{ed}.xlsx"'})


_quality_refreshing: set = set()


_quality_refresh_lock = threading.Lock()


def _refresh_quality_in_background(sd: str, ed: str) -> None:
    """Re-pull one period's VoiceOps scores on a side thread (one at a time
    per period). cached_scores commits per agent, so nothing is held open
    across the vendor calls."""
    key = (sd, ed)
    with _quality_refresh_lock:
        if key in _quality_refreshing:
            return
        _quality_refreshing.add(key)

    def run():
        from ..voiceops import VoiceOpsClient, cached_scores
        t0 = time.perf_counter()
        try:
            client = VoiceOpsClient(_settings.voiceops_client_id, _settings.voiceops_client_secret)
            conn = connect(_settings.db_path)
            try:
                cached_scores(conn, client, sd, ed)
            finally:
                conn.close()
                client.close()
            _perf_log.info("quality: refreshed %s..%s in the background (%.0f s)",
                           sd, ed, time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 - a failed refresh just leaves the saved scores in place
            logging.getLogger("isadash.quality").exception("background quality refresh failed")
        finally:
            with _quality_refresh_lock:
                _quality_refreshing.discard(key)

    threading.Thread(target=run, name="quality-refresh", daemon=True).start()


@app.get("/api/quality")
def api_quality(start_day: str = "", end_day: str = "", days: int = 30,
                fetch: int = 1) -> dict:
    """VoiceOps quality score per agent and per team for a Pacific day range.

    Its own endpoint (like /api/home_servicelevel) because a period that has
    never been requested costs ~1 API call per agent to warm. The IS #'s and
    Team Metrics tables therefore render instantly from the database and this
    fills the quality column in afterwards; once a period is cached it is
    served from SQLite and returns immediately.

    Agents VoiceOps has no scorable calls for are simply absent from `agents`,
    and contribute to no average — "counts if they have data, blank if not".
    """
    from datetime import timedelta

    from ..periods import PACIFIC
    from ..voiceops import (VoiceOpsClient, cached_scores, mapped_roster,
                           needs_refresh, weighted_quality)

    if start_day and end_day:
        sd, ed = start_day, end_day
    else:
        today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
        end = today - timedelta(days=1)          # whole days ending yesterday
        sd = (end - timedelta(days=max(1, min(days, 400)) - 1)).isoformat()
        ed = end.isoformat()

    conn = connect(_settings.db_path)
    try:
        team_of = {r["genesys_name"]: r["team"] for r in conn.execute(
            "SELECT genesys_name, team FROM agent_map WHERE scorecard = 1")}
        live = bool(fetch and _settings.voiceops_client_id and _settings.voiceops_client_secret)
        scores = cached_scores(conn, None, sd, ed)          # whatever is saved, any age
        if live:
            roster = [n for n, _e in mapped_roster(conn)]
            if roster and all(n in scores for n in roster):
                # Everyone has a saved score: show it now. If the period is still
                # live and those scores are over an hour old, re-pull them in the
                # background. (Before 2026-11-11 the visitor waited for the whole
                # re-pull: ~27 VoiceOps calls, 30+ seconds on an agent's page.)
                if needs_refresh(conn, sd, ed):
                    _refresh_quality_in_background(sd, ed)
            else:                                           # never pulled: nothing to show yet
                client = VoiceOpsClient(_settings.voiceops_client_id,
                                        _settings.voiceops_client_secret)
                try:
                    scores = cached_scores(conn, client, sd, ed)
                finally:
                    client.close()

        agents = {name: {"quality": s.get("quality_score"),
                         "calls": s.get("call_count")}
                  for name, s in scores.items()
                  if s.get("quality_score") is not None}
        buckets: dict[str, list] = {"total": [], "josh": [], "kai": []}
        for name, s in scores.items():
            buckets["total"].append(s)
            t = team_of.get(name)
            if t in ("josh", "kai"):
                buckets[t].append(s)
        teams = {k: weighted_quality(v) for k, v in buckets.items()}
        return {"start": sd, "end": ed, "agents": agents, "teams": teams}
    finally:
        conn.close()


@app.get("/api/agentview")
@cache.cached(ttl=lambda a: None if (a.get("start_day") and a.get("end_day")) else 300)
def api_agentview(days: int = 30, start_day: str = "", end_day: str = "") -> dict:
    """Every roster agent's full metric set over a trailing window OR a
    custom Pacific date range, plus the team aggregate."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from ..rangestats import utc_bounds
        start, range_end = utc_bounds(start_day, end_day)
        start_date, end_date = start_day, end_day
    else:
        days = max(1, min(days, 400))
        start = (now - timedelta(days=days)).isoformat()
        range_end = now.isoformat()
        start_date, end_date = start[:10], "9999-12-31"
    conn = connect(_settings.db_path)
    try:
        stats: dict[str, dict] = {}

        def bucket(agent: str) -> dict:
            return stats.setdefault(agent, {
                "calls": 0, "leads": set(), "cf": set(), "own": set(), "anyc": set(),
                "closed": 0, "followup": 0, "unqualified": 0, "sales_calls": 0,
                "subs": 0, "pending": 0, "multi": 0, "cv_total": 0.0, "cv_n": 0,
                "audit_qual": 0, "audit_sales": 0,
            })

        for r in conn.execute(
            """SELECT c.ani_e164, c.dispo_group, c.converted_sub_id,
                      c.converted_any_sub_id, m.genesys_name AS agent
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
            (start, range_end)):
            for who in (r["agent"], "TEAM"):
                b = bucket(who)
                b["calls"] += 1
                ph = r["ani_e164"]
                if r["dispo_group"] in ("closed", "followup", "unqualified"):
                    b["sales_calls"] += 1
                    b[r["dispo_group"]] += 1
                    if ph:
                        b["leads"].add(ph)
                        if r["dispo_group"] != "unqualified":
                            b["cf"].add(ph)
                if ph and r["converted_sub_id"]:
                    b["own"].add(ph)
                if ph and r["converted_any_sub_id"]:
                    b["anyc"].add(ph)

        for agent, added, months, cv, status, ip, *_ in _roster_sub_rows(conn):
            if added < start or added >= range_end:
                continue
            for who in (agent, "TEAM"):
                b = bucket(who)
                b["subs"] += 1
                if status == "Pending":
                    b["pending"] += 1
                if months >= 18:
                    b["multi"] += 1
                if cv > 0:
                    b["cv_total"] += cv
                    b["cv_n"] += 1
                if ip > 0:
                    b["ip_total"] = b.get("ip_total", 0.0) + ip
                    b["ip_n"] = b.get("ip_n", 0) + 1

        # sold-to-serviced per agent (all statuses, own-name subs) — EVERY sale,
        # mosquito upsells and upsells included (Dallin, 2026-10-23); the pending
        # count under it ("…undecided") is the only pending row on this view
        fr_to_agent = {}
        for r in conn.execute(
            "SELECT genesys_name, fr_employee_id FROM agent_map "
            "WHERE on_roster=1 AND fr_employee_id IS NOT NULL"):
            for fid in r["fr_employee_id"].split(","):
                fr_to_agent[fid] = r["genesys_name"]
        sts_by: dict[str, list] = {}
        for r in conn.execute(
            "SELECT sold_by, initial_status FROM subscriptions "
            "WHERE date_added_utc >= ? AND date_added_utc < ? AND sold_by != '0'",
            (start, range_end)):
            agent = fr_to_agent.get(r["sold_by"])
            if agent:
                sts_by.setdefault(agent, []).append(r["initial_status"] or "?")
                sts_by.setdefault("TEAM", []).append(r["initial_status"] or "?")

        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) s FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent",
            (start_date, end_date)):
            if r["agent"] in stats:
                stats[r["agent"]]["audit_qual"] = r["q"]
                stats[r["agent"]]["audit_sales"] = r["s"]
                stats["TEAM"]["audit_qual"] = stats["TEAM"].get("audit_qual", 0) + r["q"]
                stats["TEAM"]["audit_sales"] = stats["TEAM"].get("audit_sales", 0) + r["s"]

        from ..adjust import adjustment_stats
        bound = _audit_end_utc(conn) or range_end
        adj_end = min(range_end, bound)
        adj = adjustment_stats(conn, start, adj_end) if adj_end > start else {}
        for agent, st in adj.items():
            if agent in stats and agent != "__TEAM__":
                stats[agent]["hidden"] = st["hidden"]
                stats[agent]["leaked"] = st["leaked"]
                stats[agent]["adj_conv"] = st["converted"]
        if "TEAM" in stats and "__TEAM__" in adj:  # phone-deduped team terms
            stats["TEAM"]["hidden"] = adj["__TEAM__"]["hidden"]
            stats["TEAM"]["leaked"] = adj["__TEAM__"]["leaked"]
            stats["TEAM"]["adj_conv"] = adj["__TEAM__"]["converted"]

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        out = {}
        for agent, b in stats.items():
            conv_set = b["own"] | (b["cf"] & b["anyc"])
            leads = len(b["leads"] | conv_set)
            qual = len(b["cf"] | conv_set)
            conv = len(conv_set)
            out[agent] = {
                "calls": b["calls"], "leads": leads, "qual_leads": qual,
                "conversions": conv,
                "conv_qual_pct": pct(conv, qual),
                "conv_leads_pct": pct(conv, leads),
                "conv_calls_pct": pct(conv, b["calls"]),
                "closed_pct": pct(b["closed"], b["sales_calls"]),
                "followup_pct": pct(b["followup"], b["sales_calls"]),
                "unqualified_pct": pct(b["unqualified"], b["sales_calls"]),
                "subs": b["subs"], "pending": b["pending"],
                "multiyear_pct": pct(b["multi"], b["subs"]),
                "acv": round(b["cv_total"] / b["cv_n"], 2) if b["cv_n"] else None,
                "cv_total": round(b["cv_total"], 2),
                "init_avg": round(b.get("ip_total", 0) / b["ip_n"], 2)
                            if b.get("ip_n") else None,
                "sts_pct": _sts(sts_by.get(agent, []))["sts_pct"],
                "sts_pending": _sts(sts_by.get(agent, []))["pending"],
                "audit_qual": b["audit_qual"], "audit_sales": b["audit_sales"],
                "sheet_conv_pct": pct(b["audit_sales"], b["audit_qual"]),
                "hidden": b.get("hidden", 0), "leaked": b.get("leaked", 0),
                "true_conv_pct": pct(b.get("adj_conv", 0),
                                     max(b["audit_qual"] + b.get("hidden", 0)
                                         + b.get("leaked", 0),
                                         b.get("adj_conv", 0))),
            }
        agents = sorted(a for a in out if a != "TEAM")
        return {"days": days, "agents": agents, "stats": out}
    finally:
        conn.close()
