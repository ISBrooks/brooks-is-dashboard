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

* Outside data that can't be watched that way gets a cheap fingerprint:
  `depend_on_value()` (e.g. "how many form touches, and the newest one").

Clock punches, sign-ins and the like write to tables no cached number reads, so
they no longer throw anything away. Bookkeeping rows inside a tracked table can
be left out too (`ignore_rows`).

Data fetched from an outside service (a Google sheet, Genesys) goes through
`keep_fresh()`: the last answer is handed back at once, and when it is older
than its time limit it is re-fetched in the background, by one caller only.
Nobody waits for the outside service except the very first time. A saved result
that used such data is rebuilt when the fetched data actually changes.

Only decorate functions whose inputs are the database (via `store.connect`),
files registered with `depend_on_file`, fingerprints from `depend_on_value`,
outside data read through `keep_fresh`, their arguments and today's date. A
function that calls an outside API directly must not be cached here (or must
be given a `ttl`).

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
_row_filters: dict[str, str] = {}      # table -> SQL condition on {row} that must hold for a write to count
_probes: dict = {}                     # fingerprint key -> (monotonic time taken, value)
_MAX_SOURCES = 300
_sources: "OrderedDict[tuple, _Source]" = OrderedDict()
_sources_lock = threading.Lock()


class _Recorder:
    """What one in-flight calculation has read so far."""
    __slots__ = ("tables", "files", "values", "sources", "expires")

    def __init__(self) -> None:
        self.tables: set[str] = set()
        # path -> (mtime when read, seconds a change may go unnoticed, monotonic time read)
        self.files: dict[str, tuple[float | None, float, float]] = {}
        # fingerprint key -> (fingerprint, lag seconds, monotonic time read, probe, re-check seconds)
        self.values: dict = {}
        # keep_fresh key -> the version of that outside data this calculation saw
        self.sources: dict = {}
        self.expires: float | None = None      # monotonic deadline, if any input has a ttl


class _Entry:
    __slots__ = ("value", "tables", "files", "values", "sources", "expires", "built", "ms")

    def __init__(self, value, tables, files, expires, ms, values=None, sources=None) -> None:
        self.value = value
        self.tables: dict[str, int] = tables
        self.files = files
        self.values = values or {}
        self.sources = sources or {}
        self.expires = expires
        self.built = _clock()
        self.ms = ms


def _clock() -> float:
    """The one clock every age and time limit in this module is measured on
    (tests replace it to make time pass)."""
    return time.monotonic()


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
    item = (_mtime(p), float(max_lag_s), _clock())
    for rec in _recorders.get():
        rec.files.setdefault(p, item)


def _probe(key, probe, max_age_s: float):
    """The fingerprint for `key`, re-taken at most every max_age_s seconds."""
    now = _clock()
    hit = _probes.get(key)
    if hit and now - hit[0] < max_age_s:
        return hit[1]
    try:
        value = probe()
    except Exception:  # noqa: BLE001 - unreadable right now: counts as "changed" until it reads again
        value = ("unreadable", now)
    _probes[key] = (now, value)
    return value


def depend_on_value(key, probe, max_lag_s: float = 0, check_every_s: float = 30) -> None:
    """Make the running calculation depend on a fingerprint of outside data:
    `probe()` must be cheap and return something that changes when the data
    does. Call BEFORE reading the data. max_lag_s: how long a change may go
    unnoticed. The fingerprint is re-taken at most every check_every_s seconds.

    The FIRST registration for a key wins, for files too. So a calculation that
    can live with older data than the helper it calls (today's form leads can't
    move a number whose window ended yesterday) registers its own, longer lag
    before calling the helper."""
    recs = _recorders.get()
    if not recs:
        return
    item = (_probe(key, probe, 0), float(max_lag_s), _clock(), probe, float(check_every_s))
    for rec in recs:
        rec.values.setdefault(key, item)


# ---- outside data: answer at once, refresh behind -------------------------
class _Source:
    __slots__ = ("value", "has", "at", "version", "fetch", "ttl", "busy", "retry_after", "lock", "error")

    def __init__(self) -> None:
        self.value = None
        self.has = False
        self.at = 0.0
        self.version = 0
        self.fetch = None
        self.ttl = 0.0
        self.busy = False
        self.retry_after = 0.0
        self.lock = threading.Lock()
        self.error: Exception | None = None     # why the last first-time fetch failed


def _store(src: _Source, value) -> None:
    with _sources_lock:
        try:
            same = src.has and value == src.value
        except Exception:  # noqa: BLE001 - can't compare: treat as new
            same = False
        if not same:
            src.version += 1
        src.value, src.has, src.at = value, True, _clock()


