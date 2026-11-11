"""The saved-results layer (src/isadash/cache.py) must never show a stale or
different number: a saved result has to equal a fresh calculation, survive
unrelated writes, and be thrown away by any write to data it read."""
from __future__ import annotations

import json
import sqlite3

import pytest

from isadash import cache

YTD = {"start_day": "2026-01-01", "end_day": "2026-11-10", "mode": "sold"}


def feeds(w):
    return [
        ("form events", w._get_form_events, {}),
        ("phone payload", w._build_payload, {}),
        ("home cards", w.api_home, {}),
        ("home trends", w.api_home_trends, {}),
        ("scorecard", w.api_scorecard, YTD),
        ("revenue", w.api_revenue, {"year": 2026}),
    ]


def plain(x):
    if isinstance(x, tuple):          # (events, payload): compare the payload
        x = x[1]
    return json.loads(json.dumps(x, default=str))


def write(w, sql, args=()):
    c = sqlite3.connect(w._settings.db_path, timeout=30)
    c.execute(sql, args)
    c.commit()
    c.close()


@pytest.fixture()
def warm(app):
    """Every feed built and saved (the first call on a new table only starts tracking)."""
    for _n, fn, kw in feeds(app):
        fn(**kw)
        fn(**kw)
    return app


def test_saved_equals_fresh(warm):
    for name, fn, kw in feeds(warm):
        assert cache.is_fresh(fn, **kw), name
        assert plain(fn(**kw)) == plain(fn.uncached(**kw)), name


def test_sign_ins_and_clock_punches_discard_nothing(warm):
    write(warm, "INSERT INTO auth_log (at, event, email, ip, detail) VALUES ('x', 'test', 't', 't', 't')")
    write(warm, "CREATE TABLE IF NOT EXISTS punches (id INTEGER PRIMARY KEY, agent TEXT, in_at TEXT)")
    write(warm, "INSERT INTO punches (agent, in_at) VALUES ('Avery Stone', '2026-11-10T16:00:00')")
    write(warm, "DELETE FROM punches WHERE agent = 'Avery Stone' AND in_at = '2026-11-10T16:00:00'")
    for name, fn, kw in feeds(warm):
        assert cache.is_fresh(fn, **kw), name


def test_a_data_change_discards_only_what_read_it(warm):
    reads_audit = {s["what"].split("(")[0] for s in cache.stats()["saved"] if "audit_daily" in s["tables"]}
    assert "api_home" in reads_audit and "api_revenue" not in reads_audit
    write(warm, "UPDATE audit_daily SET sales = sales WHERE rowid = (SELECT MIN(rowid) FROM audit_daily)")
    for name, fn, kw in feeds(warm):
        assert cache.is_fresh(fn, **kw) == (fn.__name__ not in reads_audit), name


def test_new_numbers_show_up_and_revert(warm):
    before = plain(warm.api_home())
    rowid, sales = sqlite3.connect(warm._settings.db_path).execute(
        "SELECT rowid, sales FROM audit_daily ORDER BY date DESC LIMIT 1").fetchone()
    write(warm, "UPDATE audit_daily SET sales = sales + 500 WHERE rowid = ?", (rowid,))
    try:
        after = plain(warm.api_home())
        assert after != before
        assert after == plain(warm.api_home.uncached())
    finally:
        write(warm, "UPDATE audit_daily SET sales = ? WHERE rowid = ?", (sales, rowid))
    assert plain(warm.api_home()) == before


def test_callers_cannot_damage_the_saved_copy(warm):
    good = plain(warm.api_home())
    warm.api_home().clear()
    warm.api_scorecard(**YTD)["rows"].clear()
    assert plain(warm.api_home()) == good
    assert len(warm.api_scorecard(**YTD)["rows"]) == 8


def test_a_write_during_a_calculation_marks_it_stale(warm, monkeypatch):
    real = warm._roster_sub_rows

    def slow(conn):
        out = real(conn)
        write(warm, "UPDATE subscriptions SET active = active WHERE rowid = (SELECT MIN(rowid) FROM subscriptions)")
        return out

    write(warm, "UPDATE agent_map SET team = team WHERE rowid = (SELECT MIN(rowid) FROM agent_map)")
    monkeypatch.setattr(warm, "_roster_sub_rows", slow)
    warm.api_home_trends()
    monkeypatch.setattr(warm, "_roster_sub_rows", real)
    assert not cache.is_fresh(warm.api_home_trends)
    warm.api_home_trends()
    assert cache.is_fresh(warm.api_home_trends)


def test_rolling_window_expires_but_fixed_dates_do_not(warm):
    warm.api_scorecard(days=30)
    warm.api_scorecard(days=30)
    with cache._entries_lock:
        limits = {dict(k[1]).get("start_day"): e.expires for k, e in cache._entries.items()
                  if k[0] == "api_scorecard"}
    assert limits[""] is not None                 # "last 30 days up to this minute"
    assert limits["2026-01-01"] is None           # explicit dates: until the data changes


def test_form_lead_file_change_is_noticed_after_the_lag(warm):
    warm._get_form_events()
    with cache._entries_lock:
        entry = next(e for k, e in cache._entries.items() if k[0] == "_get_form_events")
    path = str(warm._settings.formleads_db)
    mtime, lag, since = entry.files[path]
    assert lag == warm._FORM_LAG_S == 900
    try:
        entry.files[path] = (mtime - 1, lag, since)             # the file just changed
        assert cache.is_fresh(warm._get_form_events)
        entry.files[path] = (mtime - 1, lag, since - lag - 1)   # ...and the copy is past the lag
        assert not cache.is_fresh(warm._get_form_events)
    finally:
        entry.files[path] = (mtime, lag, since)


def test_only_analytics_tables_are_tracked(warm):
    tracked = {r[0] for r in sqlite3.connect(warm._settings.db_path).execute("SELECT name FROM table_versions")}
    assert tracked == {"agent_map", "app_config", "audit_daily", "calls", "customers", "employees", "subscriptions"}


def test_switch_off(warm, monkeypatch):
    monkeypatch.setenv("ISDASH_CACHE", "0")
    hits = cache.stats()["hits"]
    warm.api_home()
    assert cache.stats()["hits"] == hits


def test_error_answers_are_not_saved(app):
    """Service Level asks Genesys; with no network it returns an error answer,
    which must be retried next time rather than remembered."""
    first = app.api_home_servicelevel()
    assert not isinstance(first, dict)                  # an error response, not numbers
    assert not cache.is_fresh(app.api_home_servicelevel)


def test_team_overview_survives_genesys_being_down(app):
    out = app.api_team_overview()                       # network is blocked in tests
    assert set(out["teams"]) == {"total", "josh", "kai"}
    assert out["ranges"]["d30"] == {"start": "2026-10-11", "end": "2026-11-09"}
    assert len(out["weeks"]) == 13
