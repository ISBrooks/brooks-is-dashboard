"""Marketing tab feeds (2026-10-08):

  * queue_volume  - calls OFFERED to the Inside Sales queues (Genesys queue
                    aggregates: offered / answered / abandoned), totals for a
                    range plus a 26-week trend per queue
  * forms_weekly  - form leads per Sunday-Saturday week (the Form Conversion
                    tab's deduped lead events), totals plus a 26-week trend
  * by_source     - calls handled (by tracking number) and form leads per
                    lead-source group for a range, the period before it, and
                    a 26-week trend per source

Everything here is computed on request from data the dashboard already keeps
(or one Genesys aggregates call) and cached for ten minutes.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import cache
from .genesys import GenesysClient
from .periods import CLOCK_TZ, PACIFIC, week_start
from .voiceops import pacific_weeks

log = logging.getLogger("isadash.marketing")
WEEKS = 26                      # trend length (complete Sun-Sat Pacific weeks)
_TTL_S = 600
_lock = threading.Lock()


def _cached(key, fn):
    """A sheet or Genesys answer, re-read at most every ten minutes. After ten
    minutes the next caller still gets the last answer at once while it is
    re-read in the background (cache.keep_fresh). Until 2026-11-17 the caller
    waited for the re-read - and so did everyone else who asked in that moment,
    each making their own request: the agents' clock check hit this every ten
    minutes, four or five at a time."""
    return cache.keep_fresh(("marketing",) + tuple(key), fn, _TTL_S)


def _utc(d: date, end: bool = False) -> datetime:
    """Pacific midnight at the start of d (or of the day after, end=True) in UTC."""
    dt = datetime(d.year, d.month, d.day, tzinfo=PACIFIC) + (timedelta(days=1) if end else timedelta(0))
    return dt.astimezone(timezone.utc)


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(PACIFIC).date()


def _week_index(weeks: list[tuple[str, str]]) -> dict[str, int]:
    return {s: i for i, (s, _e) in enumerate(weeks)}


def _week_of(dt: datetime) -> str:
    return week_start(dt).date().isoformat()


# --- call volume per queue ---------------------------------------------------
_names: dict = {"at": 0.0, "map": {}}


def _queue_names(g: GenesysClient, ids: list[str]) -> dict[str, str]:
    if time.monotonic() - _names["at"] > 86400 or set(ids) - set(_names["map"]):
        m = {}
        for q in ids:
            try:
                m[q] = g._request("GET", f"/api/v2/routing/queues/{q}").get("name") or q
            except Exception:                      # noqa: BLE001 — a name is cosmetic
                m[q] = q
        _names.update(at=time.monotonic(), map=m)
    return _names["map"]


def _queue_agg(g: GenesysClient, start_utc: datetime, end_utc: datetime, ids: list[str],
               granularity: str | None = None) -> dict[tuple[str, str], dict]:
    """{(queue_id, bucket_start_iso): {offered, answered, abandoned}}"""
    body = {
        "interval": f"{start_utc:%Y-%m-%dT%H:%M:%SZ}/{end_utc:%Y-%m-%dT%H:%M:%SZ}",
        "groupBy": ["queueId"],
        "filter": {"type": "and", "clauses": [
            {"type": "or", "predicates": [{"dimension": "queueId", "value": q} for q in ids]},
            {"type": "and", "predicates": [{"dimension": "mediaType", "value": "voice"}]}]},
        "metrics": ["nOffered", "tAnswered", "tAbandon"],
    }
    if granularity:
        body["granularity"] = granularity
        body["timeZone"] = "America/Los_Angeles"
    data = g._request("POST", "/api/v2/analytics/conversations/aggregates/query", json=body)
    out: dict[tuple[str, str], dict] = {}
    for grp in data.get("results", []):
        qid = (grp.get("group") or {}).get("queueId", "")
        for d in grp.get("data", []):
            start = (d.get("interval") or "").split("/")[0]
            b = out.setdefault((qid, start), {"offered": 0, "answered": 0, "abandoned": 0})
            for m in d.get("metrics", []):
                st = m.get("stats", {})
                if m["metric"] == "nOffered":
                    b["offered"] += st.get("count", 0)
                elif m["metric"] == "tAnswered":
                    b["answered"] += st.get("count", 0)
                elif m["metric"] == "tAbandon":
                    b["abandoned"] += st.get("count", 0)
    return out


def queue_volume(settings, start: date, end: date) -> dict:
    return _cached(("vol", start, end, _today()), lambda: _queue_volume(settings, start, end))


def _queue_volume(settings, start: date, end: date) -> dict:
    ids = settings.genesys_queue_ids
    today = _today()
    through = min(end, today)
    weeks = pacific_weeks(WEEKS)
    widx = _week_index(weeks)
    with GenesysClient(settings.genesys_region, settings.genesys_client_id,
                       settings.genesys_client_secret) as g:
        names = _queue_names(g, ids)
        tot = _queue_agg(g, _utc(start), _utc(through, end=True), ids) if start <= through else {}
        ser = _queue_agg(g, _utc(date.fromisoformat(weeks[0][0])),
                         _utc(date.fromisoformat(weeks[-1][1]), end=True), ids, granularity="P7D")

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    def finish(name: str, b: dict) -> dict:
        return {"name": name, "offered": b["offered"], "answered": b["answered"],
                "abandoned": b["abandoned"], "answer_pct": pct(b["answered"], b["offered"]),
                "abandon_pct": pct(b["abandoned"], b["offered"])}
    queues, total = [], {"offered": 0, "answered": 0, "abandoned": 0}
    for q in ids:
        b = {"offered": 0, "answered": 0, "abandoned": 0}
        for (qid, _s), v in tot.items():
            if qid == q:
                for k in b:
                    b[k] += v[k]
        queues.append({"id": q, **finish(names.get(q, q), b)})
        for k in total:
            total[k] += b[k]
    series = {names.get(q, q): [0] * len(weeks) for q in ids}
    for (qid, bstart), v in ser.items():
        try:
            wk = _week_of(datetime.fromisoformat(bstart.replace("Z", "+00:00")))
        except ValueError:
            continue
        i = widx.get(wk)
        if i is not None and qid in names:
            series[names[qid]][i] += v["offered"]
    return {"start": start.isoformat(), "end": end.isoformat(), "through": through.isoformat(),
            "queues": queues, "total": finish("All queues", total),
            "weeks": [s for s, _ in weeks], "series": series,
            "series_total": [sum(vals[i] for vals in series.values()) for i in range(len(weeks))]}


# --- form leads per week -----------------------------------------------------
def forms_weekly(events: list, start: date, end: date) -> dict:
    weeks = pacific_weeks(WEEKS)
    widx = _week_index(weeks)
    a, b = _utc(start), _utc(end, end=True)

    def blank() -> dict:
        return {"forms": 0, "deduped": 0, "existing": 0, "net": 0, "converted": 0}

    def bump(x: dict, ev) -> None:
        x["forms"] += ev.raw_count
        x["deduped"] += 1
        if ev.already_customer:
            x["existing"] += 1
            return
        x["net"] += 1
        if ev.converted:
            x["converted"] += 1
    total = blank()
    weekly: dict[str, dict] = {}
    s_net = [0] * len(weeks)
    s_raw = [0] * len(weeks)
    for ev in events:
        t = ev.first_touch_utc
        wk = _week_of(t)
        if a <= t < b:
            bump(total, ev)
            bump(weekly.setdefault(wk, blank()), ev)
        i = widx.get(wk)
        if i is not None:
            s_raw[i] += ev.raw_count
            if not ev.already_customer:
                s_net[i] += 1

    def row(label: str, x: dict) -> dict:
        d = date.fromisoformat(label)
        return {"week": label, "label": f"{d:%b} {d.day} – {d + timedelta(days=6):%b} {(d + timedelta(days=6)).day}",
                **x, "conv_pct": round(100.0 * x["converted"] / x["net"], 1) if x["net"] else None}
    rows = [row(k, v) for k, v in sorted(weekly.items())]
    return {"start": start.isoformat(), "end": end.isoformat(),
            "total": row(start.isoformat(), total) | {"week": None, "label": "Range total"},
            "rows": rows, "weeks": [s for s, _ in weeks],
            "series": {"Net new leads": s_net, "Raw submissions": s_raw}}


# --- leads by hour of the day (Mountain) --------------------------------------
# Calls = calls OFFERED to the Inside Sales queues (the Lead Volume chart's
# measure), forms = net new form leads (existing customers left out), each
# dropped into the Mountain-time hour it arrived in. A range is compared with
# the same number of days right before it. Only hours that have fully ended
# count, and every hour is averaged over the days that actually had that hour,
# so a range that includes today still compares fairly.
_HOUR_CHUNK_DAYS = 28           # Genesys refuses hourly buckets over long spans (31 days works, 90 doesn't)
OPEN_HOUR, CLOSE_HOUR = 8, 20   # the team answers 8 AM - 8 PM Mountain


def _mt_utc(d: date) -> datetime:
    """Mountain midnight at the start of d, in UTC."""
    return datetime(d.year, d.month, d.day, tzinfo=CLOCK_TZ).astimezone(timezone.utc)


def hourly(settings, events: list, start: date, end: date) -> dict:
    now = datetime.now(timezone.utc).astimezone(CLOCK_TZ)
    stamp = (now.date(), now.hour)
    return _cached(("hourly", start, end, stamp, len(events)), lambda: _hourly(settings, events, start, end, now))


def _hourly(settings, events: list, start: date, end: date, now: datetime) -> dict:
    today = now.date()
    through = min(end, today)
    days = (end - start).days + 1
    p_start, p_end = start - timedelta(days=days), start - timedelta(days=1)
    # nothing from the hour still in progress: its count would read low
    cutoff = now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def blank() -> list[dict]:
        return [{"calls": 0, "forms": 0, "days": 0} for _ in range(24)]
    cur, prv = blank(), blank()

    def slot(t: datetime) -> dict | None:
        if t >= cutoff:
            return None
        loc = t.astimezone(CLOCK_TZ)
        d = loc.date()
        if start <= d <= through:
            return cur[loc.hour]
        if p_start <= d <= p_end:
            return prv[loc.hour]
        return None

    # how many days each hour really had (today stops at the last finished hour)
    for lo, hi, tgt in ((start, through, cur), (p_start, min(p_end, today), prv)):
        d = lo
        while d <= hi:
            for h in range(24):
                if d < today or h < now.hour:
                    tgt[h]["days"] += 1
            d += timedelta(days=1)

    a, b = _mt_utc(p_start), min(_mt_utc(through + timedelta(days=1)), cutoff)
    if a < b:
        with GenesysClient(settings.genesys_region, settings.genesys_client_id,
                           settings.genesys_client_secret) as g:
            lo = a
            while lo < b:
                hi = min(lo + timedelta(days=_HOUR_CHUNK_DAYS), b)
                for (_q, bstart), v in _queue_agg(g, lo, hi, settings.genesys_queue_ids,
                                                  granularity="PT1H").items():
                    try:
                        t = datetime.fromisoformat(bstart.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    s = slot(t)
                    if s is not None:
                        s["calls"] += v["offered"]
                lo = hi
    for ev in events:
        t = ev.first_touch_utc
        if ev.already_customer or not (a <= t < b):
            continue
        s = slot(t)
        if s is not None:
            s["forms"] += 1

    def label(h: int) -> str:
        def one(x: int) -> str:
            return f"{(x % 12) or 12} {'AM' if x % 24 < 12 else 'PM'}"
        return f"{one(h)} – {one(h + 1)}"

    def avg(n: int, d: int) -> float | None:
        return round(n / d, 2) if d else None

    def side(x: dict) -> dict:
        return {"calls": x["calls"], "forms": x["forms"], "total": x["calls"] + x["forms"], "days": x["days"],
                "avg_calls": avg(x["calls"], x["days"]), "avg_forms": avg(x["forms"], x["days"]),
                "avg_total": avg(x["calls"] + x["forms"], x["days"])}

    def group(side_rows: list[dict], hours: list[int]) -> dict:
        """A block of hours: counts add up; the per-day average is the sum of
        each hour's own average (hours can have different day counts today)."""
        out = {"days": max((side_rows[h]["days"] for h in hours), default=0)}
        for k in ("calls", "forms", "total"):
            out[k] = sum(side_rows[h][k] for h in hours)
            parts = [side_rows[h]["avg_" + k] for h in hours if side_rows[h]["avg_" + k] is not None]
            out["avg_" + k] = round(sum(parts), 2) if parts else None
        return out
    c_rows, p_rows = [side(x) for x in cur], [side(x) for x in prv]
    open_h = list(range(OPEN_HOUR, CLOSE_HOUR))
    after_h = [h for h in range(24) if h not in open_h]
    return {"start": start.isoformat(), "end": end.isoformat(), "through": through.isoformat(),
            "prev_start": p_start.isoformat(), "prev_end": p_end.isoformat(),
            "days": (through - start).days + 1 if through >= start else 0, "prev_days": days,
            "open_hour": OPEN_HOUR, "close_hour": CLOSE_HOUR,
            "hours": [{"hour": h, "label": label(h), "open": h in open_h, "cur": c_rows[h], "prev": p_rows[h]}
                      for h in range(24)],
            "open": {"cur": group(c_rows, open_h), "prev": group(p_rows, open_h)},
            "after": {"cur": group(c_rows, after_h), "prev": group(p_rows, after_h)},
            "all": {"cur": group(c_rows, list(range(24))), "prev": group(p_rows, list(range(24)))}}


