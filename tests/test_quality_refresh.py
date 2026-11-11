"""Quality scores must never make a visitor wait when a saved score exists.

Before 2026-11-11, opening a page for a live pay period re-pulled every agent's
score from VoiceOps on the spot (~27 calls, 30+ seconds). Now saved scores are
shown at once and a live period is refreshed on a side thread.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from isadash import cache, voiceops

LIVE = ("2026-11-08", "2026-11-21")        # the pay period containing the frozen "today"
OLD = ("2026-09-13", "2026-09-26")         # long settled


class FakeVoiceOps:
    """Stands in for the vendor: records calls, optionally blocks until released."""
    calls: list = []
    gate = threading.Event()

    def __init__(self, *_a, **_k):
        pass

    def rep_summary(self, email, start_day, end_day):
        FakeVoiceOps.gate.wait(10)
        FakeVoiceOps.calls.append((email, start_day, end_day))
        return {"call_count": 10, "call_score": 80.0, "quality_score": 91.0, "compliance_score": 95.0}

    def close(self):
        pass


@pytest.fixture()
def quality(app, monkeypatch, override_settings):
    db = app._settings.db_path
    c = sqlite3.connect(db)
    c.execute("UPDATE agent_map SET voiceops_email = lower(replace(genesys_name, ' ', '.')) || '@example.test' "
              "WHERE scorecard = 1")
    c.execute("DELETE FROM voiceops_scores")
    c.commit()
    names = [r[0] for r in c.execute("SELECT genesys_name FROM agent_map WHERE scorecard = 1")]
    c.close()
    override_settings(voiceops_client_id="id", voiceops_client_secret="secret")
    monkeypatch.setattr(voiceops, "VoiceOpsClient", FakeVoiceOps)
    FakeVoiceOps.calls = []
    FakeVoiceOps.gate = threading.Event()
    FakeVoiceOps.gate.set()
    cache.clear()

    def save(period, fetched_at, score=70.0):
        c = sqlite3.connect(db)
        c.executemany(
            "INSERT OR REPLACE INTO voiceops_scores (start_day, end_day, agent, call_count, call_score,"
            " quality_score, compliance_score, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            [(period[0], period[1], n, 5, 60.0, score, 90.0, fetched_at) for n in names])
        c.commit()
        c.close()

    yield app, save, names
    c = sqlite3.connect(db)
    c.execute("UPDATE agent_map SET voiceops_email = NULL")
    c.execute("DELETE FROM voiceops_scores")
    c.commit()
    c.close()
    cache.clear()


def wait_for(cond, seconds=5):
    """Poll in real time. The clock functions are frozen in tests, so count
    short real waits instead of comparing timestamps."""
    for _ in range(int(seconds / 0.02)):
        if cond():
            return True
        threading.Event().wait(0.02)
    return bool(cond())


def test_stale_live_period_answers_at_once_and_refreshes_behind(quality):
    app, save, names = quality
    save(LIVE, "2026-11-10T15:00:00+00:00")           # five hours old at the frozen "now"
    FakeVoiceOps.gate.clear()                          # the vendor is slow: hold every call
    got = app.api_quality(start_day=LIVE[0], end_day=LIVE[1])
    assert set(got["agents"]) == set(names)
    assert all(a["quality"] == 70.0 for a in got["agents"].values())      # the saved scores, immediately
    assert FakeVoiceOps.calls == []                                       # nobody waited on the vendor
    FakeVoiceOps.gate.set()
    assert wait_for(lambda: len(FakeVoiceOps.calls) == len(names))        # ...refreshed on the side
    assert wait_for(lambda: not app._quality_refreshing)
    again = app.api_quality(start_day=LIVE[0], end_day=LIVE[1])
    assert all(a["quality"] == 91.0 for a in again["agents"].values())


def test_only_one_background_refresh_per_period(quality):
    app, save, names = quality
    save(LIVE, "2026-11-10T15:00:00+00:00")
    FakeVoiceOps.gate.clear()
    for _ in range(5):
        app.api_quality(start_day=LIVE[0], end_day=LIVE[1])
    FakeVoiceOps.gate.set()
    assert wait_for(lambda: not app._quality_refreshing)
    assert len(FakeVoiceOps.calls) == len(names)


def test_fresh_or_settled_scores_trigger_nothing(quality):
    app, save, _names = quality
    save(LIVE, "2026-11-10T19:30:00+00:00")           # 30 minutes old: still fresh
    save(OLD, "2026-09-30T00:00:00+00:00")            # old period: settled for good
    app.api_quality(start_day=LIVE[0], end_day=LIVE[1])
    app.api_quality(start_day=OLD[0], end_day=OLD[1])
    assert not wait_for(lambda: FakeVoiceOps.calls, seconds=0.3)


def test_never_pulled_period_still_fetches_before_answering(quality):
    app, _save, names = quality
    got = app.api_quality(start_day=LIVE[0], end_day=LIVE[1])
    assert len(FakeVoiceOps.calls) == len(names)
    assert all(a["quality"] == 91.0 for a in got["agents"].values())


def test_fetch_off_never_calls_the_vendor(quality):
    app, save, _names = quality
    save(LIVE, "2026-11-10T15:00:00+00:00")
    app.api_quality(start_day=LIVE[0], end_day=LIVE[1], fetch=0)
    assert not wait_for(lambda: FakeVoiceOps.calls, seconds=0.3)
