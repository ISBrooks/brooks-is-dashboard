"""Signing in and out, the current login's details and saved views, and Settings -> Users.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import auth
from ..store import connect

from .common import (
    ADMIN_COOKIE, APP_COOKIE, _admin_ok, _allowed, _client_ip, _perms, _settings, _token, _user,
    app,
)


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"


@app.post("/api/login")
def api_login(body: dict) -> JSONResponse:
    if not _settings.password_login:
        return JSONResponse({"error": "password sign-in is turned off"}, status_code=403)
    if _settings.app_password and body.get("password") == _settings.app_password:
        r = JSONResponse({"ok": True})
        r.set_cookie(APP_COOKIE, _token(_settings.app_password, "app"),
                     max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
        return r
    return JSONResponse({"error": "bad password"}, status_code=401)


@app.post("/api/admin/login")
def api_admin_login(body: dict) -> JSONResponse:
    if not _settings.password_login:
        return JSONResponse({"error": "password sign-in is turned off"}, status_code=403)
    if _settings.admin_password and body.get("password") == _settings.admin_password:
        r = JSONResponse({"ok": True})
        r.set_cookie(ADMIN_COOKIE, _token(_settings.admin_password, "admin"),
                     max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
        return r
    return JSONResponse({"error": "bad password"}, status_code=401)


@app.get("/api/admin/status")
def api_admin_status(request: Request) -> dict:
    return {"admin": _admin_ok(request)}


@app.post("/api/auth/request")
def api_auth_request(body: dict, request: Request) -> JSONResponse:
    """Step 1 of sign-in: email a one-time code (if the address has an account)."""
    email = auth.norm_email(body.get("email"))
    if not email:
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    if auth.request_code(_settings, email, _client_ip(request)) == "rate":
        return JSONResponse(
            {"error": "Too many requests. Wait a minute and try again."}, status_code=429)
    return JSONResponse({"ok": True})


@app.post("/api/auth/verify")
def api_auth_verify(body: dict, request: Request) -> JSONResponse:
    """Step 2: a correct code becomes a session cookie."""
    email = auth.norm_email(body.get("email"))
    token = auth.verify_code(_settings, email, str(body.get("code") or ""),
                             _client_ip(request),
                             request.headers.get("user-agent", "")) if email else None
    if not token:
        return JSONResponse(
            {"error": "That code is not right or has expired."}, status_code=401)
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.SESSION_COOKIE, token, max_age=60 * 60 * 24 * auth.SESSION_DAYS,
                 httponly=True, samesite="lax", secure=_is_https(request))
    return r


@app.post("/api/auth/logout")
def api_auth_logout(request: Request) -> JSONResponse:
    """Sign this browser out (the account session and any password cookies)."""
    auth.end_session(_settings.db_path, request.cookies.get(auth.SESSION_COOKIE))
    r = JSONResponse({"ok": True})
    for c in (auth.SESSION_COOKIE, APP_COOKIE, ADMIN_COOKIE):
        r.delete_cookie(c)
    return r


def _actor(request: Request) -> str:
    u = _user(request)
    return u["email"] if u else "admin password"


@app.get("/api/users")
def api_users(request: Request) -> dict:
    """Settings -> Users: every account, the permission catalogue, and the
    agents a login can be tied to."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        agents = {r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE scorecard = 1 "
            "AND genesys_name IS NOT NULL")}
        agents |= {r[0] for r in conn.execute("SELECT name FROM commission_roster")}
        g = auth.grantable(_user(request))
        return {"users": auth.list_users(conn),
                "groups": [{"name": g_, "items": [{"key": k, "label": lab} for k, lab in items]}
                           for g_, items in auth.PERMISSION_GROUPS],
                "agent_tabs": [{"key": k, "label": lab} for k, lab in auth.AGENT_TABS],
                "presets": auth.PRESETS,
                "grantable": None if g is None else sorted(g),
                "agents": sorted(agents)}
    finally:
        conn.close()


@app.post("/api/users/save")
def api_users_save(body: dict, request: Request) -> dict:
    """Invite a new user (no id) or change an existing one."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        res = auth.save_user(conn, body, _actor(request), _user(request))
    finally:
        conn.close()
    if res.get("error"):
        return res
    out = {"ok": True, "created": res["created"]}
    if res["created"] and body.get("send_invite", True):
        err = auth.send_invite(_settings, res["email"], res["name"])
        out["emailed"] = not err
        if err:
            out["email_error"] = err
    return out


@app.post("/api/users/action")
def api_users_action(body: dict, request: Request) -> dict:
    """disable | enable | signout | resend | remove for one user."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        return auth.user_action(conn, _settings, body.get("id"),
                                str(body.get("action") or ""), _actor(request), _user(request))
    finally:
        conn.close()


@app.get("/api/me")
def api_me(request: Request) -> dict:
    """Who is signed in. `user` is null for shared-password access."""
    u = _user(request)
    have = _perms(request)
    return {"user": ({"email": u["email"], "name": u["name"], "kind": u["kind"],
                      "owner": u["owner"], "agent": u["agent"]} if u else None),
            "admin": _admin_ok(request),
            "permissions": [k for k in auth.ALL_KEYS + auth.AGENT_TAB_KEYS if k in have],
            "password_login": _settings.password_login,
            # lets the page's date pickers offer This / Last pay period
            "payroll_anchor_monday": _settings.payroll_anchor_monday}


# ---- per-user preferences (saved views etc.) ---------------------------------
# Small JSON blobs keyed by name, owned by the signed-in account, so a
# manager's saved views follow them to any device. Nothing here is shared.
_PREFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


_PREF_KEYS = {"sc_views", "ms_views", "mb_views"}


@app.get("/api/me/prefs")
def api_prefs_get(request: Request, key: str = ""):
    u = _user(request)
    if not u:
        return JSONResponse({"error": "sign in to use saved views"}, status_code=403)
    if key not in _PREF_KEYS:
        return JSONResponse({"error": "unknown preference"}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        conn.executescript(_PREFS_SCHEMA)
        row = conn.execute("SELECT value FROM user_prefs WHERE user_id = ? AND key = ?",
                           (u["id"], key)).fetchone()
    finally:
        conn.close()
    return {"key": key, "value": json.loads(row["value"]) if row else None}


@app.put("/api/me/prefs")
async def api_prefs_put(request: Request):
    u = _user(request)
    if not u:
        return JSONResponse({"error": "sign in to use saved views"}, status_code=403)
    body = await request.json()
    key = body.get("key")
    if key not in _PREF_KEYS:
        return JSONResponse({"error": "unknown preference"}, status_code=400)
    raw = json.dumps(body.get("value"))
    if len(raw) > 65536:
        return JSONResponse({"error": "too large"}, status_code=413)
    conn = connect(_settings.db_path)
    try:
        conn.executescript(_PREFS_SCHEMA)
        with conn:
            conn.execute("INSERT INTO user_prefs (user_id, key, value, updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (u["id"], key, raw, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    finally:
        conn.close()
    return {"ok": True}