# --- calls + forms by source -------------------------------------------------
def by_source(conn, events: list, start: date, end: date, maps: tuple, channel_of) -> dict:
    """maps = (group_of, label_to_provider, dnis_to_provider) from the Sources
    table; channel_of(label, provider, group) -> marketing channel."""
    group_of, label_to_provider, dnis_to_provider = maps
    weeks = pacific_weeks(WEEKS)
    widx = _week_index(weeks)
    today = _today()
    through = min(end, today)
    days = (end - start).days + 1
    p_start, p_end = start - timedelta(days=days), start - timedelta(days=1)
    lo = min(p_start, date.fromisoformat(weeks[0][0]))
    hi = max(through, date.fromisoformat(weeks[-1][1]))
    a, b = _utc(lo), _utc(hi, end=True)
    cur = (_utc(start), _utc(through, end=True))
    prv = (_utc(p_start), _utc(p_end, end=True))

    def grp_of_prov(prov: str) -> str:
        return group_of.get(prov.lower(), prov)

    def label_grp(label: str | None) -> tuple[str, str, str]:
        if not label:
            return "(no source)", "(no source)", "(no source)"
        prov = label_to_provider.get(label.strip(), label.strip())
        return label, prov, grp_of_prov(prov)
    rows: dict[str, dict] = {}
    series: dict[str, dict] = {}
    chan: dict[str, str] = {}

    def bucket(g: str) -> dict:
        series.setdefault(g, {"calls": [0] * len(weeks), "forms": [0] * len(weeks)})
        return rows.setdefault(g, {"source": g, "calls": 0, "forms": 0, "prev_calls": 0, "prev_forms": 0})

    def add(g: str, kind: str, t: datetime) -> None:
        r = bucket(g)
        if cur[0] <= t < cur[1]:
            r[kind] += 1
        elif prv[0] <= t < prv[1]:
            r["prev_" + kind] += 1
        i = widx.get(_week_of(t))
        if i is not None:
            series[g][kind][i] += 1

    # calls handled by roster agents, attributed by the number dialled (DNIS)
    for r in conn.execute(
            """SELECT c.start_utc, c.dnis FROM calls c
               JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
            (a.isoformat(), b.isoformat())):
        digits = re.sub(r"\D", "", r["dnis"] or "")
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        prov = dnis_to_provider.get(digits)
        g = grp_of_prov(prov) if prov else "(no tracking number)"
        if prov and g not in chan:
            chan[g] = channel_of(None, prov, g)
        add(g, "calls", datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00")))
    # form leads (existing customers excluded, like the Sources table)
    for ev in events:
        t = ev.first_touch_utc
        if ev.already_customer or not (a <= t < b):
            continue
        groups = set()
        for s in ev.sources:
            label, prov, g = label_grp(s)
            groups.add(g)
            if g not in chan:
                chan[g] = channel_of(label, prov, g)
        for g in groups or {"(no source)"}:
            add(g, "forms", t)
    out_rows = []
    for g, r in rows.items():
        r["channel"] = chan.get(g, "Other")
        r["total"] = r["calls"] + r["forms"]
        r["prev_total"] = r["prev_calls"] + r["prev_forms"]
        r["delta_pct"] = (round(100.0 * (r["total"] - r["prev_total"]) / r["prev_total"], 1)
                          if r["prev_total"] else None)
        if r["total"] or r["prev_total"] or any(series[g]["calls"]) or any(series[g]["forms"]):
            out_rows.append(r)
    out_rows.sort(key=lambda x: -x["total"])
    n = len(weeks)
    all_series = {"calls": [sum(s["calls"][i] for s in series.values()) for i in range(n)],
                  "forms": [sum(s["forms"][i] for s in series.values()) for i in range(n)]}
    return {"start": start.isoformat(), "end": end.isoformat(), "through": through.isoformat(),
            "prev_start": p_start.isoformat(), "prev_end": p_end.isoformat(),
            "rows": out_rows, "weeks": [s for s, _ in weeks],
            "series": {g: series[g] for g in series if g in rows}, "all": all_series}


# --- Spend & ROAS ------------------------------------------------------------
# The spend sheet is the team's manual tracker: one row per lead source per
# Sunday-Saturday week on a "<year> CPA" tab (col A = the week's Saturday,
# C = Lead Source, E = Pending/Serviced, G = Spend, H = Revenue). Spend is the
# number we take from it; leads, sales and revenue come from the dashboard by
# source group (sheet values are carried along for comparison).
import csv as _csv
import io as _io
import re as _re
from pathlib import Path as _Path

_SA = {"creds": None, "path": None}


def _sheet_headers(settings) -> dict:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    path = settings.google_service_account_file
    if not path or not _Path(path).exists():
        raise RuntimeError("the Google service-account key file is missing")
    with _lock:
        c = _SA["creds"]
        if c is None or _SA["path"] != path:
            c = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
            _SA.update(creds=c, path=path)
        if not c.valid:
            c.refresh(Request())
        return {"Authorization": f"Bearer {c.token}"}


def _sheet_get(settings, url: str, params: dict | None = None) -> dict:
    import httpx
    r = httpx.get(url, params=params, headers=_sheet_headers(settings), timeout=60)
    if r.status_code == 403:
        raise RuntimeError("the service account can't open the spend sheet — is it still shared as Viewer?")
    r.raise_for_status()
    return r.json()


def _money(s) -> float | None:
    t = str(s or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not t or t in ("-", "–"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _int(s) -> int:
    try:
        return int(float(str(s or "0").replace(",", "").strip() or 0))
    except ValueError:
        return 0


def spend_sheet(settings) -> dict:
    """{'weeks': {saturday_iso: {source: {spend, sales, revenue, leads}}},
    'tabs': [...], 'sources': [...]} — every "<year> CPA" tab, cached 10 min."""
    return _cached(("spend_sheet", settings.spend_sheet_id), lambda: _spend_sheet(settings))


def _spend_sheet(settings) -> dict:
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.spend_sheet_id}"
    meta = _sheet_get(settings, base, {"fields": "sheets.properties.title"})
    tabs = sorted(p["properties"]["title"] for p in meta.get("sheets", [])
                  if _re.match(r"^\d{4} CPA$", p["properties"]["title"]))
    weeks: dict[str, dict] = {}
    names: set[str] = set()
    for tab in tabs:
        rows = _sheet_get(settings, f"{base}/values/'{tab}'!A1:H3000").get("values", [])
        for r in rows:
            if len(r) < 7 or not r[0] or not (r[2] or "").strip() or r[2].strip() == "Lead Source":
                continue
            try:
                m, d, y = (int(x) for x in r[0].split("/"))
                wk_end = date(y, m, d)
            except ValueError:
                continue
            src = " ".join(r[2].split())
            names.add(src)
            cell = weeks.setdefault(wk_end.isoformat(), {}).setdefault(
                src, {"spend": 0.0, "sales": 0, "revenue": 0.0, "leads": 0})
            cell["spend"] += _money(r[6]) or 0.0
            cell["sales"] += _int(r[4]) if len(r) > 4 else 0
            cell["revenue"] += (_money(r[7]) or 0.0) if len(r) > 7 else 0.0
            cell["leads"] += _int(r[3]) if len(r) > 3 else 0
    return {"weeks": weeks, "tabs": tabs, "sources": sorted(names)}


def _load_spend_map(path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    p = _Path(path)
    if not path:
        return out
    cache.depend_on_file(path)        # an edit to the map refreshes the saved Spend & ROAS numbers
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            name = (row.get("sheet_name") or "").strip()
            tgt = (row.get("dashboard_source") or "").strip()
            if name and tgt and not name.startswith("#"):
                out[name.lower()] = [t.strip() for t in tgt.split("|") if t.strip()]
    return out


def _norm(s: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", s.lower())


def _sales_by_group(conn, lo: date, hi: date, maps: tuple, widx: dict,
                    exclude: tuple = ()) -> tuple[dict, dict]:
    """Office-staff sales by source group (the Revenue tab's rule: every
    Completed/Pending sale with contract value, by sale date, renewal sources
    in `exclude` left out). Returns ({group: {day_iso: (sales, revenue)}}, {})."""
    group_of, label_to_provider, _ = maps
    emp = {r["fr_employee_id"] for r in conn.execute(
        "SELECT fr_employee_id FROM employees WHERE emp_type = '0'")}
    by_day: dict[str, dict[str, list]] = {}
    for r in conn.execute(
            """SELECT sold_by, source, date_added_utc, contract_value FROM subscriptions
               WHERE date_added_utc >= ? AND date_added_utc < ?
                 AND initial_status IN ('Completed','Pending')
                 AND contract_value IS NOT NULL AND contract_value > 0""",
            (_utc(lo).isoformat(), _utc(hi, end=True).isoformat())):
        if r["sold_by"] not in emp or (r["source"] or "") in exclude:
            continue
        label = (r["source"] or "").strip()
        prov = label_to_provider.get(label, label) if label else "(no source)"
        g = group_of.get(prov.lower(), prov)
        day = datetime.fromisoformat(r["date_added_utc"]).astimezone(PACIFIC).date().isoformat()
        cell = by_day.setdefault(g, {}).setdefault(day, [0, 0.0])
        cell[0] += 1
        cell[1] += float(r["contract_value"])
    return by_day, {}


def spend_roas(conn, settings, events: list, start: date, end: date, maps: tuple, channel_of,
               weeks: list | None = None, exclude_sources: tuple = ()) -> dict:
    """`weeks` = the (sunday, saturday) weeks the trend series covers; default
    the last 26 complete weeks. `exclude_sources` = the renewal labels the
    Revenue tab leaves out, so "all revenue" here equals the Revenue tab."""
    sheet = spend_sheet(settings)
    smap = _load_spend_map(settings.spend_source_map)
    weeks26 = weeks or pacific_weeks(WEEKS)
    widx = _week_index(weeks26)
    # the spend weeks inside the range (by the week's Saturday); the dashboard
    # numbers are then taken over exactly those Sun-Sat weeks
    wk_ends = sorted(w for w in sheet["weeks"] if start.isoformat() <= w <= end.isoformat())
    notes: list[str] = []
    if not wk_ends:
        last = max(sheet["weeks"]) if sheet["weeks"] else None
        return {"start": start.isoformat(), "end": end.isoformat(), "rows": [], "overhead": [],
                "totals": None, "weeks": [], "series": {}, "tabs": sheet["tabs"],
                "notes": [f"No spend week ends inside {start} – {end}. Spend is weekly (Sun–Sat); "
                          f"the sheet's latest week ends {last or '—'}."]}
    span_lo = date.fromisoformat(wk_ends[0]) - timedelta(days=6)
    span_hi = date.fromisoformat(wk_ends[-1])
    today = _today()
    lo = min(span_lo, date.fromisoformat(weeks26[0][0]))
    hi = max(min(span_hi, today), date.fromisoformat(weeks26[-1][1]))

    # dashboard: leads (calls + forms) over the span, sales + revenue by day
    bs_rows = by_source(conn, events, span_lo, span_hi, maps, channel_of)["rows"]
    leads = {r["source"]: r["calls"] + r["forms"] for r in bs_rows}
    calls_of = {r["source"]: r["calls"] for r in bs_rows}
    forms_of = {r["source"]: r["forms"] for r in bs_rows}
    by_day, _ = _sales_by_group(conn, lo, hi, maps, widx, exclude_sources)
    groups = set(leads) | set(by_day)

    def dash_totals(gs: list[str]) -> tuple[int, int, float]:
        n = sum(leads.get(g, 0) for g in gs)
        s = rv = 0
        for g in gs:
            for day, (c, v) in by_day.get(g, {}).items():
                if span_lo.isoformat() <= day <= span_hi.isoformat():
                    s += c
                    rv += v
        return n, s, rv
    by_norm = {_norm(g): g for g in groups}

    def targets(name: str) -> list[str] | None:
        if name.lower() in smap:
            return smap[name.lower()]
        g = by_norm.get(_norm(name))
        return [g] if g else None

    def ratio(a, b):
        return round(a / b, 2) if b else None
    rows, overhead, unmatched = [], [], []
    for name in sheet["sources"]:
        spend = sheet_sales = sheet_rev = 0.0
        for w in wk_ends:
            c = sheet["weeks"][w].get(name)
            if c:
                spend += c["spend"]
                sheet_sales += c["sales"]
                sheet_rev += c["revenue"]
        if not spend and not sheet_sales and not sheet_rev:
            continue                                   # nothing this period
        tg = targets(name)
        if tg == ["(overhead)"]:
            overhead.append({"source": name, "spend": round(spend, 2)})
            continue
        n, s, rv = dash_totals(tg) if tg else (None, None, None)
        if not tg:
            unmatched.append(name)
        rows.append({
            "source": name, "groups": tg or [], "matched": bool(tg),
            "calls": sum(calls_of.get(g, 0) for g in tg) if tg else None,
            "forms": sum(forms_of.get(g, 0) for g in tg) if tg else None,
            # the sheet's own name resolves the channel too ("Eforce Web" -> Paid)
            "channel": channel_of(name, tg[0], tg[0]) if tg else channel_of(name, name, name),
            "spend": round(spend, 2), "leads": n, "sales": s, "revenue": round(rv, 2) if rv is not None else None,
            "sheet_sales": int(sheet_sales), "sheet_revenue": round(sheet_rev, 2),
            "cpl": ratio(spend, n) if n else None, "cps": ratio(spend, s) if s else None,
            "roas": ratio(rv, spend) if (rv is not None and spend) else None,
            "sheet_cps": ratio(spend, sheet_sales) if sheet_sales else None,
            "sheet_roas": ratio(sheet_rev, spend) if spend else None,
        })
    rows.sort(key=lambda r: -r["spend"])
    # revenue from sources with no spend line (referrals, upsells, in-house
    # campaigns...): free revenue, part of "all revenue" but no line of its own
    mapped_groups = sorted({g for r in rows for g in r["groups"]})
    other_groups = sorted(g for g in by_day if g not in mapped_groups)
    _on, o_sales, o_rev = dash_totals(other_groups)
    # ...shown one row per source, like the spend lines, with no spend (Dallin, 2026-10-08)
    for g in other_groups:
        n, s_, rv = dash_totals([g])
        if not s_ and not rv:
            continue
        rows.append({
            "source": g, "groups": [g], "matched": True, "nospend": True,
            "calls": calls_of.get(g, 0), "forms": forms_of.get(g, 0),
            "channel": channel_of(None, g, g),
            "spend": 0.0, "leads": n, "sales": s_, "revenue": round(rv, 2),
            "sheet_sales": 0, "sheet_revenue": 0.0,
            "cpl": None, "cps": None, "roas": None, "sheet_cps": None, "sheet_roas": None})
    rows.sort(key=lambda r: (-r["spend"], -(r["revenue"] or 0)))
    src_spend = sum(r["spend"] for r in rows)
    oh_spend = sum(r["spend"] for r in overhead)
    t_leads = sum(r["leads"] or 0 for r in rows)
    t_calls = sum(r["calls"] or 0 for r in rows)
    t_forms = sum(r["forms"] or 0 for r in rows)
    t_sales = sum(r["sales"] or 0 for r in rows)
    t_rev = sum(r["revenue"] or 0 for r in rows)
    t_ssales = sum(r["sheet_sales"] for r in rows)
    t_srev = sum(r["sheet_revenue"] for r in rows)

    def tot(label, spend, sales=t_sales, rev=t_rev):
        return {"source": label, "spend": round(spend, 2), "leads": t_leads, "calls": t_calls, "forms": t_forms, "sales": sales,
                "revenue": round(rev, 2), "sheet_sales": t_ssales, "sheet_revenue": round(t_srev, 2),
                "cpl": ratio(spend, t_leads) if t_leads else None, "cps": ratio(spend, sales) if sales else None,
                "roas": ratio(rev, spend) if spend else None,
                "sheet_cps": ratio(spend, t_ssales) if t_ssales else None,
                "sheet_roas": ratio(t_srev, spend) if spend else None}
    totals = {"sources": tot("Lead sources", src_spend),
              # every spend line vs ALL revenue (lead sources + the no-spend-line sources)
              "blended": tot("All spend incl. overhead", src_spend + oh_spend)}

    # trend: all spend (sheet) vs ALL revenue (dashboard, every source group)
    s_spend = [0.0] * len(weeks26)
    s_rev = [0.0] * len(weeks26)
    s_srev = [0.0] * len(weeks26)
    # a week cut short by the start of the range (e.g. Dec 28 – Jan 3 shown
    # from Jan 1) only gets its share of that week's spend, by days: the sheet
    # is weekly, so the spend is spread evenly over the 7 days (Dallin, 2026-10-13)
    share = [((date.fromisoformat(sat) - date.fromisoformat(sun)).days + 1) / 7 for sun, sat in weeks26]
    for i, (sun, sat) in enumerate(weeks26):
        wk = sheet["weeks"].get(sat, {})
        s_spend[i] = sum(c["spend"] for c in wk.values()) * share[i]
        s_srev[i] = sum(c["revenue"] for n, c in wk.items() if targets(n) != ["(overhead)"])
        for g in by_day:
            for day, (c, v) in by_day[g].items():
                if sun <= day <= sat:
                    s_rev[i] += v
    series = {"Spend (sheet)": [round(x) for x in s_spend],
              "Revenue (dashboard)": [round(x) for x in s_rev],
              "Revenue (sheet)": [round(x) for x in s_srev]}
    # the same two lines per sheet source (spend from its line, revenue from its groups)
    by_src: dict[str, dict] = {}
    for r in rows:
        sp = [0.0] * len(weeks26)
        rv = [0.0] * len(weeks26)
        for i, (sun, sat) in enumerate(weeks26):
            c = sheet["weeks"].get(sat, {}).get(r["source"])
            if c:
                sp[i] = c["spend"] * share[i]
            for g in r["groups"]:
                for day, (_n, v) in by_day.get(g, {}).items():
                    if sun <= day <= sat:
                        rv[i] += v
        by_src[r["source"]] = {"spend": [round(x) for x in sp], "revenue": [round(x) for x in rv]}
    roas_series = [ratio(s_rev[i], s_spend[i]) if s_spend[i] else None for i in range(len(weeks26))]
    if unmatched:
        notes.append("Spend lines not matched to a dashboard source (spend counted, no leads/sales): "
                     + ", ".join(unmatched) + ". Add them to config/spend_source_map.csv.")
    if span_hi > today:
        notes.append("The last spend week isn't over yet; its dashboard numbers run through today.")
    return {"start": start.isoformat(), "end": end.isoformat(),
            "span": {"start": span_lo.isoformat(), "end": span_hi.isoformat(), "weeks": len(wk_ends)},
            "rows": rows, "overhead": overhead, "totals": totals,
            "other": {"sales": o_sales, "revenue": round(o_rev, 2), "groups": other_groups},
            "weeks": [s for s, _ in weeks26], "week_ends": [e for _, e in weeks26],
            "series": series, "roas_series": roas_series,
            "series_by_source": by_src,
            "tabs": sheet["tabs"], "latest_week": max(sheet["weeks"]) if sheet["weeks"] else None,
            "notes": notes}


# --- year-over-year history ---------------------------------------------------
# Weeks of a year = the Sundays from the Sunday on/before Jan 1 to the last
# Sunday of the year (52 or 53). Genesys queue data starts April 2023; the
# manual tracker sheet ("Call Volume/Week", "Forms/Week") goes back further
# and is shown beside it, never merged into it.
_long: dict = {}        # past years never change: cached for the life of the process


def _year_weeks(year: int) -> list[date]:
    jan1 = date(year, 1, 1)
    sun = jan1 - timedelta(days=(jan1.weekday() + 1) % 7)
    out = []
    while sun.year <= year and not (sun.year == year + 1):
        if sun <= date(year, 12, 31):
            out.append(sun)
        sun += timedelta(days=7)
    return out


def history_sheet(settings) -> dict:
    """{'calls': {year: [weekly...]}, 'forms': {year: [...]}} from the tracker
    sheet; a year whose cells are all zero is left out (not tracked)."""
    return _cached(("history_sheet", settings.history_sheet_id), lambda: _history_sheet(settings))


def _history_sheet(settings) -> dict:
    from urllib.parse import quote
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.history_sheet_id}/values/"
    out = {"calls": {}, "forms": {}}
    for key, tab in (("calls", "Call Volume/Week"), ("forms", "Forms/Week")):
        rows = _sheet_get(settings, base + quote(f"{tab}!A1:Z400", safe="!:")).get("values", [])
        if not rows:
            continue
        head = rows[0]
        for c, cell in enumerate(head):
            m = _re.match(r"^\s*(Call|Form) Volume (\d{4})\s*$", cell or "")
            if not m:
                continue
            year = int(m.group(2))
            vals = []
            for r in rows[1:]:
                label = r[c - 1].strip() if c - 1 < len(r) else ""
                raw = r[c].strip() if c < len(r) else ""
                if not label and not raw:
                    continue
                try:
                    v = int(float(raw.replace(",", ""))) if raw else 0
                except ValueError:
                    v = 0
                vals.append(v)
                if len(vals) >= 53:
                    break
            if any(vals):
                out[key][year] = vals
    return out


def _genesys_year(settings, year: int) -> list[int]:
    today = _today()
    if year < today.year and year in _long:
        return _long[year]
    weeks = _year_weeks(year)
    end = min(weeks[-1] + timedelta(days=7), today + timedelta(days=1))
    series = [0] * len(weeks)
    if weeks[0] < end:
        widx = {w.isoformat(): i for i, w in enumerate(weeks)}
        with GenesysClient(settings.genesys_region, settings.genesys_client_id,
                           settings.genesys_client_secret) as g:
            agg = _queue_agg(g, _utc(weeks[0]), _utc(end), settings.genesys_queue_ids, granularity="P7D")
        for (_q, bstart), v in agg.items():
            try:
                wk = _week_of(datetime.fromisoformat(bstart.replace("Z", "+00:00")))
            except ValueError:
                continue
            i = widx.get(wk)
            if i is not None:
                series[i] += v["offered"]
    if year < today.year:
        _long[year] = series
    return series


def volume_history(settings) -> dict:
    """Calls offered per week, one row per year, Genesys and the tracker."""
    return _cached(("vol_hist", _today()), lambda: _volume_history(settings))


def _volume_history(settings) -> dict:
    today = _today()
    sheet = history_sheet(settings)
    years = sorted(set(range(2023, today.year + 1)) | set(sheet["calls"]))
    genesys = {}
    for y in years:
        if y >= 2023:
            try:
                genesys[y] = _genesys_year(settings, y)
            except Exception as e:                     # noqa: BLE001 — one year failing shouldn't blank the view
                log.warning("genesys history %s failed: %s", y, e)
    return _history_payload(years, genesys, sheet["calls"], today)


def forms_history(settings, events: list) -> dict:
    return _cached(("forms_hist", _today(), len(events)), lambda: _forms_history(settings, events))


def _forms_history(settings, events: list) -> dict:
    today = _today()
    sheet = history_sheet(settings)
    dash: dict[int, list] = {}
    for ev in events:
        if ev.already_customer:
            continue
        d = ev.first_touch_utc.astimezone(PACIFIC).date()
        wk = d - timedelta(days=(d.weekday() + 1) % 7)
        y = d.year
        weeks = _year_weeks(y)
        if y not in dash:
            dash[y] = [0] * len(weeks)
        try:
            dash[y][weeks.index(wk)] += 1
        except ValueError:
            pass
    years = sorted(set(dash) | set(sheet["forms"]))
    return _history_payload(years, dash, sheet["forms"], today)


def _history_payload(years: list[int], primary: dict, sheet: dict, today: date) -> dict:
    """One row per year: the dashboard's weekly counts, the tracker's, and
    `combined` = dashboard where it has a count, the tracker otherwise (52
    weeks; a year's 53rd week is dropped so every year lines up)."""
    n = 52
    this_wk = (today - timedelta(days=(today.weekday() + 1) % 7))
    cur_idx = _year_weeks(today.year).index(this_wk) if this_wk in _year_weeks(today.year) else n

    def pad(vals: list | None, year: int) -> list:
        if vals is None:
            return [None] * n
        out = list(vals[:n]) + [None] * (n - len(vals[:n]))
        if year == today.year:                       # weeks that haven't happened: blank
            out = [v if i <= cur_idx else None for i, v in enumerate(out)]
        return out
    rows = []
    for y in years:
        p, s = primary.get(y), sheet.get(y)
        pp, ss = pad(p, y), pad(s, y)
        comb = [a if (a is not None and a > 0) else b for a, b in zip(pp, ss)]
        rows.append({"year": y,
                     "primary": pp, "sheet": ss, "combined": comb,
                     "combined_total": sum(v for v in comb if v),
                     "primary_total": sum(v for v in (p or []) if v) if p else None,
                     "sheet_total": sum(v for v in (s or []) if v) if s else None,
                     "primary_weeks": sum(1 for v in (p or []) if v) if p else 0,
                     "sheet_weeks": sum(1 for v in (s or []) if v) if s else 0})
    return {"years": years, "weeks": [f"Wk {i + 1}" for i in range(n)], "rows": rows,
            "current_year": today.year, "current_week": cur_idx + 1}


# --- Spend & ROAS, the sheet's layout: last week / trailing 4 / trailing 12 ---
_WINDOWS = (("w1", 1, "Last week"), ("w4", 4, "Trailing 4 weeks"), ("w12", 12, "Trailing 12 weeks"))
_KEEP = ("spend", "calls", "forms", "leads", "sales", "revenue", "sheet_sales", "sheet_revenue",
         "cpl", "cps", "roas", "sheet_cps", "sheet_roas")


def spend_chart(conn, settings, events: list, start: date, end: date, maps: tuple, channel_of,
                exclude_sources: tuple = ()) -> dict:
    """The weekly trend under Spend & ROAS for any date range. Weeks run
    Sun-Sat; a week is in when its Saturday falls inside the range and is
    already over (complete weeks only, like the year-to-date default). The
    first week's Sunday is clipped to the start so the dashboard's revenue
    never reaches before it — the sheet's spend for that week stays whole,
    because the sheet is weekly and can't be split."""
    today = _today()
    sun = start - timedelta(days=(start.weekday() + 1) % 7)
    weeks: list[tuple[str, str]] = []
    while sun <= end:
        sat = sun + timedelta(days=6)
        if start <= sat <= end and sat < today:
            weeks.append((max(sun, start).isoformat(), sat.isoformat()))
        sun += timedelta(days=7)
    if not weeks:
        return {"chart": None, "rows": [], "start": start.isoformat(), "end": end.isoformat(),
                "error": "No complete Sunday–Saturday weeks in that range yet."}
    d = spend_roas(conn, settings, events, date.fromisoformat(weeks[0][0]), date.fromisoformat(weeks[-1][1]),
                   maps, channel_of, weeks=weeks, exclude_sources=exclude_sources)
    if not d.get("series"):                       # no spend weeks on the sheet in that range
        first = min(sheet_w for sheet_w in spend_sheet(settings)["weeks"]) if spend_sheet(settings)["weeks"] else None
        return {"chart": None, "rows": [], "start": start.isoformat(), "end": end.isoformat(),
                "error": "The spend sheet has no weeks in that range" + (f" (its first week ends {first})." if first else ".")}
    return {"start": start.isoformat(), "end": end.isoformat(),
            "chart": {"weeks": d["weeks"], "week_ends": d["week_ends"], "series": d["series"],
                      "roas_series": d["roas_series"], "by_source": d["series_by_source"]},
            "rows": [{"source": r["source"], "channel": r["channel"]} for r in d["rows"]]}


def spend_windows(conn, settings, events: list, maps: tuple, channel_of, exclude_sources: tuple = ()) -> dict:
    """Per source: ROAS / cost per lead / cost per sale (the sheet's "CPA")
    over the last spend week, the trailing 4 and the trailing 12, plus spend,
    revenue, forms and calls for each. "Last week" = the latest Sun-Sat week
    with spend on the sheet."""
    sheet = spend_sheet(settings)
    wks = sorted(w for w, cells in sheet["weeks"].items() if any(c["spend"] for c in cells.values()))
    if not wks:
        return {"rows": [], "totals": {}, "windows": {}, "notes": ["The spend sheet has no weeks with spend yet."]}
    out_rows: dict[str, dict] = {}
    totals: dict[str, dict] = {"sources": {}, "blended": {}}
    windows: dict[str, dict] = {}
    others: dict[str, dict] = {}
    notes: list[str] = []
    chart = None
    for key, n, label in _WINDOWS:
        ws = wks[-n:]
        start = date.fromisoformat(ws[0]) - timedelta(days=6)
        end = date.fromisoformat(ws[-1])
        d = spend_roas(conn, settings, events, start, end, maps, channel_of, exclude_sources=exclude_sources)
        windows[key] = {"label": label, "start": start.isoformat(), "end": end.isoformat(), "weeks": len(ws)}
        others[key] = d["other"]
        for r in d["rows"]:
            row = out_rows.setdefault(r["source"], {"source": r["source"], "channel": r["channel"], "matched": r["matched"]})
            row[key] = {k: r.get(k) for k in _KEEP}
        for t in ("sources", "blended"):
            if d["totals"]:
                totals[t][key] = {k: d["totals"][t].get(k) for k in _KEEP}
        if key == "w12":
            notes = d["notes"]
    # the trend under the table: year to date, week by week — built exactly like
    # a picked range (spend_chart), so every source with spend this year is in
    # its picker, not just the ones with spend in the trailing 12 weeks
    today = _today()
    yc = spend_chart(conn, settings, events, date(today.year, 1, 1), today, maps, channel_of, exclude_sources)
    chart = yc.get("chart")
    if chart:
        chart["rows"] = yc.get("rows", [])          # source -> channel for the chart picker
    rows = sorted(out_rows.values(), key=lambda r: -((r.get("w12") or {}).get("spend") or 0))
    return {"rows": rows, "totals": totals, "windows": windows, "other": others, "latest_week": wks[-1],
            "tabs": sheet["tabs"], "chart": chart, "notes": notes}
