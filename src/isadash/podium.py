"""Podium (read-only) for the Master Metric: who made how many outbound calls.

The dashboard has its own Podium connection (scripts/podium_connect.py writes
PODIUM_* into .env). Podium's call list can only be walked in "last changed"
order from a starting time, so calls are mirrored into SQLite and each load
just fetches what changed since the last one.

The same Podium app also runs the FieldRoutes->Podium sync, and Podium allows
300 requests a minute, so everything here is paced well below that.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .store import get_config

log = logging.getLogger("isadash.podium")

TOKEN_URL = "https://accounts.podium.com/oauth/token"
API = "https://api.podium.com/v4"
_MIN_GAP_S = 0.35               # ~170 requests/min at most
_SYNC_EVERY_S = 120             # don't re-check Podium more often than this
_BACKFILL_FROM = "2026-08-30T00:00:00Z"   # how far back the first sync reaches

_SCHEMA = """
CREATE TABLE IF NOT EXISTS podium_calls (
    uid         TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,          -- when the call finished (UTC)
    updated_at  TEXT NOT NULL,
    user_uid    TEXT,
    direction   TEXT,
    event_type  TEXT,
    duration_s  INTEGER
);
CREATE INDEX IF NOT EXISTS podium_calls_created ON podium_calls (created_at);
CREATE TABLE IF NOT EXISTS podium_users (
    uid  TEXT PRIMARY KEY,
    name TEXT
);
"""
_lock = threading.Lock()
_state = {"token": None, "exp": 0.0, "last_req": 0.0, "last_sync": 0.0}


def configured(settings) -> bool:
    return bool(settings.podium_client_id and settings.podium_client_secret
                and settings.podium_refresh_token)


def _token(settings) -> str:
    if _state["token"] and time.time() < _state["exp"] - 120:
        return _state["token"]
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": settings.podium_refresh_token,
        "client_id": settings.podium_client_id, "client_secret": settings.podium_client_secret},
        timeout=30)
    r.raise_for_status()
    body = r.json()
    _state["token"] = body["access_token"]
    _state["exp"] = time.time() + int(body.get("expires_in", 3600))
    return _state["token"]


def _get(settings, path: str, params: dict, gap: float = _MIN_GAP_S) -> dict:
    for attempt in range(5):
        wait = gap - (time.time() - _state["last_req"])
        if wait > 0:
            time.sleep(wait)
        _state["last_req"] = time.time()
        try:
            x = httpx.get(API + path, params=params, timeout=60,
                          headers={"Authorization": "Bearer " + _token(settings)})
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1))
            continue
        if x.status_code == 429 or x.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        x.raise_for_status()
        return x.json()
    raise RuntimeError(f"Podium kept failing on {path}")


def _pages(settings, path: str, **params):
    while True:
        j = _get(settings, path, params)
        yield j.get("data") or []
        cur = (j.get("metadata") or {}).get("nextCursor")
        if not cur:
            return
        params = {"cursor": cur, "limit": 100}


def sync(conn: sqlite3.Connection, settings, *, force: bool = False,
         max_seconds: float = 25.0) -> dict:
    """Bring the local copy of Podium calls up to date. Returns
    {'ok', 'complete', 'new'}; `complete` is False when a long catch-up was
    cut short (the next load carries on from where it stopped)."""
    if not configured(settings):
        return {"ok": False, "complete": False, "new": 0, "error": "Podium isn't connected"}
    with _lock:
        if not force and time.time() - _state["last_sync"] < _SYNC_EVERY_S:
            return {"ok": True, "complete": True, "new": 0}
        conn.executescript(_SCHEMA)
        # users rarely change: refresh the names once a day
        last_users = get_config(conn, "podium_users_synced")
        if not last_users or json.loads(last_users) < (datetime.now(timezone.utc)
                                                       - timedelta(days=1)).isoformat():
            for page in _pages(settings, "/users", limit=100):
                conn.executemany(
                    "INSERT INTO podium_users (uid, name) VALUES (?, ?) "
                    "ON CONFLICT(uid) DO UPDATE SET name = excluded.name",
                    [(u["uid"], " ".join(((u.get("firstName") or "").strip() + " "
                                          + (u.get("lastName") or "").strip()).split()))
                     for u in page])
            _set(conn, "podium_users_synced", datetime.now(timezone.utc).isoformat())
            conn.commit()

        raw = get_config(conn, "podium_calls_since")
        since = json.loads(raw) if raw else _BACKFILL_FROM
        started, new, complete, high = time.time(), 0, True, since
        # "since" is inclusive and pages come oldest-change-first, so the
        # watermark only moves once a walk has run to its end (or is cut at a
        # page boundary, where restarting from the last timestamp is safe
        # because rows are upserted by uid).
        for page in _pages(settings, "/calls", limit=100, since=since, order="asc"):
            rows = [(c["uid"], c.get("createdAt") or c.get("updatedAt"), c.get("updatedAt"),
                     c.get("userUid"), c.get("direction"), c.get("eventType"),
                     c.get("durationSeconds")) for c in page if c.get("uid") and c.get("updatedAt")]
            conn.executemany(
                "INSERT INTO podium_calls (uid, created_at, updated_at, user_uid, direction, "
                "event_type, duration_s) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET created_at = excluded.created_at, "
                "updated_at = excluded.updated_at, user_uid = excluded.user_uid, "
                "direction = excluded.direction, event_type = excluded.event_type, "
                "duration_s = excluded.duration_s", rows)
            new += len(rows)
            if rows:
                high = max(high, max(r[2] for r in rows))
            _set(conn, "podium_calls_since", high)
            conn.commit()                          # short transactions; never held across requests
            if time.time() - started > max_seconds:
                complete = False
                break
        _state["last_sync"] = time.time() if complete else 0.0
        return {"ok": True, "complete": complete, "new": new}


def _set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO app_config (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def calls_made_by_name(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> dict[str, int]:
    """Podium user name -> outbound calls finished in [start, end) — what the
    Podium Leaderboard calls "Total Calls Made"."""
    conn.executescript(_SCHEMA)
    return {r[0]: r[1] for r in conn.execute(
        "SELECT COALESCE(u.name, c.user_uid), COUNT(*) FROM podium_calls c "
        "LEFT JOIN podium_users u ON u.uid = c.user_uid "
        "WHERE c.direction = 'outbound' AND c.user_uid IS NOT NULL "
        "AND c.created_at >= ? AND c.created_at < ? GROUP BY 1", (start_utc, end_utc))}


def match_names(podium_names, agents: list[str]) -> dict[str, str]:
    """Podium display name -> dashboard agent name. Exact match first, then
    same first name + same final surname word ('Micah Guzman' = 'Micah De Guzman')."""
    out = {}
    low = {a.lower(): a for a in agents}
    for pn in podium_names:
        if not pn:
            continue
        hit = low.get(pn.lower())
        if not hit:
            pp = pn.lower().split()
            cands = [a for a in agents
                     if len(pp) >= 2 and a.lower().split()[0] == pp[0]
                     and a.lower().split()[-1] == pp[-1]]
            hit = cands[0] if len(cands) == 1 else None
        if hit:
            out[pn] = hit
    return out


# --- conversations (the Leaderboard's "Total Conversations") ----------------
# Podium counts, per employee, the conversations they sent at least one
# message in. Messages can only be read one conversation at a time, so this is
# far too heavy to do when a page loads (a single day is ~4,000 requests). It
# is collected by the morning sync only, for the day before, and stored as one
# row per (day, employee, conversation) so any date range can be counted
# without double-counting a conversation that spans days.
# Days are Mountain-time days, the same as Podium's own Leaderboard.
MOUNTAIN = ZoneInfo("America/Denver")
_CONV_SCHEMA = """
CREATE TABLE IF NOT EXISTS podium_conv_touch (
    day              TEXT NOT NULL,
    user_uid         TEXT NOT NULL,
    conversation_uid TEXT NOT NULL,
    messages         INTEGER NOT NULL,
    PRIMARY KEY (day, user_uid, conversation_uid)
);
CREATE TABLE IF NOT EXISTS podium_conv_days (
    day          TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    scanned      INTEGER,
    skipped      INTEGER
);
"""


def collect_conversations(conn: sqlite3.Connection, settings, days: list[date], *,
                          gap: float = _MIN_GAP_S, progress=None) -> dict:
    """Read every conversation active since the earliest of `days` and record
    who sent messages in which conversation on each of those days. Replaces
    whatever was stored for those days. Nothing is written until the whole
    walk has finished, so an interrupted run leaves the old data intact."""
    if not configured(settings):
        return {"ok": False, "error": "Podium isn't connected"}
    days = sorted(set(days))
    want = {d.isoformat() for d in days}
    first = datetime(days[0].year, days[0].month, days[0].day, tzinfo=MOUNTAIN)
    nxt = days[-1] + timedelta(days=1)
    last = datetime(nxt.year, nxt.month, nxt.day, tzinfo=MOUNTAIN)
    since = f"{first.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    stop = f"{last.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    touch: dict[tuple[str, str, str], int] = {}
    scanned = skipped = 0
    params: dict = {"limit": 100, "since": since, "order": "asc"}
    while True:
        page = _get(settings, "/conversations", params, gap)
        for c in page.get("data") or []:
            scanned += 1
            try:
                mp: dict = {"since": since, "order": "asc"}
                done = False
                while not done:
                    mj = _get(settings, f"/conversations/{c['uid']}/messages", mp, gap)
                    for m in mj.get("data") or []:
                        created = m.get("createdAt") or ""
                        if created >= stop:           # oldest-first: nothing later is in range
                            done = True
                            break
                        su = m.get("senderUid") or (m.get("sender") or {}).get("uid")
                        if not su or created < since:
                            continue
                        day = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                               .astimezone(MOUNTAIN).date().isoformat())
                        if day in want:
                            k = (day, su, c["uid"])
                            touch[k] = touch.get(k, 0) + 1
                    cur = (mj.get("metadata") or {}).get("nextCursor")
                    if not cur:
                        break
                    mp = {"cursor": cur}
            except Exception:                         # one unreadable conversation never stops the run
                skipped += 1
                log.warning("podium conversation %s skipped", c.get("uid"), exc_info=True)
            if progress and scanned % 500 == 0:
                progress(scanned)
        cur = (page.get("metadata") or {}).get("nextCursor")
        if not cur:
            break
        params = {"cursor": cur, "limit": 100}

    conn.executescript(_CONV_SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in want:
        conn.execute("DELETE FROM podium_conv_touch WHERE day = ?", (d,))
        conn.execute("INSERT INTO podium_conv_days (day, collected_at, scanned, skipped) "
                     "VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET collected_at = "
                     "excluded.collected_at, scanned = excluded.scanned, skipped = excluded.skipped",
                     (d, now, scanned, skipped))
    conn.executemany("INSERT INTO podium_conv_touch (day, user_uid, conversation_uid, messages) "
                     "VALUES (?,?,?,?)", [(d, u, cu, n) for (d, u, cu), n in touch.items()])
    conn.commit()
    return {"ok": True, "days": sorted(want), "scanned": scanned, "skipped": skipped,
            "rows": len(touch)}


def conversation_days(conn: sqlite3.Connection, start: date, end: date) -> set[str]:
    """Which days in the range have conversations collected."""
    conn.executescript(_CONV_SCHEMA)
    return {r[0] for r in conn.execute(
        "SELECT day FROM podium_conv_days WHERE day >= ? AND day <= ?",
        (start.isoformat(), end.isoformat()))}


def conversations_by_name(conn: sqlite3.Connection, start: date, end: date) -> dict[str, int]:
    """Podium user name -> distinct conversations they messaged in the range."""
    conn.executescript(_CONV_SCHEMA + _SCHEMA)
    return {r[0]: r[1] for r in conn.execute(
        "SELECT COALESCE(u.name, t.user_uid), COUNT(DISTINCT t.conversation_uid) "
        "FROM podium_conv_touch t LEFT JOIN podium_users u ON u.uid = t.user_uid "
        "WHERE t.day >= ? AND t.day <= ? GROUP BY 1", (start.isoformat(), end.isoformat()))}


def missing_recent_days(conn: sqlite3.Connection, lookback: int = 3) -> list[date]:
    """Yesterday (Mountain) plus any of the few days before it that a failed
    morning run left uncollected — but never further back than the first day
    ever collected, so this can't turn into an accidental backfill."""
    conn.executescript(_CONV_SCHEMA)
    yesterday = datetime.now(timezone.utc).astimezone(MOUNTAIN).date() - timedelta(days=1)
    have = {r[0] for r in conn.execute("SELECT day FROM podium_conv_days")}
    earliest = min(have) if have else yesterday.isoformat()
    out = []
    for i in range(lookback):
        d = yesterday - timedelta(days=i)
        if d.isoformat() not in have and d.isoformat() >= earliest:
            out.append(d)
    return out