def _refresh_behind(key, src: _Source) -> None:
    """Re-fetch on a side thread; one at a time per key, and not again for a
    minute after a failure (the old answer stays in use meanwhile)."""
    with _sources_lock:
        if src.busy or _clock() < src.retry_after or src.fetch is None:
            return
        src.busy = True
    fetch = src.fetch

    def run():
        try:
            _store(src, fetch())
        except Exception as e:  # noqa: BLE001
            src.retry_after = _clock() + 60
            log.info("keep_fresh: refreshing %s failed (%s: %s); keeping the last answer",
                     key[0] if isinstance(key, tuple) and key else key, type(e).__name__, str(e)[:120])
        finally:
            src.busy = False

    threading.Thread(target=run, name="keep-fresh", daemon=True).start()


def keep_fresh(key, fetch, ttl: float, max_stale: float | None = None):
    """The answer of `fetch()` (a call to an outside service), without making
    callers wait for it more than once.

    * younger than `ttl` seconds: the saved answer.
    * older than that but younger than `max_stale` (default 6 x ttl): the saved
      answer straight away, and one background re-fetch is started.
    * nothing saved, or older than max_stale: fetched now. Callers that arrive
      meanwhile wait for that one fetch instead of each making their own.

    A failed background re-fetch keeps the last answer; a failed first fetch
    raises, as a direct call would - and for the next 30 seconds callers get
    that same error at once instead of each waiting on a service that is down.
    Inside a @cached calculation the data's version is recorded, so that
    result is rebuilt when the data changes."""
    if max_stale is None:
        max_stale = 6 * ttl
    with _sources_lock:
        src = _sources.get(key)
        if src is None:
            src = _sources[key] = _Source()
            while len(_sources) > _MAX_SOURCES:
                _sources.popitem(last=False)
        else:
            _sources.move_to_end(key)
        src.fetch, src.ttl = fetch, float(ttl)
    if src.has:
        age = _clock() - src.at
        if age < max_stale:
            if age >= ttl:
                _refresh_behind(key, src)
            return _saw(key, src)
    with src.lock:
        if not (src.has and _clock() - src.at < ttl):     # nobody fetched it while we waited
            if src.error is not None and _clock() < src.retry_after:
                raise src.error                           # it just failed: don't make this caller wait too
            try:
                _store(src, fetch())
            except Exception as e:  # noqa: BLE001
                src.error, src.retry_after = e, _clock() + 30
                raise
            src.error = None
    return _saw(key, src)


def _saw(key, src: _Source):
    for rec in _recorders.get():
        rec.sources.setdefault(key, src.version)
    return src.value


# ---- rows that don't count -------------------------------------------------
def ignore_rows(table: str, condition: str) -> None:
    """Writes to `table` only count as a change when `condition` holds, e.g.
    "{row}.key NOT IN ('last_import_at')" ({row} becomes NEW or OLD). For
    bookkeeping rows that are rewritten constantly and that no saved number
    reads. If a saved calculation ever reads such a row, it will not notice it
    changing - so only list rows that are pure bookkeeping."""
    _row_filters[table] = condition


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
            conn.execute("INSERT OR IGNORE INTO table_versions (name, v) VALUES (?, 0)", (t,))
            _ensure_triggers(conn, t)
            done.append(t)
        conn.commit()
        return done
    finally:
        conn.close()


def _trigger_bodies(t: str) -> list[tuple[str, str]]:
    """(trigger name, everything after the name) for the three triggers on t."""
    q = '"' + t.replace('"', '""') + '"'
    lit = "'" + t.replace("'", "''") + "'"
    cond = _row_filters.get(t)
    out = []
    for tag, event, row in (("i", "INSERT", "NEW"), ("u", "UPDATE", "NEW"), ("d", "DELETE", "OLD")):
        when = f" WHEN {cond.format(row=row)}" if cond else ""
        out.append((f"tv_{tag}_{t}",
                    f"AFTER {event} ON {q}{when} BEGIN UPDATE table_versions SET v = v + 1 WHERE name = {lit}; END"))
    return out


def _ensure_triggers(conn: sqlite3.Connection, t: str) -> int:
    """Create t's triggers, or replace ones whose definition is out of date.
    Returns how many were created or replaced."""
    have = {r[0]: r[1] or "" for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (t,))}
    squash = lambda sql: " ".join(sql.split())            # noqa: E731
    changed = 0
    for name, body in _trigger_bodies(t):
        quoted = '"' + name.replace('"', '""') + '"'
        old = have.get(name)
        if old is not None:
            if squash(old).endswith(squash(body)):
                continue
            conn.execute(f"DROP TRIGGER {quoted}")
        conn.execute(f"CREATE TRIGGER {quoted} {body}")
        changed += 1
    return changed


def sync_triggers() -> int:
    """Bring the triggers of every tracked table up to date with ignore_rows
    (run at start-up). Returns how many were replaced."""
    if not enabled():
        return 0
    try:
        conn = sqlite3.connect(_db_path, timeout=30)
    except sqlite3.Error:
        return 0
    try:
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM table_versions")]
        except sqlite3.Error:
            return 0                        # tracking not set up yet
        real = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        n = sum(_ensure_triggers(conn, t) for t in tables if t in real)
        conn.commit()
        if n:
            log.info("cache: %d change-counter trigger(s) brought up to date", n)
        return n
    finally:
        conn.close()


