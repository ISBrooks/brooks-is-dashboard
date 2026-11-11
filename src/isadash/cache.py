"""Saved results for expensive read-only calculations.

The dashboard's numbers only change when the data behind them changes (a sync,
a Settings edit), yet they used to be recomputed from raw rows on every page
load. `@cached` keeps a function's result until something it actually read
changes.

How it knows what a result depends on - nothing has to be declared by hand:

* While a cached function runs, every database connection it opens (through
  `store.connect`) reports which TABLES its queries read. Those become the
  result's dependencies.
* Each such table carries a version number in `table_versions`, bumped by
  database triggers on every insert / update / delete. The triggers live in the
  database itself, so a write from ANY code path - the web app, the morning
  sync, a one-off script - is seen. A result is reused only while every table
  it read still has the version it had when the calculation started.
* A file outside the database (the sourcing app's form-lead database) is a
  dependency by modification time: call `depend_on_file()` before reading it.
* Results also roll over with the Pacific calendar day, because most windows
  are "the last N complete days".

Clock punches, sign-ins and the like write to tables no cached number reads, so
they no longer throw anything away.

Only decorate functions whose inputs are the database (via `store.connect`),
files registered with `depend_on_file`, their arguments and today's date. A
function that calls an outside API or reads a sheet must not be cached here
(or must be given a `ttl`).

Set ISDASH_CACHE=0 in .env to switch all of this off.
"""
from __future__ import annotations

import contextvars
import copy
import functools
import inspect
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("isadash.perf")

_MAX_ENTRIES = 400
_IGNORE_TABLES = ("sqlite_", "table_versions")

_db_path: Path | None = None
_entries: "OrderedDict[tuple, _Entry]" = OrderedDict()
_entries_lock = threading.Lock()
_key_locks: dict[tuple, threading.Lock] = {}
_not_tables: set[str] = set()          # names the authorizer reports that aren't real tables
_stats = {"hits": 0, "misses": 0, "uncached": 0}
_recorders: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "isadash_cache_recorders", default=())


class _Recorder:
    """What one in-flight calculation has read so far."""
    __slots__ = ("tables", "files", "expires")

    def __init__(self) -> None:
        self.tables: set[str] = set()
        # path -> (mtime when read, seconds a change may go unnoticed, monotonic time read)
        self.files: dict[str, tuple[float | None, float, float]] = {}
        self.expires: float | None = None      # monotonic deadline, if any input has a ttl


class _Entry:
    __slots__ = ("value", "tables", "files", "expires", "built", "ms")

    def __init__(self, value, tables, files, expires, ms) -> None:
        self.value = value
        self.tables: dict[str, int] = tables
        self.files = files
        self.expires = expires
        self.built = time.monotonic()
        self.ms = ms


def configure(db_path) -> None:
    """Tell the cache which database carries the table versions."""
    global _db_path
    _db_path = Path(db_path)


def enabled() -> bool:
    return _db_path is not None and os.environ.get("ISDASH_CACHE", "1") != "0"


# ---- recording what a calculation reads ----------------------------------
def _authorizer(action, arg1, _arg2, _dbname, _source):
    # called by SQLite while it compiles each statement; must never raise
    if action == sqlite3.SQLITE_READ and arg1:
        for rec in _recorders.get():
            rec.tables.add(arg1)
    return sqlite3.SQLITE_OK


def watch(conn: sqlite3.Connection) -> None:
    """Called by store.connect for every new connection: if a cached
    calculation is running in this thread, record the tables it reads."""
    if _recorders.get():
        conn.set_authorizer(_authorizer)


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def depend_on_file(path, max_lag_s: float = 0) -> None:
    """Make the running calculation depend on a file. Call BEFORE reading it.
    max_lag_s: how long a change to the file may go unnoticed (0 = never). Use
    it for a file that changes constantly when slightly old numbers are fine."""
    p = str(path)
    item = (_mtime(p), float(max_lag_s), time.monotonic())
    for rec in _recorders.get():
        rec.files.setdefault(p, item)


