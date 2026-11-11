"""ONE-OFF (2026-10-06): freeze the Master Metric's scheduled hours.

Reads the old shift-schedule Google Sheet one last time, matches its short
names to tracked agents, and writes one `agent_hours` row (source
'schedule') per agent per day for every day BEFORE the Homebase cutover
(settings.hours_sheet_from, 2026-10-05). From the cutover on, hours come
from the daily Homebase upload sheet (isadash/hours.py); the schedule sheet
is no longer read by the app, so its parser lives here now.

    python scripts/freeze_schedule_hours.py            # dry run: prints what it would write
    python scripts/freeze_schedule_hours.py --write    # writes the rows

Never overwrites a 'homebase' row and never writes a day on/after the cutover.
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings     # noqa: E402
from isadash.hours import ensure              # noqa: E402
from isadash.periods import PACIFIC           # noqa: E402
from isadash.store import connect             # noqa: E402

SCHEDULE_SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
                      "1fPEv0XKoq2I2al_Vk_0uvocFlhGQNkzcrWM8CruMja4/export?format=csv&gid=0")

# ---- the schedule parser, exactly as the Master Metric used it until 2026-10-06 ----
_WEEK_LABEL = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s*$")
_SEG = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")
_NICKNAMES = {"dave": "david", "ben": "benjamin", "zach": "zachary", "nate": "nathan",
              "will": "william", "alex": "alexander", "andy": "andrew", "jake": "jacob",
              "gabe": "gabriel"}


def shift_hours(cell: str) -> float | None:
    """'9-5' -> 8, '9-4:30' -> 7.5, '8-11, 1:30-7' -> 8.5, OFF/blank -> 0, unreadable -> None."""
    s = (cell or "").strip()
    if not s or s in ("-", "–", "—") or s.upper().startswith("OFF"):
        return 0.0
    s = re.sub(r"\([^)]*\)", "", s)
    total, any_seg = 0.0, False
    for seg in re.split(r"[,;.&]|\band\b", s):
        if not seg.strip():
            continue
        m = _SEG.match(seg)
        if not m:
            return None
        a = int(m.group(1)) + int(m.group(2) or 0) / 60
        b = int(m.group(3)) + int(m.group(4) or 0) / 60
        if a < 7:
            a += 12
            b += 12
        if b <= a:
            b += 12
        if not 0 < b - a <= 16:
            return None
        total += b - a
        any_seg = True
    return total if any_seg else None


def parse_schedule(text: str, today: date) -> dict:
    rows = list(csv.reader(io.StringIO(text)))
    blocks = []
    for i, r in enumerate(rows):
        m = _WEEK_LABEL.match(r[1]) if len(r) > 1 else None
        if m:
            blocks.append((i, int(m.group(1)), int(m.group(2))))
    if not blocks:
        return {"days": {}, "unread": [], "weeks": 0}
    _, lm, ld = blocks[-1]
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(date(y, lm, ld))
        except ValueError:
            pass
    last = min(cands, key=lambda d: abs((d - (today + timedelta(days=21))).days))
    starts = [last]
    for (_, m_, d_), (_, pm, _pd) in zip(reversed(blocks[:-1]), reversed(blocks[1:])):
        prev = starts[-1]
        y = prev.year - 1 if m_ > pm else prev.year
        try:
            starts.append(date(y, m_, d_))
        except ValueError:
            starts.append(prev - timedelta(days=7))
    starts.reverse()
    days: dict[tuple[str, date], float] = {}
    unread: list[str] = []
    for (i, _, _), start in zip(blocks, starts):
        for r in rows[i + 3:i + 60]:
            name = (r[0] if r else "").strip()
            if not name:
                break
            for k in range(7):
                cell = r[1 + k] if len(r) > 1 + k else ""
                h = shift_hours(cell)
                day = start + timedelta(days=k)
                if h is None:
                    unread.append(f"{name} {day:%Y-%m-%d}: “{cell.strip()}”")
                    h = 0.0
                if h:
                    days[(name, day)] = days.get((name, day), 0.0) + h
    return {"days": days, "unread": unread, "weeks": len(blocks)}


def match_names(short_names: set[str], agents: list[str]) -> dict[str, str]:
    out = {}
    for sn in short_names:
        parts = sn.split()
        if not parts:
            continue
        first = parts[0].lower()
        formal = _NICKNAMES.get(first, first)
        initial = parts[1][0].lower() if len(parts) > 1 and parts[1] else ""
        hits = []
        for a in agents:
            ap = a.lower().split()
            if not ap:
                continue
            af = ap[0]
            if af not in (first, formal) and _NICKNAMES.get(af, af) != formal:
                continue
            if initial and not ap[-1].startswith(initial):
                continue
            hits.append(a)
        if len(hits) == 1:
            out[sn] = hits[0]
    return out


def main(write: bool) -> None:
    settings = load_settings()
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    cutover = date.fromisoformat(settings.hours_sheet_from)
    r = httpx.get(SCHEDULE_SHEET_URL, follow_redirects=True, timeout=60)
    r.raise_for_status()
    sched = parse_schedule(r.content.decode("utf-8", "replace"), today)
    conn = connect(settings.db_path)
    try:
        ensure(conn)
        agents = [x[0] for x in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE scorecard = 1 AND genesys_name IS NOT NULL")]
        mapping = match_names({n for n, _ in sched["days"]}, agents)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for (n, d), h in sched["days"].items():
            a = mapping.get(n)
            if a and d < cutover and h > 0:
                rows.append((d.isoformat(), a, round(h, 2), None, None, "schedule", now))
        rows.sort()
        per_agent = Counter(a for _, a, *_ in rows)
        print(f"schedule: {sched['weeks']} weeks, {len(sched['days'])} cells; "
              f"{len(mapping)} names matched, unmatched: {sorted({n for n, _ in sched['days']} - set(mapping))}")
        print(f"rows to freeze (days < {cutover}): {len(rows)}, "
              f"{rows[0][0] if rows else '-'} -> {rows[-1][0] if rows else '-'}")
        for a, c in sorted(per_agent.items()):
            print(f"   {a:<22} {c:>4} days")
        unread = [u for u in sched["unread"]
                  if (m := re.search(r"\d{4}-\d{2}-\d{2}", u)) and m.group(0) < cutover.isoformat()]
        if unread:
            print(f"unreadable cells counted as 0 ({len(unread)}): " + "; ".join(unread[:10]))
        if not write:
            print("dry run — nothing written (add --write)")
            return
        with conn:
            conn.executemany(
                "INSERT INTO agent_hours (day, agent, hours, regular, ot, source, loaded_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(day, agent) DO UPDATE SET hours=excluded.hours, "
                "source=excluded.source, loaded_at=excluded.loaded_at "
                "WHERE agent_hours.source = 'schedule'", rows)
        n = conn.execute("SELECT COUNT(*) FROM agent_hours WHERE source = 'schedule'").fetchone()[0]
        print(f"written; agent_hours now holds {n} frozen schedule rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main(write="--write" in sys.argv)
