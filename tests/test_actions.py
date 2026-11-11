"""Actions that change data, and who is allowed to do what.

The number tests cover what the dashboard SHOWS. These cover what people DO:
an agent clocking in and out, a login seeing only what it is allowed to, saved
preferences, a Settings edit taking effect. They sign in the way a browser
does (a session cookie) with the sign-in wall switched on.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from isadash import auth

NOW = "2026-11-10T20:00:00+00:00"


@pytest.fixture()
def login(app, override_settings):
    """login(kind=..., agent=..., permissions=[...]) -> a signed-in test browser."""
    override_settings(app_password="wall-on")
    db = app._settings.db_path
    made = []

    def make(kind="staff", agent=None, permissions=(), owner=False):
        token = f"test-session-{len(made)}"
        c = sqlite3.connect(db)
        cur = c.execute(
            "INSERT INTO users (email, name, kind, agent, permissions, is_owner, status) VALUES (?,?,?,?,?,?,'active')",
            (f"user{len(made)}@example.test", f"Test User {len(made)}", kind, agent,
             json.dumps(list(permissions)), 1 if owner else 0))
        c.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at) VALUES (?,?,?,?,?)",
                  (auth._hash(token), cur.lastrowid, NOW, NOW, "2027-06-01T00:00:00+00:00"))
        c.commit()
        c.close()
        made.append(cur.lastrowid)
        client = TestClient(app.app)
        client.cookies.set(auth.SESSION_COOKIE, token)
        return client

    yield make
    c = sqlite3.connect(db)
    for uid in made:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
    for table in ("user_prefs", "punches", "punch_audit"):
        try:
            c.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    c.close()
    auth.forget_cached_sessions()


def test_signed_out_gets_nothing(app, login):
    login()                                             # switches the wall on
    nobody = TestClient(app.app)
    assert nobody.get("/api/home").status_code == 401
    assert nobody.post("/api/my/clock/in").status_code == 401
    page = nobody.get("/agent-metrics/is-numbers")
    assert page.status_code == 200 and "/js/main.js" not in page.text      # the sign-in page


def test_a_login_sees_only_what_it_was_granted(login):
    viewer = login(permissions=["home"])
    me = viewer.get("/api/me").json()
    assert me["permissions"] == ["home"] and me["admin"] is False and me["user"]["kind"] == "staff"
    assert viewer.get("/api/home").status_code == 200
    assert viewer.get("/api/revenue").status_code == 403
    assert viewer.get("/api/managers/schedule").status_code == 403
    assert viewer.get("/api/users").status_code == 403
    assert viewer.post("/api/config", json={}).status_code == 403


def test_an_agent_login_only_reaches_its_own_page(login):
    agent = login(kind="agent", agent="Avery Stone", permissions=["me.numbers", "me.schedule"])
    me = agent.get("/api/me").json()
    assert me["user"]["kind"] == "agent" and me["user"]["agent"] == "Avery Stone"
    own = "/api/my/scorecard?start_day=2026-10-11&end_day=2026-10-24"
    assert agent.get(own).status_code == 200
    assert agent.get("/api/home").status_code == 403                 # the managers' tabs are closed to agents
    assert agent.get("/api/scorecard").status_code == 403
    assert agent.get("/api/settings").status_code == 403
    # ...and it only ever gets its own row, whatever it asks for
    asked_for_someone_else = agent.get(own + "&as_agent=Blake%20Rivers").json()
    assert asked_for_someone_else == agent.get(own).json()


def test_clock_in_and_out(app, login):
    agent = login(kind="agent", agent="Avery Stone", permissions=["me.numbers", "me.schedule"])
    db = app._settings.db_path

    def open_punches():
        c = sqlite3.connect(db)
        try:
            return c.execute("SELECT COUNT(*), SUM(out_at IS NULL) FROM punches WHERE agent = 'Avery Stone'").fetchone()
        finally:
            c.close()

    before = agent.get("/api/my/clock")
    assert before.status_code == 200 and before.json()["open"] is None

    r = agent.post("/api/my/clock/in")
    assert r.status_code == 200 and r.json().get("ok"), r.text
    assert open_punches() == (1, 1)
    on = agent.get("/api/my/clock").json()
    assert on["open"] is not None and len(on["today_punches"]) == 1

    again = agent.post("/api/my/clock/in")                # already on the clock
    assert again.status_code == 400 and not again.json().get("ok")
    assert open_punches() == (1, 1)

    out = agent.post("/api/my/clock/out")
    assert out.status_code == 200 and out.json().get("ok"), out.text
    assert open_punches() == (1, 0)
    off = agent.get("/api/my/clock").json()
    assert off["open"] is None and len(off["today_punches"]) == 1

    assert agent.post("/api/my/clock/out").status_code == 400      # nothing to clock out of


def test_nobody_can_punch_for_someone_else(app, login):
    manager = login(permissions=["settings.users", "managers.timesheets"])
    assert manager.get("/api/my/clock?as_agent=Avery%20Stone").status_code == 200     # looking is allowed
    assert manager.post("/api/my/clock/in?as_agent=Avery%20Stone").status_code == 403  # punching is not
    c = sqlite3.connect(app._settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM punches").fetchone()[0] == 0
    c.close()


def test_saved_views_round_trip_per_user(login):
    a, b = login(permissions=["home"]), login(permissions=["home"])
    view = {"views": [{"name": "My team", "agents": ["Avery Stone"]}]}
    assert a.put("/api/me/prefs", json={"key": "sc_views", "value": view}).json() == {"ok": True}
    assert a.get("/api/me/prefs?key=sc_views").json()["value"] == view
    assert b.get("/api/me/prefs?key=sc_views").json()["value"] is None           # another login's views stay theirs
    assert a.put("/api/me/prefs", json={"key": "not_a_real_key", "value": 1}).status_code == 400


def test_a_settings_edit_shows_up_at_once(app, login):
    """Saving new tiers in Settings must reach the IS #'s sheet immediately,
    even though that sheet's numbers are saved results."""
    owner = login(owner=True)
    url = "/api/scorecard?start_day=2026-10-11&end_day=2026-10-24&mode=sold"
    tiers = owner.get(url).json()["tiers"]
    assert owner.get(url).json()["tiers"] == tiers                 # second read comes from the saved copy
    changed = json.loads(json.dumps(tiers))
    changed["acv"]["t1"] = tiers["acv"]["t1"] + 123
    try:
        saved = owner.post("/api/config", json={"scorecard_tiers": changed})
        assert saved.status_code == 200, saved.text
        assert owner.get(url).json()["tiers"]["acv"]["t1"] == tiers["acv"]["t1"] + 123
    finally:
        c = sqlite3.connect(app._settings.db_path)
        c.execute("DELETE FROM app_config WHERE key = 'scorecard_tiers'")
        c.commit()
        c.close()
    assert owner.get(url).json()["tiers"] == tiers