# ---- table versions ------------------------------------------------------
def _versions() -> dict[str, int]:
    """Current version of every tracked table ({} if tracking isn't set up)."""
    try:
        conn = sqlite3.connect(_db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, AttributeError, ValueError):
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT name, v FROM table_versions")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _track(names: list[str]) -> list[str]:
    """Start versioning these tables (create the triggers). Returns the ones
    that are real tables and are now tracked."""
    conn = sqlite3.connect(_db_path, timeout=30)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS table_versions "
                     "(name TEXT PRIMARY KEY, v INTEGER NOT NULL)")
        real = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        done = []
        for t in names:
            if t not in real:
                _not_tables.add(t)       # a view, json_each(), ... - nothing to version
                continue
            q = '"' + t.replace('"', '""') + '"'
            lit = "'" + t.replace("'", "''") + "'"
            conn.execute("INSERT OR IGNORE INTO table_versions (name, v) VALUES (?, 0)", (t,))
            for tag, event in (("i", "INSERT"), ("u", "UPDATE"), ("d", "DELETE")):
                trig = '"' + f"tv_{tag}_{t}".replace('"', '""') + '"'
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trig} AFTER {event} ON {q} "
                    f"BEGIN UPDATE table_versions SET v = v + 1 WHERE name = {lit}; END")
            done.append(t)
        conn.commit()
        return done
    finally:
        conn.close()


# ---- the cache itself ----------------------------------------------------
def _pacific_day() -> str:
    from .periods import PACIFIC
    return datetime.now(timezone.utc).astimezone(PACIFIC).date().isoformat()


def _stale_reason(e: _Entry, versions: dict[str, int] | None = None) -> str | None:
    now = time.monotonic()
    if e.expires is not None and now >= e.expires:
        return "time limit"
    if e.tables:
        cur = _versions() if versions is None else versions
        changed = sorted(t for t, v in e.tables.items() if cur.get(t) != v)
        if changed:
            return "changed: " + ", ".join(changed)
    for p, (mtime, lag, since) in e.files.items():
        if _mtime(p) != mtime and now - since >= lag:
            return "file changed: " + os.path.basename(p)
    return None


def _share(e: _Entry) -> None:
    """A calculation that used this saved result depends on what it depended on."""
    for rec in _recorders.get():
        rec.tables.update(e.tables)
        for p, item in e.files.items():
            rec.files.setdefault(p, item)
        if e.expires is not None:
            rec.expires = e.expires if rec.expires is None else min(rec.expires, e.expires)


def _lookup(key) -> tuple[_Entry | None, str | None]:
    with _entries_lock:
        e = _entries.get(key)
    if e is None:
        return None, "first use"
    why = _stale_reason(e)
    if why is None:
        with _entries_lock:
            if key in _entries:
                _entries.move_to_end(key)
        return e, None
    return None, why


def _get(key, label, compute, copy_result, ttl):
    e, why = _lookup(key)
    if e is None:
        with _entries_lock:
            lock = _key_locks.setdefault(key, threading.Lock())
        with lock:                      # one calculation per key at a time; others wait for it
            e, why2 = _lookup(key)
            if e is None:
                e, value = _build(key, label, compute, ttl, why2 or why)
                if e is None:           # computed but not saved (first run on a new table):
                    return value        # a private object, no copy needed
            else:
                _stats["hits"] += 1
    else:
        _stats["hits"] += 1
    _share(e)
    return copy.deepcopy(e.value) if copy_result else e.value


