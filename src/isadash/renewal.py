"""The agent page's Renewal Calculator.

The pricing lives in the "IS Renewal Calculator" Google sheet (Eligibility
tab), read through the service account so whoever keeps that sheet up to date
updates the dashboard too. The sheet is read by its labels, not cell
addresses, so adding a subscription row or moving a block down still works:

  "Renewal Eligibility Rules"  then one rule per row until a blank row
  "IS Renewal Eligibility"     then the how-to line
  "Commission Rate"            the rate in column B
  "Minimum Pricing ..."        then a "Subscription | Monthly | Bimonthly | ..."
                               header, one row per subscription, a "Notes:" row
  "Recommended Start Prices ..."  same shape

The math (the sheet's formulas) runs in the browser. Cached ten minutes like
the other sheets; the last good read is kept in app_config so a broken or
unshared sheet still leaves the agents a working calculator.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

from .store import get_config, set_config

_LAST_GOOD = "renewal_pricing_last_good"
_TTL_S = 600
_RETRY_S = 120            # after a failed read, serve the saved copy this long before trying again
_cache: dict = {}
_lock = threading.Lock()


def _num(v) -> float | None:
    """A sheet number: 99 / 49.5 / "$99.00" / "14%" -> float; blank or text -> None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v or "").strip().replace("$", "").replace(",", "")
    pct = t.endswith("%")
    t = t.rstrip("%").strip()
    try:
        x = float(t)
    except ValueError:
        return None
    return x / 100 if pct else x


def _text(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _table(rows: list[list], title: re.Pattern) -> dict | None:
    """The block under a title row: header row (col A "Subscription"), one row
    per subscription until a blank or "Notes:" row, plus that notes text."""
    cell = lambda r, c: rows[r][c] if r < len(rows) and c < len(rows[r]) else ""  # noqa: E731
    start = next((i for i, r in enumerate(rows) if title.match(_text(cell(i, 0)))), None)
    if start is None:
        return None
    head = next((i for i in range(start + 1, min(start + 4, len(rows)))
                 if _text(cell(i, 0)).lower() == "subscription"), None)
    if head is None:
        return None
    cols = []
    for c in range(1, len(rows[head])):
        name = _text(cell(head, c))
        if not name:
            break
        cols.append(name)
    out = {"columns": cols, "rows": [], "note": ""}
    for i in range(head + 1, len(rows)):
        name = _text(cell(i, 0))
        if not name:
            break
        if name.lower().startswith("notes"):
            out["note"] = re.sub(r"^notes\s*:?\s*", "", name, flags=re.I)
            break
        out["rows"].append({"name": name, "values": [_num(cell(i, c + 1)) for c in range(len(cols))]})
    return out


def parse(rows: list[list]) -> dict:
    """The Eligibility tab's values (UNFORMATTED) -> the calculator's pricing.
    Raises ValueError when the parts the math needs aren't there."""
    cell = lambda r, c: rows[r][c] if r < len(rows) and c < len(rows[r]) else ""  # noqa: E731
    col_a = [_text(cell(i, 0)) for i in range(len(rows))]

    rules: list[str] = []
    i = next((k for k, a in enumerate(col_a) if re.match(r"^renewal eligibility rules", a, re.I)), None)
    if i is not None:
        for a in col_a[i + 1:]:
            if not a:
                break
            rules.append(re.sub(r"^[•\-\*·]\s*", "", a))

    how = ""
    i = next((k for k, a in enumerate(col_a) if re.match(r"^is renewal eligibility$", a, re.I)), None)
    if i is not None:
        for k in range(i + 1, min(i + 4, len(rows))):
            if col_a[k] and _text(cell(k, 1)) == "":
                how = col_a[k]
                break

    rate = None
    for k, a in enumerate(col_a):
        if re.match(r"^commission rate", a, re.I):
            rate = _num(cell(k, 1))
            break
    if rate is not None and rate >= 1:          # typed as 14 instead of 14%
        rate /= 100
    if rate is None or not 0 < rate < 1:
        raise ValueError("couldn't find the Commission Rate in the sheet")

    minimum = _table(rows, re.compile(r"^minimum pricing", re.I))
    start = _table(rows, re.compile(r"^recommended start prices", re.I))
    if not minimum or not minimum["rows"]:
        raise ValueError("couldn't find the Minimum Pricing table in the sheet")
    # frequencies are the price columns (Monthly / Bimonthly / Quarterly);
    # "Minimum Initial" is shown but never part of the math
    freqs = [c for c in minimum["columns"] if not re.search(r"initial", c, re.I)]
    if not freqs:
        raise ValueError("the Minimum Pricing table has no frequency columns")
    return {
        "rules": rules,
        "how": how,
        "commission_rate": rate,
        "subscriptions": [r["name"] for r in minimum["rows"]],
        "frequencies": freqs,
        "minimum": minimum,
        "start": start or {"columns": [], "rows": [], "note": ""},
    }


def _fetch(settings) -> dict:
    """Read the sheet: find the tab by its gid (renaming it is fine), else the first tab."""
    from .marketing import _sheet_headers
    import httpx
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.renewal_sheet_id}"

    def get(url: str, params: dict) -> dict:
        r = httpx.get(url, params=params, headers=_sheet_headers(settings), timeout=30)
        if r.status_code in (403, 404):
            raise RuntimeError("the service account can't open the IS Renewal Calculator sheet")
        r.raise_for_status()
        return r.json()

    meta = get(base, {"fields": "properties.title,sheets.properties(sheetId,title)"})
    tabs = [s.get("properties", {}) for s in meta.get("sheets", [])]
    tab = next((t for t in tabs if str(t.get("sheetId")) == str(settings.renewal_sheet_gid)), tabs[0] if tabs else None)
    if not tab:
        raise RuntimeError("the IS Renewal Calculator sheet has no tabs")
    title = tab.get("title", "")
    rng = quote("'" + title.replace("'", "''") + "'!A1:H200", safe="!:'")
    vals = get(f"{base}/values/{rng}", {"valueRenderOption": "UNFORMATTED_VALUE"}).get("values", [])
    out = parse(vals)
    out["sheet"] = meta.get("properties", {}).get("title", "")
    out["tab"] = title
    return out


def pricing(conn, settings) -> dict:
    """The calculator's pricing, fresh from the sheet at most every ten minutes.
    When the sheet can't be read, the last good copy comes back with
    stale=True and the reason; with no saved copy, {'error': ...}."""
    key = settings.renewal_sheet_id
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() < hit[0]:
            return hit[1]
    try:
        fresh = _fetch(settings)
        fresh["read_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fresh["stale"] = False
        saved = get_config(conn, _LAST_GOOD)
        body = {k: v for k, v in fresh.items() if k not in ("read_at", "stale")}
        old = None
        try:
            old = json.loads(saved) if saved else None
        except ValueError:
            old = None
        if not old or {k: v for k, v in old.items() if k not in ("read_at", "stale")} != body:
            set_config(conn, _LAST_GOOD, json.dumps(fresh))
        out, ttl = fresh, _TTL_S
    except Exception as e:  # noqa: BLE001 - any failure falls back to the saved copy
        saved = get_config(conn, _LAST_GOOD)
        try:
            out = json.loads(saved) if saved else None
        except ValueError:
            out = None
        if out:
            out["stale"] = True
            out["stale_reason"] = str(e)[:200]
        else:
            out = {"error": "Couldn't load the renewal pricing right now — let a manager know."}
        ttl = _RETRY_S
    with _lock:
        _cache[key] = (time.monotonic() + ttl, out)
    return out
