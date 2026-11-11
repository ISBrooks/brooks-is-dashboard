"""Team-level form-lead conversion (redefined 2026-07-13 per ops spec).

Reads normalized form touches (gmail / meta / lsa channels) from the sourcing
app's SQLite, then:

  STEP 1 — DEDUPE. Cluster touches into UNIQUE CONTACTS (union-find on shared
    phone or email), split each contact's touches into lead EVENTS (a gap
    larger than FORM_DEDUP_WINDOW_HOURS starts a fresh lead). raw_count keeps
    how many raw form submissions collapsed into each event, so we can show
    "1000 forms -> 850 unique leads".

  STEP 2 — EXCLUDE EXISTING CUSTOMERS. An event is `already_customer` only if,
    AT THE MOMENT THE FORM CAME IN, the contact had a LIVE real subscription:
      initial_status in (Completed, Pending)   [got at least a scheduled appt]
      AND date_added <= form_time               [existed before the form]
      AND (date_cancelled is NULL or > form_time)   [not yet cancelled then]
    A bare customer record with NO active subscription (the "almost sold, then
    bailed before scheduling" case) does NOT exclude — they can be a new lead.

  NUMERATOR — CONVERTED. Of the net leads, those who gained a real
    subscription after the form:
      initial_status in (Completed, Pending)    [first service done or scheduled]
      AND date_added within [form - grace, form + FORM_CONVERSION_WINDOW_DAYS]
    Current active/frozen state is IGNORED — sales-side conversion means the
    first service got scheduled/done; a sub that serviced then cancelled later
    still counts (we're not measuring long-term retention here).

Conversion = converted / net leads. Re-checked on every Sync as data matures
(the form-events cache invalidates whenever the FieldRoutes data changes).
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FORM_CHANNELS = ("gmail", "meta", "lsa")


@dataclass
class LeadEvent:
    first_touch_utc: datetime
    channel: str                 # channel of the first touch
    raw_count: int = 0           # raw form submissions that merged into this lead
    sources: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    emails: set = field(default_factory=set)
    already_customer: bool = False
    converted: bool = False


def load_source_names(path: str) -> dict[str, str]:
    """'Source N' -> 'Provider / Channel' from the sourcing master sheet."""
    out: dict[str, str] = {}
    p = Path(path)
    if not path or not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            label = (row.get("Pestroutes Source") or "").strip()
            name = (row.get("Provider / Channel") or "").strip()
            if label and name:
                out[label] = name
    return out


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_lead_events(formleads_db: str, dedup_window_hours: int,
                      exclude_sources: tuple = (),
                      exclude_source_days: tuple = ()) -> list[LeadEvent]:
    src = sqlite3.connect(f"file:{formleads_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    # exclude_sources: touches that aren't real forms (e.g. Goodzer's emailed
    # call summaries) — dropped before clustering so they neither create nor
    # extend lead events.
    excl = tuple(exclude_sources or ())
    excl_sql = (f"AND (source IS NULL OR source NOT IN ({','.join('?' * len(excl))})) "
                if excl else "")
    touches = src.execute(
        "SELECT channel, source, occurred_at, phone_e164, email FROM touches "
        f"WHERE channel IN ({','.join('?' * len(FORM_CHANNELS))}) "
        f"{excl_sql}"
        "AND occurred_at IS NOT NULL ORDER BY occurred_at",
        FORM_CHANNELS + excl,
    ).fetchall()
    src.close()

    # exclude_source_days ("SOURCE|YYYY-MM-DD", Pacific day): one-off garbage
    # dumps (e.g. a provider's feed glitching and batch-sending junk leads) —
    # dropped before clustering so all views and history recompute clean.
    if exclude_source_days:
        from zoneinfo import ZoneInfo
        pac = ZoneInfo("America/Los_Angeles")
        bad = set()
        for item in exclude_source_days:
            parts = str(item).split("|")
            if len(parts) == 2:
                bad.add((parts[0].strip(), parts[1].strip()))
        if bad:
            def _pac_day(iso: str) -> str:
                return _parse_dt(iso).astimezone(pac).date().isoformat()
            touches = [t for t in touches
                       if ((t["source"] or "").strip(),
                           _pac_day(t["occurred_at"])) not in bad]

    # Cluster identities: any shared phone or email joins two touches.
    uf = _UnionFind()
    for t in touches:
        keys = [k for k in (f"p:{t['phone_e164']}" if t["phone_e164"] else None,
                            f"e:{t['email']}" if t["email"] else None) if k]
        for k in keys[1:]:
            uf.union(keys[0], k)

    by_contact: dict[str, list] = {}
    for t in touches:
        key = f"p:{t['phone_e164']}" if t["phone_e164"] else f"e:{t['email']}"
        by_contact.setdefault(uf.find(key), []).append(t)

    window = timedelta(hours=dedup_window_hours)
    events: list[LeadEvent] = []
    for rows in by_contact.values():
        current: LeadEvent | None = None
        last_dt: datetime | None = None
        for t in rows:  # already time-ordered
            dt = _parse_dt(t["occurred_at"])
            if current is None or (last_dt and dt - last_dt > window):
                current = LeadEvent(first_touch_utc=dt, channel=t["channel"])
                events.append(current)
            last_dt = dt
            current.raw_count += 1
            current.sources.add(t["source"])
            if t["phone_e164"]:
                current.phones.add(t["phone_e164"])
            if t["email"]:
                current.emails.add(t["email"])
    log.info("formleads: %d touches -> %d contacts -> %d lead events",
             len(touches), len(by_contact), len(events))
    return events


_REAL_STATUSES = ("Completed", "Pending")


def classify_events(conn: sqlite3.Connection, events: list[LeadEvent],
                    conversion_window_days: int) -> None:
    """Mark already_customer / converted per the ops spec (see module docstring).

    Only 'real' subscriptions (initial appointment Completed or Pending) ever
    count — a Cancelled-initial or No-Appointment sub means the account was
    started but never became live, so it neither excludes nor converts.
    """
    # key -> list of (added_dt, cancelled_dt|None, active_bool) for real subs
    real_subs: dict[str, list[tuple]] = {}
    for r in conn.execute(
        "SELECT s.date_added_utc, s.date_cancelled_utc, s.active, "
        "       s.phone1_e164, s.phone2_e164, c.email "
        "FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id "
        "WHERE s.date_added_utc IS NOT NULL "
        f"AND s.initial_status IN ({','.join('?' * len(_REAL_STATUSES))})",
        _REAL_STATUSES,
    ):
        added = _parse_dt(r["date_added_utc"])
        cancelled = _parse_dt(r["date_cancelled_utc"]) if r["date_cancelled_utc"] else None
        active = bool(r["active"])
        rec = (added, cancelled, active)
        for key in (f"p:{r['phone1_e164']}" if r["phone1_e164"] else None,
                    f"p:{r['phone2_e164']}" if r["phone2_e164"] else None,
                    f"e:{r['email']}" if r["email"] else None):
            if key:
                real_subs.setdefault(key, []).append(rec)

    window = timedelta(days=conversion_window_days)
    grace = timedelta(hours=2)  # form + same-hour signup still counts as after
    for ev in events:
        keys = [f"p:{p}" for p in ev.phones] + [f"e:{e}" for e in ev.emails]
        first = ev.first_touch_utc
        recs = [rec for k in keys for rec in real_subs.get(k, ())]

        # STEP 2 — had a LIVE subscription at the moment the form arrived?
        for added, cancelled, _active in recs:
            if added <= first - grace and (cancelled is None or cancelled > first):
                ev.already_customer = True
                break
        if ev.already_customer:
            continue

        # NUMERATOR — gained a real sub (Completed/Pending initial) after the
        # form. We do NOT require it to still be active: sales-side conversion
        # is "did the first service get scheduled/done", not retention. A sub
        # that serviced then cancelled/froze later still counts.
        for added, _cancelled, _active in recs:
            if (first - grace) <= added <= (first + window):
                ev.converted = True
                break


def aggregate_events(events: list[LeadEvent], source_names: dict[str, str]) -> dict:
    """Weekly + monthly team rows and a provider rollup."""
    from .periods import month_label, week_label

    def blank() -> dict:
        return {"forms": 0, "deduped": 0, "existing": 0, "net": 0, "converted": 0}

    weekly: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    for ev in events:
        for label, table in ((week_label(ev.first_touch_utc), weekly),
                             (month_label(ev.first_touch_utc), monthly)):
            b = table.setdefault(label, blank())
            b["forms"] += ev.raw_count
            b["deduped"] += 1
            if ev.already_customer:
                b["existing"] += 1
                continue
            b["net"] += 1
            if ev.converted:
                b["converted"] += 1
        if not ev.already_customer:
            for s in ev.sources:
                name = source_names.get(s, s)
                sb = by_source.setdefault(name, {"net": 0, "converted": 0})
                sb["net"] += 1
                if ev.converted:
                    sb["converted"] += 1

    def rows(table: dict[str, dict]) -> list[dict]:
        out = []
        for label in sorted(table):
            b = table[label]
            out.append({
                "period": label, **b,
                "conv_pct": round(100.0 * b["converted"] / b["net"], 1) if b["net"] else 0.0,
            })
        return out

    sources = [{"source": name, **sb,
                "conv_pct": round(100.0 * sb["converted"] / sb["net"], 1) if sb["net"] else 0.0}
               for name, sb in sorted(by_source.items(), key=lambda kv: -kv[1]["net"])]
    return {"weekly": rows(weekly), "monthly": rows(monthly), "sources": sources}
