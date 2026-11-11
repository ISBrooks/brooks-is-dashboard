"""Daily, READ-ONLY count of form-lead emails the sourcing app could not place on a source.

The sourcing app pulls form leads from Gmail by sender, runs each email through its
provider rules (source_maps/email_providers.csv), and silently DROPS any email whose
source it cannot identify — so nothing in its database records those. This job
replays exactly that parsing over the recent lead emails and counts, per Pacific
day, how many came back "unidentified source". The IS Dashboard shows the 30-day
total on the Sourcing App card (Settings > Software Management).

It never changes anything in the sourcing app: it only reads Gmail with the
sourcing app's own token (which is why it runs as root from /opt/lead-attribution,
exactly like the app's own watchers — the Google client rewrites the token file
when it refreshes) and writes one JSON file in the dashboard's data folder.

What is counted
  forms       every email from the sourcing app's lead senders that day
  unmapped    those whose source could not be identified (the lead was dropped)
  no_contact  identified source but no phone/email found (usually not a lead)
Not covered: senders the sourcing app doesn't pull at all (see its
scripts/gmail_new_senders.py for that).

Usage:  python unmapped_forms_watch.py [--days 30] [--dry]
  Fills in every missing Pacific day in the last --days (yesterday backwards);
  days already counted are skipped, so the normal daily run is one day.
Cron:   /etc/cron.d/isdash-unmapped-forms (6:25 AM PT, after the nightly run).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCING = Path("/opt/lead-attribution")
OUT = Path("/home/isdash/brooks-is-dash/data/unmapped_forms.json")
KEEP_DAYS = 60                    # history kept in the JSON
PER_MESSAGE_SLEEP = 0.2           # Gmail quota is shared with the live DoLead watcher
MAX_PER_DAY = 800                 # fetch cap sizing (a busy day is ~300 lead emails)
PT = ZoneInfo("America/Los_Angeles")
_SITE = re.compile(r"your website\s*-\s*https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", re.I)

sys.path.insert(0, str(SOURCING / "src"))
os.chdir(SOURCING)                # the sourcing config resolves its paths from here


def log(msg: str) -> None:
    print(f"[{datetime.now(PT):%m-%d %H:%M}] {msg}", flush=True)


def load_state() -> dict:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — a corrupt file is rebuilt, not fatal
            log(f"could not read {OUT}: {e}; starting fresh")
    return {"days": {}, "last_run": None, "last_error": None}


def save_state(state: dict) -> None:
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    state["days"] = {d: v for d, v in sorted(state["days"].items()) if d >= cutoff}
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


def pt_midnight_epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=PT).timestamp())


def count_days(missing: list[date], dry: bool) -> dict[str, dict]:
    from leadsource.config import load_settings
    from leadsource.pipeline import load_maps
    from leadsource.readers.gmail import GmailReader, build_touch_from_email

    s = load_settings()
    source_map, rules = load_maps(s)
    gm = GmailReader(s.gmail_credentials_file, s.gmail_token_file, s.gmail_label)

    # Gmail's after:/before: accept epoch seconds. Query one day wider on each
    # side (header dates vs Gmail's own receipt time can straddle midnight) and
    # bucket by the email's own date, keeping only the days we're filling.
    lo, hi = min(missing), max(missing)
    query = (f"{s.gmail_lead_query_prefix} after:{pt_midnight_epoch(lo - timedelta(days=1))} "
             f"before:{pt_midnight_epoch(hi + timedelta(days=2))}")
    cap = min(20000, (len(missing) + 2) * MAX_PER_DAY)
    want = {d.isoformat() for d in missing}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    buckets = {d: {"forms": 0, "unmapped": 0, "no_contact": 0, "unmapped_senders": Counter(),
                   "unmapped_sites": [], "counted_at": now_iso} for d in want}
    fetched = 0
    for mid, raw in gm.fetch_raw_messages(query, max_results=cap):
        fetched += 1
        res = build_touch_from_email(raw, rules, source_map, raw_ref=mid)
        if not res.parsed.date:
            continue
        day = res.parsed.date.replace(tzinfo=timezone.utc).astimezone(PT).date().isoformat()
        b = buckets.get(day)
        if b is None:
            continue
        b["forms"] += 1
        if res.source is None:
            b["unmapped"] += 1
            b["unmapped_senders"][res.parsed.from_domain or "?"] += 1
            m = _SITE.search(res.parsed.text or "")
            site = (m.group(1).lower() if m else None)
            if site and site not in b["unmapped_sites"] and len(b["unmapped_sites"]) < 8:
                b["unmapped_sites"].append(site)
        elif not (res.phones or res.emails):
            b["no_contact"] += 1
        time.sleep(PER_MESSAGE_SLEEP)
    log(f"fetched {fetched} lead emails for {len(missing)} day(s) (cap {cap})")
    if fetched >= cap:
        # Gmail lists newest first, so a capped pull shorts the OLDEST day.
        oldest = lo.isoformat()
        log(f"WARNING: hit the fetch cap — {oldest} is incomplete and will be retried next run")
        buckets.pop(oldest, None)
    for b in buckets.values():
        b["unmapped_senders"] = dict(b["unmapped_senders"].most_common())
    return buckets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="fill in missing days this far back")
    ap.add_argument("--dry", action="store_true", help="count and print; write nothing")
    args = ap.parse_args()

    state = load_state()
    today = datetime.now(PT).date()
    missing = [today - timedelta(days=k) for k in range(1, args.days + 1)
               if (today - timedelta(days=k)).isoformat() not in state["days"]]
    if not missing:
        log("nothing to do — every day in the window is already counted")
        state["last_run"], state["last_error"] = datetime.now(timezone.utc).isoformat(timespec="seconds"), None
        if not args.dry:
            save_state(state)
        return 0

    log(f"counting {len(missing)} day(s): {min(missing)} .. {max(missing)}{' (dry run)' if args.dry else ''}")
    try:
        buckets = count_days(missing, args.dry)
    except Exception as e:  # noqa: BLE001 — recorded for the dashboard, never silent
        err = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"FAILED: {err}")
        state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["last_error"] = err
        if not args.dry:
            save_state(state)
        return 1

    for d in sorted(buckets):
        b = buckets[d]
        log(f"  {d}: {b['forms']} forms, {b['unmapped']} unmapped, {b['no_contact']} no-contact"
            + (f"  senders={b['unmapped_senders']} sites={b['unmapped_sites']}" if b["unmapped"] else ""))
    state["days"].update(buckets)
    state["last_run"], state["last_error"] = datetime.now(timezone.utc).isoformat(timespec="seconds"), None
    if args.dry:
        log("dry run — nothing written")
        return 0
    save_state(state)
    log(f"wrote {OUT} ({len(state['days'])} days on file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
