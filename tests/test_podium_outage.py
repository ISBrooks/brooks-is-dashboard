"""A dead Podium sign-in must be visible.

On 2026-11-19 the client secret of the Podium app that the dashboard, the
PestRoutes -> Podium sync and the Missed Call Texter all share was regenerated.
All three lost Podium, and Settings -> Software Management kept saying the sync
was OK for 28 hours: the sync records a failed lookup as its normal "not in
Podium" skip, the status agent counted "the last 24 hours" from the start of
yesterday, and the dashboard reported its own failure as "Podium kept failing".
These tests replay that day's facts and check each card now says so.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from isadash import podium, software

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(podium_client_id="id", podium_client_secret="old", podium_refresh_token="rt")
REFUSAL = '{"error":"invalid_client","error_description":"invalid client_id or client_secret"}'


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def z(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- sync card ---
def sync_facts(**state) -> dict:
    base = {"hwm": iso(3), "audit_last": iso(3), "audit_24h": {"noop": 7926, "update": 1343},
            "errors_24h": 0, "dead_letter": 0, "last_alert": None}
    return {"present": True, "service": {"state": "active", "sub": "running", "since": z(9000), "restarts": 0},
            "state": {**base, **state}, "last_tick": {"ts": z(3), "line": "Tick complete."}}


def test_sync_card_warns_when_nothing_reaches_podium():
    """The outage itself: service up, ticking, 0 errors - and every customer skipped."""
    out = software._v_podium(sync_facts(
        skipped_24h=7031, last_reached=iso(27 * 60 + 33), recent_2h={"reached": 0, "skipped": 878}), NOW)
    assert out["status"] == "warn"
    assert out["headline"] == "Running, but its updates are not reaching Podium"
    assert "878 customers were skipped" in out["notes"][0]
    pod = next(c for c in out["connections"] if c["name"] == "Podium")
    assert pod["status"] == "warn" and pod["age_h"] > 27


def test_sync_card_names_the_refused_sign_in():
    a = sync_facts(skipped_24h=7031, last_reached=iso(300), recent_2h={"reached": 0, "skipped": 878})
    a["auth_fail"] = {"ts": z(1), "line": "Client error '401 Unauthorized' for url '.../oauth/token'"}
    out = software._v_podium(a, NOW)
    assert out["status"] == "warn" and out["notes"][0] == software.PODIUM_AUTH_HELP


def test_sync_card_stays_ok_on_a_normal_day():
    """~50 found for every skip, and an old sign-in refusal no longer counts."""
    a = sync_facts(skipped_24h=142, last_reached=iso(3), recent_2h={"reached": 610, "skipped": 12})
    a["auth_fail"] = {"ts": z(90), "line": "401 Unauthorized ... oauth/token"}
    out = software._v_podium(a, NOW)
    assert out["status"] == "ok" and out["notes"] == []
    pod = next(c for c in out["connections"] if c["name"] == "Podium")
    assert pod["status"] == "ok" and "142 not in Podium" in pod["note"] and "7784 unchanged" in pod["note"]


def test_sync_card_tolerates_a_quiet_evening_and_an_old_agent():
    quiet = software._v_podium(sync_facts(
        skipped_24h=140, last_reached=iso(50), recent_2h={"reached": 4, "skipped": 9}), NOW)
    assert quiet["status"] == "ok"                       # too few skips to judge a ratio on
    old = software._v_podium(sync_facts(), NOW)          # agent v3: none of the new facts
    assert old["status"] == "ok" and len(old["connections"]) == 2


# -------------------------------------------------------------- texter card ---
def texter_facts(**data) -> dict:
    base = {"by_action_24h": {"sent": 5, "skip-customer": 14}, "by_action_7d": {"sent": 73},
            "last_sent": z(10), "last_fr_check": z(10), "stuck_errors": 0, "retrying_errors": 0,
            "send_problems_7d": 0, "status_failed_7d": 0, "recent": []}
    return {"present": True, "service": {"state": "active", "since": z(9000), "restarts": 0},
            "running_mode": "live", "config": {"dry_run": False, "poll_interval_seconds": 60},
            "data": {**base, **data}, "last_tick": {"ts": z(1), "line": "x | Tick complete"},
            "last_tick_failed": None}


AUTH_ERR = "PodiumError('Podium POST /oauth/token -> HTTP 401: " + REFUSAL + "')"


def test_texter_card_says_podium_refused_the_sign_in():
    out = software._v_mct(texter_facts(
        last_sent=z(21 * 60), stuck_errors=17, stuck_errors_7d=17, last_error={"at": z(31), "text": AUTH_ERR},
        recent=[{"started_at": z(36), "kind": "abandoned-in-queue", "action": "error", "reason": "",
                 "phone_last4": "5378", "attempts": 5, "error": AUTH_ERR}]), NOW)
    assert out["status"] == "warn"
    assert out["notes"][0].startswith(software.PODIUM_AUTH_HELP)
    assert any("17 missed call(s) in the last 7 days gave up" in n for n in out["notes"])
    assert next(c for c in out["connections"] if c["name"] == "Podium")["status"] == "warn"
    assert out["recent"][0]["detail"] == "Podium rejected the sign-in"


def test_texter_card_clears_once_texts_go_out_again_and_old_failures_age_out():
    fixed = software._v_mct(texter_facts(
        last_sent=z(5), stuck_errors=17, stuck_errors_7d=17, last_error={"at": z(31), "text": AUTH_ERR}), NOW)
    assert not any(software.PODIUM_AUTH_HELP in n for n in fixed["notes"])
    assert fixed["status"] == "warn"                     # the 17 untexted callers still show this week
    later = software._v_mct(texter_facts(
        stuck_errors=17, stuck_errors_7d=0, last_error={"at": z(8 * 24 * 60), "text": AUTH_ERR}), NOW)
    assert later["status"] == "ok" and not any("gave up" in n for n in later["notes"])
    old_agent = software._v_mct(texter_facts(stuck_errors=17), NOW)      # v3 has no 7-day count
    assert old_agent["status"] == "warn"


# ------------------------------------------------- the dashboard's own link ---
@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.setitem(podium._state, "token", None)
    monkeypatch.setitem(podium._state, "exp", 0.0)
    monkeypatch.setitem(podium._state, "last_req", 0.0)
    monkeypatch.setattr(podium.time, "sleep", lambda s: None)


def refuse(calls: list):
    def post(url, **kw):
        calls.append(url)
        return httpx.Response(401, text=REFUSAL, request=httpx.Request("POST", url))
    return post


def test_dashboard_reports_the_refusal_instead_of_kept_failing(monkeypatch, no_token):
    calls: list = []
    monkeypatch.setattr(podium.httpx, "post", refuse(calls))
    monkeypatch.setattr(podium.httpx, "get", lambda *a, **k: pytest.fail("no data request without a token"))
    with pytest.raises(podium.PodiumAuthError) as e:
        podium._get(SETTINGS, "/users", {"limit": 1})
    assert "rejected the dashboard's sign-in (HTTP 401: invalid_client" in str(e.value)
    assert len(calls) == 1                               # a refusal is not retried five times
    assert "old" not in str(e.value) and "rt" not in str(e.value).split()   # no secret in the message
    assert podium.auth_problem(SETTINGS).startswith("Podium rejected the dashboard's sign-in")


def test_sign_in_check_ignores_a_token_it_already_holds(monkeypatch, no_token):
    """A held token works for hours after the cut-off; the check must still notice."""
    monkeypatch.setitem(podium._state, "token", "still-valid")
    monkeypatch.setitem(podium._state, "exp", 9e12)
    monkeypatch.setattr(podium.httpx, "post", refuse([]))
    assert podium.auth_problem(SETTINGS) is not None
    ok = lambda url, **kw: httpx.Response(200, json={"access_token": "new", "expires_in": 3600},  # noqa: E731
                                          request=httpx.Request("POST", url))
    monkeypatch.setattr(podium.httpx, "post", ok)
    assert podium.auth_problem(SETTINGS) is None and podium._state["token"] == "new"


def test_a_cut_off_mid_run_does_not_store_a_half_read_day(monkeypatch, no_token):
    """Before: every conversation was counted 'skipped' and the day saved as collected."""
    monkeypatch.setattr(podium.httpx, "post", refuse([]))
    monkeypatch.setitem(podium._state, "token", "expires-mid-run")
    monkeypatch.setitem(podium._state, "exp", 9e12)

    def get(url, **kw):
        if url.endswith("/conversations"):
            return httpx.Response(200, json={"data": [{"uid": "c1"}, {"uid": "c2"}]},
                                  request=httpx.Request("GET", url))
        podium._state["exp"] = 0.0                       # the held token runs out here
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))
    monkeypatch.setattr(podium.httpx, "get", get)
    conn = sqlite3.connect(":memory:")
    with pytest.raises(podium.PodiumAuthError):
        podium.collect_conversations(conn, SETTINGS, [NOW.date()], gap=0)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'podium_conv_days'").fetchone() is None


# ------------------------------------------------------- the status agent ---
@pytest.fixture
def agent():
    spec = importlib.util.spec_from_file_location("isdash_agent", ROOT / "deploy" / "isdash-agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_agent_reports_found_versus_skipped_with_true_windows(agent, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = sqlite3.connect(tmp_path / "data" / "sync.db")
    db.executescript("CREATE TABLE sync_state (key TEXT, value TEXT);"
                     "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, fr_customer_id INT, "
                     "podium_uid TEXT, action TEXT, diff_json TEXT, error TEXT);"
                     "CREATE TABLE dead_letter (id INT); CREATE TABLE alerts_sent (id INT, category TEXT, "
                     "sent_at TEXT, subject TEXT);")
    now = datetime.now(timezone.utc)
    at = lambda h: (now - timedelta(hours=h)).isoformat()                 # noqa: E731
    rows = ([(at(30), "update", None)] * 40          # older than 24h, same or previous calendar day
            + [(at(26), "update", None)] * 7
            + [(at(20), "update", None)] * 5 + [(at(20), "noop", None)] * 3
            + [(at(1), "noop", "skip-no-contact")] * 25 + [(at(0.5), "noop", "skip-no-contact")] * 5)
    db.executemany("INSERT INTO audit_log (ts, action, error) VALUES (?,?,?)", rows)
    db.commit()
    db.close()
    monkeypatch.setattr(agent, "PODIUM", str(tmp_path))
    st = agent.app_podium()["state"]
    assert st["audit_24h"] == {"update": 5, "noop": 33}                  # not the 52 "updates" v3 reported
    assert st["skipped_24h"] == 30
    assert st["recent_2h"] == {"reached": 0, "skipped": 30}
    assert st["last_reached"] == at(20) and st["audit_last"] == at(0.5)
    verdict = software._v_podium({"service": {"state": "active"}, "state": {**st, "hwm": at(0.05)}}, now)
    assert verdict["headline"] == "Running, but its updates are not reaching Podium"


def test_agent_reports_recent_texter_failures_and_why(agent, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = sqlite3.connect(tmp_path / "data" / "mct.db")
    db.executescript("CREATE TABLE calls (conversation_id TEXT, started_at TEXT, ended_at TEXT, phone TEXT, "
                     "ani TEXT, kind TEXT, after_hours INT, action TEXT, reason TEXT, location_uid TEXT, "
                     "location_how TEXT, message TEXT, podium_conversation_uid TEXT, status_set INT, "
                     "attempts INT, last_error TEXT, processed_at TEXT);"
                     "CREATE TABLE alerts_sent (category TEXT, sent_at TEXT); CREATE TABLE state (key TEXT, value TEXT);")
    now = datetime.now(timezone.utc)
    at = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
    err = "PodiumError('Podium GET /contacts/+15035550123 -> HTTP 401')"
    db.executemany("INSERT INTO calls (conversation_id, started_at, ended_at, phone, kind, action, attempts, "
                   "last_error, processed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   [("old", at(9), at(9), "+15035550100", "voicemail", "error", 5, err, at(9)),
                    ("new", at(0.1), at(0.1), "+15035550123", "abandoned-in-queue", "error", 5, err, at(0.1)),
                    ("ok", at(0.2), at(0.2), "+15035550199", "abandoned-in-queue", "sent", 0, None, at(0.2))])
    db.commit()
    db.close()
    monkeypatch.setattr(agent, "MCT", str(tmp_path))
    d = agent.app_mct()["data"]
    assert (d["stuck_errors"], d["stuck_errors_7d"]) == (2, 1)
    assert d["last_error"] == {"at": at(0.1), "text": "PodiumError('Podium GET /contacts/...0123 -> HTTP 401')"}
    assert [(r["action"], r["error"]) for r in d["recent"]] == [
        ("error", d["last_error"]["text"]), ("sent", None), ("error", d["last_error"]["text"])]
    assert "5035550123" not in str(d)                                    # full numbers never leave the box
