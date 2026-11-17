"""Saved results beyond Home, and outside data that refreshes in the background.

Three things are checked here:
* every feed that is now saved gives exactly what a fresh calculation gives;
* nobody waits on a Google sheet or Genesys once an answer has been fetched
  (cache.keep_fresh), and a saved result follows that data when it changes;
* saved numbers are not thrown away for things that can't change them
  (bookkeeping rows, today's form leads for windows that ended yesterday).

The clock functions are frozen in tests, so "time passing" is simulated by
moving the recorded timestamps back.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from isadash import cache, marketing

YTD = ("2026-01-01", "2026-11-10")
MONTH = ("2026-10-01", "2026-10-31")


def plain(x):
    return json.loads(json.dumps(x, default=str))


def write(db, sql, args=()):
    c = sqlite3.connect(db, timeout=30)
    c.execute(sql, args)
    c.commit()
    c.close()


def saved(w):
    """(name, feed, arguments) for every feed saved in this round."""
    return [
        ("spend windows", w.api_marketing_spend_windows, {}),
        ("spend chart", w.api_marketing_spend_chart, {"start_day": YTD[0], "end_day": YTD[1]}),
        ("spend table", w.api_marketing_spend, {"start_day": MONTH[0], "end_day": MONTH[1]}),
        ("volume by source", w.api_marketing_bysource, {"start_day": MONTH[0], "end_day": MONTH[1]}),
        ("revenue overview", w.api_revenue_overview, {"by": "agent"}),
        ("source revenue", w.api_source_revenue, {"year": 2026, "level": "channel"}),
        ("source revenue overview", w.api_source_revenue_overview, {"level": "channel"}),
        ("compare agents", w.api_agentview, {"start_day": MONTH[0], "end_day": MONTH[1]}),
        ("inbound vs other", w.api_inbound_split, {"start_day": MONTH[0], "end_day": MONTH[1]}),
        ("date-range table", w.api_range, {"start": MONTH[0], "end": MONTH[1]}),
        ("leaked leads", w.api_leaked, {"start_day": MONTH[0], "end_day": MONTH[1]}),
        ("form conversion", w.api_formleads, {}),
        ("home daily", w.api_home_daily, {}),
    ]


@pytest.fixture()
def ready(app):
    """Everything built and saved (the first call on a new table only starts tracking)."""
    for _n, fn, kw in saved(app):
        fn(**kw)
        fn(**kw)
    return app


def test_every_saved_feed_equals_a_fresh_calculation(ready):
    for name, fn, kw in saved(ready):
        assert cache.is_fresh(fn, **kw), f"{name} was not saved"
        got = fn(**kw)
        assert isinstance(got, dict) and "error" not in got, name
        assert plain(got) == plain(fn.uncached(**kw)), name


def test_team_metrics_saves_the_database_part_and_adds_genesys_on_top(ready):
    w = ready
    a = w.api_teammetrics(start_day=MONTH[0], end_day=MONTH[1], mode="sold", phone=False)
    b = w.api_teammetrics(start_day=MONTH[0], end_day=MONTH[1], mode="sold", phone=True)
    assert cache.is_fresh(w._teammetrics_db, start_day=MONTH[0], end_day=MONTH[1], mode="sold")
    assert "phone_metrics" not in a["total"]
    assert "error" in b["total"]["phone_metrics"]            # Genesys can't be reached in tests: shown, not saved
    b["total"].pop("phone_metrics")
    assert plain(a) == plain(b)                               # ...and everything else is identical
    again = w.api_teammetrics(start_day=MONTH[0], end_day=MONTH[1], mode="sold", phone=False)
    assert plain(again) == plain(a)                           # the Genesys add-on never leaks into the saved copy


# ---- outside data: answer at once, refresh behind -----------------------------
def source(key):
    return cache._sources[key]


@pytest.fixture()
def clock(monkeypatch):
    """A clock the test can move forward. (The frozen test clock only applies
    on the main thread; background refreshes would otherwise see another time.)"""
    t = {"now": cache._clock()}
    monkeypatch.setattr(cache, "_clock", lambda: t["now"])

    class Clock:
        def advance(self, seconds: float) -> None:
            t["now"] += seconds
    return Clock()


def wait_for(cond, seconds=5):
    for _ in range(int(seconds / 0.02)):
        if cond():
            return True
        threading.Event().wait(0.02)
    return bool(cond())


def test_outside_data_is_fetched_once_then_served_from_memory(clock):
    calls = []
    key = ("test", "fresh")
    fetch = lambda: calls.append(1) or {"n": len(calls)}        # noqa: E731
    assert cache.keep_fresh(key, fetch, 600) == {"n": 1}
    assert cache.keep_fresh(key, fetch, 600) == {"n": 1}
    assert len(calls) == 1


def test_old_answer_comes_back_at_once_and_is_refreshed_behind(clock):
    gate, calls = threading.Event(), []
    key = ("test", "stale")

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            gate.wait(10)                                       # the outside service is slow
        return {"n": len(calls)}

    cache.keep_fresh(key, fetch, 600)
    clock.advance(601)                                          # past its ten minutes
    assert cache.keep_fresh(key, fetch, 600) == {"n": 1}        # nobody waits: the old answer, immediately
    assert cache.keep_fresh(key, fetch, 600) == {"n": 1}
    assert wait_for(lambda: len(calls) == 2)                    # one refresh started behind the scenes...
    assert len(calls) == 2                                      # ...not one per caller
    gate.set()
    assert wait_for(lambda: source(key).value == {"n": 2})
    assert cache.keep_fresh(key, fetch, 600) == {"n": 2}


def test_many_callers_share_one_first_fetch(clock):
    """The clock check's problem: five checks arriving together each read the
    schedule sheet themselves. Now the first one fetches and the rest wait for it."""
    gate, calls, out = threading.Event(), [], []
    key = ("test", "herd")

    def fetch():
        calls.append(1)
        gate.wait(10)
        return "schedule"

    threads = [threading.Thread(target=lambda: out.append(cache.keep_fresh(key, fetch, 600))) for _ in range(5)]
    for t in threads:
        t.start()
    assert wait_for(lambda: len(calls) == 1)
    threading.Event().wait(0.2)
    assert len(calls) == 1
    gate.set()
    for t in threads:
        t.join(5)
    assert out == ["schedule"] * 5 and len(calls) == 1


def test_a_failed_refresh_keeps_the_last_answer(clock):
    key = ("test", "fail")
    state = {"fail": False, "calls": 0}

    def fetch():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("sheet unreachable")
        return "good"

    cache.keep_fresh(key, fetch, 600)
    state["fail"] = True
    clock.advance(601)
    assert cache.keep_fresh(key, fetch, 600) == "good"
    assert wait_for(lambda: state["calls"] == 2 and not source(key).busy)
    assert cache.keep_fresh(key, fetch, 600) == "good"
    threading.Event().wait(0.1)
    assert state["calls"] == 2                                  # no hammering: it waits a minute before retrying
    clock.advance(61)
    state["fail"] = False
    assert cache.keep_fresh(key, fetch, 600) == "good"
    assert wait_for(lambda: state["calls"] == 3)                # ...and then tries again


def test_a_failed_first_fetch_is_reported_and_not_retried_for_30_seconds(clock):
    calls = []
    key = ("test", "first-fail")

    def fetch():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("Genesys is down")
        return "back"

    for _ in range(4):                                          # four visitors while it is down
        with pytest.raises(RuntimeError):
            cache.keep_fresh(key, fetch, 300)
    assert len(calls) == 1                                      # only the first one waited on Genesys
    clock.advance(31)
    with pytest.raises(RuntimeError):
        cache.keep_fresh(key, fetch, 300)
    assert len(calls) == 2
    clock.advance(31)
    assert cache.keep_fresh(key, fetch, 300) == "back"          # and it recovers by itself


def test_very_old_data_is_fetched_before_answering(clock):
    calls = []
    key = ("test", "ancient")
    fetch = lambda: calls.append(1) or len(calls)               # noqa: E731
    cache.keep_fresh(key, fetch, 600)
    clock.advance(6 * 600 + 1)                                  # an hour old: too old to hand out
    assert cache.keep_fresh(key, fetch, 600) == 2


def test_spend_and_roas_follows_the_spend_sheet(ready, monkeypatch, clock):
    w = ready
    key = ("marketing", "spend_sheet", w._settings.spend_sheet_id)
    before = plain(w.api_marketing_spend_windows())
    real = marketing._spend_sheet

    # the sheet is re-read with nothing changed: the saved result stays
    clock.advance(601)
    assert cache.is_fresh(w.api_marketing_spend_windows)        # ...and this check starts the re-read
    assert wait_for(lambda: not source(key).busy and source(key).at > 0)
    assert cache.is_fresh(w.api_marketing_spend_windows)

    # someone edits the sheet: after the next re-read the result is rebuilt with the new spend
    def edited(settings):
        sheet = real(settings)
        for wk in sheet["weeks"].values():
            for cell in wk.values():
                cell["spend"] += 1000.0
        return sheet

    monkeypatch.setattr(marketing, "_spend_sheet", edited)
    version = source(key).version
    clock.advance(601)
    cache.is_fresh(w.api_marketing_spend_windows)
    assert wait_for(lambda: source(key).version > version)
    assert not cache.is_fresh(w.api_marketing_spend_windows)
    after = plain(w.api_marketing_spend_windows())
    assert after != before
    assert after == plain(w.api_marketing_spend_windows.uncached())

    monkeypatch.setattr(marketing, "_spend_sheet", real)        # put the sheet back for the other tests
    version = source(key).version
    clock.advance(601)
    cache.is_fresh(w.api_marketing_spend_windows)
    assert wait_for(lambda: source(key).version > version)
    assert plain(w.api_marketing_spend_windows()) == before


# ---- not rebuilding for things that can't change the numbers -------------------
def _entry(name):
    with cache._entries_lock:
        return next(e for k, e in cache._entries.items() if k[0] == name)


def _age_fingerprint(entry, seconds):
    was, lag, since, probe, every = entry.values["form touches"]
    entry.values["form touches"] = (was, lag, since - seconds, probe, every)


def test_todays_form_leads_do_not_rebuild_numbers_that_ended_yesterday(ready):
    w = ready
    for fn in (w._get_form_events, w.api_home, w.api_home_trends, w._team_overview_db, w.api_marketing_spend_windows):
        fn()
        fn()
    fl = w._settings.formleads_db
    write(fl, "INSERT INTO touches VALUES ('t-new', 'gmail', 'Source 21', '2026-11-10T19:59:00+00:00', "
              "'+13335550000', NULL, 'ref-new', '2026-11-10T19:59:00+00:00')")
    cache._probes.clear()                                       # the fingerprint is re-taken
    try:
        events, home, spend = _entry("_get_form_events"), _entry("api_home"), _entry("api_marketing_spend_windows")
        assert events.values["form touches"][1] == w._FORM_LAG_S == 900
        assert home.values["form touches"][1] == w._FORM_SETTLED_LAG_S == 7200
        saved_since = {id(e): e.values["form touches"] for e in (events, home, spend)}

        assert cache.is_fresh(w._get_form_events)               # within 15 minutes: nothing moves
        for e in (events, home, spend):
            _age_fingerprint(e, 901)                            # 15 minutes later
        assert not cache.is_fresh(w._get_form_events)           # the lead list is rebuilt...
        assert cache.is_fresh(w.api_home)                       # ...Home's cards are not
        assert cache.is_fresh(w.api_home_trends)
        assert cache.is_fresh(w._team_overview_db)
        assert cache.is_fresh(w.api_marketing_spend_windows)
        _age_fingerprint(home, 7200)                            # two hours later they follow
        assert not cache.is_fresh(w.api_home)
        for e in (events, home, spend):
            e.values["form touches"] = saved_since[id(e)]
    finally:
        write(fl, "DELETE FROM touches WHERE dedup_key = 't-new'")
        cache._probes.clear()
        cache.clear()


def test_form_file_changes_alone_do_not_rebuild_anything(ready):
    """The sourcing app rewrites its database file every few minutes without
    adding a form lead. That used to rebuild Home every 15 minutes, day and night."""
    w = ready
    w._get_form_events()
    w._get_form_events()
    e = _entry("_get_form_events")
    path = str(w._settings.formleads_db)
    mtime, lag, since = e.files[path]
    assert lag == w._FORM_FILE_BACKSTOP_S == 6 * 3600
    try:
        e.files[path] = (mtime - 1, lag, since - 3600)          # the file changed an hour ago; the touches didn't
        assert cache.is_fresh(w._get_form_events)
        e.files[path] = (mtime - 1, lag, since - lag - 1)       # the backstop: noticed after six hours
        assert not cache.is_fresh(w._get_form_events)
    finally:
        e.files[path] = (mtime, lag, since)


def test_bookkeeping_rows_do_not_throw_saved_numbers_away(ready):
    w = ready
    db = w._settings.db_path
    kw = {"start_day": "2026-10-11", "end_day": "2026-10-24", "mode": "sold"}
    w.api_scorecard(**kw)
    w.api_scorecard(**kw)
    assert cache.is_fresh(w.api_scorecard, **kw)
    cache.sync_triggers()
    for key in ("hours_sheet_last_import", "renewal_pricing_last_good", "sync_delta_watermark",
                "agent_tab_rollout:me.renewal"):
        write(db, "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)", (key, '"x"'))
        write(db, "UPDATE app_config SET value = ? WHERE key = ?", ('"y"', key))
        write(db, "DELETE FROM app_config WHERE key = ?", (key,))
    assert cache.is_fresh(w.api_scorecard, **kw)                # the IS #'s sheet reads none of those
    write(db, "INSERT OR REPLACE INTO app_config (key, value) VALUES ('saturday_regulars', '[]')")
    assert not cache.is_fresh(w.api_scorecard, **kw)            # any other row still counts
    write(db, "DELETE FROM app_config WHERE key = 'saturday_regulars'")


def test_change_counters_are_brought_up_to_date(ready):
    """Production's app_config triggers were created before ignore_rows existed."""
    db = ready._settings.db_path
    c = sqlite3.connect(db)
    for name in ("tv_i_app_config", "tv_u_app_config", "tv_d_app_config"):
        c.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    c.execute('CREATE TRIGGER IF NOT EXISTS "tv_u_app_config" AFTER UPDATE ON "app_config" '
              "BEGIN UPDATE table_versions SET v = v + 1 WHERE name = 'app_config'; END")
    c.commit()
    c.close()
    assert cache.sync_triggers() == 3                           # one replaced, two created
    assert cache.sync_triggers() == 0                           # and then it leaves them alone
    c = sqlite3.connect(db)
    sqls = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'app_config'")]
    c.close()
    assert len(sqls) == 3 and all("hours_sheet_last_import" in s for s in sqls)