# ---- the cache itself ----------------------------------------------------
def _pacific_day() -> str:
    from .periods import PACIFIC
    return datetime.now(timezone.utc).astimezone(PACIFIC).date().isoformat()


def _stale_reason(e: _Entry, versions: dict[str, int] | None = None) -> str | None:
    now = _clock()
    if e.expires is not None and now >= e.expires:
        return "time limit"
    if e.tables:
        cur = _versions() if versions is None else versions
        changed = sorted(t for t, v in e.tables.items() if cur.get(t) != v)
        if changed:
            return "changed: " + ", ".join(changed)
    for p, (mtime, lag, since) in e.files.items():
        if now - since >= lag and _mtime(p) != mtime:
            return "file changed: " + os.path.basename(p)
    for key, (was, lag, since, probe, every) in e.values.items():
        if now - since >= lag and _probe(key, probe, every) != was:
            return f"changed: {key}"
    for key, version in e.sources.items():
        src = _sources.get(key)
        if src is None or src.version != version:
            return f"changed: {key[0] if isinstance(key, tuple) and key else key}"
        if now - src.at >= src.ttl:
            _refresh_behind(key, src)       # keep outside data moving while this result is in use
    return None


def _share(e: _Entry) -> None:
    """A calculation that used this saved result depends on what it depended on."""
    for rec in _recorders.get():
        rec.tables.update(e.tables)
        for p, item in e.files.items():
            rec.files.setdefault(p, item)
        for k, item in e.values.items():
            rec.values.setdefault(k, item)
        for k, version in e.sources.items():
            rec.sources.setdefault(k, version)
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


def _get(key, label, compute, copy_result, ttl, keep=None):
    e, why = _lookup(key)
    if e is None:
        with _entries_lock:
            lock = _key_locks.setdefault(key, threading.Lock())
        with lock:                      # one calculation per key at a time; others wait for it
            e, why2 = _lookup(key)
            if e is None:
                e, value = _build(key, label, compute, ttl, why2 or why, keep)
                if e is None:           # computed but not saved (first run on a new table):
                    return value        # a private object, no copy needed
            else:
                _stats["hits"] += 1
    else:
        _stats["hits"] += 1
    _share(e)
    return copy.deepcopy(e.value) if copy_result else e.value


def _build(key, label, compute, ttl, why, keep=None) -> tuple[_Entry | None, object]:
    before = _versions()                # BEFORE reading, so a write mid-way makes the result stale
    rec = _Recorder()
    token = _recorders.set(_recorders.get() + (rec,))
    t0 = time.perf_counter()
    try:
        value = compute()
    finally:
        _recorders.reset(token)
    ms = (time.perf_counter() - t0) * 1000
    if keep is not None and not keep(value):
        _stats["uncached"] += 1          # e.g. an error answer from an outside service
        return None, value
    tables = {t for t in rec.tables if not t.startswith(_IGNORE_TABLES) and t not in _not_tables}
    # everything this calculation read is also read by whatever called it
    for outer in _recorders.get():
        outer.tables.update(tables)
        for p, item in rec.files.items():
            outer.files.setdefault(p, item)
        for k, item in rec.values.items():
            outer.values.setdefault(k, item)
        for k, version in rec.sources.items():
            outer.sources.setdefault(k, version)

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
        mine = _clock() + ttl
        expires = mine if expires is None else min(expires, mine)
    e = _Entry(value, {t: before[t] for t in tables if t in before}, dict(rec.files), expires, ms,
               dict(rec.values), dict(rec.sources))
    with _entries_lock:
        _entries[key] = e
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            old, _ = _entries.popitem(last=False)
            _key_locks.pop(old, None)
    _stats["misses"] += 1
    log.info("cache: built %s in %.0f ms (%s)", label, ms, why)
    return e, value


def cached(*, copy_result: bool = True, ttl=None, keep=None):
    """Save a function's result until what it read changes (see module docs).

    copy_result=False hands every caller the SAME object - only for results no
    caller modifies (big payloads where a copy per request would cost too much).
    ttl: seconds, or a function of the call's arguments returning seconds/None,
    for results that also drift with the clock (a rolling "last 30 days").
    keep: a function of the result; return False to hand it back WITHOUT saving
    it (so an error from an outside service is retried, not remembered)."""
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
            return _get(key, shown, lambda: fn(*args, **kwargs), copy_result, t, keep)

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
        items = [(k[0], e.ms, sorted(e.tables),
                  [os.path.basename(p) for p in e.files] + [str(k2) for k2 in e.values]
                  + [str(k2[0] if isinstance(k2, tuple) and k2 else k2) for k2 in e.sources])
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
