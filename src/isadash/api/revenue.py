"""Revenue tab, and the same numbers by lead source for Marketing -> Sales Data.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import cache
from ..store import connect

from .common import (
    _RENEWAL_SOURCES, _channel_for, _load_source_channels, _load_source_maps, _revenue_data,
    _settings, app,
)


@app.get("/api/revenue")
@cache.cached()
def api_revenue(year: int = 0) -> dict:
    d = dict(_revenue_data(year))
    d.pop("_people", None)
    return d


@app.get("/api/source_revenue")
@cache.cached()
def api_source_revenue(year: int = 0, level: str = "channel") -> dict:
    """Sources tab: the Revenue tab's yearly view sliced by marketing channel
    (level=channel: Paid / Organic / Affiliate Marketing / Generic / Other, each opening
    to its source groups) or by source group (level=source, each opening to
    its providers)."""
    by = "source" if level == "source" else "channel"
    d = dict(_revenue_data(year, by=by))
    d.pop("_people", None)
    return d


@app.get("/api/source_revenue_overview")
@cache.cached()
def api_source_revenue_overview(level: str = "channel") -> dict:
    """Sources tab: the Revenue tab's all-time overview, by channel or source."""
    return api_revenue_overview(by="source" if level == "source" else "channel")


@app.get("/api/revenue_overview")
@cache.cached()
def api_revenue_overview(by: str = "agent") -> dict:
    """All-time companion to /api/revenue, for the Revenue tab's Overview.
    by="source" groups by lead source instead (Sources tab).

    Same inclusion rules as the yearly view — office-staff sellers (FieldRoutes
    type 0) grouped by person, Completed/Pending initials, sale date in Pacific,
    renewals excluded, mosquito/upsells counted — so every figure reconciles with
    the year tabs. Only the grouping differs:
      - chart: one series per YEAR across Jan-Dec (team revenue) so years can be
        compared month by month; the current year stops at the current month
      - table: each person's revenue per year, best year, best month, total
      - records: ALL-TIME team and individual biggest day / week / month
    """
    from datetime import timedelta

    from ..periods import PACIFIC, week_start
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    OTHER_MIN = 12000.0  # an ALL-TIME total below this bundles into "Other"
    now_pac = datetime.now(timezone.utc).astimezone(PACIFIC)
    cur_year = now_pac.year
    years = list(range(2023, cur_year + 1))  # 2023 is the first year, as in /api/revenue
    yi = {y: i for i, y in enumerate(years)}
    conn = connect(_settings.db_path)
    try:
        emp = {r["fr_employee_id"]: r["name"] for r in conn.execute(
            "SELECT fr_employee_id, name FROM employees WHERE emp_type = '0' AND name IS NOT NULL")}
        start = datetime(2023, 1, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()
        # end at the close of the current month — the same cutoff the year tab uses
        _ey, _em = (cur_year, now_pac.month + 1) if now_pac.month < 12 else (cur_year + 1, 1)
        end = datetime(_ey, _em, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()

        # by="source": the same subscriptions, bucketed by lead-source GROUP the
        # way the Sources table groups them (label -> provider -> group)
        if by in ("source", "channel"):
            group_of, label_to_provider, _ = _load_source_maps()
            chan_of = _load_source_channels()

            def src_of(label) -> tuple[str, str]:
                """(row name, member name) for a subscription's source label:
                by="source" -> (group, provider); by="channel" -> (channel, group).
                A channel comes from the label, else its provider, else its group."""
                if not label:
                    grp = prov = "(no source)"
                else:
                    prov = label_to_provider.get(label.strip(), label.strip())
                    grp = group_of.get(prov.lower(), prov)
                if by == "source":
                    return grp, prov
                return _channel_for(chan_of, label, prov, grp), grp
            c_part: dict = {}      # (row name, member name) -> per-month / per-year revenue

        p_year: dict[str, list] = {}   # person -> revenue per year
        p_month: dict = {}             # (person, year, month) -> revenue
        a_day: dict = {}
        a_week: dict = {}
        t_ym = {y: [0.0] * 12 for y in years}
        t_day: dict = {}
        t_week: dict = {}
        t_month: dict = {}             # (year, month) -> revenue
        for r in conn.execute(
            f"""SELECT s.sold_by, s.source, s.date_added_utc, s.contract_value
                FROM subscriptions s
                WHERE s.date_added_utc >= ? AND s.date_added_utc < ?
                  AND s.initial_status IN ('Completed','Pending')
                  AND s.contract_value IS NOT NULL AND s.contract_value > 0
                  AND (s.source IS NULL OR s.source NOT IN
                       ({','.join('?' * len(_RENEWAL_SOURCES))}))""",
            (start, end, *_RENEWAL_SOURCES)):
            name = emp.get(r["sold_by"])
            if not name:  # field/tech or unknown seller — not office staff
                continue
            if by != "agent":
                name, prov = src_of(r["source"])
            cv = float(r["contract_value"])
            dt = datetime.fromisoformat(r["date_added_utc"])
            pac = dt.astimezone(PACIFIC)
            y, mo = pac.year, pac.month
            if y not in yi:
                continue
            day, wk = pac.date().isoformat(), week_start(dt).date().isoformat()
            p_year.setdefault(name, [0.0] * len(years))[yi[y]] += cv
            if by != "agent":
                c_part.setdefault((name, prov), [0.0] * len(years))[yi[y]] += cv
            p_month[(name, y, mo)] = p_month.get((name, y, mo), 0.0) + cv
            a_day[(name, day)] = a_day.get((name, day), 0.0) + cv
            a_week[(name, wk)] = a_week.get((name, wk), 0.0) + cv
            t_ym[y][mo - 1] += cv
            t_day[day] = t_day.get(day, 0.0) + cv
            t_week[wk] = t_week.get(wk, 0.0) + cv
            t_month[(y, mo)] = t_month.get((y, mo), 0.0) + cv

        # all-time labels need the YEAR, which the single-year view can leave off
        def wk_label(iso: str) -> str:
            d = datetime.fromisoformat(iso + "T00:00:00")
            e = d + timedelta(days=6)
            return f"{d:%b} {d.day} – {e:%b} {e.day}, {e.year}"

        def mo_label(k: tuple) -> str:
            return f"{MONTHS[k[1] - 1]} {k[0]}"

        def label(kind: str, k):
            return wk_label(k) if kind == "week" else (mo_label(k) if kind == "month" else k)

        def team_rec(d: dict, kind: str):
            if not d:
                return None
            k = max(d, key=d.get)
            return {"label": label(kind, k), "amount": round(d[k], 2)}

        def indiv_rec(d: dict, kind: str):
            if not d:
                return None
            (name, k), amt = max(d.items(), key=lambda kv: kv[1])
            return {"agent": name, "label": label(kind, k), "amount": round(amt, 2)}

        a_month = {(name, (y, mo)): v for (name, y, mo), v in p_month.items()}

        best_month: dict[str, tuple] = {}
        for (name, y, mo), amt in p_month.items():
            if name not in best_month or amt > best_month[name][1]:
                best_month[name] = ((y, mo), amt)

        agents = []
        other = [0.0] * len(years)
        other_n = 0
        for name, ys in sorted(p_year.items(), key=lambda kv: -sum(kv[1])):
            total = sum(ys)
            if total >= OTHER_MIN or by == "channel":
                by_i = max(range(len(years)), key=lambda i: ys[i])   # best year's index
                bm = best_month.get(name)
                agents.append({
                    "agent": name, "years": [round(v, 2) for v in ys], "total": round(total, 2),
                    "best_year": {"label": str(years[by_i]), "amount": round(ys[by_i], 2)},
                    "best_month": ({"label": mo_label(bm[0]), "amount": round(bm[1], 2)}
                                   if bm else None)})
            else:
                for i, v in enumerate(ys):
                    other[i] += v
                other_n += 1
        if other_n:
            agents.append({"agent": f"Other ({other_n})", "is_other": True,
                           "years": [round(v, 2) for v in other], "total": round(sum(other), 2)})
        if by != "agent":    # a row made of 2+ members can be opened to see them
            for row in agents:
                kids = [(p, v) for (g, p), v in c_part.items() if g == row["agent"]]
                if len(kids) >= 2:
                    row["children"] = [
                        {"agent": p, "years": [round(x, 2) for x in v], "total": round(sum(v), 2)}
                        for p, v in sorted(kids, key=lambda kv: -sum(kv[1]))]

        year_totals = [round(sum(t_ym[y]), 2) for y in years]
        best_y = max(range(len(years)), key=lambda i: year_totals[i])
        series = []
        for y in years:
            mons = t_ym[y][:now_pac.month if y == cur_year else 12]
            series.append({"agent": str(y), "months": [round(v, 2) for v in mons],
                           "total": round(sum(mons), 2)})

        return {
            "mode": "overview", "by": by, "year": None, "years": years,
            "seller_count": len(p_year),
            "team_total": round(sum(year_totals), 2),
            "year_totals": year_totals,
            "team_best_year": ({"label": str(years[best_y]), "amount": year_totals[best_y]}
                               if any(year_totals) else None),
            "team_records": {"day": team_rec(t_day, "day"),
                             "week": team_rec(t_week, "week"),
                             "month": team_rec(t_month, "month")},
            "hall_of_fame": {"day": indiv_rec(a_day, "day"),
                             "week": indiv_rec(a_week, "week"),
                             "month": indiv_rec(a_month, "month")},
            # shaped like the yearly payload so the same chart component draws it
            "chart": {"year": "", "months": MONTHS, "agents": series},
            "agents": agents,
        }
    finally:
        conn.close()