def _build(key, label, compute, ttl, why) -> tuple[_Entry | None, object]:
    before = _versions()                # BEFORE reading, so a write mid-way makes the result stale
    rec = _Recorder()
    token = _recorders.set(_recorders.get() + (rec,))
    t0 = time.perf_counter()
    try:
        value = compute()
    finally:
        _recorders.reset(token)
    ms = (time.perf_counter() - t0) * 1000
    tables = {t for t in rec.tables if not t.startswith(_IGNORE_TABLES) and t not in _not_tables}
    # everything this calculation read is also read by whatever called it
    for outer in _recorders.get():
        outer.tables.update(tables)
        for p, item in rec.files.items():
            outer.files.setdefault(p, item)

    untracked = sorted(t for t in tables if t not in before)
    if untracked:
        try:
            started = _track(untracked)
        except sqlite3.Error:
            log.exception("cache: could not start tracking %s", untracked)
            started = untracked
        if started:
            # No version existed when we began, so we can't prove nothing changed
            # meanwhile. Hand the result back unsaved; the next call is tracked.
            _stats["uncached"] += 1
            log.info("cache: %s now tracking %s (%.0f ms, not saved this once)", label, started, ms)
            return None, value
        tables = {t for t in tables if t not in _not_tables}

    expires = rec.expires
    if ttl:
        mine = time.monotonic() + ttl
        expires = mine if expires is None else min(expires, mine)
    e = _Entry(value, {t: before[t] for t in tables if t in before}, dict(rec.files), expires, ms)
    with _entries_lock:
        _entries[key] = e
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            old, _ = _entries.popitem(last=False)
            _key_locks.pop(old, None)
    _stats["misses"] += 1
    log.info("cache: built %s in %.0f ms (%s)", label, ms, why)
    return e, value


def cached(*, copy_result: bool = True, ttl=None):
    """Save a function's result until what it read changes (see module docs).

    copy_result=False hands every caller the SAME object - only for results no
    caller modifies (big payloads where a copy per request would cost too much).
    ttl: seconds, or a function of the call's arguments returning seconds/None,
    for results that also drift with the clock (a rolling "last 30 days")."""
    def deco(fn):
        sig = inspect.signature(fn)
        label = fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not enabled():
                return fn(*args, **kwargs)
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                params = tuple(sorted(bound.arguments.items()))
                key = (label, params, _pacific_day())
                hash(key)
            except TypeError:           # an argument that can't be a key: just compute
                return fn(*args, **kwargs)
            t = ttl(dict(params)) if callable(ttl) else ttl
            shown = label + ("(" + ", ".join(f"{k}={v}" for k, v in params) + ")" if params else "")
            return _get(key, shown, lambda: fn(*args, **kwargs), copy_result, t)

        wrapper.uncached = fn
        return wrapper
    return deco


def is_fresh(fn, **kwargs) -> bool:
    """True if a saved, still-valid result exists for this call (for the warmer)."""
    sig = inspect.signature(fn)
    bound = sig.bind(**kwargs)
    bound.apply_defaults()
    key = (fn.__name__, tuple(sorted(bound.arguments.items())), _pacific_day())
    e, _ = _lookup(key)
    return e is not None


def clear() -> None:
    """Forget everything (after a manual Sync, so fresh numbers show at once)."""
    with _entries_lock:
        _entries.clear()
        _key_locks.clear()


def stats() -> dict:
    with _entries_lock:
        items = [(k[0], e.ms, sorted(e.tables), [os.path.basename(p) for p in e.files])
                 for k, e in _entries.items()]
    return {**_stats, "entries": len(items),
            "saved": [{"what": n, "build_ms": round(ms), "tables": t, "files": f}
                      for n, ms, t, f in items]}


# ---- keeping the common pages ready --------------------------------------
def start_warmer(calls, interval_s: float = 60, first_delay_s: float = 8) -> threading.Thread:
    """Background thread that keeps a few results ready so no visitor waits for
    them. `calls` is a function returning [(cached_fn, kwargs), ...] - evaluated
    each round because the arguments can depend on today's date.

    It waits until the data has been quiet for one round before rebuilding, so
    it doesn't recompute over and over while a sync is still writing."""
    def run():
        last = None
        time.sleep(first_delay_s)
        while True:
            try:
                if enabled():
                    now = _versions()
                    if last is not None and now != last:
                        last = now      # data is moving: look again next round
                    else:
                        last = now
                        for fn, kwargs in calls():
                            try:
                                if not is_fresh(fn, **kwargs):
                                    fn(**kwargs)
                            except Exception:  # noqa: BLE001 - one bad feed mustn't stop the rest
                                log.exception("cache: warming %s failed", getattr(fn, "__name__", fn))
            except Exception:  # noqa: BLE001
                log.exception("cache: warmer round failed")
            time.sleep(interval_s)

    t = threading.Thread(target=run, name="cache-warmer", daemon=True)
    t.start()
    return t
