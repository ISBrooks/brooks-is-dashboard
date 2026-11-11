"""FastAPI app serving the ISA dashboard.

Reads data/isadash.sqlite (written by scripts/backfill.py) and serves:
  GET /api/data     — all weekly + monthly per-agent rows, ready to render
  GET /api/meta     — coverage info
  static web/ at /  — the single-page UI

Aggregation result is cached and invalidated on the SQLite file's mtime, so
a backfill/refresh run shows up on the next page load without a restart.
"""
from __future__ import annotations

import logging

import base64
import functools
import hashlib
import html as _html
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, cache
from .config import load_settings
from .report import Bucket, aggregate, week_to_date_label
from .store import connect

app = FastAPI(title="Brooks IS Dashboard")
_settings = load_settings()
cache.configure(_settings.db_path)

# Performance log (journalctl -u brooks-is-dash | grep -E "cache:|slow:"): which
# saved results were rebuilt and why, and any request slower than a second.
_perf_log = logging.getLogger("isadash.perf")
if not _perf_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _perf_log.addHandler(_h)
    _perf_log.setLevel(logging.INFO)
    _perf_log.propagate = False
_SLOW_MS = 1000
# How long a new form lead may take to show up in form-lead numbers. The sourcing
# app's database changes every few minutes all day and rebuilding the lead list
# from it takes several seconds, so it is re-read at most this often. A Sync
# (manual or the morning one) always refreshes it. 0 = re-read on every change.
_FORM_LAG_S = 900


# --- access control -------------------------------------------------------
# User accounts (auth.py, docs/ACCOUNTS_PLAN.md): an invited user signs in with
# an emailed one-time code and gets a server-side session; what they can see
# is the set of permission keys ticked for them in Settings -> Users, checked
# here on every API request (_ACCESS below).
# While PASSWORD_LOGIN is on, the two old shared passwords still work:
# APP_PASSWORD = the Manager preset, ADMIN_PASSWORD = everything.
def _token(password: str, realm: str) -> str:
    return hmac.new(password.encode(), realm.encode(), hashlib.sha256).hexdigest()[:40]

APP_COOKIE, ADMIN_COOKIE = "is_app", "is_admin"


def _user(request: Request) -> dict | None:
    """The signed-in account for this request (None for password-only access)."""
    return auth.session_user(_settings.db_path, request.cookies.get(auth.SESSION_COOKIE))


def _pw_cookie(request: Request, cookie: str, password: str, realm: str) -> bool:
    return bool(_settings.password_login and password
                and request.cookies.get(cookie) == _token(password, realm))


def _app_ok(request: Request) -> bool:
    """Signed in (an account of either kind, or the shared app password)."""
    if not _settings.app_password:
        return True
    if _user(request):
        return True
    return _pw_cookie(request, APP_COOKIE, _settings.app_password, "app")


_ALL_PERMS = frozenset(auth.ALL_KEYS)
_ADMIN_PERMS = frozenset(auth.ADMIN_KEYS)
_MANAGER_PERMS = frozenset(auth.PRESETS["Manager"])
# an agent login holds exactly one key: its own page (the /api/my/* feeds,
# which only ever return that agent's numbers)
_AGENT_PERMS = frozenset({"my"})


def _perms(request: Request) -> frozenset:
    """The permission keys this request holds."""
    if not _settings.app_password:
        return _ALL_PERMS                    # no access control configured (local dev)
    u = _user(request)
    if u:
        if u["kind"] != "staff":
            # agents never get the tabs — only their own page's sub-tabs
            return frozenset({"my"} | {k for k in u["permissions"] if k in auth.AGENT_TAB_KEYS})
        if u["owner"] or "*" in u["permissions"]:
            return _ALL_PERMS
        return frozenset(u["permissions"])
    if _pw_cookie(request, APP_COOKIE, _settings.app_password, "app"):
        if _pw_cookie(request, ADMIN_COOKIE, _settings.admin_password, "admin"):
            return _ALL_PERMS
        return _MANAGER_PERMS
    return frozenset()


def _admin_ok(request: Request) -> bool:
    """Full admin: holds every admin-area key."""
    return _ADMIN_PERMS <= _perms(request)


# Which permission opens each API path: any ONE of the listed keys is enough
# (a few feeds serve more than one tab). None = any signed-in staff user.
# A path that isn't listed here is admin-only, so a new endpoint is locked
# until someone decides who should see it.
_RANGE_TABS = ("metrics.phone.conv", "metrics.phone.audit", "metrics.forms",
               "metrics.multiyear", "metrics.acv")
_ACCESS: dict[str, tuple | None] = {
    "GET /api/me": None,
    "GET /api/me/prefs": None,
    "PUT /api/me/prefs": None,
    "GET /api/admin/status": None,
    "POST /api/admin/login": None,
    "GET /api/sync/status": None,
    "GET /api/config": None,                 # tier/weight numbers the tables colour by
    "GET /api/data": None,                   # header coverage line; agent rows need a phone key
    "GET /api/home": ("home",),
    "GET /api/home_servicelevel": ("home",),
    "GET /api/home_trends": ("home",),
    "GET /api/home_daily": ("home",),
    "GET /api/home_quality": ("home",),
    "GET /api/home_ytd": ("home",),
    "GET /api/revenue": ("revenue",),
    "GET /api/revenue_overview": ("revenue",),
    "GET /api/source_revenue": ("sources",),
    "GET /api/source_revenue_overview": ("sources",),
    "GET /api/scorecard": ("home", "agent.scorecard", "agent.power"),
    "GET /api/quality": ("agent.scorecard", "agent.power", "metrics.team"),
    "GET /api/scorecard_calls": ("agent.scorecard",),
    "GET /api/scorecard_mm": ("agent.scorecard",),
    "POST /api/powerscore_export": ("agent.power",),
    "GET /api/agentview": ("agent.compare",),
    "GET /api/mastermetric": ("agent.master",),
    "GET /api/inbound_split": ("agent.inbound",),
    "GET /api/agentsubs": ("agent.scorecard", "metrics.multiyear", "metrics.acv"),
    "GET /api/teammetrics": ("metrics.team",),
    "GET /api/team_overview": ("metrics.team",),
    "GET /api/range": _RANGE_TABS,
    "GET /api/export": _RANGE_TABS,
    "GET /api/audit": ("metrics.phone.audit",),
    "GET /api/leaked": ("metrics.phone.leaked",),
    "GET /api/formleads": ("metrics.forms",),
    "GET /api/formleads_export": ("metrics.forms",),
    "GET /api/sources": ("sources",),
    "GET /api/source_dispositions": ("sources",),
    "GET /api/source_detail": ("sources",),
    "GET /api/marketing/volume": ("marketing.volume",),
    "GET /api/marketing/forms": ("marketing.forms",),
    "GET /api/marketing/bysource": ("marketing.bysource",),
    "GET /api/marketing/spend": ("marketing.spend",),
    "GET /api/marketing/volume_history": ("marketing.volume",),
    "GET /api/marketing/forms_history": ("marketing.volume",),
    "GET /api/marketing/hourly": ("marketing.volume",),
    "GET /api/marketing/spend_windows": ("marketing.spend",),
    "GET /api/marketing/spend_chart": ("marketing.spend",),
    "GET /api/commissions/lookup": ("payroll.lookup",),
    "GET /api/commissions/rules": ("payroll.rules",),
    "GET /api/commissions/roster": ("payroll.roster",),
    "POST /api/commissions/roster": ("payroll.roster",),
    "GET /api/commissions/reports": ("payroll.downloads",),
    "GET /api/commissions/report.xlsx": ("payroll.downloads",),
    "GET /api/commissions/database.xlsx": ("payroll.downloads",),
    "GET /api/commissions/d2d.xlsx": ("payroll.downloads",),
    "GET /api/commissions/intake/meta": ("payroll.adjust",),
    "GET /api/commissions/intake/account": ("payroll.adjust",),
    "POST /api/commissions/intake": ("payroll.adjust",),
    "GET /api/commissions/intake/list": ("payroll.adjust",),
    "POST /api/commissions/intake/delete": ("payroll.adjust",),
    "GET /api/commissions/runpayroll/status": ("payroll.run",),
    "POST /api/commissions/runpayroll": ("payroll.run",),
    "POST /api/config": ("settings.agents",),
    "POST /api/config/reset": ("settings.agents",),
    "GET /api/settings": ("settings.agents",),
    "POST /api/settings/add": ("settings.agents",),
    "POST /api/settings/agent": ("settings.agents",),
    "GET /api/software": ("settings.software",),
    "POST /api/software/run": ("settings.software",),
    "GET /api/users": ("settings.users",),
    "POST /api/users/save": ("settings.users",),
    "POST /api/users/action": ("settings.users",),
    "POST /api/sync": ("sync",),
    # the agent page. An agent login sees its own agent; someone who manages
    # users may preview any agent's page (?as=Name).
    "GET /api/my/tabs": ("my", "settings.users"),
    "GET /api/my/renewal": ("me.renewal", "settings.users"),
    "GET /api/my/payroll/periods": ("me.payroll", "settings.users"),
    "GET /api/my/payroll/report.csv": ("me.payroll", "settings.users"),
    "GET /api/my/payroll/lookup": ("me.payroll", "settings.users"),
    "GET /api/my/scorecard": ("me.numbers", "settings.users"),
    "GET /api/my/quality": ("me.numbers", "settings.users"),
    "GET /api/my/power": ("me.numbers", "settings.users"),
    "GET /api/my/calls": ("me.numbers", "settings.users"),
    "GET /api/my/mm": ("me.numbers", "settings.users"),
    "GET /api/my/ytd": ("me.numbers", "settings.users"),
    "GET /api/my/inbound": ("me.numbers", "settings.users"),
    "GET /api/my/schedule": ("me.schedule", "settings.users"),
    "GET /api/my/schedule_day": ("me.schedule", "settings.users"),
    "GET /api/my/clock": ("me.schedule", "settings.users"),
    "POST /api/my/clock/in": ("me.schedule",),
    "POST /api/my/clock/out": ("me.schedule",),
    "GET /api/managers/schedule": ("managers.schedule",),
    "POST /api/managers/schedule/targets": ("managers.schedule",),
    "POST /api/managers/schedule/shift": ("managers.schedule",),
    "POST /api/managers/schedule/move": ("managers.schedule",),
    "POST /api/managers/schedule/week": ("managers.schedule",),
    "POST /api/managers/schedule/publish": ("managers.schedule",),
    "POST /api/managers/schedule/template": ("managers.schedule",),
    "GET /api/managers/live": ("managers.schedule",),
    "GET /api/managers/templates": ("managers.schedule",),
    "GET /api/managers/templates/one": ("managers.schedule",),
    "POST /api/managers/templates": ("managers.schedule",),
    "GET /api/managers/saturdays": ("managers.schedule",),
    "POST /api/managers/saturdays": ("managers.schedule",),
    "GET /api/managers/timesheet": ("managers.timesheets",),
    "GET /api/managers/timesheet.csv": ("managers.timesheets",),
    "GET /api/managers/hours_report": ("managers.timesheets",),
    "GET /api/managers/hours_report.csv": ("managers.timesheets",),
    "POST /api/managers/punch": ("managers.timesheets",),
    "GET /api/managers/punch_history": ("managers.timesheets",),
    "GET /api/managers/adherence": ("managers.timesheets",),
    "GET /api/managers/clock_rules": ("managers.timesheets",),
    "POST /api/managers/clock_rules": ("managers.timesheets",),
    "POST /api/managers/auto_clockout": ("managers.timesheets",),
    "POST /api/managers/clock_override": ("managers.timesheets",),
    "GET /api/my/timeoff/preview": ("me.schedule", "settings.users"),
    "GET /api/my/pto": ("me.schedule", "settings.users"),
    "GET /api/my/timeoff": ("me.schedule", "settings.users"),
    "POST /api/my/timeoff": ("me.schedule",),
    "POST /api/my/timeoff/cancel": ("me.schedule",),
    "GET /api/managers/timeoff": ("managers.timeoff",),
    "POST /api/managers/timeoff/decide": ("managers.timeoff",),
    "POST /api/managers/timeoff/revoke": ("managers.timeoff",),
    "POST /api/managers/timeoff/save": ("managers.timeoff",),
    "GET /api/managers/timeoff/preview": ("managers.timeoff",),
    "GET /api/managers/pto": ("managers.agents",),
    "GET /api/managers/pto/ledger": ("managers.agents",),
    "POST /api/managers/pto/adjust": ("managers.agents",),
    "GET /api/managers/agents": ("managers.agents",),
    "POST /api/managers/agents": ("managers.agents",),
}
# the only "any signed-in user" paths an AGENT login may use
_AGENT_OPEN = {"GET /api/me", "GET /api/config", "GET /api/my/clock", "POST /api/my/clock/in", "POST /api/my/clock/out",
               "POST /api/my/timeoff", "POST /api/my/timeoff/cancel"}


def _allowed(request: Request) -> bool:
    """May this request use this API path?"""
    key = f"{request.method} {request.url.path}"
    if key not in _ACCESS:
        return _admin_ok(request)
    need = _ACCESS[key]
    if need is None:
        u = _user(request)
        return not (u and u["kind"] == "agent") or key in _AGENT_OPEN
    have = _perms(request)
    return any(k in have for k in need)


def _client_ip(request: Request) -> str | None:
    # behind Caddy the peer is 127.0.0.1; the proxy appends the real client last
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip() or None
    return request.client.host if request.client else None


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"


_LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Brooks IS Dashboard</title><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow,noarchive">
<meta name=theme-color content="#1e3659">
<link rel=icon type="image/png" href="__ICON__">
<style>body{font:15px/1.5 "Segoe UI",system-ui,sans-serif;background:#f3f1ee;color:#1f2b3d;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{background:#fff;border:1px solid #e2e3e6;border-top:5px solid #1e3659;border-radius:14px;
padding:30px 32px 34px;width:340px;box-shadow:0 10px 30px rgba(30,54,89,.10);text-align:center}
[hidden]{display:none}
.logo{display:block;width:250px;max-width:100%;height:auto;margin:4px auto 12px}
h1{font-size:12px;font-weight:800;letter-spacing:.26em;text-transform:uppercase;color:#1e3659;margin:0 0 22px}
p{color:#66728a;font-size:13px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cfd4db;border-radius:8px;font-size:15px;color:#1f2b3d}
input:focus{outline:none;border-color:#1e3659;box-shadow:0 0 0 3px rgba(151,195,216,.45)}
#cd{text-align:center;font-size:22px;letter-spacing:6px}
button{width:100%;margin-top:12px;padding:11px;border:none;border-radius:8px;background:#1e3659;
color:#fff;font-weight:700;font-size:15px;cursor:pointer}
button:hover{background:#27456f}
button:disabled{opacity:.6;cursor:default}
.err{color:#b23b2e;font-size:13px;margin-top:10px;min-height:16px}
button.alt{margin-top:14px;font-size:12px;color:#66728a;background:none;width:auto;padding:0;
font-weight:400;text-decoration:underline}button.alt:hover{background:none;color:#1e3659}</style></head>
<body><div class=box>
<img class=logo src="__LOGO__" alt="Brooks Pest Control">
<h1>IS Dashboard</h1>
<form id=f-email onsubmit="return sendCode(event)">
<p>Sign in with your work email</p>
<input id=em type=email autofocus autocomplete=email placeholder="you@brookspest.com">
<button type=submit id=b-email>Email me a code</button><div class=err id=err-email></div>
<button type=button class=alt onclick="show('f-pw')">Use the shared password instead</button></form>
<form id=f-code hidden onsubmit="return checkCode(event)">
<p id=sent></p>
<input id=cd inputmode=numeric autocomplete=one-time-code maxlength=6 placeholder="000000">
<button type=submit id=b-code>Sign in</button><div class=err id=err-code></div>
<button type=button class=alt onclick="show('f-email')">Use a different email or send again</button></form>
<form id=f-pw hidden onsubmit="return go(event)">
<p>Enter the access password</p>
<input id=pw type=password autocomplete=current-password placeholder="Password">
<button type=submit>Sign in</button><div class=err id=err-pw></div>
<button type=button class=alt onclick="show('f-email')">Sign in with email instead</button></form>
</div>
<script>
const $=id=>document.getElementById(id);let EMAIL='';
function show(id){for(const f of ['f-email','f-code','f-pw'])$(f).hidden=f!==id;
const i=$(id).querySelector('input');if(i)i.focus();}
async function post(url,body){return fetch(url,{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function msg(r,fallback){try{return (await r.json()).error||fallback;}catch(e){return fallback;}}
async function sendCode(e){e.preventDefault();$('err-email').textContent='';
EMAIL=$('em').value.trim();$('b-email').disabled=true;
try{const r=await post('/api/auth/request',{email:EMAIL});
if(r.ok){$('sent').textContent='If '+EMAIL+' has an account, a 6-digit code is on its way. It expires in 10 minutes.';
$('cd').value='';$('err-code').textContent='';show('f-code');}
else{$('err-email').textContent=await msg(r,'Something went wrong. Try again.');}}
catch(x){$('err-email').textContent='Could not reach the server. Try again.';}
$('b-email').disabled=false;return false;}
async function checkCode(e){e.preventDefault();$('b-code').disabled=true;
try{const r=await post('/api/auth/verify',{email:EMAIL,code:$('cd').value});
if(r.ok){location.reload();return false;}
$('err-code').textContent=await msg(r,'That code is not right or has expired.');}
catch(x){$('err-code').textContent='Could not reach the server. Try again.';}
$('b-code').disabled=false;return false;}
async function go(e){e.preventDefault();
const r=await post('/api/login',{password:$('pw').value});
if(r.ok){location.reload();}else{$('err-pw').textContent='Incorrect password';}
return false;}</script></body></html>"""


# Kept out of search engines: every response carries a noindex directive, and
# the login wall means a crawler never sees anything past it anyway.
_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
_NOINDEX = "noindex, nofollow, noarchive"


_OPEN_PATHS = {"/api/login", "/api/auth/request", "/api/auth/verify", "/api/auth/logout"}


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    if path == "/robots.txt":
        return PlainTextResponse(_ROBOTS_TXT, headers={"X-Robots-Tag": _NOINDEX})
    if not _app_ok(request) and path not in _OPEN_PATHS:
        if path.startswith("/api"):
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        else:
            resp = HTMLResponse(_login_html())  # any page -> login until authed
        resp.headers["X-Robots-Tag"] = _NOINDEX
        return resp
    if path.startswith("/api") and path not in _OPEN_PATHS and not _allowed(request):
        resp = JSONResponse({"error": "locked"}, status_code=403)
        resp.headers["X-Robots-Tag"] = _NOINDEX
        return resp
    _t0 = time.perf_counter()
    resp = await call_next(request)
    _ms = (time.perf_counter() - _t0) * 1000
    if _ms >= _SLOW_MS and path.startswith("/api"):
        _perf_log.info("slow: %s %s%s %.0f ms", request.method, path,
                       "?" + request.url.query if request.url.query else "", _ms)
    resp.headers["X-Robots-Tag"] = _NOINDEX
    if not path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


_PW_LINK = ('<button type=button class=alt onclick="show(\'f-pw\')">'
            'Use the shared password instead</button>')
assert _PW_LINK in _LOGIN_HTML


@functools.lru_cache(maxsize=None)
def _brand_uri(name: str) -> str:
    """A web/brand/ image as a data: URI. The login page is served before any
    static file is reachable, so its logo and favicon travel inline."""
    try:
        data = (WEB_DIR / "brand" / name).read_bytes()
    except OSError:
        log.warning("brand asset missing: %s", name)
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _login_html() -> str:
    # after cutover the shared-password option disappears from the login page
    page = _LOGIN_HTML if _settings.password_login else _LOGIN_HTML.replace(_PW_LINK, "")
    return (page.replace("__LOGO__", _brand_uri("login-logo.png"))
                .replace("__ICON__", _brand_uri("favicon-32.png")))


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


def _roster_names(conn) -> list[str]:
    """The inside-sales roster for scheduling / time clock / agent records:
    tracked agents minus the Bruntwork outbound reps."""
    skip = set(_settings.outbound_agents)
    return [r[0] for r in conn.execute(
        "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL AND scorecard = 1")
        if r[0] not in skip]


@app.on_event("startup")
def _timeclock_startup() -> None:
    from . import timeclock
    timeclock.start_sweeper(_settings, connect, _roster_names)


def _warm_calls() -> list:
    """What the Home tab asks for on load, with the same arguments the page
    sends (web/app.js: loadHome, loadHomeYtd) so a visitor finds them ready."""
    from .periods import PACIFIC
    now = datetime.now(timezone.utc)
    year = now.astimezone(PACIFIC).year
    return [
        (_get_form_events, {}),
        (_build_payload, {}),
        (api_home, {}),
        (api_home_trends, {}),
        # the page builds this range in the browser: Jan 1 .. today's UTC date
        (api_scorecard, {"start_day": f"{year}-01-01", "end_day": now.date().isoformat(), "mode": "sold"}),
        (api_revenue, {"year": year}),      # behind /api/home_ytd
        (api_home_quality, {}),
        (api_home_servicelevel, {}),
        (_team_overview_db, {}),            # Team Metrics -> Team Overview
    ]


@app.on_event("startup")
def _cache_startup() -> None:
    cache.start_warmer(_warm_calls)


# ---- time clock (agent) + timesheets / agent records (IS Managers) ----------
@app.get("/api/my/clock")
def api_my_clock(request: Request, as_agent: str = ""):
    from . import timeclock
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        return timeclock.status(conn, _settings, name, _roster_names(conn) + [name])
    finally:
        conn.close()


@app.post("/api/my/clock/in")
def api_my_clock_in(request: Request):
    from . import timeclock
    u = _user(request)
    name = u["agent"] if (u and u["kind"] == "agent") else None   # only the agent themself, never a preview
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeclock.clock_in(conn, _settings, name, _roster_names(conn) + [name],
                               _client_ip(request), request.headers.get("user-agent"))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/my/clock/out")
def api_my_clock_out(request: Request):
    from . import timeclock
    u = _user(request)
    name = u["agent"] if (u and u["kind"] == "agent") else None
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeclock.clock_out(conn, name)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- schedule builder (IS Managers -> Schedule) ----
@app.get("/api/managers/schedule")
def api_managers_schedule(monday: str = ""):
    from datetime import date

    from . import shiftplan
    try:
        from .periods import CLOCK_TZ
        m = date.fromisoformat(monday) if monday else shiftplan.monday_of(datetime.now(timezone.utc).astimezone(CLOCK_TZ).date())
    except ValueError:
        return JSONResponse({"error": "bad monday"}, status_code=400)
    m = shiftplan.monday_of(m)
    conn = connect(_settings.db_path)
    try:
        from . import timeoff
        d = shiftplan.week(conn, m, _roster_names(conn), timeoff.approved_days(conn, m, m + __import__("datetime").timedelta(days=6)))
    finally:
        conn.close()
    # does the sheet have this week (so "Start from the sheet" can be offered)?
    try:
        from . import schedule
        d["sheet_has_week"] = m.isoformat() in schedule.website_schedule(_settings)["weeks"]
    except Exception:
        d["sheet_has_week"] = False
    return d


@app.post("/api/managers/schedule/shift")
def api_managers_schedule_shift(request: Request, body: dict):
    from datetime import date

    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        from . import timeoff
        day = date.fromisoformat(body["day"])
        r = shiftplan.set_shift(conn, _who(request), str(body.get("agent") or ""), day, body.get("cell"),
                                timeoff.approved_days(conn, day, day))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/move")
def api_managers_schedule_move(request: Request, body: dict):
    from datetime import date

    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        from . import timeoff
        to_day = date.fromisoformat(body["to_day"])
        r = shiftplan.move_shift(conn, _who(request), body["agent"], date.fromisoformat(body["day"]),
                                 body["to_agent"], to_day, bool(body.get("copy")), timeoff.approved_days(conn, to_day, to_day))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/week")
def api_managers_schedule_week(request: Request, body: dict):
    """action: create (source blank|last|template|sheet) | delete"""
    from datetime import date

    from . import schedule, shiftplan
    m = shiftplan.monday_of(date.fromisoformat(body["monday"]))
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "delete":
            r = shiftplan.delete_week(conn, m)
        else:
            src = body.get("source") or "blank"
            sheet_rows = None
            if src == "sheet":
                sheet_rows = schedule.website_schedule(_settings)["weeks"].get(m.isoformat(), {})
            from datetime import timedelta

            from . import timeoff
            r = shiftplan.create_week(conn, _who(request), m, src, body.get("template_id"), _roster_names(conn), sheet_rows,
                                      timeoff.approved_days(conn, m, m + timedelta(days=6)))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/targets")
def api_managers_schedule_targets(request: Request, body: dict):
    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.set_coverage_targets(conn, body.get("targets") or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/publish")
def api_managers_schedule_publish(request: Request, body: dict):
    from datetime import date

    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.publish(conn, _who(request), shiftplan.monday_of(date.fromisoformat(body["monday"])), bool(body.get("on")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/schedule/template")
def api_managers_schedule_template(request: Request, body: dict):
    """action: save (name, monday) | delete (id)"""
    from datetime import date

    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "delete":
            r = shiftplan.delete_template(conn, int(body["id"]))
        else:
            r = shiftplan.save_template(conn, _who(request), body.get("name") or "", shiftplan.monday_of(date.fromisoformat(body["monday"])))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- who's on the clock right now (Schedule tab side box) ----
@app.get("/api/managers/live")
def api_managers_live():
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        return timeclock.live_now(conn, _settings, _roster_names(conn))
    finally:
        conn.close()


# ---- templates screen (IS Managers -> Templates) ----
@app.get("/api/managers/templates")
def api_managers_templates():
    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        return {"templates": shiftplan.template_list(conn)}
    finally:
        conn.close()


@app.get("/api/managers/templates/one")
def api_managers_template_one(id: int = 0):
    from . import shiftplan
    conn = connect(_settings.db_path)
    try:
        r = shiftplan.template_get(conn, id, _roster_names(conn))
        return JSONResponse(r, status_code=404 if r.get("error") else 200)
    finally:
        conn.close()


@app.post("/api/managers/templates")
def api_managers_templates_post(request: Request, body: dict):
    """action: create (name, source blank|week|copy, monday, from_id) | rename (id, name) | delete (id)
    | set (id, agent, dows, cell) | move (id, agent, dow, to_agent, to_dow, copy) | clear_agent (id, agent)"""
    from datetime import date

    from . import shiftplan
    who = _who(request)
    act = body.get("action")
    conn = connect(_settings.db_path)
    try:
        if act == "create":
            m = None
            if body.get("monday"):
                try:
                    m = date.fromisoformat(body["monday"])
                except ValueError:
                    return JSONResponse({"error": "Bad date."}, status_code=400)
            r = shiftplan.template_create(conn, who, body.get("name") or "", body.get("source") or "blank", m, body.get("from_id"))
        elif act == "rename":
            r = shiftplan.template_rename(conn, who, body.get("id"), body.get("name") or "")
        elif act == "delete":
            r = shiftplan.delete_template(conn, int(body.get("id") or 0))
        elif act == "set":
            dows = body.get("dows")
            if dows is None:
                dows = [body.get("dow")]
            try:
                dows = [int(k) for k in dows]
            except (TypeError, ValueError):
                return JSONResponse({"error": "Bad day."}, status_code=400)
            r = shiftplan.template_set(conn, who, body.get("id"), str(body.get("agent") or ""), dows, body.get("cell"))
        elif act == "move":
            r = shiftplan.template_move(conn, who, body.get("id"), str(body.get("agent") or ""), int(body.get("dow") or 0),
                                        str(body.get("to_agent") or ""), int(body.get("to_dow") or 0), bool(body.get("copy")))
        elif act == "clear_agent":
            r = shiftplan.template_clear_agent(conn, who, body.get("id"), str(body.get("agent") or ""))
        else:
            r = {"error": "Unknown action."}
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- Saturday rotation tracker (Schedule tab, below the grid) ----
@app.get("/api/managers/saturdays")
def api_managers_saturdays():
    from . import saturdays
    conn = connect(_settings.db_path)
    try:
        return saturdays.tracker(conn, _roster_names(conn))
    finally:
        conn.close()


@app.post("/api/managers/saturdays")
def api_managers_saturdays_post(request: Request, body: dict):
    """action: mark (agent, day, worked true|false|null) | regular (agent, on)"""
    from datetime import date

    from . import saturdays
    agent = str(body.get("agent") or "").strip()
    if not agent:
        return JSONResponse({"error": "No agent given."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        if body.get("action") == "regular":
            r = saturdays.set_regular(conn, agent, bool(body.get("on")))
        elif body.get("action") == "mark":
            try:
                day = date.fromisoformat(body.get("day") or "")
            except ValueError:
                return JSONResponse({"error": "Bad date."}, status_code=400)
            w = body.get("worked")
            r = saturdays.mark(conn, _who(request), agent, day, None if w is None else bool(w))
        else:
            r = {"error": "Unknown action."}
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- time off requests ----
def _agent_self(request: Request) -> str | None:
    u = _user(request)
    return (u["agent"] or None) if (u and u["kind"] == "agent") else None


@app.get("/api/my/timeoff")
def api_my_timeoff(request: Request, as_agent: str = ""):
    from . import pto, timeoff
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        return {"agent": name, "requests": timeoff.mine(conn, name), "pto": pto.summary(conn, name)}
    finally:
        conn.close()


@app.post("/api/my/timeoff")
def api_my_timeoff_create(request: Request, body: dict):
    from datetime import date

    from . import shiftplan, timeoff
    name = _agent_self(request)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        try:
            a, b = date.fromisoformat(body["start"]), date.fromisoformat(body["end"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
        hours = body.get("hours")
        r = timeoff.create(conn, _settings, name, _roster_names(conn) + [name], a, b, body.get("kind") or "pto",
                           float(hours) if hours not in (None, "") else None, body.get("note") or "",
                           shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/my/timeoff/preview")
def api_my_timeoff_preview(request: Request, start: str = "", end: str = "", as_agent: str = ""):
    from datetime import date

    from . import shiftplan, timeoff
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end or start)
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.preview(conn, _settings, name, _roster_names(conn) + [name], a, b, shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if not r.get("error") else 400)
    finally:
        conn.close()


@app.post("/api/my/timeoff/cancel")
def api_my_timeoff_cancel(request: Request, body: dict):
    from . import timeoff
    name = _agent_self(request)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.cancel(conn, name, int(body.get("id") or 0))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/timeoff")
def api_managers_timeoff(agent: str = ""):
    """The queue, the roster (for "Add time off"), and — when an agent is
    picked — every request that agent has ever had (to edit older ones)."""
    from . import pto, timeoff
    conn = connect(_settings.db_path)
    try:
        d = timeoff.queue(conn)
        d["agents"] = sorted(_roster_names(conn))
        if agent:
            d["history"] = timeoff.mine(conn, agent, 200)
            d["history_agent"] = agent
            d["history_balance"] = pto.balance(conn, agent)
        return d
    finally:
        conn.close()


@app.get("/api/managers/timeoff/preview")
def api_managers_timeoff_preview(agent: str = "", start: str = "", end: str = ""):
    """The add/edit form's helper line: scheduled hours on those days, the
    projected PTO balance on the first day, and who else is off."""
    from datetime import date

    from . import shiftplan, timeoff
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end or start)
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    if not agent:
        return JSONResponse({"error": "Pick the agent."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        r = timeoff.preview(conn, _settings, agent, _roster_names(conn) + [agent], a, b, shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if not r.get("error") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/save")
def api_managers_timeoff_save(request: Request, body: dict):
    """Add (no id) or change (id) a time-off request, any status — see timeoff.manager_save."""
    from datetime import date

    from . import shiftplan, timeoff
    try:
        a, b = date.fromisoformat(body.get("start") or ""), date.fromisoformat(body.get("end") or body.get("start") or "")
    except ValueError:
        return JSONResponse({"error": "Pick a start and end date."}, status_code=400)
    try:
        req_id = int(body["id"]) if body.get("id") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "Bad request id."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        r = timeoff.manager_save(conn, _settings, _who(request), req_id, agent, _roster_names(conn) + ([agent] if agent else []),
                                 a, b, body.get("kind") or "pto", body.get("hours"), body.get("unpaid_hours"),
                                 body.get("note") or "", body.get("status") or "approved", body.get("decision_note"),
                                 shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/decide")
def api_managers_timeoff_decide(request: Request, body: dict):
    from . import shiftplan, timeoff
    conn = connect(_settings.db_path)
    try:
        r = timeoff.decide(conn, _settings, _who(request), int(body.get("id") or 0), bool(body.get("approve")),
                           body.get("note") or "", _roster_names(conn), shiftplan.published_weeks(conn))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/timeoff/revoke")
def api_managers_timeoff_revoke(request: Request, body: dict):
    from . import timeoff
    conn = connect(_settings.db_path)
    try:
        r = timeoff.revoke(conn, _who(request), int(body.get("id") or 0), body.get("note") or "")
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


# ---- PTO ----
@app.get("/api/my/pto")
def api_my_pto(request: Request, as_agent: str = ""):
    from . import pto
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        pto.accrue(conn, [name])
        d = pto.summary(conn, name)
        d["recent"] = pto.ledger(conn, name, 12)
        return d
    finally:
        conn.close()


@app.get("/api/managers/pto")
def api_managers_pto():
    from . import pto
    conn = connect(_settings.db_path)
    try:
        names = _roster_names(conn)
        pto.accrue(conn, names)
        return {"agents": [pto.summary(conn, a) for a in names]}
    finally:
        conn.close()


@app.get("/api/managers/pto/ledger")
def api_managers_pto_ledger(agent: str = ""):
    from . import pto
    conn = connect(_settings.db_path)
    try:
        return {"agent": agent, "rows": pto.ledger(conn, agent, 500)}
    finally:
        conn.close()


@app.post("/api/managers/pto/adjust")
def api_managers_pto_adjust(request: Request, body: dict):
    from datetime import date

    from . import pto
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        try:
            hours = float(body.get("hours"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Hours must be a number (negative to take hours away)."}, status_code=400)
        day = date.fromisoformat(body["day"]) if body.get("day") else None
        r = pto.adjust(conn, _who(request), agent, hours, body.get("note") or "", day, body.get("kind") or "adjust")
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


def _who(request: Request) -> str:
    u = _user(request)
    return (u["email"] if u else None) or "admin"


@app.get("/api/managers/timesheet")
def api_managers_timesheet(start_day: str = "", end_day: str = ""):
    from datetime import date

    from . import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.timesheet(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                   _roster_names(conn))
    finally:
        conn.close()


@app.get("/api/managers/timesheet.csv")
def api_managers_timesheet_csv(start_day: str = "", end_day: str = ""):
    from datetime import date

    from fastapi.responses import Response

    from . import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        ts = timeclock.timesheet(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                 _roster_names(conn))
    finally:
        conn.close()
    return Response(timeclock.timesheet_csv(ts), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="timesheet_{start_day}_{end_day}.csv"'})


def _hours_report(start_day: str, end_day: str):
    """(report, None) or (None, error response). No dates = the latest closed pay period."""
    from datetime import date
    from . import timeclock
    if not start_day and not end_day:
        closed_end, closed_start, _run = _payroll_periods()
        start_day, end_day = closed_start.isoformat(), closed_end.isoformat()
    if not _valid_range(start_day, end_day) or (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days > 92:
        return None, JSONResponse({"error": "Pick a date range of at most 93 days."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.hours_report(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                      _roster_names(conn)), None
    finally:
        conn.close()


@app.get("/api/managers/hours_report")
def api_managers_hours_report(start_day: str = "", end_day: str = ""):
    """Regular / daily OT / weekly OT / PTO hours per agent for a date range (payroll)."""
    rep, err = _hours_report(start_day, end_day)
    return err or rep


@app.get("/api/managers/hours_report.csv")
def api_managers_hours_report_csv(start_day: str = "", end_day: str = ""):
    from fastapi.responses import Response

    from . import timeclock
    rep, err = _hours_report(start_day, end_day)
    if err:
        return err
    return Response(timeclock.hours_report_csv(rep), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="hours_report_{rep["start"]}_{rep["end"]}.csv"'})


@app.post("/api/managers/punch")
def api_managers_punch(request: Request, body: dict):
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.edit_punch(conn, _who(request), body.get("id"), body.get("agent"), body.get("in_at"),
                                 body.get("out_at"), body.get("reason") or "", bool(body.get("delete")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/adherence")
def api_managers_adherence(start_day: str = "", end_day: str = ""):
    from datetime import date

    from . import timeclock
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return timeclock.adherence(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day),
                                   _roster_names(conn))
    finally:
        conn.close()


@app.get("/api/managers/clock_rules")
def api_managers_clock_rules():
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"rules": timeclock.clock_rules(conn), "auto": timeclock.auto_rules(conn),
                "overrides": timeclock.overrides(conn), "agents": sorted(_roster_names(conn))}
    finally:
        conn.close()


@app.post("/api/managers/clock_rules")
def api_managers_clock_rules_save(request: Request, body: dict):
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.set_clock_rules(conn, _who(request), body or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/auto_clockout")
def api_managers_auto_clockout(request: Request, body: dict):
    """The auto clock-out setting: enabled, grace_min, stamp (shift_end|run), no_shift_h."""
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        r = timeclock.set_auto_rules(conn, _who(request), body or {})
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.post("/api/managers/clock_override")
def api_managers_clock_override(request: Request, body: dict):
    from datetime import date

    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        try:
            day = date.fromisoformat(str(body.get("day") or ""))
        except ValueError:
            return JSONResponse({"error": "Pick a day."}, status_code=400)
        r = timeclock.set_override(conn, _who(request), agent, day, body.get("note") or "", bool(body.get("remove")))
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.get("/api/managers/punch_history")
def api_managers_punch_history(id: int = 0):
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"history": timeclock.punch_history(conn, id)}
    finally:
        conn.close()


@app.get("/api/managers/agents")
def api_managers_agents():
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        return {"agents": timeclock.hr_list(conn, _roster_names(conn))}
    finally:
        conn.close()


@app.post("/api/managers/agents")
def api_managers_agents_save(request: Request, body: dict):
    from . import timeclock
    conn = connect(_settings.db_path)
    try:
        agent = str(body.get("agent") or "")
        if agent not in _roster_names(conn):
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        r = timeclock.hr_save(conn, _who(request), agent, body)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
    finally:
        conn.close()


@app.on_event("startup")
def _auth_startup() -> None:
    # creates the account tables on first run and guarantees the owner exists
    conn = connect(_settings.db_path)
    try:
        auth.ensure_owner(conn, _settings.owner_email)
        auth.migrate_agent_tabs(conn)
        auth.rollout_agent_tabs(conn)
    finally:
        conn.close()


@app.get("/api/my/tabs")
def api_my_tabs(request: Request, as_agent: str = ""):
    """Which of the agent page's sub-tabs this agent may see. For a
    user-manager previewing /?as=Name it is that agent's login's setting
    (all three when they have no login yet)."""
    u = _user(request)
    if u and u["kind"] == "agent":
        return {"agent": u["agent"], "tabs": [k for k in auth.AGENT_TAB_KEYS if k in u["permissions"]]}
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        rows = conn.execute("SELECT permissions FROM users WHERE kind = 'agent' AND agent = ? AND status != 'disabled'", (name,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"agent": name, "tabs": list(auth.AGENT_TAB_KEYS), "login": False}
    have = set()
    for r in rows:
        have |= set(auth.clean_agent_tabs(json.loads(r["permissions"] or "[]")))
    return {"agent": name, "tabs": [k for k in auth.AGENT_TAB_KEYS if k in have], "login": True}


@app.get("/api/my/renewal")
def api_my_renewal():
    """The Renewal Calculator's rules and pricing tables, read from the
    IS Renewal Calculator sheet (ten-minute cache, last good copy kept)."""
    from . import renewal
    conn = connect(_settings.db_path)
    try:
        d = renewal.pricing(conn, _settings)
    finally:
        conn.close()
    return JSONResponse(d, status_code=503 if "error" in d else 200)


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


def _rows_for(table: dict[str, dict[str, Bucket]]) -> list[dict]:
    rows: list[dict] = []
    for label in sorted(table):
        total = Bucket()
        for agent in sorted(table[label]):
            b = table[label][agent]
            rows.append({"period": label, "agent": agent, **b.row()})
            total.merge_counts(b)
        rows.append({"period": label, "agent": "__TOTAL__", **total.row()})
    return rows


@cache.cached(copy_result=False)     # ~650 KB, read-only: every caller shares one object
def _build_payload() -> dict:
    from .adjust import adjustment_stats
    from .periods import PACIFIC, UTC
    conn = connect(_settings.db_path)
    try:
        agg = aggregate(conn)
        # honest-conversion inputs: audit sums + hidden/leaked per period
        for r in conn.execute(
            "SELECT date, agent, qualified, sales FROM audit_daily"):
            dt = datetime.fromisoformat(r["date"] + "T12:00:00+00:00")
            from .periods import month_label, week_label
            for label, table in ((week_label(dt), agg.weekly),
                                 (month_label(dt), agg.monthly)):
                if label in table and r["agent"] in table[label]:
                    table[label][r["agent"]].audit_qual += r["qualified"]
                    table[label][r["agent"]].audit_sales += r["sales"]
        # bound every period to the days the audit sheet covers, so the
        # honest numerator and denominator describe the SAME days — the
        # current week otherwise shows conversions the audit team hasn't
        # reviewed yet (that's how >100% happened)
        audit_bound = _audit_end_utc(conn)
        for label, agents in agg.weekly.items():
            if not any(b.audit_qual for b in agents.values()):
                continue
            a, b_ = _week_utc_bounds(label)
            b_ = min(b_, audit_bound) if audit_bound else b_
            if b_ <= a:
                continue
            for agent, st in adjustment_stats(conn, a, b_).items():
                if agent in agents:
                    agents[agent].hidden = st["hidden"]
                    agents[agent].leaked = st["leaked"]
                    agents[agent].honest_conv = st["converted"]
        for label, agents in agg.monthly.items():
            if not any(b.audit_qual for b in agents.values()):
                continue
            y, m = int(label[:4]), int(label[5:7])
            start_l = datetime(y, m, 1, tzinfo=PACIFIC)
            end_l = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=PACIFIC)
            a = start_l.astimezone(UTC).isoformat()
            b_ = end_l.astimezone(UTC).isoformat()
            b_ = min(b_, audit_bound) if audit_bound else b_
            if b_ <= a:
                continue
            for agent, st in adjustment_stats(conn, a, b_).items():
                if agent in agents:
                    agents[agent].hidden = st["hidden"]
                    agents[agent].leaked = st["leaked"]
                    agents[agent].honest_conv = st["converted"]
        cov = conn.execute(
            "SELECT COUNT(*), MIN(start_utc), MAX(start_utc) FROM calls"
        ).fetchone()
        subs = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "wtd_label": week_to_date_label(now),
        "conversion_window_days": _settings.conversion_window_days,
        "coverage": {
            "calls": cov[0], "subscriptions": subs,
            "first_call": cov[1], "last_call": cov[2],
        },
        "weekly": _rows_for(agg.weekly),
        "monthly": _rows_for(agg.monthly),
    }


# /api/data as ready-to-send JSON, rebuilt only when the payload object changes
_data_json: dict = {"src": None, "full": b"", "lite": b""}


def _json_bytes(obj) -> bytes:
    # the same encoding FastAPI's JSONResponse uses
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


@app.get("/api/data")
def api_data(request: Request) -> Response:
    payload = _build_payload()
    if _data_json["src"] is not payload:
        lite = {k: payload[k] for k in ("coverage", "generated_at") if k in payload}
        _data_json.update(src=payload, full=_json_bytes(payload), lite=_json_bytes(lite))
    # everyone gets the header's coverage line; the per-agent weekly/monthly
    # rows behind Phone Conversion + Trends need one of those permissions
    have = _perms(request)
    full = bool({"metrics.phone.conv", "metrics.phone.trends"} & have)
    return Response(_data_json["full" if full else "lite"], media_type="application/json")


@cache.cached(copy_result=False)     # tens of thousands of events, read-only for every caller
def _get_form_events():
    """Build + classify form-lead events. Saved until the dashboard tables it
    reads change, or the sourcing app's database changed and the saved copy is
    older than _FORM_LAG_S."""
    from .formleads import (aggregate_events, build_lead_events,
                            classify_events, load_source_names)
    fl_path = Path(_settings.formleads_db)
    cache.depend_on_file(fl_path, _FORM_LAG_S)
    if _settings.sourcing_master:
        cache.depend_on_file(_settings.sourcing_master)
    if not fl_path.exists():
        return None, {"error": f"FORMLEADS_DB not found: {fl_path}"}
    events = build_lead_events(str(fl_path), _settings.form_dedup_window_hours,
                               tuple(_settings.form_exclude_sources),
                               tuple(_settings.form_exclude_source_days))
    conn = connect(_settings.db_path)
    try:
        n_cust = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        classify_events(conn, events, _settings.form_conversion_window_days)
    finally:
        conn.close()
    payload = aggregate_events(events, load_source_names(_settings.sourcing_master))
    payload["customers_loaded"] = n_cust
    payload["conversion_window_days"] = _settings.form_conversion_window_days
    payload["dedup_window_hours"] = _settings.form_dedup_window_hours
    return events, payload


@app.get("/api/formleads")
def api_formleads() -> dict:
    _, payload = _get_form_events()
    # per-source economics live behind the marketing password (/api/sources);
    # this endpoint stays team-level only
    return {k: v for k, v in payload.items() if k != "sources"}


def _phone_window_stats(conn, start_utc: str, end_utc: str) -> dict:
    """Unique qualified leads + unique converted leads on ISA queues,
    roster agents, within [start, end). Converted = own-name sale OR
    pitched + bought from any seller."""
    cf, own, anyc = set(), set(), set()
    for r in conn.execute(
        """SELECT c.ani_e164, c.dispo_group, c.converted_sub_id,
                  c.converted_any_sub_id
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
             AND c.start_utc >= ? AND c.start_utc < ?""",
        (start_utc, end_utc),
    ):
        if r["dispo_group"] in ("closed", "followup"):
            cf.add(r["ani_e164"])
        if r["converted_sub_id"]:
            own.add(r["ani_e164"])
        if r["converted_any_sub_id"]:
            anyc.add(r["ani_e164"])
    conv = own | (cf & anyc)
    qual = cf | conv
    pct = round(100.0 * len(conv) / len(qual), 1) if qual else 0.0
    return {"qual": len(qual), "conv": len(conv), "pct": pct}


@app.get("/api/home")
@cache.cached()
def api_home() -> dict:
    """Home cards. Every window is WHOLE Pacific calendar days ending
    YESTERDAY (today is never included — no partial-day noise), and the
    comparison delta is the same number of whole days immediately before.
    Audit-sheet metrics (Phone / All Leads) end at the last AUDITED day if
    the sheet lags behind yesterday. Each card carries its date range."""
    from datetime import timedelta

    from .periods import PACIFIC
    now = datetime.now(timezone.utc)
    # anchor = start of today (Pacific) = end of the last complete day
    today_pac = now.astimezone(PACIFIC).date()
    anchor = datetime(today_pac.year, today_pac.month, today_pac.day, tzinfo=PACIFIC)
    anchor_utc = anchor.astimezone(timezone.utc)

    def rng(end_dt, days: int) -> dict:
        """Inclusive Pacific day range for the `days` whole days ending before end_dt."""
        last = (end_dt.astimezone(PACIFIC) - timedelta(days=1)).date()
        first = (end_dt.astimezone(PACIFIC) - timedelta(days=days)).date()
        return {"start": first.isoformat(), "end": last.isoformat()}

    events, _ = _get_form_events()
    conn = connect(_settings.db_path)
    try:
        out: dict = {"generated_at": now.isoformat(),
                     "anchor_end": (today_pac - timedelta(days=1)).isoformat()}
        for days in (30, 7):
            cur_start = anchor_utc - timedelta(days=days)
            prev_start = anchor_utc - timedelta(days=2 * days)
            # forms
            def form_stats(a, b):
                evs = [e for e in (events or [])
                       if a <= e.first_touch_utc < b and not e.already_customer]
                conv = sum(1 for e in evs if e.converted)
                return {"net": len(evs), "conv": conv,
                        "pct": round(100.0 * conv / len(evs), 1) if evs else 0.0}
            cur, prev = form_stats(cur_start, anchor_utc), form_stats(prev_start, cur_start)
            out[f"form{days}"] = {**cur, "prev_pct": prev["pct"],
                                  "range": rng(anchor_utc, days),
                                  "prev_range": rng(cur_start, days)}
            # phone
            cur = _phone_window_stats(conn, cur_start.isoformat(), anchor_utc.isoformat())
            prev = _phone_window_stats(conn, prev_start.isoformat(), cur_start.isoformat())
            out[f"phone{days}"] = {**cur, "prev_pct": prev["pct"],
                                   "range": rng(anchor_utc, days),
                                   "prev_range": rng(cur_start, days)}
        # windows for sheet/all/honest metrics are anchored at the last
        # audited day (never later than yesterday's end) so every part
        # describes the same whole days
        ab = _audit_end_utc(conn)
        bound = min(ab, anchor_utc.isoformat()) if ab else anchor_utc.isoformat()

        def audit_window(days_back_hi: int, days_back_lo: int) -> dict:
            """Audit-sheet sales/qualified over whole audited Pacific days."""
            from .periods import PACIFIC
            bound_dt = datetime.fromisoformat(bound)
            a_day = (bound_dt - timedelta(days=days_back_hi)).astimezone(PACIFIC).date().isoformat()
            b_day = (bound_dt - timedelta(days=days_back_lo)).astimezone(PACIFIC).date().isoformat()
            r = conn.execute(
                "SELECT COALESCE(SUM(qualified),0), COALESCE(SUM(sales),0) "
                "FROM audit_daily WHERE date >= ? AND date < ?",
                (a_day, b_day)).fetchone()
            q, s = r[0], r[1]
            return {"qual": q, "sales": s,
                    "pct": round(100.0 * s / q, 1) if q else None}

        # SHEET CONVERSION — the default conversion metric (audit team's own
        # numbers, exactly as kept on the master sheet)
        bound_dt0 = datetime.fromisoformat(bound)
        for days in (30, 7):
            cur = audit_window(days, 0)
            prev = audit_window(2 * days, days)
            out[f"sheet{days}"] = {**cur, "prev_pct": prev["pct"],
                                   "range": rng(bound_dt0, days),
                                   "prev_range": rng(bound_dt0 - timedelta(days=days), days)}

        # all-leads conversion: SHEET numbers + FORM numbers, added together
        # (per ops decision 2026-07-13 — no cross-channel modeling, just the
        # audit team's qualified/sales plus the form funnel's net/converted,
        # over the same audited-day window)
        bound_dt_all = datetime.fromisoformat(bound)
        for days in (30, 7):
            a = bound_dt_all - timedelta(days=days)
            aw = audit_window(days, 0)
            sheet_q, sheet_s = aw["qual"], aw["sales"]
            form_net = form_conv = 0
            for e in (events or []):
                if not (a <= e.first_touch_utc < bound_dt_all) or e.already_customer:
                    continue
                form_net += 1
                if e.converted:
                    form_conv += 1
            total = sheet_q + form_net
            conv = sheet_s + form_conv
            out[f"all{days}"] = {"total": total, "conv": conv,
                                 "sheet_q": sheet_q, "sheet_s": sheet_s,
                                 "form_net": form_net, "form_conv": form_conv,
                                 "pct": round(100.0 * conv / total, 1) if total else 0.0,
                                 "range": rng(bound_dt_all, days),
                                 "prev_range": rng(bound_dt_all - timedelta(days=days), days)}

        # honest conversion 30d + 7d: team terms are PHONE-DEDUPED
        # (__TEAM__), windows BOUNDED to audited days, prior-window deltas
        from .adjust import adjustment_stats

        def honest_window(days_back_hi: int, days_back_lo: int) -> dict | None:
            """Whole Pacific audited days, anchored at the audit bound —
            'trailing 7 days' means the LAST 7 AUDITED DAYS, so the call
            window and the audit-sheet days always line up exactly."""
            from .periods import PACIFIC
            bound_dt = datetime.fromisoformat(bound)
            a_dt = bound_dt - timedelta(days=days_back_hi)
            b_dt = bound_dt - timedelta(days=days_back_lo)
            if b_dt <= a_dt:
                return None
            a_day = a_dt.astimezone(PACIFIC).date().isoformat()
            b_day = b_dt.astimezone(PACIFIC).date().isoformat()
            q = conn.execute(
                "SELECT COALESCE(SUM(qualified),0) FROM audit_daily "
                "WHERE date >= ? AND date < ?", (a_day, b_day)).fetchone()[0]
            adj = adjustment_stats(conn, a_dt.isoformat(), b_dt.isoformat())
            t = adj.get("__TEAM__", {"hidden": 0, "leaked": 0, "converted": 0})
            denom = max(q + t["hidden"] + t["leaked"], t["converted"])
            return {"audit_qual": q, "sys_conv": t["converted"],
                    "hidden": t["hidden"], "leaked": t["leaked"],
                    "pct": round(100.0 * t["converted"] / denom, 1) if q else None}

        for days in (30, 7):
            cur = honest_window(days, 0)
            prev = honest_window(2 * days, days)
            if cur:
                cur["prev_pct"] = prev["pct"] if prev else None
            out[f"true{days}"] = cur

        # sold-to-serviced, trailing 30d vs the 30d before (roster-sold,
        # all statuses — pending sits outside the denominator)
        def sts_window(a: str, b: str) -> dict:
            sts_rows = [r[0] for r in conn.execute(
                """SELECT s.initial_status FROM subscriptions s
                   WHERE s.date_added_utc >= ? AND s.date_added_utc < ?
                     AND s.sold_by IN (SELECT TRIM(value) FROM agent_map,
                        json_each('["'||REPLACE(fr_employee_id,',','","')||'"]')
                        WHERE on_roster=1 AND fr_employee_id IS NOT NULL)""",
                (a, b))]
            return _sts(sts_rows)
        for days in (30, 7):
            cs = anchor_utc - timedelta(days=days)
            cur = sts_window(cs.isoformat(), anchor_utc.isoformat())
            prev = sts_window((anchor_utc - timedelta(days=2 * days)).isoformat(), cs.isoformat())
            out[f"sts{days}"] = {**cur, "prev_pct": prev["sts_pct"],
                                 "range": rng(anchor_utc, days), "prev_range": rng(cs, days)}
        # team multiyear % + ACV, trailing windows (roster-sold subscriptions)
        subs = _roster_sub_rows(conn)

        def sub_window(a: str, b: str) -> dict:
            win = [(m, cv, ip, sg) for _, added, m, cv, _st, ip, sg in subs if a <= added < b]
            n = len(win)
            multi = sum(1 for m, _cv, _ip, _sg in win if m >= 18)
            cvs = [cv for _m, cv, _ip, _sg in win if cv > 0]
            ips = [ip for _m, _cv, ip, _sg in win if ip > 0]
            sgs = [sg for _m, _cv, _ip, sg in win if sg is not None]
            return {"subs": n,
                    "multiyear_pct": round(100.0 * multi / n, 1) if n else 0.0,
                    "acv": round(sum(cvs) / len(cvs), 2) if cvs else 0.0,
                    "init_avg": round(sum(ips) / len(ips), 2) if ips else 0.0,
                    "signed_pct": round(100.0 * sum(sgs) / len(sgs), 1) if sgs else 0.0}

        for days in (30, 7):
            cs_dt = anchor_utc - timedelta(days=days)
            cur_s = cs_dt.isoformat()
            prev_s = (anchor_utc - timedelta(days=2 * days)).isoformat()
            cur = sub_window(cur_s, anchor_utc.isoformat())
            prev = sub_window(prev_s, cur_s)
            out[f"team{days}"] = {**cur, "prev_multiyear_pct": prev["multiyear_pct"],
                                  "prev_acv": prev["acv"],
                                  "prev_init_avg": prev["init_avg"],
                                  "prev_signed_pct": prev["signed_pct"],
                                  "range": rng(anchor_utc, days), "prev_range": rng(cs_dt, days)}
        return out
    finally:
        conn.close()


@app.get("/api/revenue")
@cache.cached()
def api_revenue(year: int = 0) -> dict:
    d = dict(_revenue_data(year))
    d.pop("_people", None)
    return d


def _revenue_data(year: int = 0, by: str = "agent") -> dict:
    """Revenue = SUM(contract value) by SALE DATE for every OFFICE-STAFF seller
    ever (FieldRoutes employee type 0), grouped by PERSON — so departed reps
    (e.g. sold big in 2024) still show. by="source" keeps every rule the same
    but groups by lead source (the Sources tab's groups) instead of seller, so
    the two views always add up to the same total. Mosquito/upsells COUNT as revenue; only
    the specific RENEWALS are excluded (not new sales). Low sellers (< a yearly
    threshold) fold into an "Other" bucket. Returns per-person months, team
    totals, team + individual records, and the year list."""
    from datetime import timedelta

    from .periods import PACIFIC, week_start
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    OTHER_MIN = 12000.0  # a person's yearly revenue below this bundles into "Other"
    now = datetime.now(timezone.utc)
    conn = connect(_settings.db_path)
    try:
        now_pac = now.astimezone(PACIFIC)
        cur_year = now_pac.year
        years = list(range(2023, cur_year + 1))  # 2023 is the first year
        year = cur_year if not year else max(2023, min(int(year), cur_year))

        # the current year stops at the current month — no empty future months
        # (so the chart/table end cleanly instead of diving to $0)
        n_months = now_pac.month if year == cur_year else 12

        # office-staff sellers only (type 0): fr employee id -> person name
        emp = {r["fr_employee_id"]: r["name"] for r in conn.execute(
            "SELECT fr_employee_id, name FROM employees WHERE emp_type = '0' AND name IS NOT NULL")}

        y_start = datetime(year, 1, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()
        _ey, _em = (year, n_months + 1) if n_months < 12 else (year + 1, 1)
        y_end = datetime(_ey, _em, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()

        # by="source": the same subscriptions, bucketed by lead-source GROUP the
        # way the Sources table groups them (label -> provider -> group)
        if by in ("source", "channel"):
            group_of, label_to_provider, _ = _load_source_maps()
            chan_of = _load_source_channels()

            def src_of(label) -> tuple[str, str]:
                """(row name, member name) for a subscription's source label:
                by="source" -> (group, provider); by="channel" -> (channel, group).
                A channel comes from the label, else its provider, else its group."""
                if not label:
                    grp = prov = "(no source)"
                else:
                    prov = label_to_provider.get(label.strip(), label.strip())
                    grp = group_of.get(prov.lower(), prov)
                if by == "source":
                    return grp, prov
                return _channel_for(chan_of, label, prov, grp), grp
            c_part: dict = {}      # (row name, member name) -> per-month / per-year revenue

        agent_month: dict[str, list] = {}
        a_day: dict = {}
        a_week: dict = {}
        t_day: dict = {}
        t_week: dict = {}
        t_month: dict = {}
        n_sales = 0        # how many subscriptions make up team_total
        a_sales: dict[str, int] = {}
        for r in conn.execute(
            f"""SELECT s.sold_by, s.source, s.date_added_utc, s.contract_value
                FROM subscriptions s
                WHERE s.date_added_utc >= ? AND s.date_added_utc < ?
                  AND s.initial_status IN ('Completed','Pending')
                  AND s.contract_value IS NOT NULL AND s.contract_value > 0
                  AND (s.source IS NULL OR s.source NOT IN
                       ({','.join('?' * len(_RENEWAL_SOURCES))}))""",
            (y_start, y_end, *_RENEWAL_SOURCES)):
            name = emp.get(r["sold_by"])
            if not name:  # field/tech or unknown seller — not office staff
                continue
            if by != "agent":
                name, prov = src_of(r["source"])
            cv = float(r["contract_value"])
            dt = datetime.fromisoformat(r["date_added_utc"])
            pac = dt.astimezone(PACIFIC)
            n_sales += 1
            a_sales[name] = a_sales.get(name, 0) + 1
            mo, day, wk = pac.month, pac.date().isoformat(), week_start(dt).date().isoformat()
            agent_month.setdefault(name, [0.0] * 12)[mo - 1] += cv
            if by != "agent":
                c_part.setdefault((name, prov), [0.0] * 12)[mo - 1] += cv
            a_day[(name, day)] = a_day.get((name, day), 0.0) + cv
            a_week[(name, wk)] = a_week.get((name, wk), 0.0) + cv
            t_day[day] = t_day.get(day, 0.0) + cv
            t_week[wk] = t_week.get(wk, 0.0) + cv
            t_month[mo] = t_month.get(mo, 0.0) + cv

        def wk_label(iso: str) -> str:
            d = datetime.fromisoformat(iso + "T00:00:00")
            e = d + timedelta(days=6)
            return f"{d:%b} {d.day} – {e:%b} {e.day}"

        def team_rec(d: dict, kind: str):
            if not d:
                return None
            k = max(d, key=d.get)
            label = wk_label(k) if kind == "week" else (MONTHS[k - 1] if kind == "month" else k)
            return {"label": label, "amount": round(d[k], 2)}

        def indiv_rec(d: dict, kind: str):
            if not d:
                return None
            (name, k), amt = max(d.items(), key=lambda kv: kv[1])
            label = wk_label(k) if kind == "week" else (MONTHS[k - 1] if kind == "month" else k)
            return {"agent": name, "label": label, "amount": round(amt, 2)}

        # hall of fame is over REAL people (before bundling)
        a_month = {(name, mo + 1): v for name, mons in agent_month.items()
                   for mo, v in enumerate(mons) if v > 0}

        # per-agent bests for the year (feed the Best Day / Best Week columns)
        best_day: dict[str, tuple] = {}
        for (name, k), amt in a_day.items():
            if name not in best_day or amt > best_day[name][1]:
                best_day[name] = (k, amt)
        best_week: dict[str, tuple] = {}
        for (name, k), amt in a_week.items():
            if name not in best_week or amt > best_week[name][1]:
                best_week[name] = (k, amt)

        # individuals >= threshold; the long tail folds into "Other (N)"
        agents = []
        other = [0.0] * 12
        other_n = 0
        for name, mons in sorted(agent_month.items(), key=lambda kv: -sum(kv[1])):
            if sum(mons) >= OTHER_MIN or by == "channel":
                bd, bw = best_day.get(name), best_week.get(name)
                agents.append({"agent": name, "months": [round(x, 2) for x in mons[:n_months]],
                               "total": round(sum(mons), 2),
                               "best_day": {"date": bd[0], "amount": round(bd[1], 2)} if bd else None,
                               "best_week": {"label": wk_label(bw[0]), "amount": round(bw[1], 2)} if bw else None})
            else:
                for i in range(12):
                    other[i] += mons[i]
                other_n += 1
        if other_n:
            agents.append({"agent": f"Other ({other_n})", "is_other": True,
                           "months": [round(x, 2) for x in other[:n_months]], "total": round(sum(other), 2)})
        if by != "agent":    # a row made of 2+ members can be opened to see them
            for row in agents:
                kids = [(p, m) for (g, p), m in c_part.items() if g == row["agent"]]
                if len(kids) >= 2:
                    row["children"] = [
                        {"agent": p, "months": [round(x, 2) for x in m[:n_months]],
                         "total": round(sum(m), 2)}
                        for p, m in sorted(kids, key=lambda kv: -sum(kv[1]))]

        people = {}
        for name, mons in agent_month.items():
            bd, bw = best_day.get(name), best_week.get(name)
            top = max(range(12), key=lambda i: mons[i])
            people[name] = {
                "revenue": round(sum(mons), 2), "sales": a_sales.get(name, 0),
                "best_day": {"date": bd[0], "amount": round(bd[1], 2)} if bd else None,
                "best_week": {"label": wk_label(bw[0]), "amount": round(bw[1], 2)} if bw else None,
                "best_month": {"label": MONTHS[top], "amount": round(mons[top], 2)} if mons[top] > 0 else None}
        return {
            "_people": people, "by": by,
            "year": year, "years": years, "months": MONTHS[:n_months], "agents": agents,
            "seller_count": len(agent_month),
            "team_month_totals": [round(t_month.get(m, 0.0), 2) for m in range(1, n_months + 1)],
            "team_total": round(sum(t_month.values()), 2),
            "team_sales": n_sales,
            "team_records": {"day": team_rec(t_day, "day"),
                             "week": team_rec(t_week, "week"),
                             "month": team_rec(t_month, "month")},
            "hall_of_fame": {"day": indiv_rec(a_day, "day"),
                             "week": indiv_rec(a_week, "week"),
                             "month": indiv_rec(a_month, "month")},
        }
    finally:
        conn.close()


@app.get("/api/source_revenue")
def api_source_revenue(year: int = 0, level: str = "channel") -> dict:
    """Sources tab: the Revenue tab's yearly view sliced by marketing channel
    (level=channel: Paid / Organic / Affiliate Marketing / Generic / Other, each opening
    to its source groups) or by source group (level=source, each opening to
    its providers)."""
    by = "source" if level == "source" else "channel"
    d = dict(_revenue_data(year, by=by))
    d.pop("_people", None)
    return d


@app.get("/api/source_revenue_overview")
def api_source_revenue_overview(level: str = "channel") -> dict:
    """Sources tab: the Revenue tab's all-time overview, by channel or source."""
    return api_revenue_overview(by="source" if level == "source" else "channel")


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


@app.get("/api/home_ytd")
def api_home_ytd() -> dict:
    """Home's year-to-date counters. Taken straight from the Revenue tab's own
    feed so the two always show the same total (every office-staff seller,
    upsells in, renewals out) — only the two totals are returned, so Home
    doesn't need the Revenue permission."""
    from .periods import PACIFIC
    year = datetime.now(timezone.utc).astimezone(PACIFIC).year
    d = api_revenue(year=year)
    return {"year": year, "revenue": d.get("team_total"), "sales": d.get("team_sales"),
            "sellers": d.get("seller_count")}


@app.get("/api/revenue_overview")
def api_revenue_overview(by: str = "agent") -> dict:
    """All-time companion to /api/revenue, for the Revenue tab's Overview.
    by="source" groups by lead source instead (Sources tab).

    Same inclusion rules as the yearly view — office-staff sellers (FieldRoutes
    type 0) grouped by person, Completed/Pending initials, sale date in Pacific,
    renewals excluded, mosquito/upsells counted — so every figure reconciles with
    the year tabs. Only the grouping differs:
      - chart: one series per YEAR across Jan-Dec (team revenue) so years can be
        compared month by month; the current year stops at the current month
      - table: each person's revenue per year, best year, best month, total
      - records: ALL-TIME team and individual biggest day / week / month
    """
    from datetime import timedelta

    from .periods import PACIFIC, week_start
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    OTHER_MIN = 12000.0  # an ALL-TIME total below this bundles into "Other"
    now_pac = datetime.now(timezone.utc).astimezone(PACIFIC)
    cur_year = now_pac.year
    years = list(range(2023, cur_year + 1))  # 2023 is the first year, as in /api/revenue
    yi = {y: i for i, y in enumerate(years)}
    conn = connect(_settings.db_path)
    try:
        emp = {r["fr_employee_id"]: r["name"] for r in conn.execute(
            "SELECT fr_employee_id, name FROM employees WHERE emp_type = '0' AND name IS NOT NULL")}
        start = datetime(2023, 1, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()
        # end at the close of the current month — the same cutoff the year tab uses
        _ey, _em = (cur_year, now_pac.month + 1) if now_pac.month < 12 else (cur_year + 1, 1)
        end = datetime(_ey, _em, 1, tzinfo=PACIFIC).astimezone(timezone.utc).isoformat()

        # by="source": the same subscriptions, bucketed by lead-source GROUP the
        # way the Sources table groups them (label -> provider -> group)
        if by in ("source", "channel"):
            group_of, label_to_provider, _ = _load_source_maps()
            chan_of = _load_source_channels()

            def src_of(label) -> tuple[str, str]:
                """(row name, member name) for a subscription's source label:
                by="source" -> (group, provider); by="channel" -> (channel, group).
                A channel comes from the label, else its provider, else its group."""
                if not label:
                    grp = prov = "(no source)"
                else:
                    prov = label_to_provider.get(label.strip(), label.strip())
                    grp = group_of.get(prov.lower(), prov)
                if by == "source":
                    return grp, prov
                return _channel_for(chan_of, label, prov, grp), grp
            c_part: dict = {}      # (row name, member name) -> per-month / per-year revenue

        p_year: dict[str, list] = {}   # person -> revenue per year
        p_month: dict = {}             # (person, year, month) -> revenue
        a_day: dict = {}
        a_week: dict = {}
        t_ym = {y: [0.0] * 12 for y in years}
        t_day: dict = {}
        t_week: dict = {}
        t_month: dict = {}             # (year, month) -> revenue
        for r in conn.execute(
            f"""SELECT s.sold_by, s.source, s.date_added_utc, s.contract_value
                FROM subscriptions s
                WHERE s.date_added_utc >= ? AND s.date_added_utc < ?
                  AND s.initial_status IN ('Completed','Pending')
                  AND s.contract_value IS NOT NULL AND s.contract_value > 0
                  AND (s.source IS NULL OR s.source NOT IN
                       ({','.join('?' * len(_RENEWAL_SOURCES))}))""",
            (start, end, *_RENEWAL_SOURCES)):
            name = emp.get(r["sold_by"])
            if not name:  # field/tech or unknown seller — not office staff
                continue
            if by != "agent":
                name, prov = src_of(r["source"])
            cv = float(r["contract_value"])
            dt = datetime.fromisoformat(r["date_added_utc"])
            pac = dt.astimezone(PACIFIC)
            y, mo = pac.year, pac.month
            if y not in yi:
                continue
            day, wk = pac.date().isoformat(), week_start(dt).date().isoformat()
            p_year.setdefault(name, [0.0] * len(years))[yi[y]] += cv
            if by != "agent":
                c_part.setdefault((name, prov), [0.0] * len(years))[yi[y]] += cv
            p_month[(name, y, mo)] = p_month.get((name, y, mo), 0.0) + cv
            a_day[(name, day)] = a_day.get((name, day), 0.0) + cv
            a_week[(name, wk)] = a_week.get((name, wk), 0.0) + cv
            t_ym[y][mo - 1] += cv
            t_day[day] = t_day.get(day, 0.0) + cv
            t_week[wk] = t_week.get(wk, 0.0) + cv
            t_month[(y, mo)] = t_month.get((y, mo), 0.0) + cv

        # all-time labels need the YEAR, which the single-year view can leave off
        def wk_label(iso: str) -> str:
            d = datetime.fromisoformat(iso + "T00:00:00")
            e = d + timedelta(days=6)
            return f"{d:%b} {d.day} – {e:%b} {e.day}, {e.year}"

        def mo_label(k: tuple) -> str:
            return f"{MONTHS[k[1] - 1]} {k[0]}"

        def label(kind: str, k):
            return wk_label(k) if kind == "week" else (mo_label(k) if kind == "month" else k)

        def team_rec(d: dict, kind: str):
            if not d:
                return None
            k = max(d, key=d.get)
            return {"label": label(kind, k), "amount": round(d[k], 2)}

        def indiv_rec(d: dict, kind: str):
            if not d:
                return None
            (name, k), amt = max(d.items(), key=lambda kv: kv[1])
            return {"agent": name, "label": label(kind, k), "amount": round(amt, 2)}

        a_month = {(name, (y, mo)): v for (name, y, mo), v in p_month.items()}

        best_month: dict[str, tuple] = {}
        for (name, y, mo), amt in p_month.items():
            if name not in best_month or amt > best_month[name][1]:
                best_month[name] = ((y, mo), amt)

        agents = []
        other = [0.0] * len(years)
        other_n = 0
        for name, ys in sorted(p_year.items(), key=lambda kv: -sum(kv[1])):
            total = sum(ys)
            if total >= OTHER_MIN or by == "channel":
                by_i = max(range(len(years)), key=lambda i: ys[i])   # best year's index
                bm = best_month.get(name)
                agents.append({
                    "agent": name, "years": [round(v, 2) for v in ys], "total": round(total, 2),
                    "best_year": {"label": str(years[by_i]), "amount": round(ys[by_i], 2)},
                    "best_month": ({"label": mo_label(bm[0]), "amount": round(bm[1], 2)}
                                   if bm else None)})
            else:
                for i, v in enumerate(ys):
                    other[i] += v
                other_n += 1
        if other_n:
            agents.append({"agent": f"Other ({other_n})", "is_other": True,
                           "years": [round(v, 2) for v in other], "total": round(sum(other), 2)})
        if by != "agent":    # a row made of 2+ members can be opened to see them
            for row in agents:
                kids = [(p, v) for (g, p), v in c_part.items() if g == row["agent"]]
                if len(kids) >= 2:
                    row["children"] = [
                        {"agent": p, "years": [round(x, 2) for x in v], "total": round(sum(v), 2)}
                        for p, v in sorted(kids, key=lambda kv: -sum(kv[1]))]

        year_totals = [round(sum(t_ym[y]), 2) for y in years]
        best_y = max(range(len(years)), key=lambda i: year_totals[i])
        series = []
        for y in years:
            mons = t_ym[y][:now_pac.month if y == cur_year else 12]
            series.append({"agent": str(y), "months": [round(v, 2) for v in mons],
                           "total": round(sum(mons), 2)})

        return {
            "mode": "overview", "by": by, "year": None, "years": years,
            "seller_count": len(p_year),
            "team_total": round(sum(year_totals), 2),
            "year_totals": year_totals,
            "team_best_year": ({"label": str(years[best_y]), "amount": year_totals[best_y]}
                               if any(year_totals) else None),
            "team_records": {"day": team_rec(t_day, "day"),
                             "week": team_rec(t_week, "week"),
                             "month": team_rec(t_month, "month")},
            "hall_of_fame": {"day": indiv_rec(a_day, "day"),
                             "week": indiv_rec(a_week, "week"),
                             "month": indiv_rec(a_month, "month")},
            # shaped like the yearly payload so the same chart component draws it
            "chart": {"year": "", "months": MONTHS, "agents": series},
            "agents": agents,
        }
    finally:
        conn.close()


# Genesys is asked at most every 30 minutes (the windows are whole days ending
# yesterday, so the answer barely moves); an error answer is never saved.
@app.get("/api/home_servicelevel")
@cache.cached(ttl=1800, keep=lambda v: isinstance(v, dict))
def api_home_servicelevel() -> dict:
    """Genesys service level (30d + 7d + weekly trend) for the home row. Kept in
    its own endpoint so the DB-driven home renders instantly and this fills in
    when Genesys responds."""
    from datetime import timedelta

    from .genesys import GenesysClient
    from .periods import PACIFIC, week_start
    now = datetime.now(timezone.utc)
    qids = _settings.genesys_queue_ids
    # whole Pacific days ending YESTERDAY, like every other home card
    today_pac = now.astimezone(PACIFIC).date()
    anchor = datetime(today_pac.year, today_pac.month, today_pac.day,
                      tzinfo=PACIFIC).astimezone(timezone.utc)

    def rng(end_dt, days: int) -> dict:
        last = (end_dt.astimezone(PACIFIC) - timedelta(days=1)).date()
        first = (end_dt.astimezone(PACIFIC) - timedelta(days=days)).date()
        return {"start": first.isoformat(), "end": last.isoformat()}

    this_week = week_start(now)  # Sunday 00:00 Pacific — align the trend to full weeks
    wk_start = (this_week - timedelta(weeks=13)).astimezone(timezone.utc)
    wk_end = this_week.astimezone(timezone.utc)
    g = GenesysClient(_settings.genesys_region, _settings.genesys_client_id,
                      _settings.genesys_client_secret)
    try:
        sl30 = g.queue_stats(anchor - timedelta(days=30), anchor, qids)
        sl7 = g.queue_stats(anchor - timedelta(days=7), anchor, qids)
        series = g.service_level_series(wk_start, wk_end, qids, "P7D")
    except Exception as e:  # never break the home page on a Genesys hiccup
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    finally:
        g.close()
    return {
        "sl30": {"pct": sl30.get("service_level"), "offered": sl30.get("offered"),
                 "range": rng(anchor, 30)},
        "sl7": {"pct": sl7.get("service_level"), "offered": sl7.get("offered"),
                "range": rng(anchor, 7)},
        "weeks": [s["start"] for s in series],
        "values": [s["service_level"] for s in series],
    }


@app.get("/api/home_trends")
@cache.cached()
def api_home_trends() -> dict:
    """13 weekly points for each home metric — the last-90-days sparklines.
    Same definitions as the home cards, bucketed into full Sunday–Saturday
    Pacific weeks (last point = the last COMPLETE week)."""
    from datetime import timedelta

    from .periods import week_start
    now = datetime.now(timezone.utc)
    events, _ = _get_form_events()
    conn = connect(_settings.db_path)
    try:
        this_week = week_start(now)  # Sunday 00:00 Pacific of the current week
        N = 13
        wins = [(this_week - timedelta(weeks=N - i),
                 this_week - timedelta(weeks=N - 1 - i)) for i in range(N)]
        weeks = [a.date().isoformat() for a, _b in wins]  # Sunday start dates

        subs = _roster_sub_rows(conn)  # (id, added_iso, months, cv, status, ip)
        sts_rows = conn.execute(
            """SELECT s.date_added_utc, s.initial_status FROM subscriptions s
               WHERE s.date_added_utc >= ? AND s.sold_by IN (
                 SELECT TRIM(value) FROM agent_map,
                   json_each('["'||REPLACE(fr_employee_id,',','","')||'"]')
                 WHERE on_roster=1 AND fr_employee_id IS NOT NULL)""",
            (wins[0][0].astimezone(timezone.utc).isoformat(),)).fetchall()

        keys = ("all", "phone", "form", "multiyear", "acv", "aip", "sts", "signed")
        series: dict[str, list] = {k: [] for k in keys}
        for a, b in wins:
            # subs are stored as UTC instants; audit_daily is keyed by Pacific day
            a_iso = a.astimezone(timezone.utc).isoformat()
            b_iso = b.astimezone(timezone.utc).isoformat()
            a_day = a.date().isoformat()
            b_day = b.date().isoformat()
            q, s = conn.execute(
                "SELECT COALESCE(SUM(qualified),0), COALESCE(SUM(sales),0) "
                "FROM audit_daily WHERE date >= ? AND date < ?", (a_day, b_day)).fetchone()
            series["phone"].append(round(100.0 * s / q, 1) if q else None)
            fn = fc = 0
            for e in (events or []):
                if a <= e.first_touch_utc < b and not e.already_customer:
                    fn += 1
                    if e.converted:
                        fc += 1
            series["form"].append(round(100.0 * fc / fn, 1) if fn else None)
            tot, cnv = q + fn, s + fc
            series["all"].append(round(100.0 * cnv / tot, 1) if tot else None)
            win = [(m, cv, ip, sg) for _id, added, m, cv, _st, ip, sg in subs if a_iso <= added < b_iso]
            n = len(win)
            cvs = [cv for _m, cv, _ip, _sg in win if cv and cv > 0]
            ips = [ip for _m, _cv, ip, _sg in win if ip and ip > 0]
            sgs = [sg for _m, _cv, _ip, sg in win if sg is not None]
            series["multiyear"].append(round(100.0 * sum(1 for m, _cv, _ip, _sg in win if m >= 18) / n, 1) if n else None)
            series["acv"].append(round(sum(cvs) / len(cvs)) if cvs else None)
            # avg initial-service price — same counted subs as ACV
            series["aip"].append(round(sum(ips) / len(ips)) if ips else None)
            # signed-agreement % — signed ÷ contract-checked (NULL = not yet checked)
            series["signed"].append(round(100.0 * sum(sgs) / len(sgs), 1) if sgs else None)
            win_sts = [st for (added, st) in sts_rows if added and a_iso <= added < b_iso]
            series["sts"].append(_sts(win_sts).get("sts_pct"))
        return {"weeks": weeks, "series": series}
    finally:
        conn.close()


# Multiyear / contract-value metrics count only subscriptions whose initial
# appointment was serviced ("Completed") or is still scheduled ("Pending").
# Pending naturally falls off on later refreshes if the first service never
# happens — past periods converge to serviced-only, the current week shows
# what agents are selling right now.
_COUNTED_STATUSES = ("Completed", "Pending")
# Upsell sources excluded from EVERY IS#'s / Power Score / Team-Metrics number
# (conversion, ACV, multiyear, subs, AIP, APAY, and their revenue totals). These
# are the exact exclusions in the manager's PestRoutes Sales Report ("Global:
# Mosquito upsell Campaign", "Global: Upsell", "Global: Bounced Check Fee").
# Mosquito upsells / upsells only count as REVENUE on the Revenue tab + Home YTD
# chart (which use their own include-upsells logic and do NOT reference this).
_EXCLUDED_SOURCES = ("MOSQUITO UPSELL CAMPAIGN", "UPSELL", "BOUNCED CHECK FEE")
# Renewals excluded from REVENUE (not new sales) — matches the manager's Sales
# Report exclusions. NOTE: "Inside Sales Renewal" & "Bad Debt Renewal" are kept
# (the report counts those), so this is an exact list, not a %RENEWAL% match.
_RENEWAL_SOURCES = ("ZOLD - Renewal", "Zoldz - Renewal", "Rate Raise Renewal",
                    "Renewal - In Contract", "Renewal - No Contract", "Renewal - Out of Contract")


@app.get("/api/home_daily")
def api_home_daily() -> dict:
    """Per-day breakdown for the Home metric drop-downs: the last 7 COMPLETE
    Pacific days (through yesterday). Forms: net/converted/pct by first-touch
    day. Subs (same counted base as the ACV/Multiyear cards): n, multiyear %,
    ACV, avg initial price by sale day."""
    from datetime import timedelta

    from .periods import PACIFIC
    today_pac = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    days = [(today_pac - timedelta(days=i)).isoformat() for i in range(7, 0, -1)]
    day_set = set(days)

    events, _ = _get_form_events()
    form = {d: {"net": 0, "conv": 0} for d in days}
    for e in (events or []):
        if e.already_customer:
            continue
        d = e.first_touch_utc.astimezone(PACIFIC).date().isoformat()
        if d in day_set:
            form[d]["net"] += 1
            if e.converted:
                form[d]["conv"] += 1

    conn = connect(_settings.db_path)
    try:
        subs = _roster_sub_rows(conn)
    finally:
        conn.close()
    sub = {d: {"n": 0, "my": 0, "cv_sum": 0.0, "cv_n": 0, "ip_sum": 0.0, "ip_n": 0,
               "sg": 0, "sg_n": 0}
           for d in days}
    for _a, added, months, cv, _st, ip, sg in subs:
        d = datetime.fromisoformat(added).astimezone(PACIFIC).date().isoformat()
        if d not in day_set:
            continue
        k = sub[d]
        k["n"] += 1
        if months >= 18:
            k["my"] += 1
        if cv > 0:
            k["cv_sum"] += cv; k["cv_n"] += 1
        if ip > 0:
            k["ip_sum"] += ip; k["ip_n"] += 1
        if sg is not None:
            k["sg_n"] += 1
            if sg:
                k["sg"] += 1

    # VoiceOps quality per single Pacific day. Cache only — the nightly sync warms
    # exactly these 7 days — so the drop-down never waits on the vendor.
    from .voiceops import cached_scores, weighted_quality
    qual = {}
    conn = connect(_settings.db_path)
    try:
        for d in days:
            rows = list(cached_scores(conn, None, d, d).values())
            scored = [r for r in rows if r.get("quality_score") is not None]
            qual[d] = {"pct": weighted_quality(rows),
                       "calls": sum(r.get("call_count") or 0 for r in scored),
                       "agents": len(scored)}
    finally:
        conn.close()

    out = []
    for d in days:
        f, k = form[d], sub[d]
        out.append({
            "date": d,
            "form": {"net": f["net"], "conv": f["conv"],
                     "pct": round(100.0 * f["conv"] / f["net"], 1) if f["net"] else None},
            "subs": k["n"],
            "multiyear_pct": round(100.0 * k["my"] / k["n"], 1) if k["n"] else None,
            "acv": round(k["cv_sum"] / k["cv_n"], 2) if k["cv_n"] else None,
            "aip": round(k["ip_sum"] / k["ip_n"], 2) if k["ip_n"] else None,
            "signed_pct": round(100.0 * k["sg"] / k["sg_n"], 1) if k["sg_n"] else None,
            "quality": qual[d],
        })
    return {"days": out}


@app.get("/api/formleads_export")
def api_formleads_export(start_day: str = "", end_day: str = ""):
    """CSV of NET form leads (deduped, already-customers excluded) whose first
    touch falls in the Pacific date range — the same rows behind the Form
    Conversion tab's Net Leads number. Sold rows are enriched with the matched
    subscription + the customer's name from FieldRoutes. Lead names for UNSOLD
    rows come from the touch feed IF the sourcing app provides them (auto-
    detected); it doesn't capture names yet, so they're blank until it does."""
    import csv as _csv
    import io
    import sqlite3 as _sq
    from datetime import timedelta

    from fastapi.responses import PlainTextResponse

    from .periods import PACIFIC
    from .rangestats import utc_bounds
    if not (start_day and end_day):
        return JSONResponse({"error": "start_day + end_day required"}, status_code=400)
    a_iso, b_iso = utc_bounds(start_day, end_day)
    a_dt = datetime.fromisoformat(a_iso); b_dt = datetime.fromisoformat(b_iso)

    events, _ = _get_form_events()
    net = [e for e in (events or [])
           if a_dt <= e.first_touch_utc < b_dt and not e.already_customer]

    # lead names + pest issue from the touch feed (sourcing app captures them
    # at ingest; columns auto-detected so this degrades gracefully)
    import re as _re

    def _clean_name(v: str) -> str:
        # some gmail-parsed names carry the whole email body — keep line 1 only
        v = (v or "").splitlines()[0].strip()
        return v[:60]

    def _clean_pest(v: str) -> str:
        # strip form-field labels like "what_bugs_do_you_want_to_get_rid_of?: "
        v = (v or "").splitlines()[0].strip()
        v = _re.sub(r"^[\w\s_]+\?:\s*", "", v)
        return v[:80]

    touch_names: dict[str, str] = {}
    touch_pest: dict[str, str] = {}
    try:
        src = _sq.connect(f"file:{_settings.formleads_db}?mode=ro", uri=True)
        cols = {r[1] for r in src.execute("PRAGMA table_info(touches)")}
        name_col = next((c for c in ("name", "lead_name", "full_name") if c in cols), None)
        pest_col = next((c for c in ("pest_issue", "issue", "pest") if c in cols), None)
        sel = [c for c in (name_col, pest_col) if c]
        if sel:
            for row in src.execute(
                f"SELECT phone_e164, email, {', '.join(sel)} FROM touches "
                f"WHERE ({' OR '.join(c + ' IS NOT NULL' for c in sel)}) "
                f"AND occurred_at >= ?",
                ((a_dt - timedelta(days=1)).isoformat(),)):
                ph, em = row[0], row[1]
                vals = dict(zip(sel, row[2:]))
                for k in (ph, em):
                    if not k:
                        continue
                    if name_col and vals.get(name_col):
                        touch_names.setdefault(k, _clean_name(vals[name_col]))
                    if pest_col and vals.get(pest_col):
                        touch_pest.setdefault(k, _clean_pest(vals[pest_col]))
        src.close()
    except Exception:
        pass

    # bulk sold-sub matching: identity key -> [(added_dt, sub row)]
    conn = connect(_settings.db_path)
    try:
        lo = (a_dt - timedelta(hours=2)).isoformat()
        hi = (b_dt + timedelta(days=_settings.form_conversion_window_days)).isoformat()
        by_key: dict[str, list] = {}
        for r in conn.execute(
            """SELECT s.subscription_id, s.customer_id, s.date_added_utc,
                      s.sold_by_name, s.contract_value,
                      s.phone1_e164, s.phone2_e164, c.email
               FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
               WHERE s.initial_status IN ('Completed','Pending')
                 AND s.date_added_utc >= ? AND s.date_added_utc <= ?""", (lo, hi)):
            rec = (datetime.fromisoformat(r["date_added_utc"]), r)
            for k in (r["phone1_e164"], r["phone2_e164"],
                      (r["email"] or "").strip().lower() or None):
                if k:
                    by_key.setdefault(k, []).append(rec)
    finally:
        conn.close()

    window = timedelta(days=_settings.form_conversion_window_days)
    grace = timedelta(hours=2)
    rows = []
    sold_cust_ids = set()
    for e in sorted(net, key=lambda x: x.first_touch_utc):
        keys = list(e.phones) + [m.strip().lower() for m in e.emails]
        sub = None
        for k in keys:
            for added, r in by_key.get(k, ()):
                if (e.first_touch_utc - grace) <= added <= (e.first_touch_utc + window):
                    if sub is None or added < sub[0]:
                        sub = (added, r)
        if sub:
            sold_cust_ids.add(sub[1]["customer_id"])
        local = e.first_touch_utc.astimezone(PACIFIC)
        rows.append({
            "lead_date": local.date().isoformat(),
            "lead_time": local.strftime("%H:%M"),
            "channel": e.channel,
            "sources": " | ".join(sorted(str(x) for x in e.sources)),
            "phones": " | ".join(sorted(e.phones)),
            "email": " | ".join(sorted(e.emails)),
            "submissions": e.raw_count,
            "lead_name": next((touch_names[k] for k in keys if k in touch_names), ""),
            "pest_issue": next((touch_pest[k] for k in keys if k in touch_pest), ""),
            "sold": "YES" if sub else "",
            "sold_date": sub[1]["date_added_utc"][:10] if sub else "",
            "sold_rep": (sub[1]["sold_by_name"] or "") if sub else "",
            "contract_value": sub[1]["contract_value"] if sub else "",
            "customer_id": sub[1]["customer_id"] if sub else "",
        })

    # customer names for sold rows, live from FieldRoutes
    names: dict[str, tuple] = {}
    if sold_cust_ids:
        try:
            from .fieldroutes import FieldRoutesClient
            ids = sorted(sold_cust_ids)
            with FieldRoutesClient(_settings.pestroutes_base_url,
                                   _settings.pestroutes_auth_key,
                                   _settings.pestroutes_auth_token) as fr:
                for i in range(0, len(ids), 200):
                    got = fr.request("customer", "get", {
                        "customerIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
                    for c in got.get("customers") or []:
                        names[str(c.get("customerID"))] = (
                            (c.get("fname") or "").strip(), (c.get("lname") or "").strip())
        except Exception:
            pass  # export still works without names

    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["lead_date", "lead_time", "first_name", "last_name", "phone",
                "email", "pest_issue", "channel", "sold", "sold_date",
                "sold_rep", "contract_value", "fr_customer_id"])
    for r in rows:
        fn, ln = names.get(str(r["customer_id"]), ("", ""))
        if not (fn or ln) and r["lead_name"]:
            parts = r["lead_name"].split(None, 1)
            fn, ln = parts[0], (parts[1] if len(parts) > 1 else "")
        w.writerow([r["lead_date"], r["lead_time"], fn, ln, r["phones"],
                    r["email"], r["pest_issue"], r["channel"], r["sold"],
                    r["sold_date"], r["sold_rep"], r["contract_value"],
                    r["customer_id"]])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="net_form_leads_{start_day}_to_{end_day}.csv"'})


def _roster_sub_rows(conn):
    """Counted subscriptions sold by roster agents: (agent, date_added_utc,
    agreement_length, contract_value, initial_status). soldBy matches ANY of
    the agent's FR employee IDs."""
    fr_to_agent: dict[str, str] = {}
    for r in conn.execute(
        "SELECT genesys_name, fr_employee_id FROM agent_map "
        "WHERE on_roster = 1 AND fr_employee_id IS NOT NULL"
    ):
        for fid in r["fr_employee_id"].split(","):
            fr_to_agent[fid] = r["genesys_name"]
    out = []
    for r in conn.execute(
        "SELECT sold_by, date_added_utc, agreement_length, contract_value, "
        "initial_status, initial_price, signed_agreement FROM subscriptions "
        "WHERE date_added_utc IS NOT NULL AND sold_by != '0' "
        "AND initial_status IN (?, ?) "
        "AND (source IS NULL OR UPPER(source) NOT IN "
        f"({','.join('?' * len(_EXCLUDED_SOURCES))}))",
        _COUNTED_STATUSES + _EXCLUDED_SOURCES
    ):
        agent = fr_to_agent.get(r["sold_by"])
        if agent:
            out.append((agent, r["date_added_utc"], r["agreement_length"] or 0,
                        r["contract_value"] or 0.0, r["initial_status"],
                        r["initial_price"] or 0.0, r["signed_agreement"]))
    return out


def _bucket(months: int) -> str:
    # one-time treatments (agreementLength=0) are "below 12 mo" by policy
    if months < 12:
        return "lt12"
    if months < 18:
        return "m12"
    if months < 24:
        return "m18"
    return "m24"


@app.get("/api/agentsubs")
def api_agentsubs() -> dict:
    """Per-agent per-period subscription metrics: duration buckets, multiyear
    % (18mo+), and average/total contract value."""
    from .periods import month_label, week_label
    conn = connect(_settings.db_path)
    try:
        subs = _roster_sub_rows(conn)
    finally:
        conn.close()

    def blank() -> dict:
        return {"subs": 0, "pending": 0, "lt12": 0, "m12": 0, "m18": 0,
                "m24": 0, "cv_total": 0.0, "cv_n": 0,
                "ip_total": 0.0, "ip_n": 0}

    weekly: dict[str, dict[str, dict]] = {}
    monthly: dict[str, dict[str, dict]] = {}
    for agent, added, months, cv, status, ip in subs:
        dt = datetime.fromisoformat(added)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        for label, table in ((week_label(dt), weekly), (month_label(dt), monthly)):
            b = table.setdefault(label, {}).setdefault(agent, blank())
            b["subs"] += 1
            if status == "Pending":
                b["pending"] += 1
            b[_bucket(months)] += 1
            if cv > 0:
                b["cv_total"] += cv
                b["cv_n"] += 1
            if ip > 0:
                b["ip_total"] += ip
                b["ip_n"] += 1

    def finish(table):
        rows = []
        for label in sorted(table):
            total = blank()
            for agent in sorted(table[label]):
                b = table[label][agent]
                for k in total:
                    total[k] += b[k]
                rows.append({"period": label, "agent": agent, **_derive(b)})
            rows.append({"period": label, "agent": "__TOTAL__", **_derive(total)})
        return rows

    def _derive(b):
        multi = b["m18"] + b["m24"]
        n = b["subs"]
        def pct(x):
            return round(100.0 * x / n, 1) if n else 0.0
        return {**{k: b[k] for k in ("subs", "pending", "lt12", "m12", "m18",
                                     "m24")},
                "lt12_pct": pct(b["lt12"]), "m12_pct": pct(b["m12"]),
                "m18_pct": pct(b["m18"]), "m24_pct": pct(b["m24"]),
                "multiyear_pct": pct(multi),
                "acv": round(b["cv_total"] / b["cv_n"], 2) if b["cv_n"] else 0.0,
                "cv_total": round(b["cv_total"], 2),
                "init_avg": round(b["ip_total"] / b["ip_n"], 2) if b["ip_n"] else 0.0}

    return {"weekly": finish(weekly), "monthly": finish(monthly)}


def _sts(rows) -> dict:
    """Sold-to-serviced: of subscriptions that actually got an initial
    appointment SCHEDULED, how many first services happened.

    'No Appointment' is excluded entirely — the caller bailed before an
    appointment ever existed (an unclosed lead, not a service falloff).
    Pending is scheduled-but-undecided and sits outside the denominator.
    """
    completed = sum(1 for st in rows if st == "Completed")
    pending = sum(1 for st in rows if st == "Pending")
    no_appt = sum(1 for st in rows if st == "No Appointment")
    failed = len(rows) - completed - pending - no_appt  # No Show, Cancelled…
    denom = completed + failed
    return {"sold": len(rows), "serviced": completed, "pending": pending,
            "failed": failed, "no_appt": no_appt,
            "sts_pct": round(100.0 * completed / denom, 1) if denom else None}


def _load_source_maps():
    """(group_of, label_to_provider, dnis_to_provider) for the marketing views.
    Base = the sourcing app's read-only dispute_groups.csv; then our own overrides
    win on top. Shared by the Sources table AND the per-source detail view so
    grouping/attribution is identical in both."""
    import csv as _csv
    import re as _re
    from pathlib import Path as _P

    from .formleads import load_source_names
    group_of: dict[str, str] = {}

    def _load_groups(path: str) -> None:
        p = _P(path)
        if not p.exists():
            return
        with p.open(encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                g = (row.get("group") or "").strip()
                for k in ((row.get("source") or "").strip(),
                          (row.get("provider") or "").strip()):
                    if k and g and not k.startswith("#"):
                        group_of[k.lower()] = g

    _load_groups(_settings.sourcing_groups)          # base (never edited)
    _load_groups(_settings.source_group_overrides)   # our deltas win

    label_to_provider = load_source_names(_settings.sourcing_master)
    dnis_to_provider: dict[str, str] = {}
    mp = _P(_settings.sourcing_master)
    if mp.exists():
        with mp.open(encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                prov = (row.get("Provider / Channel") or "").strip()
                for tok in _re.split(r"[,/;]", row.get("DNIS") or ""):
                    digits = _re.sub(r"\D", "", tok)
                    if len(digits) == 11 and digits.startswith("1"):
                        digits = digits[1:]
                    if len(digits) == 10 and prov:
                        dnis_to_provider[digits] = prov
    return group_of, label_to_provider, dnis_to_provider


# ---- Marketing tab: Call Volume / Form Leads / By Source (marketing.py) ----
@app.get("/api/marketing/volume")
def api_marketing_volume(start_day: str = "", end_day: str = ""):
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return marketing.queue_volume(_settings, date.fromisoformat(start_day), date.fromisoformat(end_day))
    except Exception as e:                         # Genesys down: say so
        logging.getLogger("isadash.marketing").exception("queue volume failed")
        return JSONResponse({"error": f"Couldn't read Genesys: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/forms")
def api_marketing_forms(start_day: str = "", end_day: str = ""):
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, payload = _get_form_events()
    if events is None:
        return JSONResponse({"error": (payload or {}).get("error", "form leads unavailable")}, status_code=502)
    return marketing.forms_weekly(events, date.fromisoformat(start_day), date.fromisoformat(end_day))


@app.get("/api/marketing/bysource")
def api_marketing_bysource(start_day: str = "", end_day: str = ""):
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.by_source(conn, events or [], date.fromisoformat(start_day),
                                   date.fromisoformat(end_day), maps,
                                   lambda label, prov, grp: _channel_for(chan, label, prov, grp))
    finally:
        conn.close()


@app.get("/api/marketing/volume_history")
def api_marketing_volume_history():
    """Year-over-year calls offered per week: Genesys (from April 2023) beside
    the team's manual tracker sheet."""
    from . import marketing
    try:
        return marketing.volume_history(_settings)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("volume history failed")
        return JSONResponse({"error": f"Couldn't build the history: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/forms_history")
def api_marketing_forms_history():
    """Year-over-year net new form leads per week: dashboard beside the tracker."""
    from . import marketing
    events, _ = _get_form_events()
    try:
        return marketing.forms_history(_settings, events or [])
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("forms history failed")
        return JSONResponse({"error": f"Couldn't build the history: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/hourly")
def api_marketing_hourly(start_day: str = "", end_day: str = ""):
    """Lead Volume -> Leads by hour: calls offered + net new form leads per
    Mountain-time hour of the day, against the same number of days before."""
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    try:
        return marketing.hourly(_settings, events or [], date.fromisoformat(start_day), date.fromisoformat(end_day))
    except Exception as e:                         # Genesys down: say so
        logging.getLogger("isadash.marketing").exception("hourly volume failed")
        return JSONResponse({"error": f"Couldn't read Genesys: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/marketing/spend_windows")
def api_marketing_spend_windows():
    """Marketing -> Spend & ROAS: per source, last week / trailing 4 / trailing 12."""
    from . import marketing
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_windows(conn, _settings, events or [], maps,
                                       lambda label, prov, grp: _channel_for(chan, label, prov, grp),
                                       exclude_sources=_RENEWAL_SOURCES)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("spend windows failed")
        return JSONResponse({"error": f"Couldn't build Spend & ROAS: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


@app.get("/api/marketing/spend_chart")
def api_marketing_spend_chart(start_day: str = "", end_day: str = ""):
    """Marketing -> Spend & ROAS: the weekly spend / revenue / CPA % chart for
    a chosen date range (the page shows year to date until one is picked)."""
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_chart(conn, _settings, events or [], date.fromisoformat(start_day),
                                     date.fromisoformat(end_day), maps,
                                     lambda label, prov, grp: _channel_for(chan, label, prov, grp),
                                     exclude_sources=_RENEWAL_SOURCES)
    except Exception as e:
        logging.getLogger("isadash.marketing").exception("spend chart failed")
        return JSONResponse({"error": f"Couldn't build the chart: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


@app.get("/api/marketing/spend")
def api_marketing_spend(start_day: str = "", end_day: str = ""):
    """Marketing -> Spend & ROAS: the spend sheet's weekly spend per source
    joined to the dashboard's leads, sales and revenue by source group."""
    from datetime import date

    from . import marketing
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    events, _ = _get_form_events()
    maps = _load_source_maps()
    chan = _load_source_channels()
    conn = connect(_settings.db_path)
    try:
        return marketing.spend_roas(conn, _settings, events or [], date.fromisoformat(start_day),
                                    date.fromisoformat(end_day), maps,
                                    lambda label, prov, grp: _channel_for(chan, label, prov, grp))
    except Exception as e:                         # sheet unreachable etc.
        logging.getLogger("isadash.marketing").exception("spend & roas failed")
        return JSONResponse({"error": f"Couldn't build Spend & ROAS: {type(e).__name__}: {e}"[:300]}, status_code=502)
    finally:
        conn.close()


def _load_source_channels() -> dict[str, str]:
    """lower-case source name (label, provider or group) -> marketing channel.
    Generated file first, then our overrides win. Comment lines start with #."""
    import csv as _csv
    from pathlib import Path as _P
    out: dict[str, str] = {}
    for path in (_settings.source_channels, _settings.source_channel_overrides):
        p = _P(path)
        if not path or not p.exists():
            continue
        with p.open(encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                name = (row.get("name") or "").strip()
                ch = (row.get("channel") or "").strip()
                if name and ch and not name.startswith("#"):
                    out[name.lower()] = ch
    return out


def _channel_for(chan_of: dict[str, str], *names) -> str:
    """The channel for a subscription's source, trying its label, provider and
    group in that order: an exact row first, then a trailing-* prefix row.
    Nothing matching -> "Other"."""
    keys = [n.strip().lower() for n in names if n]
    for k in keys:
        if k in chan_of:
            return chan_of[k]
    for k in keys:
        for pat, ch in chan_of.items():
            if pat.endswith("*") and k.startswith(pat[:-1]):
                return ch
    return "Other"


@app.get("/api/sources")
def api_sources(request: Request, days: int = 30, grouped: int = 1,
                start_day: str = "", end_day: str = "") -> dict:
    """Marketing view: every metric by lead source. Requires the admin cookie.
    Always returns GROUP rows (all Elocal offices -> Elocal, etc., per the
    sourcing app's dispute_groups.csv + our own overrides). A group made of 2+
    providers carries them as a `children` list for inline expand/collapse.
    (`grouped` is accepted for backward-compat but no longer changes anything.)"""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    import re
    from datetime import timedelta

    from .formleads import load_source_names
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from .rangestats import utc_bounds
        start, range_end = utc_bounds(start_day, end_day)
    else:
        days = max(1, min(days, 400))
        start, range_end = (now - timedelta(days=days)).isoformat(), now.isoformat()

    # grouping + attribution maps (base sourcing-app file + our overrides)
    group_of, label_to_provider, dnis_to_provider = _load_source_maps()

    def provider_for_label(label: str | None) -> str:
        if not label:
            return "(no source)"
        return label_to_provider.get(label.strip(), label.strip())

    rows: dict[str, dict] = {}

    def fresh(prov: str) -> dict:
        return {
            # 📞 Genesys — phone dispositions only (unique callers)
            "source": prov, "leads": set(), "qual_cf": set(), "closed_set": set(),
            # 📝 forms
            "form_net": 0, "form_conv": 0,
            # 🗂 PestRoutes — Pending + Serviced accounts only
            "sold": 0, "pending": 0, "my": 0, "cv_sum": 0.0, "cv_n": 0,
            "init_sum": 0.0, "init_n": 0, "apay": 0, "apay_n": 0,
            # Sold→Serviced: serviced / (serviced + cancelled + no-show)
            "svc": 0, "svc_denom": 0,
        }

    def bucket(prov: str) -> dict:
        return rows.setdefault(prov, fresh(prov))

    # --- phone leads by DNIS ---
    conn = connect(_settings.db_path)
    try:
        for r in conn.execute(
            """SELECT c.ani_e164 ph, c.dnis, c.dispo_group
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
                 AND c.start_utc >= ? AND c.start_utc < ?""", (start, range_end)):
            digits = re.sub(r"\D", "", r["dnis"] or "")
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            prov = dnis_to_provider.get(digits)
            if not prov:
                continue
            b = bucket(prov)
            # Genesys dispositions ONLY — no PestRoutes converted_sub_id lookup,
            # so Ph Leads / Qualified / Closed are a pure phone-disposition funnel.
            if r["dispo_group"] in ("closed", "followup", "unqualified"):
                b["leads"].add(r["ph"])
                if r["dispo_group"] != "unqualified":
                    b["qual_cf"].add(r["ph"])
            if r["dispo_group"] == "closed":
                b["closed_set"].add(r["ph"])

        # --- form leads by source ---
        events, _ = _get_form_events()
        a_dt = datetime.fromisoformat(start)
        b_dt = datetime.fromisoformat(range_end)
        for e in (events or []):
            if not (a_dt <= e.first_touch_utc < b_dt) or e.already_customer:
                continue
            provs = {provider_for_label(s) for s in e.sources} or {"(no source)"}
            for prov in provs:
                b = bucket(prov)
                b["form_net"] += 1
                if e.converted:
                    b["form_conv"] += 1

        # --- subscriptions by source (all sellers — marketing view) ---
        # Count ONLY Pending + Serviced(Completed) by subscription status. Never-
        # scheduled ('No Appointment') and failed (No Show / Cancelled / Not
        # Serviced) don't count anywhere in the PestRoutes section.
        for r in conn.execute(
            """SELECT s.source, s.initial_status, s.agreement_length,
                      s.contract_value, s.initial_price, c.card_on_file
               FROM subscriptions s
               LEFT JOIN customers c ON c.customer_id = s.customer_id
               WHERE s.date_added_utc >= ? AND s.date_added_utc < ?""",
            (start, range_end)):
            st = r["initial_status"]
            # 'No Appointment' (never scheduled) and anything undecided beyond the
            # ones below never count on this table
            if st not in ("Completed", "Pending", "Cancelled", "No Show"):
                continue
            b = bucket(provider_for_label(r["source"]))
            # Sold→Serviced: of subs whose first service was DECIDED (serviced,
            # cancelled, or no-show), the share that got serviced
            if st in ("Completed", "Cancelled", "No Show"):
                b["svc_denom"] += 1
                if st == "Completed":
                    b["svc"] += 1
            # every other PestRoutes metric is Pending + Serviced only
            if st not in ("Completed", "Pending"):
                continue
            b["sold"] += 1
            if st == "Pending":
                b["pending"] += 1
            if (r["agreement_length"] or 0) >= 18:
                b["my"] += 1
            if r["contract_value"]:
                b["cv_sum"] += r["contract_value"]
                b["cv_n"] += 1
            if r["initial_price"]:
                b["init_sum"] += r["initial_price"]
                b["init_n"] += 1
            if r["card_on_file"] is not None:
                b["apay_n"] += 1
                if r["card_on_file"]:
                    b["apay"] += 1
    finally:
        conn.close()

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    def finalize(name: str, b: dict) -> dict:
        leads = len(b["leads"]); qual = len(b["qual_cf"]); closed = len(b["closed_set"])
        return {
            "source": name,
            # 📞 Genesys — phone dispositions only
            "leads": leads, "qual": qual, "qual_pct": pct(qual, leads),
            "closed": closed, "close_pct": pct(closed, qual),
            # 📝 Forms
            "form_net": b["form_net"], "form_conv": b["form_conv"],
            "form_pct": pct(b["form_conv"], b["form_net"]),
            # 🗂 PestRoutes — Pending + Serviced accounts only
            "subs_sold": b["sold"], "sts_pct": pct(b["svc"], b["svc_denom"]),
            "pending": b["pending"], "multiyear_pct": pct(b["my"], b["sold"]),
            "acv": round(b["cv_sum"] / b["cv_n"], 2) if b["cv_n"] else None,
            "init_avg": round(b["init_sum"] / b["init_n"], 2) if b["init_n"] else None,
            "apay_pct": pct(b["apay"], b["apay_n"]),
        }

    # roll the per-provider buckets up into their group, keeping each group's
    # providers as expandable children (a group with 2+ providers gets an arrow).
    _SETS = ("leads", "qual_cf", "closed_set")
    _NUMS = ("form_net", "form_conv", "sold", "pending", "my", "cv_sum", "cv_n",
             "init_sum", "init_n", "apay", "apay_n", "svc", "svc_denom")
    from collections import defaultdict as _dd
    by_group: dict[str, list] = _dd(list)
    for prov, b in rows.items():
        by_group[group_of.get(prov.lower(), prov)].append((prov, b))
    vol = lambda r: -(r["leads"] + r["form_net"] + r["subs_sold"])

    out = []
    for g, members in by_group.items():
        if len(members) == 1:
            out.append(finalize(g, members[0][1]))
            continue
        comb = fresh(g)
        for _, b in members:
            for k in _SETS:
                comb[k] |= b[k]
            for k in _NUMS:
                comb[k] += b[k]
        row = finalize(g, comb)
        row["children"] = sorted((finalize(p, b) for p, b in members), key=vol)
        out.append(row)
    out.sort(key=vol)
    return {"days": days, "rows": out}


def _norm_dnis(raw) -> str:
    import re
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _source_dnis(source: str) -> set:
    """DNIS digits whose provider belongs to `source` (a group or a provider)."""
    group_of, _lbl, dnis_to_provider = _load_source_maps()
    return {d for d, prov in dnis_to_provider.items()
            if prov and (prov == source or group_of.get(prov.lower(), prov) == source)}


def _dispo_counts(conn, src_dnis: set, a_iso: str, b_iso: str) -> dict:
    """{closed|followup|unqualified: {total, items:[{name,count}]}} for a source's
    calls in [a_iso, b_iso), broken down by wrap-up disposition."""
    from collections import defaultdict as _dd
    dispo = {g: _dd(int) for g in ("closed", "followup", "unqualified")}
    for r in conn.execute(
        """SELECT c.dnis, c.dispo_group, c.wrapup_name
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
        (a_iso, b_iso)):
        if r["dispo_group"] not in dispo or _norm_dnis(r["dnis"]) not in src_dnis:
            continue
        dispo[r["dispo_group"]][r["wrapup_name"] or "(no disposition)"] += 1
    return {
        g: {"total": sum(w.values()),
            "items": sorted(({"name": k, "count": v} for k, v in w.items()),
                            key=lambda x: -x["count"])}
        for g, w in dispo.items()
    }


@app.get("/api/source_dispositions")
def api_source_dispositions(request: Request, source: str = "",
                            start_day: str = "", end_day: str = "") -> dict:
    """Disposition breakdown for ONE source over a chosen Pacific date range —
    powers the range picker on the detail overlay. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    if not (source and start_day and end_day):
        return JSONResponse({"error": "source + range required"}, status_code=400)
    from .rangestats import utc_bounds
    a_iso, b_iso = utc_bounds(start_day, end_day)
    conn = connect(_settings.db_path)
    try:
        dispositions = _dispo_counts(conn, _source_dnis(source), a_iso, b_iso)
    finally:
        conn.close()
    return {"source": source, "start": start_day, "end": end_day,
            "dispositions": dispositions}


@app.get("/api/source_detail")
def api_source_detail(request: Request, source: str = "") -> dict:
    """Deep-dive for ONE source (a group like 'Elocal' OR a single provider like
    'Elocal San Jose'): last-week Genesys disposition breakdown + 13 weekly
    Sun-Sat series (call/form volume and PestRoutes economics). Admin only. Uses
    fixed windows (last complete week + trailing 13 weeks), not the page range."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    if not source:
        return JSONResponse({"error": "source required"}, status_code=400)

    from datetime import timedelta

    from .periods import week_start
    group_of, label_to_provider, dnis_to_provider = _load_source_maps()

    # a provider belongs to this view if it IS the requested name (child click)
    # or rolls up into it (group click)
    def prov_matches(prov: str) -> bool:
        return bool(prov) and (prov == source or group_of.get(prov.lower(), prov) == source)

    def label_matches(label) -> bool:
        lbl = (label or "").strip()
        return prov_matches(label_to_provider.get(lbl, lbl) if lbl else "(no source)")

    src_dnis = {d for d, prov in dnis_to_provider.items() if prov_matches(prov)}

    # trailing 13 complete Sun-Sat weeks (current partial week excluded), like Home
    now = datetime.now(timezone.utc)
    this_week = week_start(now)
    N = 13
    wins = [(this_week - timedelta(weeks=N - i),
             this_week - timedelta(weeks=N - 1 - i)) for i in range(N)]
    weeks = [a.date().isoformat() for a, _b in wins]
    win_utc = [(a.astimezone(timezone.utc).isoformat(),
                b.astimezone(timezone.utc).isoformat()) for a, b in wins]
    range_start, range_end = win_utc[0][0], win_utc[-1][1]

    def wk(ts_iso: str) -> int:
        for i, (a, b) in enumerate(win_utc):
            if a <= ts_iso < b:
                return i
        return -1

    callers = [set() for _ in range(N)]       # unique dispositioned callers / week
    qual_callers = [set() for _ in range(N)]  # unique Closed/Follow-Up callers / week
    closed_callers = [set() for _ in range(N)]  # unique Closed callers / week
    forms = [0] * N
    revenue = [0.0] * N; sub_cnt = [0] * N; my_cnt = [0] * N
    cv_sum = [0.0] * N; cv_n = [0] * N; ip_sum = [0.0] * N; ip_n = [0] * N
    sold = [0] * N; serviced = [0] * N
    svc = [0] * N; svc_denom = [0] * N  # Sold->Serviced: serviced / decided outcomes

    conn = connect(_settings.db_path)
    try:
        # UNIQUE CALLERS per week (a caller who calls 3x counts once), matching the
        # Sources table's "Ph Leads"/"Qual" basis — dispositioned callers only.
        for r in conn.execute(
            """SELECT c.ani_e164 ph, c.dnis, c.dispo_group, c.start_utc
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
                 AND c.start_utc >= ? AND c.start_utc < ?""",
            (range_start, range_end)):
            if r["dispo_group"] not in ("closed", "followup", "unqualified"):
                continue
            if _norm_dnis(r["dnis"]) not in src_dnis:
                continue
            i = wk(r["start_utc"])
            if i < 0:
                continue
            callers[i].add(r["ph"])
            if r["dispo_group"] in ("closed", "followup"):
                qual_callers[i].add(r["ph"])
            if r["dispo_group"] == "closed":
                closed_callers[i].add(r["ph"])
        total_calls = [len(s) for s in callers]
        qual_calls = [len(s) for s in qual_callers]

        events, _ = _get_form_events()
        for e in (events or []):
            if e.already_customer or not any(label_matches(s) for s in e.sources):
                continue
            i = wk(e.first_touch_utc.astimezone(timezone.utc).isoformat())
            if i >= 0:
                forms[i] += 1

        for r in conn.execute(
            """SELECT s.source, s.initial_status, s.agreement_length,
                      s.contract_value, s.initial_price, s.date_added_utc
               FROM subscriptions s
               WHERE s.date_added_utc >= ? AND s.date_added_utc < ?""",
            (range_start, range_end)):
            st = r["initial_status"]
            if not label_matches(r["source"]):
                continue
            i = wk(r["date_added_utc"])
            if i < 0:
                continue
            # Sold->Serviced ratio: serviced / (serviced + cancelled + no-show),
            # by sale week — same definition as the table's "Sold->Svc%".
            if st in ("Completed", "Cancelled", "No Show"):
                svc_denom[i] += 1
                if st == "Completed":
                    svc[i] += 1
            if st not in ("Completed", "Pending"):
                continue
            sub_cnt[i] += 1; sold[i] += 1
            if st == "Completed":
                serviced[i] += 1
            if (r["agreement_length"] or 0) >= 18:
                my_cnt[i] += 1
            cv = r["contract_value"] or 0
            if cv:
                revenue[i] += cv; cv_sum[i] += cv; cv_n[i] += 1
            if r["initial_price"]:
                ip_sum[i] += r["initial_price"]; ip_n[i] += 1

        # last-week disposition breakdown (wrap-ups grouped by category)
        dispositions = _dispo_counts(conn, src_dnis, *win_utc[-1])
    finally:
        conn.close()

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    series = {
        "total_calls": total_calls, "qual_calls": qual_calls, "forms": forms,
        "qual_leads": [qual_calls[i] + forms[i] for i in range(N)],
        "qual_pct": [pct(qual_calls[i], total_calls[i]) for i in range(N)],
        "close_pct": [pct(len(closed_callers[i]), qual_calls[i]) for i in range(N)],
        "revenue": [round(x, 2) for x in revenue],
        "multiyear_pct": [pct(my_cnt[i], sub_cnt[i]) for i in range(N)],
        "acv": [round(cv_sum[i] / cv_n[i]) if cv_n[i] else None for i in range(N)],
        "avg_init": [round(ip_sum[i] / ip_n[i]) if ip_n[i] else None for i in range(N)],
        "sold": sold, "serviced": serviced,
        "sts_pct": [pct(svc[i], svc_denom[i]) for i in range(N)],
    }
    return {
        "source": source, "weeks": weeks,
        "last_week": {"start": wins[-1][0].date().isoformat(),
                      "end": (wins[-1][1] - timedelta(days=1)).date().isoformat()},
        "dispositions": dispositions, "series": series,
        "has_calls": sum(total_calls) > 0, "has_forms": sum(forms) > 0,
    }


@app.get("/api/config")
def api_config() -> dict:
    """Scorecard tiers + power-score weights. Readable by any signed-in user
    (both the scorecard and power-score views need them to render)."""
    conn = connect(_settings.db_path)
    try:
        return _load_config(conn)
    finally:
        conn.close()


@app.post("/api/config")
def api_config_update(body: dict, request: Request):
    """Save edited tiers / power-score weights. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        if "scorecard_tiers" in body:
            _save_config(conn, "scorecard_tiers", _validate_tiers(body["scorecard_tiers"]))
        if "power_score" in body:
            _save_config(conn, "power_score", _validate_power(body["power_score"]))
        return _load_config(conn)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/config/reset")
def api_config_reset(body: dict, request: Request):
    """Restore a section to its built-in defaults. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    key = body.get("key")
    if key not in ("scorecard_tiers", "power_score"):
        return JSONResponse({"error": "unknown key"}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        conn.execute("DELETE FROM app_config WHERE key = ?", (key,))
        conn.commit()
        return _load_config(conn)
    finally:
        conn.close()


@app.post("/api/sync")
def api_sync_start(request: Request) -> dict:
    # Admins: unlimited. Managers (app password): allowed, but only when no
    # sync has STARTED in the last hour (any trigger — nightly timer, admin,
    # or manager — counts; data younger than an hour doesn't need a re-pull).
    # The FieldRoutes client self-paces at 300 req/min (1/3 of the shared
    # tenant cap) and start_sync refuses concurrent runs, so this cadence is
    # comfortably inside rate limits.
    if not _admin_ok(request):
        import json as _j
        from .store import get_config
        conn = connect(_settings.db_path)
        try:
            last_raw = get_config(conn, "last_sync_started")
        finally:
            conn.close()
        if last_raw:
            try:
                last = datetime.fromisoformat(_j.loads(last_raw))
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            except (ValueError, TypeError):
                elapsed = 1e9
            if elapsed < 3600:
                mins = max(1, int((3600 - elapsed) // 60) + 1)
                ago = max(1, int(elapsed // 60))
                return JSONResponse(
                    {"error": "cooldown", "retry_minutes": mins,
                     "message": f"Data was synced {ago} min ago — next sync "
                                f"available in {mins} min."},
                    status_code=429)
    from .sync import start_sync
    started = start_sync(_settings)
    return {"started": started}


@app.get("/api/sync/status")
def api_sync_status() -> dict:
    from .sync import STATE
    return {k: v for k, v in STATE.items()}


@app.get("/api/range")
def api_range(start: str, end: str) -> dict:
    from .rangestats import range_stats, utc_bounds
    conn = connect(_settings.db_path)
    try:
        payload = range_stats(conn, start, end, _EXCLUDED_SOURCES,
                              _audit_end_utc(conn))
    finally:
        conn.close()
    # team-level form-lead funnel for the same range
    events, _ = _get_form_events()
    a, b = utc_bounds(start, end)
    a_dt = datetime.fromisoformat(a)
    b_dt = datetime.fromisoformat(b)
    f = {"forms": 0, "deduped": 0, "existing": 0, "net": 0, "converted": 0}
    for e in (events or []):
        if not (a_dt <= e.first_touch_utc < b_dt):
            continue
        f["forms"] += e.raw_count       # raw submissions
        f["deduped"] += 1               # unique lead events
        if e.already_customer:
            f["existing"] += 1
            continue
        f["net"] += 1
        if e.converted:
            f["converted"] += 1
    f["conv_pct"] = round(100.0 * f["converted"] / f["net"], 1) if f["net"] else 0.0
    payload["forms"] = f
    return payload


@app.get("/api/export")
def api_export(dataset: str, start: str, end: str):
    """CSV download of the RECORDS behind a metric for a date range.
    dataset: leads | accounts | adjustments"""
    import csv
    import io

    from fastapi.responses import Response

    from .rangestats import export_accounts, export_adjustments, export_leads
    conn = connect(_settings.db_path)
    try:
        if dataset == "leads":
            rows = export_leads(conn, start, end)
        elif dataset == "accounts":
            rows = export_accounts(conn, start, end, _EXCLUDED_SOURCES)
        elif dataset == "adjustments":
            rows = export_adjustments(conn, start, end, _audit_end_utc(conn))
        else:
            return Response("unknown dataset", status_code=400)
    finally:
        conn.close()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    else:
        buf.write("no records in range\n")
    fname = f"{dataset}_{start}_to_{end}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/leaked")
def api_leaked(days: int = 30, start_day: str = "", end_day: str = "") -> dict:
    """Leaked-leads report: callers an agent didn't pitch (marked unqualified
    or other) who bought from a different seller within 14 days."""
    from datetime import timedelta

    from .adjust import adjustment_stats
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from .rangestats import utc_bounds
        start, end = utc_bounds(start_day, end_day)
    else:
        days = max(1, min(days, 400))
        start, end = (now - timedelta(days=days)).isoformat(), now.isoformat()
    conn = connect(_settings.db_path)
    try:
        adj = adjustment_stats(conn, start, end, detail=True)
    finally:
        conn.close()
    summary = [{"agent": a, "leaked": s["leaked"], "cc_leaked": s["cc_leaked"],
                "hidden": s["hidden"]}
               for a, s in sorted(adj.items(), key=lambda kv: -kv[1]["leaked"])
               if a != "__TEAM__"]
    rows = [r for s in adj.values() for r in s["rows"]]
    rows.sort(key=lambda r: r["call_date"], reverse=True)
    return {"days": days, "summary": summary, "rows": rows}


# With no dates given the window is "the last N days up to this minute", which
# drifts with the clock - keep that form only briefly. Explicit dates are saved
# until the data changes.
@app.get("/api/scorecard")
@cache.cached(ttl=lambda a: None if (a.get("start_day") and a.get("end_day")) else 300)
def api_scorecard(start_day: str = "", end_day: str = "", days: int = 30,
                  mode: str = "sold") -> dict:
    """Per-agent pay-period scorecard (the agents flagged scorecard=1).
    mode='sold'    -> subs whose DATE SOLD is in range (Completed/Pending).
    mode='serviced'-> subs whose INITIAL SERVICE DATE is in range — for past
                     ranges this leaves only serviced accounts (pending ones
                     resolve to serviced or cancel out).
    Conversion is the audit sheet's sales/qualified (always by day range)."""
    from datetime import timedelta

    from .rangestats import utc_bounds
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        a, b = utc_bounds(start_day, end_day)
        sd, ed = start_day, end_day
    else:
        days = max(1, min(days, 400))
        a, b = (now - timedelta(days=days)).isoformat(), now.isoformat()
        sd, ed = a[:10], now.date().isoformat()
    serviced_mode = mode == "serviced"

    conn = connect(_settings.db_path)
    try:
        # scorecard agents + their FR employee id set; power-score flag too
        agents: dict[str, set] = {}
        powerscore_set: set = set()
        team_of: dict[str, str] = {}
        gid_of: dict[str, str] = {}
        for r in conn.execute(
            "SELECT genesys_name, genesys_user_id, fr_employee_id, powerscore, team FROM agent_map "
            "WHERE scorecard = 1 AND fr_employee_id IS NOT NULL"):
            agents[r["genesys_name"]] = set(r["fr_employee_id"].split(","))
            gid_of[r["genesys_name"]] = r["genesys_user_id"] or ""
            team_of[r["genesys_name"]] = r["team"] or ""
            if r["powerscore"]:
                powerscore_set.add(r["genesys_name"])
        fr_to_agent = {fid: name for name, ids in agents.items() for fid in ids}

        stat = {name: {"subs": 0, "m18": 0, "m24": 0, "cv": 0.0, "cv_n": 0,
                       "rev_cv": 0.0, "ip": 0.0, "ip_n": 0, "apay": 0, "apay_n": 0,
                       "sg": 0, "sg_n": 0, "recurring": 0, "monthly": 0, "inb": 0}
                for name in agents}
        own_inbound = _own_inbound_subs(conn)     # sales tied to the agent's own inbound call

        mosq = {s.upper() for s in _EXCLUDED_SOURCES}
        if serviced_mode:
            date_clause = "s.initial_service_date >= ? AND s.initial_service_date <= ?"
            date_args = (sd, ed)
        else:
            date_clause = "s.date_added_utc >= ? AND s.date_added_utc < ?"
            date_args = (a, b)
        # Pull EVERY source; split per-metric: REVENUE counts mosquito/upsells
        # but not renewals; ACV/multiyear/subs exclude mosquito (current rule).
        for r in conn.execute(
            f"""SELECT s.subscription_id, s.sold_by, s.agreement_length, s.contract_value,
                       s.initial_price, s.source, s.signed_agreement,
                       s.billing_frequency, s.service_frequency, c.card_on_file
                FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
                WHERE {date_clause}
                  AND s.initial_status IN ('Completed','Pending')""",
            date_args):
            name = fr_to_agent.get(r["sold_by"])
            if not name:
                continue
            k = stat[name]
            src = r["source"] or ""
            cv = r["contract_value"] or 0
            if cv and src not in _RENEWAL_SOURCES:
                k["rev_cv"] += cv            # revenue: mosquito in, renewals out
            if src.upper() in mosq:
                continue                     # everything below excludes mosquito
            k["subs"] += 1
            if str(r["subscription_id"]) in own_inbound.get(gid_of.get(name, ""), ()):
                k["inb"] += 1
            months = r["agreement_length"] or 0
            if months >= 24:
                k["m24"] += 1
            elif months >= 18:
                k["m18"] += 1
            if cv:
                k["cv"] += cv; k["cv_n"] += 1
            if r["initial_price"]:
                k["ip"] += r["initial_price"]; k["ip_n"] += 1
            if r["card_on_file"] is not None:
                k["apay_n"] += 1
                if r["card_on_file"]:
                    k["apay"] += 1
            if r["signed_agreement"] is not None:
                k["sg_n"] += 1
                if r["signed_agreement"]:
                    k["sg"] += 1
            sf = r["service_frequency"]
            if sf is not None and sf != -1:  # recurring (not one-time)
                k["recurring"] += 1
                bf = r["billing_frequency"]
                if bf == 30 or (bf == -1 and sf == 30):
                    k["monthly"] += 1

        # sold -> serviced per agent: same dates and upsell exclusions as the
        # rest of the sheet, but EVERY first-service status, since the ratio
        # is serviced / (serviced + cancelled + no-show)
        sts_by: dict[str, list] = {name: [] for name in agents}
        for r in conn.execute(
            f"""SELECT s.sold_by, s.initial_status, s.source FROM subscriptions s
                WHERE {date_clause}""", date_args):
            name = fr_to_agent.get(r["sold_by"])
            if name and (r["source"] or "").upper() not in mosq:
                sts_by[name].append(r["initial_status"])

        # audit-sheet conversion per agent
        conv = {name: [0, 0] for name in agents}  # [qual, sales]
        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) sa FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent", (sd, ed)):
            if r["agent"] in conv:
                conv[r["agent"]] = [r["q"], r["sa"]]

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        rows = []
        for name in sorted(agents):
            k = stat[name]; q, sa = conv[name]
            n = k["subs"]
            my18 = k["m18"]; my24 = k["m24"]
            rows.append({
                "agent": name,
                "conversion_pct": pct(sa, q),
                "call_grade": None,   # no data source in the dashboard
                "acv": round(k["cv"] / k["cv_n"], 0) if k["cv_n"] else None,
                "multi_total_pct": pct(my18 + my24, n),
                "multi24_pct": pct(my24, n),
                # IS#'s / Power Score revenue: upsells EXCLUDED (renewals kept,
                # matching the manager's report). rev_all keeps upsells in and
                # renewals out — used only by the Home YTD table.
                "rev": round(k["cv"], 2),
                "rev_all": round(k["rev_cv"], 2),
                "apay_pct": pct(k["apay"], k["apay_n"]),
                "multi18_pct": pct(my18, n),
                "aip": round(k["ip"] / k["ip_n"], 2) if k["ip_n"] else None,
                "signed_pct": pct(k["sg"], k["sg_n"]),
                "monthly_pct": pct(k["monthly"], k["recurring"]),
                "subs": n,
                # Total Sales NOT tied to an inbound call this agent took (see
                # _own_inbound_subs) — the outbound / other-channel count
                "outbound_subs": n - k["inb"],
                "sts_pct": _sts(sts_by[name])["sts_pct"],
                "team": team_of.get(name, ""),
                "powerscore": name in powerscore_set,
            })
        # the team as one pooled book of business (every sub / every audited
        # call counted once) — what the agent page compares each rep against.
        # Revenue is the average per rep who had any counted subs.
        tot = {f: sum(k[f] for k in stat.values()) for f in next(iter(stat.values()), {})}
        tq = sum(v[0] or 0 for v in conv.values())
        ts = sum(v[1] or 0 for v in conv.values())
        team = None
        if tot:
            sellers = sum(1 for k in stat.values() if k["subs"])
            tn = tot["subs"]
            team = {
                "agent": "Team average",
                "conversion_pct": pct(ts, tq),
                "call_grade": None,
                "acv": round(tot["cv"] / tot["cv_n"], 0) if tot["cv_n"] else None,
                "multi_total_pct": pct(tot["m18"] + tot["m24"], tn),
                "multi24_pct": pct(tot["m24"], tn),
                "rev": round(tot["cv"] / sellers, 2) if sellers else None,
                "apay_pct": pct(tot["apay"], tot["apay_n"]),
                "multi18_pct": pct(tot["m18"], tn),
                "aip": round(tot["ip"] / tot["ip_n"], 2) if tot["ip_n"] else None,
                "signed_pct": pct(tot["sg"], tot["sg_n"]),
                "monthly_pct": pct(tot["monthly"], tot["recurring"]),
                "subs": round(tn / sellers, 1) if sellers else None,
                "outbound_subs": round((tn - tot["inb"]) / sellers, 1) if sellers else None,
                "sts_pct": _sts([st for v in sts_by.values() for st in v])["sts_pct"],
            }
        return {"start": sd, "end": ed, "rows": rows, "team": team,
                "tiers": _load_config(conn)["scorecard_tiers"]}
    finally:
        conn.close()


def _scorecard_calls(start_day: str, end_day: str) -> dict:
    """Genesys call handling per IS #'s agent for a Pacific day range: inbound
    calls taken, calls that rang them and weren't answered, average talk and
    average after-call work. Its own feed (like quality) so the sheet renders
    from the database first and these columns fill in a moment later."""
    from datetime import date

    from . import mastermetric
    from .periods import PACIFIC
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    a, b = date.fromisoformat(start_day), min(date.fromisoformat(end_day), today)
    conn = connect(_settings.db_path)
    try:
        ids = {r["genesys_name"]: r["genesys_user_id"] for r in conn.execute(
            "SELECT genesys_name, genesys_user_id FROM agent_map "
            "WHERE scorecard = 1 AND genesys_name IS NOT NULL")}
    finally:
        conn.close()
    gen = mastermetric.genesys_by_user(_settings, a, b) if a <= b else {}
    agents, tot, reps = {}, {"answered": 0, "missed": 0, "talk_ms": 0, "talk_n": 0,
                             "acw_ms": 0, "acw_n": 0}, 0

    def shape(g, answered, missed):
        return {"calls_taken": answered, "calls_missed": missed,
                # answered for every one missed; blank when nothing was missed
                "ans_missed": round(g["answered"] / g["missed"], 2) if g["missed"] else None,
                "avg_talk_s": round(g["talk_ms"] / g["talk_n"] / 1000) if g["talk_n"] else None,
                "avg_acw_s": round(g["acw_ms"] / g["acw_n"] / 1000) if g["acw_n"] else None}
    for name, uid in ids.items():
        g = gen.get(uid or "")
        if not g or not (g["answered"] or g["missed"]):
            continue
        agents[name] = shape(g, g["answered"], g["missed"])
        reps += 1
        for k in tot:
            tot[k] += g[k]
    # team: talk/ACW pooled across every call; taken/missed as the average per rep
    team = shape(tot, round(tot["answered"] / reps, 1), round(tot["missed"] / reps, 1)) if reps else None
    return {"start": start_day, "end": end_day, "agents": agents, "team": team}


def _own_inbound_subs(conn) -> dict[str, set[str]]:
    """{genesys_user_id: {subscription_id}} — every sale the dashboard's call
    matcher (match.run_matcher, re-run on every sync) tied to an inbound call
    THAT agent took: the caller's number equals the account's phone, the sale is
    in the agent's own name, and it was added within the conversion window
    (14 days) after the call. The earliest qualifying call gets the credit.
    It's a floor: a customer who called from one number and signed up under
    another isn't matched, so "other / outbound" is a ceiling."""
    out: dict[str, set[str]] = {}
    for r in conn.execute(
            "SELECT agent_user_id, converted_sub_id FROM calls "
            "WHERE converted_sub_id IS NOT NULL AND converted_sub_id != ''"):
        out.setdefault(r["agent_user_id"] or "", set()).add(str(r["converted_sub_id"]))
    return out


def _inbound_split(start_day: str, end_day: str) -> dict:
    """Agent Metrics -> Inbound vs Other. Per scorecard agent, over sales
    dated in the range: revenue + count from the agent's OWN inbound calls
    (_own_inbound_subs) vs everything else. Counts EVERY Completed/Pending
    subscription with contract value (upsells, mosquito and renewals all
    included — Dallin, 2026-10-06), credited by FieldRoutes employee id like
    the IS #'s tab. Nothing is stored: it is computed from the matcher's
    columns, so any range the call history covers can be viewed."""
    from datetime import timedelta

    from .rangestats import utc_bounds
    a, b = utc_bounds(start_day, end_day)
    conn = connect(_settings.db_path)
    try:
        agents: dict[str, set] = {}
        gid_of: dict[str, str] = {}
        for r in conn.execute(
                "SELECT genesys_name, genesys_user_id, fr_employee_id FROM agent_map "
                "WHERE scorecard = 1 AND fr_employee_id IS NOT NULL"):
            agents[r["genesys_name"]] = set(r["fr_employee_id"].split(","))
            gid_of[r["genesys_name"]] = r["genesys_user_id"] or ""
        fr_to_agent = {fid: n for n, ids in agents.items() for fid in ids}
        own = _own_inbound_subs(conn)
        stat = {n: {"agent": n, "total": 0.0, "inbound": 0.0, "n_subs": 0, "n_inbound": 0}
                for n in agents}
        for r in conn.execute(
                """SELECT subscription_id, sold_by, contract_value FROM subscriptions
                   WHERE date_added_utc >= ? AND date_added_utc < ?
                     AND initial_status IN ('Completed','Pending')
                     AND contract_value IS NOT NULL AND contract_value > 0""", (a, b)):
            name = fr_to_agent.get(r["sold_by"])
            if not name:
                continue
            k = stat[name]
            cv = float(r["contract_value"])
            k["total"] += cv
            k["n_subs"] += 1
            if str(r["subscription_id"]) in own.get(gid_of[name], ()):
                k["inbound"] += cv
                k["n_inbound"] += 1
        first_call = conn.execute("SELECT MIN(start_utc) FROM calls").fetchone()[0]
    finally:
        conn.close()

    def finish(k: dict) -> dict:
        k["other"] = round(k["total"] - k["inbound"], 2)
        k["n_other"] = k["n_subs"] - k["n_inbound"]
        k["pct_inbound"] = round(100.0 * k["inbound"] / k["total"], 1) if k["total"] else None
        k["total"] = round(k["total"], 2)
        k["inbound"] = round(k["inbound"], 2)
        return k
    rows = sorted((finish(k) for k in stat.values() if k["n_subs"]), key=lambda x: -x["total"])
    team = finish({"agent": "Team", "total": sum(k["total"] for k in rows),
                   "inbound": sum(k["inbound"] for k in rows),
                   "n_subs": sum(k["n_subs"] for k in rows),
                   "n_inbound": sum(k["n_inbound"] for k in rows)}) if rows else None
    notes = []
    if first_call:
        # a sale can only be matched to a call up to 14 days before it
        cov = (datetime.fromisoformat(first_call.replace("Z", "+00:00"))
               + timedelta(days=_settings.conversion_window_days)).date().isoformat()
        if start_day < cov:
            notes.append(f"Call history on file starts {first_call[:10]}, so sales before {cov} "
                         "can't be matched to a call and all read as “other”.")
    return {"start": start_day, "end": end_day, "rows": rows, "team": team, "notes": notes,
            "calls_from": first_call[:10] if first_call else None}


@app.get("/api/inbound_split")
def api_inbound_split(start_day: str = "", end_day: str = ""):
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    return _inbound_split(start_day, end_day)


@app.get("/api/my/inbound")
def api_my_inbound(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own Inbound vs Other row plus the team row."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    d = _inbound_split(start_day, end_day)
    return {"start": d["start"], "end": d["end"], "agent": name,
            "rows": [r for r in d["rows"] if r["agent"] == name], "team": d["team"],
            "notes": d["notes"]}


@app.get("/api/scorecard_calls")
def api_scorecard_calls(start_day: str = "", end_day: str = ""):
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return _scorecard_calls(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.scorecard").exception("scorecard calls failed")
        return JSONResponse({"error": f"Genesys didn't answer ({type(e).__name__})."}, status_code=502)


@app.get("/api/my/schedule")
def api_my_schedule(request: Request, as_agent: str = ""):
    """The signed-in agent's shifts: last week, this week and every week the
    schedule sheet has after that."""
    from . import schedule
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        # match the sheet's short names against the tracked roster only (every
        # Genesys user would make "Aaron" or "Alex" ambiguous), plus whoever is asking
        agents = [r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL "
            "AND (scorecard = 1 OR genesys_name = ?)", (name,))]
    finally:
        conn.close()
    try:
        from . import shiftplan, timeoff
        conn = connect(_settings.db_path)
        try:
            dbw = shiftplan.published_weeks(conn)
            off = timeoff.approved_days(conn)
        finally:
            conn.close()
        return schedule.my_schedule(_settings, name, agents, db_weeks=dbw, time_off=off)
    except Exception as e:
        logging.getLogger("isadash.schedule").exception("schedule failed")
        return JSONResponse({"error": f"Couldn't read the schedule sheet: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/my/schedule_day")
def api_my_schedule_day(request: Request, day: str = "", as_agent: str = ""):
    """Who is on (and off) on one day — for coverage and shift swaps. Any
    signed-in agent may look; names are the tracked roster's."""
    from datetime import date

    from . import schedule
    if not _my_agent(request, as_agent):
        return JSONResponse(_NO_AGENT, status_code=403)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "bad day"}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        agents = [r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL AND scorecard = 1")]
    finally:
        conn.close()
    try:
        from . import shiftplan, timeoff
        conn = connect(_settings.db_path)
        try:
            dbw = shiftplan.published_weeks(conn)
            off = timeoff.approved_days(conn, d, d)
        finally:
            conn.close()
        return schedule.day_roster(_settings, d, agents, dbw, off)
    except Exception as e:
        logging.getLogger("isadash.schedule").exception("day roster failed")
        return JSONResponse({"error": f"Couldn't read the schedule sheet: {type(e).__name__}: {e}"[:300]}, status_code=502)


@app.get("/api/my/ytd")
def api_my_ytd(request: Request, as_agent: str = ""):
    """The signed-in agent's own year so far: revenue and sales to date and
    their best day, week and month — the same numbers the Revenue tab shows
    for them (upsells in, renewals out)."""
    from .periods import PACIFIC
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    conn = connect(_settings.db_path)
    try:
        row = conn.execute("SELECT fr_name FROM agent_map WHERE genesys_name = ?",
                           (name,)).fetchone()
    finally:
        conn.close()
    year = datetime.now(timezone.utc).astimezone(PACIFIC).year
    people = _revenue_data(year).get("_people") or {}
    me = people.get((row["fr_name"] if row and row["fr_name"] else name)) or people.get(name)
    if not me:
        # FieldRoutes sometimes spells a name differently ("Micah Guzman" for
        # "Micah De Guzman"): same first name + same final surname word, if unique
        parts = name.lower().split()
        close = [v for k, v in people.items()
                 if len(parts) >= 2 and k.lower().split()[:1] == parts[:1]
                 and k.lower().split()[-1:] == parts[-1:]]
        me = close[0] if len(close) == 1 else {}
    return {"year": year, "revenue": me.get("revenue", 0), "sales": me.get("sales", 0),
            "best_day": me.get("best_day"), "best_week": me.get("best_week"),
            "best_month": me.get("best_month")}


@app.get("/api/my/calls")
def api_my_calls(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own Genesys call handling, plus the team's."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        d = _scorecard_calls(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.scorecard").exception("my calls failed")
        return JSONResponse({"error": f"Genesys didn't answer ({type(e).__name__})."}, status_code=502)
    return {"me": d["agents"].get(name), "team": d["team"]}


def _mm_only(start_day: str, end_day: str) -> dict:
    from datetime import date

    from . import mastermetric
    conn = connect(_settings.db_path)
    try:
        return mastermetric.mm_only(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day))
    finally:
        conn.close()


@app.get("/api/scorecard_mm")
def api_scorecard_mm(start_day: str = "", end_day: str = ""):
    """IS #'s: each agent's MM (Master Metric) for the range — filled in after the sheet renders."""
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        return _mm_only(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.mastermetric").exception("scorecard mm failed")
        return JSONResponse({"error": f"Couldn't build MM ({type(e).__name__})."}, status_code=502)


@app.get("/api/my/mm")
def api_my_mm(request: Request, start_day: str = "", end_day: str = "", as_agent: str = ""):
    """The signed-in agent's own MM, plus the team's."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    try:
        d = _mm_only(start_day, end_day)
    except Exception as e:
        logging.getLogger("isadash.mastermetric").exception("my mm failed")
        return JSONResponse({"error": f"Couldn't build MM ({type(e).__name__})."}, status_code=502)
    return {"me": d["agents"].get(name), "team": d["team"]}


@app.get("/api/mastermetric")
def api_mastermetric(start_day: str = "", end_day: str = ""):
    """Agent Metrics -> Master Metric: productivity + availability per agent
    (Genesys calls, Homebase hours from the daily upload sheet, Podium
    outbound). See mastermetric.py for the formulas."""
    from datetime import date

    from . import mastermetric
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        return mastermetric.build(conn, _settings, date.fromisoformat(start_day),
                                  date.fromisoformat(end_day))
    except Exception as e:           # Genesys down etc. — say so instead of a blank tab
        logging.getLogger("isadash.mastermetric").exception("master metric failed")
        return JSONResponse({"error": f"Couldn't build the Master Metric: {type(e).__name__}: {e}"[:300]},
                            status_code=502)
    finally:
        conn.close()


# ---- the agent page (ACCOUNTS_PLAN segment 4) -----------------------------
# Every /api/my/* feed works out WHO from the signed-in session, never from
# the request, so an agent login can only ever get its own numbers. The one
# exception is a user-manager previewing a page with ?as=Agent Name.
def _my_agent(request: Request, as_agent: str = "") -> str | None:
    u = _user(request)
    if u and u["kind"] == "agent":
        return u["agent"] or None
    if as_agent and "settings.users" in _perms(request):
        return as_agent.strip() or None
    return None


def _valid_range(start_day: str, end_day: str) -> bool:
    from datetime import date
    try:
        a, b = date.fromisoformat(start_day), date.fromisoformat(end_day)
    except ValueError:
        return False
    return a <= b and (b - a).days <= 400


def _pay_period(which: str):
    """(start Sunday, end Saturday, through) for the 'current' or 'last' pay
    period, Pacific. `through` is the end, capped at today — the current
    period is measured as if it ended today."""
    from datetime import date, timedelta

    from .periods import PACIFIC
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    end0 = date.fromisoformat(_settings.payroll_anchor_monday) - timedelta(days=2)
    n = ((today - end0).days + 13) // 14          # smallest n with end0 + 14n >= today
    end = end0 + timedelta(days=14 * n)
    if which == "last":
        end -= timedelta(days=14)
    return end - timedelta(days=13), end, min(end, today)


_NO_AGENT = {"error": "This login isn't linked to an agent yet."}


@app.get("/api/my/scorecard")
def api_my_scorecard(request: Request, start_day: str = "", end_day: str = "",
                     mode: str = "sold", as_agent: str = ""):
    """The signed-in agent's own IS #'s row, plus the pooled team row."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    d = api_scorecard(start_day=start_day, end_day=end_day,
                      mode="serviced" if mode == "serviced" else "sold")
    mine = [r for r in d["rows"] if r["agent"] == name]
    # everyone's sheet metrics too (the Genesys call columns and the quality
    # score stay the agent's own + the team's), for the expandable team row
    return {"start": d["start"], "end": d["end"], "agent": name,
            "rows": mine, "team": d["team"], "tiers": d["tiers"],
            "everyone": sorted(d["rows"], key=lambda r: -(r.get("rev") or 0)),
            "updated": _data_updated()}


def _data_updated() -> str | None:
    """When the data was last refreshed: the end of the last sync, falling
    back to the database file's own timestamp."""
    import json as _j

    from .store import get_config
    try:
        conn = connect(_settings.db_path)
        try:
            raw = get_config(conn, "last_sync_steps")
        finally:
            conn.close()
        if raw:
            fin = (_j.loads(raw) or {}).get("finished")
            if fin:
                return fin
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(_settings.db_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


@app.get("/api/my/quality")
def api_my_quality(request: Request, start_day: str = "", end_day: str = "",
                   as_agent: str = ""):
    """VoiceOps quality for the signed-in agent and the whole team."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _valid_range(start_day, end_day):
        return JSONResponse({"error": "Pick a valid date range."}, status_code=400)
    q = api_quality(start_day=start_day, end_day=end_day)
    return {"me": (q["agents"].get(name) or {}).get("quality"),
            "team": q["teams"].get("total")}


@app.get("/api/my/power")
def api_my_power(request: Request, period: str = "current", as_agent: str = "",
                 dates_only: int = 0):
    """The Power Score ranking for the current or last pay period — serviced
    between, the same inputs the managers' Power Score tab uses. Only the
    columns the Power Score shows are sent."""
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    which = "last" if period == "last" else "current"
    start, end, _through = _pay_period(which)
    # the whole pay period, future days included: appointments still booked
    # inside it count as they will once serviced (projected), like the
    # managers' Power Score tab (Dallin, 2026-10-16)
    if dates_only:      # the page asks for the period first, to set its date picker
        return {"period": which, "start": start.isoformat(), "end": end.isoformat(),
                "period_end": end.isoformat(), "agent": name}
    d = api_scorecard(start_day=start.isoformat(), end_day=end.isoformat(),
                      mode="serviced")
    q = api_quality(start_day=start.isoformat(), end_day=end.isoformat())
    # only the metrics the Power Score uses (its rules are editable in Settings)
    conn = connect(_settings.db_path)
    try:
        ps = _load_config(conn)["power_score"]
    finally:
        conn.close()
    used = {x["metric"] for g in ("points", "penalties", "bonuses") for x in ps.get(g, [])}
    calls = mm = None
    if used & set(_PS_CALL_KEYS):
        try:
            calls = _scorecard_calls(start.isoformat(), end.isoformat())["agents"]
        except Exception:                       # noqa: BLE001 — Genesys down: those columns blank
            logging.getLogger("isadash.scorecard").exception("power score calls failed")
    if "mm" in used:
        try:
            mm = _mm_only(start.isoformat(), end.isoformat())["agents"]
        except Exception:                       # noqa: BLE001
            logging.getLogger("isadash.mastermetric").exception("power score mm failed")
    rows = []
    for r in d["rows"]:
        if not r["powerscore"]:
            continue
        row = {k: r.get(k) for k in ({"agent", "powerscore", "rev"} | used)}
        row["quality_score"] = (q["agents"].get(r["agent"]) or {}).get("quality")
        for k in _PS_CALL_KEYS:
            if k in used:
                row[k] = ((calls or {}).get(r["agent"]) or {}).get(k)
        if "mm" in used:
            row["mm"] = (mm or {}).get(r["agent"])
        rows.append(row)
    return {"period": which, "start": start.isoformat(), "end": end.isoformat(),
            "period_end": end.isoformat(), "agent": name, "rows": rows}


@app.get("/api/home_quality")
@cache.cached()
def api_home_quality() -> dict:
    """VoiceOps quality score for the Home row: 30d + 7d bubbles and the 13-week
    trend. Served entirely from the voiceops_scores cache (the nightly sync warms
    exactly these periods), so it is fast and never blocks on the vendor — the
    row simply reads blank until the first sync populates it.

    Weeks are strict Sunday-Saturday Pacific, and the bubbles are whole Pacific
    days ending yesterday, matching every other Home card.
    """
    from datetime import timedelta

    from .periods import PACIFIC
    from .voiceops import cached_scores, pacific_weeks, weighted_quality

    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    yday = today - timedelta(days=1)

    def window(days: int, offset: int = 0):
        end = yday - timedelta(days=offset)
        start = end - timedelta(days=days - 1)
        return start.isoformat(), end.isoformat()

    conn = connect(_settings.db_path)
    try:
        def bucket(days: int, offset: int = 0):
            sd, ed = window(days, offset)
            rows = list(cached_scores(conn, None, sd, ed).values())
            calls = sum(r.get("call_count") or 0 for r in rows
                        if r.get("quality_score") is not None)
            return {"pct": weighted_quality(rows), "calls": calls,
                    "range": {"start": sd, "end": ed}}

        q30, q7 = bucket(30), bucket(7)
        q30["prev_pct"] = bucket(30, 30)["pct"]
        q30["prev_range"] = bucket(30, 30)["range"]
        q7["prev_pct"] = bucket(7, 7)["pct"]
        q7["prev_range"] = bucket(7, 7)["range"]

        weeks, values = [], []
        for sd, ed in pacific_weeks(13, today):
            weeks.append(sd)
            values.append(weighted_quality(list(cached_scores(conn, None, sd, ed).values())))
        return {"q30": q30, "q7": q7, "weeks": weeks, "values": values}
    finally:
        conn.close()


@app.post("/api/powerscore_export")
def api_powerscore_export(body: dict):
    """Excel workbook for the Power Score view.

    Tab 1 is the table EXACTLY as the browser rendered it: the browser computes
    the scores (config-driven, client-side), so it sends the finished rows and
    this endpoint writes them verbatim — no second copy of the scoring logic to
    drift. Tab 2 is every PestRoutes account that fed those numbers, selected
    with the same rules as /api/scorecard (power-score agents, Completed/Pending,
    the Sold or Serviced date clause, mosquito/upsell sources excluded) and
    carrying every subscription + customer field we store.
    Values only — no formulas — so nothing needs recalculating on open.
    """
    import io
    import re as _re
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return JSONResponse({"error": "openpyxl is not installed on the server"}, status_code=500)
    from fastapi.responses import Response
    from .periods import PACIFIC
    from .rangestats import utc_bounds

    sd, ed = str(body.get("start_day") or ""), str(body.get("end_day") or "")
    mode = "serviced" if body.get("mode") == "serviced" else "sold"
    if not (_re.fullmatch(r"\d{4}-\d{2}-\d{2}", sd) and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", ed)):
        return JSONResponse({"error": "start_day and end_day required (YYYY-MM-DD)"}, status_code=400)
    columns = [str(c) for c in (body.get("columns") or [])]
    formats = [str(f) for f in (body.get("formats") or [])]
    rows = [list(r) for r in (body.get("rows") or []) if isinstance(r, (list, tuple))]
    if not columns:
        return JSONResponse({"error": "columns required"}, status_code=400)

    F = "Arial"
    HEAD, BODY = Font(name=F, bold=True), Font(name=F)
    NOTE, FILL = Font(name=F, italic=True, color="666666"), PatternFill("solid", fgColor="DDE7F0")
    NUMFMT = {"pct": '0.0"%"', "money0": "$#,##0", "money2": "$#,##0.00", "num1": "0.0"}

    def finish(ws, widths, fmts=None):
        for c in ws[1]:
            c.font, c.fill = HEAD, FILL
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for i, c in enumerate(row):
                c.font = BODY
                if fmts and i < len(fmts) and fmts[i] in NUMFMT and isinstance(c.value, (int, float)):
                    c.number_format = NUMFMT[fmts[i]]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb = Workbook()
    # ---- tab 1: the table as shown --------------------------------------
    ws = wb.active
    ws.title = "Power Score"
    ws.append(columns)
    for r in rows:
        ws.append([v if isinstance(v, (int, float, str)) or v is None else str(v) for v in r])
    n1 = ws.max_row
    finish(ws, [5, 22] + [13] * (len(columns) - 2), formats)
    mode_txt = ("subscriptions whose INITIAL SERVICE DATE is in range" if mode == "serviced"
                else "subscriptions whose SALE DATE is in range")
    for i, t in enumerate([
        f"Power Score {sd} to {ed} (Pacific days) · mode: {mode} — {mode_txt}.",
        "Exactly the table shown on the dashboard at export time (scores use the weights saved in Settings). "
        "The Accounts tab lists every PestRoutes account behind these numbers.",
        "Conversion % comes from the audit sheet and Quality Score from VoiceOps — those are not account-based, "
        "so they have no rows on the Accounts tab.",
    ], start=n1 + 2):
        ws.cell(row=i, column=1, value=t).font = NOTE

    # ---- tab 2: every contributing account -------------------------------
    conn = connect(_settings.db_path)
    try:
        fr_to_agent = {}
        for r in conn.execute("SELECT genesys_name, fr_employee_id FROM agent_map "
                              "WHERE scorecard = 1 AND powerscore = 1 AND fr_employee_id IS NOT NULL"):
            for fid in r["fr_employee_id"].split(","):
                fr_to_agent[fid] = r["genesys_name"]
        if mode == "serviced":
            clause, args = "s.initial_service_date >= ? AND s.initial_service_date <= ?", (sd, ed)
        else:
            a, b = utc_bounds(sd, ed)
            clause, args = "s.date_added_utc >= ? AND s.date_added_utc < ?", (a, b)
        mosq = {x.upper() for x in _EXCLUDED_SOURCES}
        # The fields agents and managers actually use — trimmed to this exact
        # list (and these exact header names) at the owner's request, 2026-09-21.
        SUB = ["customer_id", "sold_by_name", "phone1_e164", "phone2_e164", "agreement_length",
               "contract_value", "service_type", "initial_status", "source", "initial_price",
               "active", "date_cancelled_utc", "initial_service_date", "billing_frequency",
               "service_frequency", "contract_added", "signed_agreement"]
        CUST = ["email", "card_on_file"]
        header = ["Agent", "Sale date (PT)"] + SUB + [f"customer_{c}" for c in CUST]
        fmts2 = (["", ""] + ["money2" if c in ("contract_value", "initial_price") else "" for c in SUB]
                 + [""] * len(CUST))
        # sold_by / date_added_utc are needed for the agent match and sale date
        # even though they are not columns on the sheet
        sel = ", ".join([f"s.{c} AS s_{c}" for c in SUB + ["sold_by", "date_added_utc"]]
                        + [f"c.{c} AS c_{c}" for c in CUST])
        acc = wb.create_sheet("Accounts")
        acc.append(header)
        bf_i = 2 + SUB.index("billing_frequency")

        def billing_words(v):
            # FieldRoutes stores -1 for "bill at time of service"
            return "Billed after each completed service" if v in (-1, "-1") else v

        n = 0
        for r in conn.execute(
                f"""SELECT {sel} FROM subscriptions s
                    LEFT JOIN customers c ON c.customer_id = s.customer_id
                    WHERE {clause} AND s.initial_status IN ('Completed','Pending')
                    ORDER BY s.date_added_utc""", args):
            agent = fr_to_agent.get(r["s_sold_by"])
            if not agent or (r["s_source"] or "").upper() in mosq:
                continue
            try:
                sale_pt = datetime.fromisoformat(r["s_date_added_utc"]).astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                sale_pt = r["s_date_added_utc"]
            row = [agent, sale_pt] + [r[f"s_{c}"] for c in SUB] + [r[f"c_{c}"] for c in CUST]
            row[bf_i] = billing_words(row[bf_i])
            acc.append(row)
            n += 1
        finish(acc, [24, 17] + [15] * (len(header) - 2), fmts2)
        acc.cell(row=acc.max_row + 2, column=1,
                 value=f"{n} accounts · power-score agents only · serviced or pending initial · "
                       f"mosquito/upsell sources excluded (they count toward no Power Score metric) · "
                       f"{'initial service date' if mode == 'serviced' else 'sale date'} {sd} to {ed}.").font = NOTE
    finally:
        conn.close()

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="PowerScore_{mode}_{sd}_{ed}.xlsx"'})


_quality_refreshing: set = set()
_quality_refresh_lock = threading.Lock()


def _refresh_quality_in_background(sd: str, ed: str) -> None:
    """Re-pull one period's VoiceOps scores on a side thread (one at a time
    per period). cached_scores commits per agent, so nothing is held open
    across the vendor calls."""
    key = (sd, ed)
    with _quality_refresh_lock:
        if key in _quality_refreshing:
            return
        _quality_refreshing.add(key)

    def run():
        from .voiceops import VoiceOpsClient, cached_scores
        t0 = time.perf_counter()
        try:
            client = VoiceOpsClient(_settings.voiceops_client_id, _settings.voiceops_client_secret)
            conn = connect(_settings.db_path)
            try:
                cached_scores(conn, client, sd, ed)
            finally:
                conn.close()
                client.close()
            _perf_log.info("quality: refreshed %s..%s in the background (%.0f s)",
                           sd, ed, time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 - a failed refresh just leaves the saved scores in place
            logging.getLogger("isadash.quality").exception("background quality refresh failed")
        finally:
            with _quality_refresh_lock:
                _quality_refreshing.discard(key)

    threading.Thread(target=run, name="quality-refresh", daemon=True).start()


@app.get("/api/quality")
def api_quality(start_day: str = "", end_day: str = "", days: int = 30,
                fetch: int = 1) -> dict:
    """VoiceOps quality score per agent and per team for a Pacific day range.

    Its own endpoint (like /api/home_servicelevel) because a period that has
    never been requested costs ~1 API call per agent to warm. The IS #'s and
    Team Metrics tables therefore render instantly from the database and this
    fills the quality column in afterwards; once a period is cached it is
    served from SQLite and returns immediately.

    Agents VoiceOps has no scorable calls for are simply absent from `agents`,
    and contribute to no average — "counts if they have data, blank if not".
    """
    from datetime import timedelta

    from .periods import PACIFIC
    from .voiceops import (VoiceOpsClient, cached_scores, mapped_roster,
                           needs_refresh, weighted_quality)

    if start_day and end_day:
        sd, ed = start_day, end_day
    else:
        today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
        end = today - timedelta(days=1)          # whole days ending yesterday
        sd = (end - timedelta(days=max(1, min(days, 400)) - 1)).isoformat()
        ed = end.isoformat()

    conn = connect(_settings.db_path)
    try:
        team_of = {r["genesys_name"]: r["team"] for r in conn.execute(
            "SELECT genesys_name, team FROM agent_map WHERE scorecard = 1")}
        live = bool(fetch and _settings.voiceops_client_id and _settings.voiceops_client_secret)
        scores = cached_scores(conn, None, sd, ed)          # whatever is saved, any age
        if live:
            roster = [n for n, _e in mapped_roster(conn)]
            if roster and all(n in scores for n in roster):
                # Everyone has a saved score: show it now. If the period is still
                # live and those scores are over an hour old, re-pull them in the
                # background. (Before 2026-11-11 the visitor waited for the whole
                # re-pull: ~27 VoiceOps calls, 30+ seconds on an agent's page.)
                if needs_refresh(conn, sd, ed):
                    _refresh_quality_in_background(sd, ed)
            else:                                           # never pulled: nothing to show yet
                client = VoiceOpsClient(_settings.voiceops_client_id,
                                        _settings.voiceops_client_secret)
                try:
                    scores = cached_scores(conn, client, sd, ed)
                finally:
                    client.close()

        agents = {name: {"quality": s.get("quality_score"),
                         "calls": s.get("call_count")}
                  for name, s in scores.items()
                  if s.get("quality_score") is not None}
        buckets: dict[str, list] = {"total": [], "josh": [], "kai": []}
        for name, s in scores.items():
            buckets["total"].append(s)
            t = team_of.get(name)
            if t in ("josh", "kai"):
                buckets[t].append(s)
        teams = {k: weighted_quality(v) for k, v in buckets.items()}
        return {"start": sd, "end": ed, "agents": agents, "teams": teams}
    finally:
        conn.close()


# ---- editable tunables (scorecard tiers + power-score weights) ----------
# Defaults mirror the pay-period sheet (green >= t1, yellow >= t2, else red;
# below min = red text). Stored overrides live in the app_config table and are
# managed from the Settings screen; these are the fallback if nothing's saved.
_DEFAULT_SCORECARD_TIERS = {
    "conversion_pct": {"t1": 75, "t2": 70, "min": 55},
    "call_grade":     {"t1": 90, "t2": 80, "min": 70},
    "acv":            {"t1": 750, "t2": 710, "min": 650},
    "multi_total_pct": {"t1": 65, "t2": 60, "min": 50},
    "multi24_pct":    {"t1": 40, "t2": 25, "min": 15},
    # a single 70% floor (2026-09-30): at/above = green, below = red
    "monthly_pct":    {"t1": 70, "t2": 70, "min": 70},
    # optional bands (2026-10-02): nothing is coloured until numbers are
    # entered in Settings. avg_acw_s is in seconds and LOWER is better there
    # (Tier 1 = at or under, "minimum" = the most allowed).
    "sts_pct":        {"t1": None, "t2": None, "min": None},
    "aip":            {"t1": None, "t2": None, "min": None},
    "signed_pct":     {"t1": None, "t2": None, "min": None},
    "quality_score":  {"t1": None, "t2": None, "min": None},
    "ans_missed":     {"t1": None, "t2": None, "min": None},
    "avg_acw_s":      {"t1": None, "t2": None, "min": None},
    # MM (Master Metric): a goal (t1) and a minimum only — green at/above the
    # goal, yellow between the minimum and the goal, red under (2026-10-13)
    "mm":             {"t1": None, "t2": None, "min": None},
}
# Power Score (v2, 2026-10-21): every rule is editable in Settings — any IS #'s
# metric can earn points on a ladder (first threshold cleared wins), carry a
# penalty, or pay a rank bonus. Lower-is-better metrics read "at most" / "above".
PS_METRIC_KEYS = ("conversion_pct", "acv", "multi_total_pct", "multi24_pct", "rev", "subs",
                  "outbound_subs", "sts_pct", "apay_pct", "multi18_pct", "aip", "signed_pct",
                  "quality_score", "monthly_pct", "calls_taken", "calls_missed", "ans_missed",
                  "avg_talk_s", "avg_acw_s", "mm")
PS_LOWER_BETTER = ("avg_acw_s", "calls_missed")
_PS_CALL_KEYS = ("calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s")
# the shape saved before 2026-10-21 (fixed metrics); converted on read
_LEGACY_POWER_SCORE = {
    "scales": {
        "conversion_pct": [[75, 8], [70, 6], [66, 4], [63, 2], [55, 1]],
        "acv":            [[760, 5], [740, 4], [720, 3], [700, 2], [675, 1]],
        "multi_total_pct": [[70, 5], [65, 4], [60, 3], [57.5, 2], [53.75, 1]],
        "apay_pct":       [[95, 5], [90, 4], [88, 3], [85, 2], [80, 1]],
    },
    "multi24": {"threshold": 15, "penalty": -2},
    "quality": {"threshold": 67, "penalty": -2},
    "monthly": {"threshold": 60, "penalty": -2},
    "revenue": {"points": [3, 2, 1]},
}


def _ps_from_legacy(p: dict) -> dict:
    """The old fixed-metric shape -> v2 lists, keeping every saved number."""
    import copy
    old = copy.deepcopy(_LEGACY_POWER_SCORE)
    for k in ("multi24", "quality", "monthly", "revenue"):
        if isinstance(p.get(k), dict):
            old[k].update({kk: vv for kk, vv in p[k].items() if vv is not None})
    if isinstance(p.get("scales"), dict):
        old["scales"].update({k: v for k, v in p["scales"].items() if v})
    pts = old["revenue"].get("points")
    return {"version": 2,
            "points": [{"metric": m, "scale": old["scales"][m]}
                       for m in ("conversion_pct", "acv", "multi_total_pct", "apay_pct") if old["scales"].get(m)],
            "penalties": [{"metric": m, "threshold": old[g]["threshold"], "penalty": old[g]["penalty"]}
                          for m, g in (("quality_score", "quality"), ("multi24_pct", "multi24"),
                                       ("monthly_pct", "monthly"))],
            "bonuses": [{"metric": "rev", "points": pts if isinstance(pts, list) and pts else [3, 2, 1]}]}


_DEFAULT_POWER_SCORE = _ps_from_legacy({})
_CONFIG_DEFAULTS = {"scorecard_tiers": _DEFAULT_SCORECARD_TIERS,
                    "power_score": _DEFAULT_POWER_SCORE}


def _load_config(conn) -> dict:
    """{scorecard_tiers, power_score} — stored overrides atop the defaults."""
    import copy
    import json
    out = copy.deepcopy(_CONFIG_DEFAULTS)
    for r in conn.execute("SELECT key, value FROM app_config "
                          "WHERE key IN ('scorecard_tiers','power_score')"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            pass  # keep default on a corrupt row
    # a power_score saved before a section existed (e.g. "quality", added
    # 2026-09-16) must still carry that section's defaults to the frontend
    # same for tiers saved before a metric had a band (monthly_pct, 2026-09-30)
    if isinstance(out.get("scorecard_tiers"), dict):
        for k, v in _DEFAULT_SCORECARD_TIERS.items():
            out["scorecard_tiers"].setdefault(k, copy.deepcopy(v))
    # a power score saved in the old fixed-metric shape is converted (same numbers)
    ps = out.get("power_score")
    if not isinstance(ps, dict) or ps.get("version") != 2:
        try:
            out["power_score"] = _validate_power(ps if isinstance(ps, dict) else {})
        except ValueError:
            out["power_score"] = copy.deepcopy(_DEFAULT_POWER_SCORE)
    return out


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"expected a number, got {x!r}")
    return x


def _validate_tiers(t: dict) -> dict:
    import copy
    out = copy.deepcopy(_DEFAULT_SCORECARD_TIERS)
    if not isinstance(t, dict):
        raise ValueError("scorecard_tiers must be an object")
    for metric, band in t.items():
        if metric not in out or not isinstance(band, dict):
            continue
        for k in ("t1", "t2", "min"):
            if band.get(k) is not None:
                out[metric][k] = _num(band[k])
    return out


def _validate_power(p: dict) -> dict:
    """v2 power score: {version: 2, points: [{metric, scale: [[thr, pts]...]}],
    penalties: [{metric, threshold, penalty}], bonuses: [{metric, points: [..]}]}.
    Unknown metrics are refused; incomplete rows are dropped; ladders are
    sorted best-first (descending, or ascending for lower-is-better)."""
    if not isinstance(p, dict):
        raise ValueError("power_score must be an object")
    if p.get("version") != 2:
        p = _ps_from_legacy(p)

    def metric(x) -> str:
        m = x.get("metric") if isinstance(x, dict) else None
        if m not in PS_METRIC_KEYS:
            raise ValueError(f"unknown power score metric {m!r}")
        return m
    out: dict = {"version": 2, "points": [], "penalties": [], "bonuses": []}
    for x in (p.get("points") or [])[:20]:
        m = metric(x)
        scale = [[_num(a[0]), _num(a[1])] for a in (x.get("scale") or [])
                 if isinstance(a, (list, tuple)) and len(a) == 2 and a[0] is not None and a[1] is not None]
        if scale:
            scale.sort(key=lambda z: z[0], reverse=m not in PS_LOWER_BETTER)
            out["points"].append({"metric": m, "scale": scale[:12]})
    for x in (p.get("penalties") or [])[:20]:
        m = metric(x)
        if x.get("threshold") is None or x.get("penalty") is None:
            continue
        out["penalties"].append({"metric": m, "threshold": _num(x["threshold"]), "penalty": _num(x["penalty"])})
    for x in (p.get("bonuses") or [])[:10]:
        m = metric(x)
        pts = [max(0, v) for v in (x.get("points") or [])
               if isinstance(v, (int, float)) and not isinstance(v, bool)][:25]
        if pts:
            out["bonuses"].append({"metric": m, "points": pts})
    # one rule per metric (2026-11-04): two rules on the same metric showed up
    # as two columns on the Power Score (e.g. Quality Score twice)
    seen: set = set()
    dup: list = []
    for g in ("points", "penalties", "bonuses"):
        for x in out[g]:
            if x["metric"] in seen and x["metric"] not in dup:
                dup.append(x["metric"])
            seen.add(x["metric"])
    if dup:
        raise ValueError("each metric can be in only one Power Score rule; used more than once: "
                         + ", ".join(dup))
    return out


def _save_config(conn, key: str, value) -> None:
    import json
    conn.execute("INSERT INTO app_config(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def _resolve_fr_ids(fr, name: str) -> set:
    """Find a person's FieldRoutes employee IDs (one per office) by name."""
    parts = name.split()
    if len(parts) < 2:
        return set()
    fname, lname = parts[0].lower(), parts[-1]
    ids = set()
    for oid in _settings.pestroutes_office_ids:
        got = fr.request("employee", "search",
                         {"lname": lname, "officeIDs": oid, "includeData": 1})
        for e in got.get("employees") or []:
            if (e.get("fname") or "").strip().lower().startswith(fname):
                ids.add(str(e.get("employeeID")))
    return ids


@app.get("/api/teammetrics")
def api_teammetrics(start_day: str, end_day: str, mode: str = "sold",
                    phone: bool = True) -> dict:
    """Total / Josh / Kai team metrics for a date range. Sales metrics per
    team; form + phone-queue metrics are team-total only."""
    from .rangestats import utc_bounds
    a, b = utc_bounds(start_day, end_day)
    serviced_mode = mode == "serviced"

    conn = connect(_settings.db_path)
    try:
        # key agents -> team; FR id -> team; and per-agent audit lookup
        name_team: dict[str, str] = {}
        fr_team: dict[str, str] = {}
        fr_gid: dict[str, str] = {}          # FR employee id -> the agent's Genesys id
        for r in conn.execute(
            "SELECT genesys_name, genesys_user_id, fr_employee_id, team FROM agent_map "
            "WHERE scorecard = 1"):
            t = r["team"] or "none"
            name_team[r["genesys_name"]] = t
            for fid in (r["fr_employee_id"] or "").split(","):
                if fid:
                    fr_team[fid] = t
                    fr_gid[fid] = r["genesys_user_id"] or ""
        own_inbound = _own_inbound_subs(conn)   # sales tied to the seller's own inbound call

        def blank():
            return {"subs": 0, "m18": 0, "m24": 0, "cv": 0.0, "cv_n": 0,
                    "rev_cv": 0.0, "rev_n": 0, "inb_n": 0,
                    "ip": 0.0, "ip_n": 0, "apay": 0, "apay_n": 0,
                    "recurring": 0, "monthly": 0, "signed": 0,
                    "aud_q": 0, "aud_s": 0}
        buckets = {"total": blank(), "josh": blank(), "kai": blank()}

        def add(team, fn):
            fn(buckets["total"])
            if team in ("josh", "kai"):
                fn(buckets[team])

        # subscription metrics. REVENUE counts mosquito/upsells but not renewals;
        # everything else excludes mosquito (current rule).
        mosq = {s.upper() for s in _EXCLUDED_SOURCES}
        date_clause = ("s.initial_service_date >= ? AND s.initial_service_date <= ?"
                       if serviced_mode else
                       "s.date_added_utc >= ? AND s.date_added_utc < ?")
        date_args = (start_day, end_day) if serviced_mode else (a, b)
        for r in conn.execute(
            f"""SELECT s.subscription_id, s.sold_by, s.agreement_length, s.contract_value,
                       s.initial_price, s.billing_frequency, s.service_frequency,
                       s.signed_agreement, s.source, c.card_on_file
                FROM subscriptions s LEFT JOIN customers c ON c.customer_id = s.customer_id
                WHERE {date_clause} AND s.initial_status IN ('Completed','Pending')""",
            date_args):
            team = fr_team.get(r["sold_by"])
            if team is None:
                continue
            inbound = str(r["subscription_id"]) in own_inbound.get(fr_gid.get(r["sold_by"], ""), ())

            def upd(k, r=r, inbound=inbound):
                src = r["source"] or ""
                cv = r["contract_value"] or 0
                # HEADLINE Total Sales / Total Revenue: mosquito + upsells IN,
                # renewals out (matches the manager's with-mosquito Sales Report).
                # Everything below the mosquito skip keeps excluding them.
                if src not in _RENEWAL_SOURCES:
                    k["rev_n"] += 1
                    if inbound:
                        k["inb_n"] += 1
                    if cv:
                        k["rev_cv"] += cv
                if src.upper() in mosq:
                    return                   # everything below excludes mosquito
                k["subs"] += 1
                months = r["agreement_length"] or 0
                if months >= 24:
                    k["m24"] += 1
                elif months >= 18:
                    k["m18"] += 1
                if cv:
                    k["cv"] += cv; k["cv_n"] += 1
                if r["initial_price"]:
                    k["ip"] += r["initial_price"]; k["ip_n"] += 1
                if r["card_on_file"] is not None:
                    k["apay_n"] += 1
                    if r["card_on_file"]:
                        k["apay"] += 1
                sf = r["service_frequency"]
                if sf is not None and sf != -1:  # recurring (not one-time)
                    k["recurring"] += 1
                    bf = r["billing_frequency"]
                    if bf == 30 or (bf == -1 and sf == 30):
                        k["monthly"] += 1
                if r["signed_agreement"]:  # real COMPLETED contract, not a draft
                    k["signed"] += 1
            add(team, upd)

        # sold -> serviced per team: every first-service status (the ratio is
        # serviced / (serviced + cancelled + no-show)), same dates and upsell
        # exclusions as the agent sheet
        sts_rows: dict[str, list] = {"total": [], "josh": [], "kai": []}
        for r in conn.execute(
            f"""SELECT s.sold_by, s.initial_status, s.source FROM subscriptions s
                WHERE {date_clause}""", date_args):
            team = fr_team.get(r["sold_by"])
            if team is None or (r["source"] or "").upper() in mosq:
                continue
            sts_rows["total"].append(r["initial_status"])
            if team in ("josh", "kai"):
                sts_rows[team].append(r["initial_status"])

        # audit sheet conversion per team
        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) s FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent", (start_day, end_day)):
            team = name_team.get(r["agent"])
            if team is None:
                continue
            add(team, lambda k, r=r: (k.__setitem__("aud_q", k["aud_q"] + r["q"]),
                                      k.__setitem__("aud_s", k["aud_s"] + r["s"])))

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        def finish(k):
            return {
                "phone_conversion": pct(k["aud_s"], k["aud_q"]),
                # Total Sales / Total Revenue are the HEADLINE pair: mosquito +
                # upsells INCLUDED, renewals excluded (rev_n/rev_cv). All quality
                # metrics below still run on k["subs"]/k["cv"] (upsells excluded),
                # so their denominators are untouched by this pair.
                "total_sales": k["rev_n"], "audit_sales": k["aud_s"],
                # of Total Sales, the ones not tied to the seller's own inbound call
                "outbound_sales": k["rev_n"] - k["inb_n"],
                "total_revenue": round(k["rev_cv"], 2),
                "multiyear_pct": pct(k["m18"] + k["m24"], k["subs"]),
                "multi24_pct": pct(k["m24"], k["subs"]),
                "multi18_pct": pct(k["m18"], k["subs"]),
                "acv": round(k["cv"] / k["cv_n"], 2) if k["cv_n"] else None,
                "avg_initial": round(k["ip"] / k["ip_n"], 2) if k["ip_n"] else None,
                "apay_pct": pct(k["apay"], k["apay_n"]),
                "monthly_billing_pct": pct(k["monthly"], k["recurring"]),
                "signed_doc_pct": pct(k["signed"], k["subs"]),
            }
        out = {t: finish(buckets[t]) for t in buckets}
        for t in out:
            out[t]["sts_pct"] = _sts(sts_rows[t])["sts_pct"]

        # --- team-total only: form conversion + all leads ---
        events, _ = _get_form_events()
        a_dt = datetime.fromisoformat(a)
        b_dt = datetime.fromisoformat(b)
        form_net = form_conv = 0
        for e in (events or []):
            if not (a_dt <= e.first_touch_utc < b_dt) or e.already_customer:
                continue
            form_net += 1
            if e.converted:
                form_conv += 1
        tot = buckets["total"]
        out["total"]["form_conversion"] = pct(form_conv, form_net)
        out["total"]["form_net"] = form_net
        out["total"]["form_converted"] = form_conv
        # total leads = (sheet sales + form conv) / (sheet qual + form net)
        denom = tot["aud_q"] + form_net
        out["total"]["total_leads_conversion"] = \
            pct(tot["aud_s"] + form_conv, denom)
    finally:
        conn.close()

    # --- team-total only: Genesys queue stats ---
    if not phone:                     # the overview cards skip the live Genesys call
        out["start"] = start_day
        out["end"] = end_day
        return out
    try:
        from .genesys import GenesysClient
        with GenesysClient(_settings.genesys_region, _settings.genesys_client_id,
                           _settings.genesys_client_secret) as g:
            qs = g.queue_stats(datetime.fromisoformat(a), datetime.fromisoformat(b),
                               _settings.genesys_queue_ids)
        out["total"]["phone_metrics"] = qs
    except Exception as e:  # don't fail the whole tab if Genesys hiccups
        out["total"]["phone_metrics"] = {"error": str(e)[:120]}

    out["start"] = start_day
    out["end"] = end_day
    return out


_TEAM_OV_METRICS = ("total_revenue", "total_sales", "outbound_sales", "total_leads_conversion",
                    "phone_conversion", "form_conversion", "acv", "multiyear_pct",
                    "multi24_pct", "multi18_pct", "sts_pct", "avg_initial", "apay_pct",
                    "monthly_billing_pct", "signed_doc_pct", "quality_score")
_TEAM_OV_CALLS = ("calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s")
@cache.cached()
def _team_overview_db() -> dict:
    """The database part of Team Overview (everything except the Genesys
    call-handling cards, which api_team_overview adds per request).

    Team Metrics -> Team Overview cards: every team metric for the whole
    team, Josh's team and Kai's team over the last 30 and 7 COMPLETE days
    (each against the period before it) plus a 13-week trend. Sold between,
    the same as Home. Built from the same per-range function the any-dates
    table uses, so the two can't disagree. Cached until the day or data changes."""
    from datetime import timedelta

    from .periods import PACIFIC
    from .voiceops import pacific_weeks
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    end = today - timedelta(days=1)

    def win(days: int, back: int) -> tuple[str, str]:
        e = end - timedelta(days=days * back)
        return (e - timedelta(days=days - 1)).isoformat(), e.isoformat()
    ranges = {"d30": win(30, 0), "p30": win(30, 1), "d7": win(7, 0), "p7": win(7, 1)}
    weeks = pacific_weeks(13)

    def pull(s: str, e: str) -> dict:
        d = api_teammetrics(start_day=s, end_day=e, mode="sold", phone=False)
        try:                           # VoiceOps: cached scores only, never a live pull here
            q = api_quality(start_day=s, end_day=e, fetch=0).get("teams") or {}
        except Exception:
            q = {}
        for t in ("total", "josh", "kai"):
            d[t]["quality_score"] = q.get(t)
        return d
    w = {k: pull(*v) for k, v in ranges.items()}
    series = [pull(s, e) for s, e in weeks]
    teams = {}
    for t in ("total", "josh", "kai"):
        teams[t] = {m: {"d30": w["d30"][t].get(m), "p30": w["p30"][t].get(m),
                        "d7": w["d7"][t].get(m), "p7": w["p7"][t].get(m),
                        "series": [x[t].get(m) for x in series]}
                    for m in _TEAM_OV_METRICS}
        teams[t]["_sales"] = {"d30": w["d30"][t].get("total_sales"),
                              "d7": w["d7"][t].get("total_sales")}
    return {"ranges": ranges, "weeks": weeks, "teams": teams, "end": end.isoformat()}


@app.get("/api/team_overview")
def api_team_overview() -> dict:
    """Team Overview cards. The database part is saved until its data changes
    (_team_overview_db, ~5 s to build); the Genesys call-handling cards are
    added here on every request from Genesys's own short-lived cache, so a
    Genesys hiccup is never saved."""
    from datetime import date as _date
    core = _team_overview_db()           # a private copy: safe to add to
    ranges, weeks, teams = core["ranges"], core["weeks"], core["teams"]
    end = _date.fromisoformat(core["end"])
    # Genesys call handling per team (calls taken / missed, talk, after-call
    # work): one per-day pull covers every window, summed over each team's reps
    try:

        from . import mastermetric
        first = min(ranges["p30"][0], weeks[0][0] if weeks else ranges["p30"][0])
        daily = mastermetric.genesys_daily(_settings, _date.fromisoformat(first), end)
        conn = connect(_settings.db_path)
        try:
            uid_team = {r["genesys_user_id"]: (r["team"] or "none") for r in conn.execute(
                "SELECT genesys_user_id, team FROM agent_map "
                "WHERE scorecard = 1 AND genesys_user_id IS NOT NULL")}
        finally:
            conn.close()

        def calls(team: str, s: str, e: str) -> dict:
            tot = {"answered": 0, "missed": 0, "talk_ms": 0, "talk_n": 0, "acw_ms": 0, "acw_n": 0}
            for uid, t in uid_team.items():
                if team != "total" and t != team:
                    continue
                for day, g in (daily.get(uid) or {}).items():
                    if s <= day <= e:
                        for k in tot:
                            tot[k] += g[k]
            return {"calls_taken": tot["answered"], "calls_missed": tot["missed"],
                    "ans_missed": round(tot["answered"] / tot["missed"], 2) if tot["missed"] else None,
                    "avg_talk_s": round(tot["talk_ms"] / tot["talk_n"] / 1000) if tot["talk_n"] else None,
                    "avg_acw_s": round(tot["acw_ms"] / tot["acw_n"] / 1000) if tot["acw_n"] else None}
        for t in ("total", "josh", "kai"):
            cw = {k: calls(t, *v) for k, v in ranges.items()}
            cs = [calls(t, s, e) for s, e in weeks]
            for m in _TEAM_OV_CALLS:
                teams[t][m] = {"d30": cw["d30"][m], "p30": cw["p30"][m], "d7": cw["d7"][m],
                               "p7": cw["p7"][m], "series": [x[m] for x in cs]}
    except Exception:      # Genesys down: those cards show a dash, the rest still load
        logging.getLogger("isadash.teamoverview").exception("team overview: Genesys part failed")
    payload = {"ranges": {k: {"start": v[0], "end": v[1]} for k, v in ranges.items()},
               "weeks": [s for s, _ in weeks], "teams": teams}
    return payload


@app.get("/api/software")
def api_software(request: Request):
    """Software Management: status of every Brooks app, gathered ON DEMAND over
    SSH when the tab opens (a few seconds; nothing polls in the background).
    Admin-only — it exposes infrastructure."""
    if not _allowed(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    from . import software
    return software.collect(_settings)


@app.post("/api/software/run")
def api_software_run(request: Request, body: dict):
    """Run one allow-listed action on the app's host. The agent on that host
    enforces the same allow-list, so nothing outside it can execute."""
    if not _allowed(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    from . import software
    action = str((body or {}).get("action") or "")
    res = software.run_action(_settings, action)
    logging.getLogger("isadash.software").info(
        "software action %s by admin -> %s", action, res.get("message") or res.get("error"))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/settings")
def api_settings(request: Request) -> dict:
    """Agent roster config: key agents with their flags, plus candidates to add."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        agents = []
        for r in conn.execute(
            "SELECT genesys_user_id, genesys_name, fr_employee_id, active, "
            "powerscore, team FROM agent_map WHERE scorecard = 1 "
            "ORDER BY genesys_name"):
            agents.append({
                "id": r["genesys_user_id"], "name": r["genesys_name"],
                "fr_ids": len((r["fr_employee_id"] or "").split(","))
                          if r["fr_employee_id"] else 0,
                "active": bool(r["active"]), "powerscore": bool(r["powerscore"]),
                "team": r["team"] or ""})
        # candidates to ADD = every office-staff SELLER (FieldRoutes type-0
        # employee) with their lifetime revenue, so you search the people who
        # actually sell subscriptions — not the Genesys user list.
        tracked = {a["name"] for a in agents}
        rev: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.name, SUM(s.contract_value) v FROM subscriptions s "
            "JOIN employees e ON e.fr_employee_id = s.sold_by "
            "WHERE e.emp_type = '0' AND s.contract_value > 0 GROUP BY e.name"):
            rev[r["name"]] = r["v"] or 0
        fr_count: dict[str, int] = {}
        for r in conn.execute(
            "SELECT name, COUNT(*) c FROM employees WHERE emp_type = '0' "
            "AND name IS NOT NULL AND name != '' GROUP BY name"):
            fr_count[r["name"]] = r["c"]
        sellers = sorted(
            [{"name": n, "fr_count": c, "revenue": round(rev.get(n, 0)),
              "tracked": n in tracked} for n, c in fr_count.items()],
            key=lambda s: -s["revenue"])
        return {"agents": agents, "sellers": sellers}
    finally:
        conn.close()


@app.post("/api/settings/add")
def api_settings_add(body: dict, request: Request) -> dict:
    """Add an office-staff seller (by name) as a tracked key agent — resolves
    their FieldRoutes IDs from the employees cache; reuses a matching Genesys
    user id if one exists, else a synthetic key."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "missing name"}
    conn = connect(_settings.db_path)
    try:
        ids = sorted({r[0] for r in conn.execute(
            "SELECT fr_employee_id FROM employees WHERE name = ? AND emp_type = '0'",
            (name,)) if r[0]})
        if not ids:
            return {"error": "no FieldRoutes IDs found for that seller"}
        row = conn.execute("SELECT genesys_user_id FROM agent_map WHERE genesys_name = ? LIMIT 1",
                           (name,)).fetchone()
        gid = row[0] if row else "fr:" + name
        conn.execute(
            """INSERT INTO agent_map
                 (genesys_user_id, genesys_name, fr_employee_id, on_roster,
                  scorecard, active, powerscore, team)
               VALUES (?, ?, ?, 1, 1, ?, ?, ?)
               ON CONFLICT(genesys_user_id) DO UPDATE SET
                 genesys_name = excluded.genesys_name,
                 fr_employee_id = excluded.fr_employee_id, scorecard = 1,
                 on_roster = 1,
                 active = excluded.active, powerscore = excluded.powerscore,
                 team = excluded.team""",
            (gid, name, ",".join(ids), 1 if body.get("active", True) else 0,
             1 if body.get("powerscore") else 0, body.get("team") or None))
        conn.commit()
        return {"ok": True, "id": gid, "fr_ids": len(ids)}
    finally:
        conn.close()


@app.post("/api/settings/agent")
def api_settings_update(body: dict, request: Request) -> dict:
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    gid = body.get("id")
    if not gid:
        return {"error": "missing id"}
    conn = connect(_settings.db_path)
    try:
        cur = conn.execute(
            "SELECT genesys_name, fr_employee_id FROM agent_map "
            "WHERE genesys_user_id = ?", (gid,)).fetchone()
        if not cur:
            return {"error": "unknown agent"}
        sets, args = [], []
        for f in ("scorecard", "active", "powerscore"):
            if f in body:
                sets.append(f"{f}=?"); args.append(1 if body[f] else 0)
        if "team" in body:
            sets.append("team=?"); args.append(body["team"] or None)
        resolved = None
        # promoting to key with no FR ids yet -> resolve them from FieldRoutes
        if body.get("scorecard") and not cur["fr_employee_id"]:
            fr = FieldRoutesClient(_settings.pestroutes_base_url,
                                   _settings.pestroutes_auth_key,
                                   _settings.pestroutes_auth_token)
            try:
                ids = _resolve_fr_ids(fr, cur["genesys_name"])
            finally:
                fr.close()
            if ids:
                sets.append("fr_employee_id=?"); args.append(",".join(sorted(ids)))
                resolved = len(ids)
        if sets:
            args.append(gid)
            conn.execute(f"UPDATE agent_map SET {', '.join(sets)} "
                         "WHERE genesys_user_id = ?", args)
            conn.commit()
        return {"ok": True, "resolved_fr_ids": resolved}
    finally:
        conn.close()


@app.get("/api/commissions/roster")
def api_commissions_roster(request: Request) -> dict:
    """Settings -> Commissions: the roster the IS Commissions app calculates
    for (inbound full-rate ISAs, outbound 3% agents), plus seller candidates
    to add — same candidate pool as the Agents subtab."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        roster = [{"name": r["name"], "role": r["role"], "added": r["added_at"][:10],
                   "start_date": r["start_date"] or ""}
                  for r in conn.execute(
                      "SELECT name, role, added_at, start_date FROM commission_roster "
                      "ORDER BY role, name")]
        on = {r["name"] for r in roster}
        rev: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.name, SUM(s.contract_value) v FROM subscriptions s "
            "JOIN employees e ON e.fr_employee_id = s.sold_by "
            "WHERE e.emp_type = '0' AND s.contract_value > 0 GROUP BY e.name"):
            rev[r["name"]] = r["v"] or 0
        names = {r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM employees WHERE emp_type = '0' "
            "AND name IS NOT NULL AND name != ''")}
        # tracked dashboard agents too — covers reps (e.g. outbound) whose
        # employees rows haven't synced on this box
        names |= {r["genesys_name"] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE scorecard = 1 "
            "AND genesys_name IS NOT NULL")}
        sellers = sorted(
            [{"name": n, "revenue": round(rev.get(n, 0))}
             for n in names if n not in on],
            key=lambda s: (-s["revenue"], s["name"]))
        return {"roster": roster, "sellers": sellers}
    finally:
        conn.close()


@app.post("/api/commissions/roster")
def api_commissions_roster_save(body: dict, request: Request) -> dict:
    """Add / change role / remove one commission-roster entry."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "missing name"}
    conn = connect(_settings.db_path)
    try:
        if body.get("remove"):
            conn.execute("DELETE FROM commission_roster WHERE name = ?", (name,))
        elif "start_date" in body:
            # RULES §1a: inside-sales start date; blank clears it. Validated —
            # a malformed date would make the engine exclude every sale.
            raw = (body.get("start_date") or "").strip()
            if raw:
                try:
                    from datetime import date as _date
                    raw = _date.fromisoformat(raw).isoformat()
                except ValueError:
                    return {"error": "start date must be YYYY-MM-DD"}
            conn.execute("UPDATE commission_roster SET start_date = ? WHERE name = ?",
                         (raw or None, name))
        else:
            role = body.get("role")
            if role not in ("inbound", "outbound"):
                return {"error": "role must be inbound or outbound"}
            conn.execute(
                """INSERT INTO commission_roster (name, role) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET role = excluded.role""",
                (name, role))
        conn.commit()
        logging.getLogger("isadash.commissions").info(
            "commission roster: %s -> %s", name,
            "removed" if body.get("remove") else
            f"start {body.get('start_date') or '(none)'}" if "start_date" in body
            else body.get("role"))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/commissions/rules")
def api_commissions_rules(request: Request) -> dict:
    """The IS Commissions rulebook (COMMISSION_RULES.md), verbatim. The
    commissions app treats that file as the single source of truth — every
    rule change lands there first — so the dashboard renders it rather than
    keeping a copy that could drift."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    p = Path(_settings.commission_rules_path)
    if not p.is_file():
        return {"error": f"rules file not found at {p} — set COMMISSION_RULES_PATH"}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return {"error": f"could not read rules file: {e}"}
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return {"markdown": text, "updated": mtime.isoformat()}


def _lookup_why(st: dict) -> str:
    """Plain-English status for the Commission Lookup card, derived from the
    commission engine's sub_status. Wording mirrors COMMISSION_RULES.md."""
    state = st.get("state") or "not_started"
    q = st.get("qualifying") or 0
    cov, tgt = st.get("coverage_paid") or 0, st.get("coverage_target") or 0
    fp = (st.get("first_paid") or "")[:10]
    vd = (st.get("vested") or "")[:10]
    dc = (st.get("cancel_date") or "")[:10]
    cr = st.get("cancel_reason") or "no reason recorded"
    if state == "vested":
        return (f"VESTED {vd} — {q} qualifying services and ${cov:,.0f} of "
                f"${tgt:,.0f} real-money coverage. Locked permanently; can never "
                f"be deducted.")
    if state == "paid":
        return (f"PAID — first service completed and its invoice paid {fp}. Not "
                f"vested yet ({q}/3 services, ${cov:,.0f}/${tgt:,.0f} coverage), "
                f"so it is still deducted if the account cancels.")
    if state == "pending":
        return ("NOT PAID YET — commission pays the period the first service is "
                "both completed and its invoice fully paid.")
    if state == "not_started":
        return "No completed-and-paid first service yet — nothing has been earned."
    if state == "clawed_back":
        return (f"DEDUCTED — cancelled {dc} ({cr}) before vesting "
                f"({q}/3 services, ${cov:,.0f}/${tgt:,.0f} coverage). If the "
                f"customer later pays up / reactivates, it is re-paid automatically.")
    if state == "cancelled_unpaid":
        return (f"Cancelled {dc} ({cr}) before the first invoice was paid — "
                f"nothing was ever paid, nothing is owed.")
    if state == "excluded_renewal":
        return (f"Cancelled {dc} as RENEWAL — rep keeps the commission (rule §5).")
    if state == "excluded_moved":
        return (f"Cancelled {dc} as MOVED — no deduction (rule §5).")
    if state == "excluded_resigned":
        return (f"Cancelled {dc} but the customer re-signed — no deduction (rule §5).")
    if state == "half_kept_fee_paid":
        return (f"Cancelled {dc} but the cancellation fee for THIS cancellation "
                f"was paid with real money — rep keeps half (rule §5).")
    if state == "excluded_prestart":
        return ("Sold before the rep joined inside sales (their ISA start date) — "
                "not an ISA sale: no commission and no deduction (rule §1a).")
    return state


@app.get("/api/commissions/lookup")
def api_commissions_lookup(q: str = "") -> dict:
    """Agent Metrics -> Commission Lookup: account number, subscription id, or
    customer name -> every subscription's commission state, from the IS
    Commissions engine database (read-only)."""
    q = (q or "").strip()
    if not q:
        return {"results": []}
    db = Path(_settings.commissions_db)
    if not db.is_file():
        return {"error": f"commissions database not found at {db} — set COMMISSIONS_DB"}
    import sqlite3 as _sq
    conn = _sq.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    try:
        if q.isdigit():
            where, args = ("(sub.customer_id = ? OR sub.subscription_id = ?)",
                           [q, q])
        else:
            where, args = ("cu.name LIKE ?", [f"%{q}%"])
        subs = conn.execute(f"""
            SELECT sub.subscription_id, sub.customer_id, cu.name AS customer,
                   cu.card_on_file, sub.rep, sub.service_type, sub.source,
                   sub.contract_value, sub.date_added, sub.active,
                   sub.date_cancelled AS cancel_date, sub.cancel_reason,
                   st.state, st.rate, st.expected, st.first_paid_date AS first_paid,
                   st.qualifying_completed AS qualifying, st.coverage_paid,
                   st.coverage_target, st.vested_date AS vested,
                   COALESCE(st.flags,'') AS flags
            FROM subscriptions sub
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            LEFT JOIN sub_status st USING (subscription_id)
            WHERE {where}
            ORDER BY sub.customer_id, sub.date_added DESC LIMIT 40""",
            args).fetchall()
        out = []
        for r in subs:
            d = dict(r)
            d["why"] = _lookup_why(d)
            d["splits"] = [dict(a) for a in conn.execute(
                "SELECT rep, share, type, note FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL",
                (r["subscription_id"],))]
            for k in ("date_added", "cancel_date", "first_paid", "vested"):
                d[k] = (d[k] or "")[:10]
            out.append(d)
        synced = conn.execute(
            "SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"results": out,
                "data_as_of": (synced["value"] if synced else None)}
    except _sq.OperationalError as e:
        return {"error": f"lookup failed: {e}"}
    finally:
        conn.close()


# --------------- Payroll -> Downloads (xlsx reports on demand) ---------------
_BRANCHES = {"1": "Riverside", "2": "Orange County", "3": "Portland",
             "4": "San Diego", "5": "Fresno", "6": "LA", "8": "Sacramento",
             "9": "San Jose", "10": "Spokane"}


def _comm_db():
    import sqlite3 as _sq
    db = Path(_settings.commissions_db)
    if not db.is_file():
        return None
    conn = _sq.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    return conn


def _comm_reports_dir() -> Path:
    return Path(_settings.commissions_db).parent / "reports"


# Go-live (Dallin, 2026-10-15): 10/11-10/24 (payroll 10/26) is the first pay
# period paid FROM the app. Every earlier statement is a parallel run —
# re-simulated under later rules, never the basis of pay — and says so.
PRE_GO_LIVE_BANNER = ("PRE-GO-LIVE — parallel run, not the basis of pay. "
                      "The manual payroll sheet paid this period.")


def _live_from():
    """Sunday start of the first pay period paid from the app."""
    from datetime import date as _date
    return _date.fromisoformat(_settings.payroll_live_from)


def _is_pre_go_live(period_end) -> bool:
    """A period ending before go-live (a date or 'YYYY-MM-DD')."""
    from datetime import date as _date
    if isinstance(period_end, str):
        period_end = _date.fromisoformat(period_end)
    return period_end < _live_from()


def _mark_pre_go_live(wb) -> None:
    """Stamp a pre-go-live payroll workbook so a forwarded copy can't pass
    for a statement that was paid: red banner over the Summary, red tabs,
    and the workbook title."""
    from openpyxl.styles import Alignment, Font, PatternFill
    ws = wb["Summary"]
    ws.insert_rows(1)
    ws.merge_cells("A1:D1")
    ws["A1"] = PRE_GO_LIVE_BANNER
    ws["A1"].font = Font(name="Arial", size=11, bold=True, color="9C0006")
    ws["A1"].fill = PatternFill("solid", fgColor="FFC7CE")
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A3"
    for sh in wb.worksheets:
        sh.sheet_properties.tabColor = "C00000"
    wb.properties.title = PRE_GO_LIVE_BANNER


def _tech_from_note(note: str) -> str:
    """Tech name from an upsell note: 'tech ref[erral][:] First Last' — the
    name must look like a name (capitalized word with lowercase letters), so
    'tech referral ACV +176' never matches."""
    import re as _re
    m = _re.search(r"tech ref(?:erral)?:?\s+([A-Z][a-z]+(?:\s+[A-Z][\w.'-]*)+)",
                   note or "")
    return m.group(1).strip() if m else "(tech not named on the form)"


def _xlsx_response(wb, filename: str):
    import io
    from fastapi.responses import Response
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _sheet_fill(ws, header: list, rows: list, money_cols: tuple = (),
                widths: dict | None = None) -> None:
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    HDR = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    FILL = PatternFill("solid", fgColor="1F3864")
    ws.append(header)
    for c in range(1, len(header) + 1):
        ws.cell(row=ws.max_row, column=c).font = HDR
        ws.cell(row=ws.max_row, column=c).fill = FILL
    for r in rows:
        ws.append(r)
    for c in money_cols:
        for row in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            row[0].number_format = '$#,##0.00;($#,##0.00);"-"'
    for c, w in (widths or {}).items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = ws.cell(row=2, column=1).coordinate


@app.get("/api/commissions/reports")
def api_commissions_reports() -> dict:
    """What is available to download: statement periods, D2D years, DB counts."""
    import re as _re
    periods = []
    if _comm_reports_dir().is_dir():
        for f in _comm_reports_dir().glob("commission_statement_*.xlsx"):
            m = _re.match(r"commission_statement_(\d{4}-\d{2}-\d{2})", f.name)
            if m:
                periods.append(m.group(1))
    periods = sorted(set(periods), reverse=True)
    conn = _comm_db()
    years, counts, as_of = [], {}, None
    if conn:
        try:
            tags = set()
            for (d2d,) in conn.execute(
                    "SELECT COALESCE(d2d_host, d2d_rep) FROM upsells "
                    "WHERE d2d_host IS NOT NULL OR d2d_rep IS NOT NULL"):
                m = _re.search(r"20\d\d", d2d or "")
                if m:
                    tags.add(m.group(0))
            years = sorted(tags, reverse=True)
            for state, n in conn.execute(
                    "SELECT state, COUNT(*) FROM sub_status GROUP BY state"):
                counts[state] = n
            row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
            as_of = row["value"] if row else None
        finally:
            conn.close()
    return {"periods": periods, "d2d_years": years, "states": counts,
            "data_as_of": as_of, "live_from": _live_from().isoformat(),
            "pre_go_live": [p for p in periods if _is_pre_go_live(p)]}


@app.get("/api/commissions/report.xlsx")
def api_commissions_report_xlsx(period: str):
    """One pay period's payroll workbook: Summary, Commissions, Deductions,
    Tech Referral Upsells (for branch managers), then one sheet per rep."""
    import re as _re
    from collections import defaultdict
    from datetime import date as _date, timedelta as _td
    from openpyxl import Workbook, load_workbook
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", period or ""):
        return JSONResponse({"error": "period must be YYYY-MM-DD"}, status_code=400)
    src = _comm_reports_dir() / f"commission_statement_{period}.xlsx"
    if not src.is_file():
        return JSONResponse({"error": f"no statement for {period}"}, status_code=404)
    pre = _is_pre_go_live(period)

    stmt = load_workbook(src, read_only=True, data_only=True)
    hdr, comm_rows, ded_rows = None, [], []
    for tab, dest in (("Commissions", comm_rows), ("Deductions", ded_rows)):
        it = stmt[tab].iter_rows(values_only=True)
        h = [str(x) for x in next(it)]
        if hdr is None:
            hdr = h
        for r in it:
            if r and r[0] and r[4] is not None:
                dest.append([("" if v is None else v) for v in r[:len(hdr)]])

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    pos, neg = defaultdict(float), defaultdict(float)
    for r in comm_rows:
        pos[r[0]] += float(r[4])
    for r in ded_rows:
        neg[r[0]] += float(r[4])
    srows, tp, tn = [], 0.0, 0.0
    for rep in sorted(set(pos) | set(neg)):
        p, n = round(pos.get(rep, 0), 2), round(neg.get(rep, 0), 2)
        tp += p; tn += n
        srows.append([rep, p, n, round(p + n, 2)])
    srows.append(["TOTAL", round(tp, 2), round(tn, 2), round(tp + tn, 2)])
    _sheet_fill(summary, ["Rep", "Commissions", "Deductions", "Net"], srows,
                money_cols=(2, 3, 4), widths={1: 22, 2: 13, 3: 13, 4: 13})

    _sheet_fill(wb.create_sheet("Commissions"), hdr, comm_rows, money_cols=(5,),
                widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})
    _sheet_fill(wb.create_sheet("Deductions"), hdr, ded_rows, money_cols=(5,),
                widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})

    # tech-referral upsells credited this period, for the branch managers
    end = _date.fromisoformat(period)
    start = end - _td(days=13)
    tech = []
    conn = _comm_db()
    if conn:
        try:
            for d, cust, name, office, d2d, amount, note in conn.execute("""
                    SELECT u.date, sub.customer_id, cu.name, sub.office_id,
                           u.d2d_rep, u.amount, COALESCE(u.note,'')
                    FROM upsells u
                    LEFT JOIN subscriptions sub USING (subscription_id)
                    LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                    WHERE u.removed_at IS NULL AND u.date >= ? AND u.date <= ?
                      AND lower(COALESCE(u.note,'')) LIKE '%tech ref%'
                    ORDER BY sub.office_id, u.date""",
                    (start.isoformat(), end.isoformat())):
                t = (d2d or "").strip() or _tech_from_note(note)
                # 9% of the ACV change; 5% on commercial accounts (RULES §12)
                rate = 0.05 if "commercial 5%" in note.lower() else 0.09
                tech.append([cust or "", _BRANCHES.get(str(office), "?"),
                             t, round(float(amount) / rate, 2), name or "", d])
        finally:
            conn.close()
    _sheet_fill(wb.create_sheet("Tech Referral Upsells"),
                ["Account #", "Branch", "Tech", "ACV change", "Customer", "Date"],
                tech or [["(none this period)"]], money_cols=(4,),
                widths={1: 11, 2: 15, 3: 24, 4: 12, 5: 24, 6: 12})

    # one sheet per rep: their commissions, a spacer row, their deductions
    for rep in sorted(set(pos) | set(neg)):
        safe = _re.sub(r"[\\/*?\[\]:]", "", rep)[:31]
        ws = wb.create_sheet(safe)
        mine_c = [r for r in comm_rows if r[0] == rep]
        mine_d = [r for r in ded_rows if r[0] == rep]
        net = round(pos.get(rep, 0) + neg.get(rep, 0), 2)
        ws.append([("PRE-GO-LIVE (not the basis of pay) — " if pre else "") +
                   f"{rep} — {period} payroll: "
                   f"{len(mine_c)} commissions ${pos.get(rep, 0):,.2f}, "
                   f"{len(mine_d)} deductions ${neg.get(rep, 0):,.2f}, net ${net:,.2f}"])
        ws.append([])
        _sheet_fill(ws, hdr, mine_c + ([[]] if mine_d else []) + mine_d,
                    money_cols=(5,), widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})
    # items the live close left for a human decision (RULES 8c): second tab
    if "Review" in stmt.sheetnames:
        rv = list(stmt["Review"].iter_rows(values_only=True))
        if len(rv) > 1:
            ws = wb.create_sheet("Review", 1)
            _sheet_fill(ws, [str(x) for x in rv[0]],
                        [[("" if v is None else v) for v in r] for r in rv[1:]],
                        money_cols=(), widths={1: 14, 2: 12, 3: 26, 4: 110})
    if pre:
        _mark_pre_go_live(wb)
    return _xlsx_response(
        wb, f"payroll_{period}{'_PRE-GO-LIVE' if pre else ''}.xlsx")


@app.get("/api/commissions/database.xlsx")
def api_commissions_database_xlsx():
    """The live commission database: every account's state, plus focused
    sheets for pending-payment, paid-not-vested, splits, and upsell credits."""
    from openpyxl import Workbook
    conn = _comm_db()
    if conn is None:
        return JSONResponse({"error": "commissions database not found"}, status_code=404)
    try:
        hdr = ["Rep", "Account #", "Customer", "Subscription #", "Plan", "Source",
               "Contract Value", "Rate", "Expected $", "State", "Sold",
               "First Paid", "Services", "Coverage Paid", "Coverage Target",
               "Vested", "Active", "Cancelled", "Cancel Reason", "Card", "Flags"]
        all_rows = []
        for r in conn.execute("""
                SELECT st.rep, sub.customer_id, cu.name, sub.subscription_id,
                       sub.service_type, sub.source, sub.contract_value, st.rate,
                       st.expected, st.state, sub.date_added,
                       st.first_paid_date, st.qualifying_completed,
                       st.coverage_paid, st.coverage_target, st.vested_date,
                       sub.active, sub.date_cancelled, sub.cancel_reason,
                       cu.card_on_file, COALESCE(st.flags,'')
                FROM sub_status st
                JOIN subscriptions sub USING (subscription_id)
                LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                ORDER BY st.rep, sub.date_added DESC"""):
            row = list(r)
            for i in (10, 11, 15, 17):
                row[i] = (row[i] or "")[:10]
            row[16] = "Yes" if row[16] else "No"
            row[19] = "Yes" if row[19] else "No"
            all_rows.append(row)

        wb = Workbook()
        ws = wb.active
        ws.title = "All accounts"
        W = {1: 20, 2: 11, 3: 24, 4: 13, 5: 20, 6: 16, 7: 13, 8: 7, 9: 11,
             10: 15, 11: 11, 12: 11, 13: 9, 14: 13, 15: 14, 16: 11, 17: 7,
             18: 11, 19: 16, 20: 6, 21: 28}
        _sheet_fill(ws, hdr, all_rows, money_cols=(7, 9, 14, 15), widths=W)

        pending = [r for r in all_rows
                   if r[9] in ("pending", "not_started") and r[16] == "Yes"]
        _sheet_fill(wb.create_sheet("Pending payment"), hdr, pending,
                    money_cols=(7, 9, 14, 15), widths=W)
        paid = [r for r in all_rows if r[9] == "paid"]
        _sheet_fill(wb.create_sheet("Paid - not vested"), hdr, paid,
                    money_cols=(7, 9, 14, 15), widths=W)

        splits = []
        for a in conn.execute("SELECT * FROM assignments WHERE removed_at IS NULL "
                              "ORDER BY created_at DESC").fetchall():
            s = conn.execute(
                """SELECT sub.customer_id, cu.name, sub.contract_value, st.state
                   FROM subscriptions sub
                   LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                   LEFT JOIN sub_status st USING (subscription_id)
                   WHERE sub.subscription_id = ?""",
                (a["subscription_id"],)).fetchone()
            splits.append([a["rep"], a["share"], a["type"], a["subscription_id"],
                           s["customer_id"] if s else "", (s and s["name"]) or "",
                           s["contract_value"] if s else "",
                           (s and s["state"]) or "", (a["created_at"] or "")[:10],
                           a["note"] or ""])
        _sheet_fill(wb.create_sheet("Splits & assignments"),
                    ["Rep", "Share", "Type", "Subscription #", "Account #",
                     "Customer", "Contract Value", "State", "Booked", "Note"],
                    splits, money_cols=(7,),
                    widths={1: 20, 2: 7, 3: 10, 4: 13, 5: 11, 6: 24, 7: 13,
                            8: 14, 9: 11, 10: 50})

        cols = {r[1] for r in conn.execute("PRAGMA table_info(upsells)")}
        ups = [[u["rep"], u["subscription_id"], u["amount"], u["acv_increase"],
                u["date"], u["d2d_rep"] or "",
                (u["d2d_host"] if "d2d_host" in cols else "") or "",
                u["note"] or ""]
               for u in conn.execute(
                   "SELECT * FROM upsells WHERE removed_at IS NULL ORDER BY date DESC")]
        _sheet_fill(wb.create_sheet("Upsell credits"),
                    ["ISA Rep", "Subscription #", "Credit $", "ACV increase",
                     "Date", "D2D Rep / Tech", "D2D Host (auto)", "Note"],
                    ups, money_cols=(3, 4),
                    widths={1: 20, 2: 13, 3: 10, 4: 12, 5: 11, 6: 22, 7: 24, 8: 55})
        return _xlsx_response(
            wb, f"commission_database_{datetime.now():%Y-%m-%d}.xlsx")
    finally:
        conn.close()


@app.get("/api/commissions/d2d.xlsx")
def api_commissions_d2d_xlsx(year: str):
    """Door-to-door year-end report: every D2D account upsold by inside sales,
    for reps carrying the given year tag (plus date-matched rows with no tag)."""
    import re as _re
    from openpyxl import Workbook
    if not _re.fullmatch(r"20\d\d", year or ""):
        return JSONResponse({"error": "year must be like 2026"}, status_code=400)
    conn = _comm_db()
    if conn is None:
        return JSONResponse({"error": "commissions database not found"}, status_code=404)
    try:
        hdr = ["D2D Rep (as tagged)", "Account #", "Customer", "Subscription #",
               "Plan", "Branch", "ISA Rep (upseller)", "Upsell Date",
               "Upsell Credit", "ACV Increase", "Note"]
        tagged, untagged = [], []
        for u in conn.execute("""
                SELECT u.rep AS isa, u.subscription_id, u.amount, u.acv_increase,
                       u.date, u.d2d_rep, u.d2d_host, COALESCE(u.note,'') AS note,
                       sub.customer_id, cu.name, sub.service_type, sub.office_id
                FROM upsells u
                LEFT JOIN subscriptions sub USING (subscription_id)
                LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                WHERE u.removed_at IS NULL
                  AND (u.d2d_host IS NOT NULL OR u.d2d_rep IS NOT NULL)
                ORDER BY COALESCE(u.d2d_host, u.d2d_rep), u.date"""):
            # d2d_host is the auto-detected year-tagged seller from the host
            # sub's soldBy; d2d_rep is the form era's free text (or, on tech
            # referrals, the TECH's name — branch-manager payroll, not D2D)
            label = u["d2d_host"] or u["d2d_rep"]
            row = [label, u["customer_id"] or "", u["name"] or "",
                   u["subscription_id"], u["service_type"] or "",
                   _BRANCHES.get(str(u["office_id"]), "?"),
                   u["isa"], u["date"], u["amount"], u["acv_increase"], u["note"]]
            m = _re.search(r"20\d\d", label or "")
            if m and m.group(0) == year:
                tagged.append(row)
            elif (not m and str(u["date"] or "").startswith(year)
                  and not u["d2d_host"]
                  and "tech ref" not in (u["note"] or "").lower()):
                untagged.append(row)
        wb = Workbook()
        ws = wb.active
        ws.title = f"D2D {year}"
        W = {1: 30, 2: 11, 3: 24, 4: 13, 5: 20, 6: 14, 7: 20, 8: 11, 9: 12,
             10: 12, 11: 55}
        _sheet_fill(ws, hdr, tagged or [["(no tagged rows)"]],
                    money_cols=(9, 10), widths=W)
        _sheet_fill(wb.create_sheet(f"No year tag ({year} dates)"), hdr,
                    untagged or [["(none)"]], money_cols=(9, 10), widths=W)
        return _xlsx_response(wb, f"d2d_upsells_{year}.xlsx")
    finally:
        conn.close()


# --------------- Agent page -> Payroll (own report + own lookup) -------------
# Everything here is for ONE agent: `_my_agent` works out who from the
# signed-in session (never from the request), and every query is filtered to
# that name. An agent only ever sees FINAL pay periods — a draft run can still
# be re-run on its payroll Monday, so it isn't shown until that day passes.
_AGENT_TYPE_LABEL = {
    "initial": "New sale", "upsell": "Upsell", "restore": "Deduction given back",
    "trueup": "Contract value change", "transfer": "Account reassigned",
    "feeback": "Cancel fee paid - half back", "halfkeep": "Cancelled - half kept",
    "clawback": "Cancelled - deducted", "vest": "Vested",
    "baseline": "Manual payroll sheet",
}
_AGENT_STATE_LABEL = {
    "paid": "Paid - not vested yet", "vested": "Vested (locked)",
    "pending": "Not paid yet", "not_started": "Not earned yet",
    "clawed_back": "Deducted", "cancelled_unpaid": "Cancelled before first payment",
    "half_kept_fee_paid": "Cancelled - fee paid, half kept",
    "excluded_renewal": "Cancelled as renewal - kept",
    "excluded_moved": "Cancelled as moved - kept",
    "excluded_resigned": "Cancelled then re-signed - kept",
    "excluded_prestart": "Before your ISA start date",
}
_stmt_cache: dict = {}


def _us_date(v) -> str:
    """'2026-10-14' -> '10/14/2026' (blank stays blank)."""
    from datetime import date as _date
    s = str(v or "")[:10]
    try:
        return _date.fromisoformat(s).strftime("%m/%d/%Y")
    except ValueError:
        return s


def _agent_line_text(kind: str, amount: float, note: str = "", rate=None,
                     cancel_date: str = "", cancel_reason: str = "",
                     first_paid: str = "") -> str:
    """Plain-English explanation of one pay line. Built from the line's own
    fields; the engine's raw note is only mined for dates and numbers — it is
    never shown, since it carries manager emails, other reps' names (split
    changes) and internal state strings."""
    import re as _re
    note = note or ""
    if rate is None:
        m = _re.search(r"rate=([\d.]+)%|\(([\d.]+)%\)", note)
        if m:
            rate = float(m.group(1) or m.group(2)) / 100
    pct = f" at {float(rate) * 100:g}%" if rate not in (None, "") else ""
    if not cancel_date:
        m = _re.search(r"cancelled (\d{4}-\d{2}-\d{2})(?: \(([^)]*)\))?", note)
        if m:
            cancel_date, cancel_reason = m.group(1), cancel_reason or (m.group(2) or "")
    cxl = _us_date(cancel_date) or "(date not recorded)"
    why = cancel_reason if cancel_reason and cancel_reason != "(none)" else "no reason recorded"
    if kind == "initial":
        return ("First service completed and paid" +
                (f" {_us_date(first_paid)}" if first_paid else "") + f" - commission{pct}")
    if kind == "upsell":
        return f"Upsell credit{pct and ' -' + pct.replace(' at', '')} of the contract value increase"
    if kind == "restore":
        if "renewal" in note.lower():
            return "Cancel reason changed to Renewal - earlier deduction given back"
        return "Account reinstated - earlier deduction given back"
    if kind == "trueup":
        m = _re.search(r"contract value ([\d,.]+) -> ([\d,.]+)", note)
        if m:
            a, b = (float(x.replace(",", "")) for x in m.groups())
            return f"Contract value changed ${a:,.2f} -> ${b:,.2f} - commission adjusted{pct}"
        return f"Contract value changed - commission adjusted{pct}"
    if kind == "transfer":
        return ("Account reassigned to you - commission moved to you" if amount > 0
                else "Account reassigned to another rep - commission moved off")
    if kind == "feeback":
        m = _re.search(r"fee paid (\d{4}-\d{2}-\d{2})", note)
        when = f" {_us_date(m.group(1))}" if m else ""
        return f"Cancellation fee paid{when} after the deduction - half given back"
    if kind == "halfkeep":
        return f"Cancelled {cxl} and the cancellation fee was paid - you keep half"
    if kind == "clawback":
        return f"Cancelled {cxl} ({why}) before vesting - commission taken back"
    if kind == "vest":
        return "Vested - locked permanently, can never be deducted"
    if kind == "baseline":
        return "What the manual payroll sheet paid you, net of any deductions"
    return "Adjustment"


def _agent_final_periods() -> list[str]:
    """Statement periods an agent may see, newest first: paid from the app
    (on/after go-live), with a statement file, and FINAL — marked final, or
    its payroll Monday (period end + 2 days) has passed (Pacific)."""
    import re as _re
    from datetime import date as _date, timedelta as _td

    from .periods import PACIFIC
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    final = set()
    conn = _comm_db()
    if conn:
        try:
            final = {r[0] for r in conn.execute(
                "SELECT period_end FROM payroll_runs WHERE status = 'final'")}
        except Exception:                        # older engine DB: no table yet
            final = set()
        finally:
            conn.close()
    out = []
    if _comm_reports_dir().is_dir():
        for f in _comm_reports_dir().glob("commission_statement_*.xlsx"):
            m = _re.fullmatch(r"commission_statement_(\d{4}-\d{2}-\d{2})\.xlsx", f.name)
            if not m or _is_pre_go_live(m.group(1)):
                continue
            p = m.group(1)
            if p in final or today > _date.fromisoformat(p) + _td(days=2):
                out.append(p)
    return sorted(set(out), reverse=True)


def _stmt_lines(period: str):
    """(commission rows, deduction rows) of a statement, all reps; cached by
    file mtime so the periods list doesn't re-read every workbook."""
    from openpyxl import load_workbook
    src = _comm_reports_dir() / f"commission_statement_{period}.xlsx"
    if not src.is_file():
        return None
    mt = src.stat().st_mtime
    hit = _stmt_cache.get(period)
    if hit and hit[0] == mt:
        return hit[1]
    wb = load_workbook(src, read_only=True, data_only=True)
    try:
        out = []
        for tab in ("Commissions", "Deductions"):
            it = wb[tab].iter_rows(values_only=True)
            next(it, None)
            out.append([tuple(r) for r in it if r and r[0] and r[4] is not None])
    finally:
        wb.close()
    if len(_stmt_cache) > 12:
        _stmt_cache.clear()
    _stmt_cache[period] = (mt, tuple(out))
    return _stmt_cache[period][1]


def _agent_totals(rep: str, period: str) -> dict | None:
    lines = _stmt_lines(period)
    if lines is None:
        return None
    comm = [r for r in lines[0] if r[0] == rep]
    ded = [r for r in lines[1] if r[0] == rep]
    tc = round(sum(float(r[4]) for r in comm), 2)
    td = round(sum(float(r[4]) for r in ded), 2)
    return {"commission": tc, "deductions": td, "payout": round(tc + td, 2),
            "commission_lines": len(comm), "deduction_lines": len(ded)}


@app.get("/api/my/payroll/periods")
def api_my_payroll_periods(request: Request, as_agent: str = ""):
    """The agent's final pay periods, newest first, with their own totals."""
    from datetime import date as _date, timedelta as _td
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    out = []
    for p in _agent_final_periods():
        t = _agent_totals(name, p)
        if t is None:
            continue
        end = _date.fromisoformat(p)
        out.append({"period_end": p, "start": (end - _td(days=13)).isoformat(), **t})
    return {"agent": name, "periods": out}


@app.get("/api/my/payroll/report.csv")
def api_my_payroll_report_csv(request: Request, period: str = "", as_agent: str = ""):
    """The agent's own commission report for one final pay period: totals,
    then every commission and deduction line, from the locked statement."""
    import csv
    import io
    import re as _re
    from datetime import date as _date, timedelta as _td

    from fastapi.responses import Response
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", period or ""):
        return JSONResponse({"error": "period must be YYYY-MM-DD"}, status_code=400)
    if period not in _agent_final_periods():
        return JSONResponse({"error": "That pay period isn't available yet."}, status_code=404)
    lines = _stmt_lines(period)
    comm = [r for r in lines[0] if r[0] == name]
    ded = [r for r in lines[1] if r[0] == name]
    t = _agent_totals(name, period)
    end = _date.fromisoformat(period)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Inside Sales Commission Report"])
    w.writerow(["Agent", name])
    w.writerow(["Pay period", f"{end - _td(days=13):%m/%d/%Y} - {end:%m/%d/%Y}"])
    w.writerow([])
    w.writerow(["Total commission", f"{t['commission']:.2f}", f"{t['commission_lines']} lines"])
    w.writerow(["Total deductions", f"{t['deductions']:.2f}", f"{t['deduction_lines']} lines"])
    w.writerow(["Commission payout", f"{t['payout']:.2f}"])
    w.writerow([])
    w.writerow(["Section", "Account #", "Customer", "Subscription #", "Plan",
                "Sold", "First Paid", "Contract Value", "Rate", "Amount",
                "Line Type", "Explanation", "Account Status Now",
                "Services Completed", "Coverage Paid", "Coverage Needed to Vest",
                "Cancel Date", "Cancel Reason"])
    num = lambda v: "" if v in (None, "") else f"{float(v):.2f}"
    for section, rows in (("Commission", comm), ("Deduction", ded)):
        for r in rows:
            amt = float(r[4])
            w.writerow([
                section, r[2] or "", r[3] or "", r[1] or "", r[9] or "",
                _us_date(r[10]), _us_date(r[11]), num(r[7]),
                "" if r[6] in (None, "") else f"{float(r[6]) * 100:g}%",
                f"{amt:.2f}", _AGENT_TYPE_LABEL.get(r[5], r[5] or ""),
                _agent_line_text(r[5], amt, r[20] or "", r[6], r[17] or "",
                                 r[18] or "", r[11] or ""),
                _AGENT_STATE_LABEL.get(r[16], r[16] or ""),
                "" if r[12] is None else r[12], num(r[13]), num(r[14]),
                _us_date(r[17]), "" if r[18] in (None, "(none)") else r[18]])
    u = _user(request)
    logging.getLogger("isadash.commissions").info(
        "agent report %s downloaded for %s by %s", period, name,
        (u or {}).get("email") or "?")
    safe = _re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),     # BOM: Excel opens it cleanly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="commission_report_{safe}_{period}.csv"',
                 "Cache-Control": "no-store"})


@app.get("/api/my/payroll/lookup")
def api_my_payroll_lookup(request: Request, q: str = "", as_agent: str = ""):
    """The agent's Commission Lookup — ONLY accounts tied to them: they sold
    it, are on a split/assignment, hold an upsell credit on it, or have ever
    had a pay line on it. Anything else simply isn't found. Shows their own
    share and their own pay history (final periods only); other reps on the
    account are never named."""
    import sqlite3 as _sq
    name = _my_agent(request, as_agent)
    if not name:
        return JSONResponse(_NO_AGENT, status_code=403)
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"results": []}
    conn = _comm_db()
    if conn is None:
        return {"error": "Commission data isn't available right now."}
    final = _agent_final_periods()
    # anything on/before this date is already on a finished payroll (or the
    # manual sheet, before go-live)
    from datetime import timedelta as _td
    last_final = final[0] if final else (_live_from() - _td(days=1)).isoformat()
    try:
        mine = """
            WITH mine AS (
              SELECT subscription_id FROM sub_status WHERE rep = :me
              UNION SELECT subscription_id FROM assignments
                    WHERE rep = :me AND removed_at IS NULL
              UNION SELECT subscription_id FROM upsells
                    WHERE rep = :me AND removed_at IS NULL
              UNION SELECT subscription_id FROM ledger
                    WHERE rep = :me AND kind != 'baseline')"""
        args = {"me": name}
        if q.isdigit():
            where = "(sub.customer_id = :q OR sub.subscription_id = :q)"
            args["q"] = q
        else:
            where = "cu.name LIKE :q ESCAPE '\\'"
            args["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        subs = conn.execute(mine + f"""
            SELECT sub.subscription_id, sub.customer_id, cu.name AS customer,
                   sub.service_type, sub.contract_value, sub.date_added,
                   sub.active, sub.date_cancelled AS cancel_date, sub.cancel_reason,
                   st.rep AS seller, st.state, st.rate, st.expected,
                   st.first_paid_date AS first_paid,
                   st.qualifying_completed AS qualifying, st.coverage_paid,
                   st.coverage_target, st.vested_date AS vested
            FROM subscriptions sub
            JOIN mine USING (subscription_id)
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            LEFT JOIN sub_status st USING (subscription_id)
            WHERE {where}
            ORDER BY sub.date_added DESC LIMIT 40""", args).fetchall()
        out = []
        for r in subs:
            d = dict(r)
            sid = d["subscription_id"]
            assigns = conn.execute(
                "SELECT rep, share, type FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL", (sid,)).fetchall()
            my_assign = [a for a in assigns if a["rep"] == name]
            ups = conn.execute(
                "SELECT amount, date FROM upsells WHERE subscription_id = ? "
                "AND rep = ? AND removed_at IS NULL", (sid, name)).fetchall()
            if my_assign:
                share = min(1.0, sum(float(a["share"]) for a in my_assign))
                kind = my_assign[0]["type"]
                pct = f"{share * 100:.0f}%"
                role = {"split": f"Split - your share {pct}",
                        "outbound": f"Shared with outbound - your share {pct}",
                        "rescue": "Rescued by you",
                        "reassign": "Reassigned to you"}.get(kind, f"Your share {pct}")
                if kind in ("rescue", "reassign") and share < 1:
                    role += f" - your share {pct}"
            elif d["seller"] == name and not assigns:
                share, role = 1.0, "You sold it"
            elif ups:
                share, role = 0.0, "Upsell credit"
            else:
                share, role = 0.0, "No longer on your account"
            # their own pay history: app payroll lines in final periods only.
            # Manual-sheet (pre-go-live) amounts are never shown (Dallin,
            # 2026-10-26) — only the fact that it was paid that way.
            hist, manual, pending = [], False, False
            for l in conn.execute(
                    "SELECT period_end, amount, kind, COALESCE(note,'') AS note "
                    "FROM ledger WHERE subscription_id = ? AND rep = ? "
                    "ORDER BY period_end, id", (sid, name)):
                if l["kind"] == "baseline":
                    manual = manual or float(l["amount"]) != 0
                    continue
                if l["period_end"] not in final:
                    pending = True
                    continue
                hist.append({"period_end": l["period_end"],
                             "label": _AGENT_TYPE_LABEL.get(l["kind"], "Adjustment"),
                             "amount": round(float(l["amount"]), 2),
                             "text": _agent_line_text(
                                 l["kind"], float(l["amount"]), l["note"],
                                 cancel_date=(d["cancel_date"] or "")[:10]
                                 if l["kind"] in ("clawback", "halfkeep") else "",
                                 cancel_reason=d["cancel_reason"] or "",
                                 first_paid=d["first_paid"] or "")})
            state = d["state"] or "not_started"
            why = _lookup_why(d)
            got = sum(h["amount"] for h in hist)
            fp = (d["first_paid"] or "")[:10]
            cx = (d["cancel_date"] or "")[:10]
            if role == "No longer on your account":
                why = ("This account is no longer credited to you; your pay "
                       "history on it is below.")
            elif (pending or (state in ("paid", "vested") and share > 0 and got <= 0
                              and fp and fp > last_final)
                  or (state in ("clawed_back", "half_kept_fee_paid")
                      and cx and cx > last_final)):
                why += " Not on a finished payroll yet - it will show on an upcoming one."
            mine_exp = (round(float(d["expected"]) * share, 2)
                        if d["expected"] is not None and share else None)
            out.append({
                "subscription_id": sid, "customer_id": d["customer_id"],
                "customer": d["customer"], "service_type": d["service_type"],
                "contract_value": d["contract_value"], "rate": d["rate"],
                "your_commission": mine_exp, "role": role,
                "upsell_credit": round(sum(float(u["amount"]) for u in ups), 2) if ups else None,
                "date_added": (d["date_added"] or "")[:10], "first_paid": fp,
                "qualifying": d["qualifying"], "coverage_paid": d["coverage_paid"],
                "coverage_target": d["coverage_target"],
                "vested": (d["vested"] or "")[:10], "active": bool(d["active"]),
                "cancel_date": cx, "cancel_reason": d["cancel_reason"],
                "state": state, "state_label": _AGENT_STATE_LABEL.get(state, state),
                "why": why, "history": hist, "manual_paid": manual,
                "paid_to_you": round(got, 2) if hist else None})
        synced = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"results": out, "data_as_of": synced["value"] if synced else None}
    except _sq.OperationalError:
        logging.getLogger("isadash.commissions").exception("agent lookup failed")
        return {"error": "Lookup failed - try again in a minute."}
    finally:
        conn.close()


# --------------- Payroll -> Submit Adjustment (structured intake) -----------
# The form writes a structured row into the commission engine's intake_queue
# and immediately spawns the engine's form_intake.py --queue-only, which
# verifies against the synced PestRoutes data and books it (or HOLDs it) —
# the same brain that processes the Google Form, minus all the free-text
# parsing. Nothing books without that verification step.
_INTAKE_TYPES = ("split", "outbound", "rescue", "upsell", "tech_upsell")
_INTAKE_DDL = """CREATE TABLE IF NOT EXISTS intake_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT DEFAULT (datetime('now','localtime')),
    submitted_by TEXT,
    type TEXT NOT NULL,
    account TEXT NOT NULL,
    agent1 TEXT, agent2 TEXT, tech TEXT, branch TEXT, acv REAL,
    sub_id_hint TEXT, note TEXT,
    status TEXT DEFAULT 'NEW',
    detail TEXT, est_amount REAL, rep TEXT, sub_id TEXT,
    processed_at TEXT, booked_ref TEXT)"""


def _comm_db_rw():
    """Read-write handle on the commission engine's DB (WAL, short writes)."""
    import sqlite3 as _sq
    db = Path(_settings.commissions_db)
    if not db.is_file():
        return None
    conn = _sq.connect(str(db), timeout=15)
    conn.row_factory = _sq.Row
    return conn


def _intake_roster() -> tuple[list[str], list[str]]:
    conn = connect(_settings.db_path)
    try:
        rows = conn.execute(
            "SELECT name, role FROM commission_roster ORDER BY name").fetchall()
        return ([r["name"] for r in rows if r["role"] == "inbound"],
                [r["name"] for r in rows if r["role"] == "outbound"])
    finally:
        conn.close()


def _comm_python() -> Path:
    """The commissions app's interpreter (its venv, else ours)."""
    import sys as _sys
    d = Path(_settings.commissions_app_dir)
    for py in (d / ".venv" / "bin" / "python3", d / ".venv" / "bin" / "python",
               d / "venv" / "bin" / "python3", d / "venv" / "bin" / "python",
               d / ".venv" / "Scripts" / "python.exe"):
        if py.exists():
            return py
    return Path(_sys.executable)


def _spawn_commissions(script_args: list[str], logname: str) -> bool:
    """Run a commissions-app script detached, output appended to logs/<name>.
    Best effort: the service may run with ProtectHome=read-only — if the logs
    dir isn't writable, spawn without a log rather than not at all."""
    import os as _os
    import subprocess as _sp
    d = Path(_settings.commissions_app_dir)
    script = d / "scripts" / script_args[0]
    if not script.is_file():
        return False
    kw = ({"start_new_session": True} if _os.name == "posix"
          else {"creationflags": 0x08000000})              # CREATE_NO_WINDOW
    try:
        try:
            logd = d / "logs"
            logd.mkdir(exist_ok=True)
            lf = open(logd / logname, "ab")
        except OSError:
            lf = _sp.DEVNULL
        try:
            _sp.Popen([str(_comm_python()), str(script)] + script_args[1:],
                      cwd=str(d), stdout=lf, stderr=lf, **kw)
        finally:
            if lf is not _sp.DEVNULL:
                lf.close()
        return True
    except OSError:
        logging.getLogger("isadash.commissions").exception(
            "spawn failed: %s", script_args)
        return False


def _run_intake_processor() -> bool:
    """Spawn form_intake.py --queue-only (detached) so a dashboard submission
    books within seconds. If it can't run, the row waits as NEW for the next
    pipeline run."""
    return _spawn_commissions(["form_intake.py", "--queue-only"], "intake.log")


@app.get("/api/commissions/intake/meta")
def api_commissions_intake_meta() -> dict:
    """Dropdown data for the Submit Adjustment form."""
    inbound, outbound = _intake_roster()
    as_of = None
    conn = _comm_db()
    if conn:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_sync'").fetchone()
            as_of = row["value"] if row else None
        finally:
            conn.close()
    return {"inbound": inbound, "outbound": outbound,
            "branches": sorted(_BRANCHES.values()), "data_as_of": as_of}


@app.get("/api/commissions/intake/account")
def api_commissions_intake_account(acct: str = "") -> dict:
    """Live account check for the form: the customer + every subscription,
    so the submitter confirms the right account (and can pick the sub)."""
    acct = (acct or "").strip()
    if not acct.isdigit():
        return {"error": "enter the PestRoutes account number (digits only)"}
    conn = _comm_db()
    if conn is None:
        return {"error": "commissions database not found"}
    try:
        cust = conn.execute(
            "SELECT customer_id, name, card_on_file, office_id, status "
            "FROM customers WHERE customer_id = ?", (acct,)).fetchone()
        # soldBy employee names live in the dashboard's own DB; a year-tagged
        # name (e.g. 'R- Max Barker 2026') marks a D2D-sold subscription
        import re as _re
        emp: dict[str, str] = {}
        dconn = connect(_settings.db_path)
        try:
            for r in dconn.execute(
                    "SELECT fr_employee_id, name FROM employees "
                    "WHERE name IS NOT NULL"):
                emp[str(r["fr_employee_id"])] = r["name"]
        finally:
            dconn.close()
        subs = []
        for r in conn.execute("""
                SELECT sub.subscription_id, sub.rep, sub.service_type,
                       sub.source, sub.contract_value, sub.date_added,
                       sub.active, sub.office_id, st.state, sub.sold_by
                FROM subscriptions sub
                LEFT JOIN sub_status st USING (subscription_id)
                WHERE sub.customer_id = ?
                ORDER BY sub.active DESC, sub.date_added DESC""", (acct,)):
            asg = conn.execute(
                "SELECT rep, share, type FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL",
                (r["subscription_id"],)).fetchall()
            ups = conn.execute(
                "SELECT COUNT(*) FROM upsells WHERE subscription_id = ? "
                "AND removed_at IS NULL", (r["subscription_id"],)).fetchone()[0]
            seller = emp.get(str(r["sold_by"] or ""), "")
            subs.append({
                "sub_id": r["subscription_id"], "rep": r["rep"],
                "service_type": r["service_type"], "source": r["source"],
                "cv": r["contract_value"],
                "sold": (r["date_added"] or "")[:10],
                "active": bool(r["active"]), "state": r["state"],
                "branch": _BRANCHES.get(str(r["office_id"]), ""),
                "d2d": seller if _re.search(r"\b20\d\d\b", seller) else "",
                "assignments": [f'{a["rep"]} {a["share"]:.0%} ({a["type"]})'
                                for a in asg],
                "upsells": ups})
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"tracked": bool(subs),
                "customer": ({"id": cust["customer_id"], "name": cust["name"],
                              "card": bool(cust["card_on_file"]),
                              "status": cust["status"],
                              "branch": _BRANCHES.get(str(cust["office_id"]), "")}
                             if cust else None),
                "subs": subs,
                "data_as_of": row["value"] if row else None}
    finally:
        conn.close()


@app.post("/api/commissions/intake")
def api_commissions_intake(body: dict, request: Request) -> dict:
    """Queue one structured adjustment and kick the booking processor."""
    typ = (body.get("type") or "").strip()
    acct = str(body.get("account") or "").strip()
    a1 = (body.get("agent1") or "").strip()
    a2 = (body.get("agent2") or "").strip()
    tech = (body.get("tech") or "").strip()
    branch = (body.get("branch") or "").strip()
    note = (body.get("note") or "").strip()
    hint = str(body.get("sub_id") or "").strip()
    raw_acv = body.get("acv")
    try:
        acv = float(raw_acv) if raw_acv not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "ACV change must be a number"}

    if typ not in _INTAKE_TYPES:
        return {"error": f"unknown adjustment type '{typ}'"}
    if not acct.isdigit():
        return {"error": "account # must be the numeric PestRoutes account number"}
    inbound, outbound = _intake_roster()
    if a1 not in inbound:
        return {"error": "pick the ISA rep from the roster dropdown"}
    if typ == "split" and (a2 not in inbound or a2 == a1):
        return {"error": "a split needs a second, different ISA rep"}
    if typ == "outbound" and a2 not in outbound:
        return {"error": "pick the outbound agent from the roster dropdown"}
    if typ in ("upsell", "tech_upsell") and (acv is None or acv <= 0):
        return {"error": "enter the ACV change (a positive dollar amount)"}
    if typ == "tech_upsell" and len(tech) < 2:
        return {"error": "tech referral needs the technician's name"}

    u = _user(request)
    by = (u and (u.get("name") or u.get("email"))) or "admin"
    conn = _comm_db_rw()
    if conn is None:
        return {"error": "commissions database not found"}
    try:
        conn.execute(_INTAKE_DDL)
        cur = conn.execute(
            """INSERT INTO intake_queue(submitted_by, type, account, agent1,
               agent2, tech, branch, acv, sub_id_hint, note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (by, typ, acct, a1, a2, tech, branch, acv, hint, note))
        conn.commit()
        qid = cur.lastrowid
    finally:
        conn.close()
    processing = _run_intake_processor()
    logging.getLogger("isadash.commissions").info(
        "intake #%s queued by %s: %s acct %s (%s%s)%s", qid, by, typ, acct, a1,
        f" + {a2}" if a2 else "", "" if processing else " — processor NOT started")
    return {"ok": True, "id": qid, "processing": processing}


@app.get("/api/commissions/intake/list")
def api_commissions_intake_list(limit: int = 30) -> dict:
    """Recent submissions with their booking outcome, newest first."""
    import sqlite3 as _sq
    conn = _comm_db()
    if conn is None:
        return {"rows": []}
    try:
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT id, submitted_at, submitted_by, type, account, agent1,
                   agent2, tech, branch, acv, status, detail, est_amount, rep,
                   sub_id FROM intake_queue ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit), 200)),))]
        except _sq.OperationalError:          # no submissions yet
            rows = []
        return {"rows": rows}
    finally:
        conn.close()


@app.post("/api/commissions/intake/delete")
def api_commissions_intake_delete(body: dict, request: Request) -> dict:
    """Delete one submission and take off whatever it booked. Runs the
    engine's form_intake.py --delete, which holds the same lock as booking,
    so a delete never races a submission being booked."""
    import json as _json
    import os as _os
    import subprocess as _sp
    try:
        qid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"error": "missing submission id"}
    u = _user(request)
    by = (u and (u.get("name") or u.get("email"))) or "admin"
    d = Path(_settings.commissions_app_dir)
    script = d / "scripts" / "form_intake.py"
    if not script.is_file():
        return {"error": "commissions app not found"}
    kw = {} if _os.name == "posix" else {"creationflags": 0x08000000}
    log = logging.getLogger("isadash.commissions")
    try:
        p = _sp.run([str(_comm_python()), str(script), "--delete", str(qid),
                     "--by", by], cwd=str(d), capture_output=True, text=True,
                    timeout=120, **kw)
    except _sp.TimeoutExpired:
        return {"error": "the adjustment processor is busy — try again in a minute"}
    out = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            out = _json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict):
        log.error("intake delete #%s failed (exit %s): %s", qid, p.returncode,
                  (p.stderr or p.stdout or "")[-800:])
        return {"error": "delete failed — nothing was changed"}
    log.info("intake #%s deleted by %s: %s", qid, by, out.get("message"))
    return out if out.get("ok") else {"error": out.get("message") or "delete failed"}


# --------------- Payroll -> Lock & run payroll (the button) ------------------
def _payroll_periods() -> tuple:
    """(latest CLOSED period end, its start, the running period's end) —
    14-day Sun..Sat periods anchored on the payroll calendar. A period is
    closed from the Sunday after its Saturday end."""
    from datetime import date as _date, timedelta as _td
    anchor_end = _date.fromisoformat(_settings.payroll_anchor_monday) - _td(days=2)
    today = _date.today()
    k = ((today - anchor_end).days - 1) // 14
    closed_end = anchor_end + _td(days=14 * k)
    return closed_end, closed_end - _td(days=13), closed_end + _td(days=14)


def _pid_alive(pid) -> bool:
    import os as _os
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if _os.name == "posix":
        return Path(f"/proc/{pid}").exists()
    import ctypes                              # NEVER os.kill(pid, 0) on
    h = ctypes.windll.kernel32.OpenProcess(    # Windows — that terminates it
        0x1000, False, pid)                    # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def _payroll_run_status() -> dict:
    import json as _json
    st = {}
    f = Path(_settings.commissions_app_dir) / "data" / "payroll_run.json"
    if f.is_file():
        try:
            st = _json.loads(f.read_text())
        except (OSError, ValueError):
            st = {}
    # a stale "running" (box rebooted mid-run) must not wedge the button
    if st.get("state") == "running" and not _pid_alive(st.get("pid")):
        st["state"] = "stale"
    return st


@app.get("/api/commissions/runpayroll/status")
def api_commissions_runpayroll_status() -> dict:
    """Button state: the latest closed period, whether its statement exists,
    and the live run status."""
    from datetime import date as _d, timedelta as _td
    closed_end, closed_start, next_end = _payroll_periods()
    stmt = _comm_reports_dir() / f"commission_statement_{closed_end}.xlsx"
    ran_at = None
    if stmt.is_file():
        ran_at = datetime.fromtimestamp(stmt.stat().st_mtime).strftime(
            "%a %m/%d %I:%M %p")
    return {"period_end": closed_end.isoformat(),
            "period_start": closed_start.isoformat(),
            "next_period_end": next_end.isoformat(),
            "statement_exists": stmt.is_file(), "ran_at": ran_at,
            "pre_go_live": _is_pre_go_live(closed_end),
            "live_from": _live_from().isoformat(),
            "payroll_monday": (closed_end + _td(days=2)).isoformat(),
            # its payroll Monday has passed: paid, locked (the commissions app
            # refuses the re-run too; later changes land in the next period)
            "paid_locked": stmt.is_file() and _d.today() > closed_end + _td(days=2),
            "run": _payroll_run_status()}


@app.post("/api/commissions/runpayroll")
def api_commissions_runpayroll(body: dict, request: Request) -> dict:
    """Lock adjustments & run payroll for the latest closed period: books
    every pending submission, pulls fresh PestRoutes data, and generates the
    statement. Detached; progress via the status endpoint."""
    closed_end, _, _ = _payroll_periods()
    period = (body or {}).get("period_end") or closed_end.isoformat()
    if period != closed_end.isoformat():
        return {"error": f"only the latest closed period ({closed_end}) can "
                         "be run from here"}
    if _is_pre_go_live(closed_end):
        return {"error": f"{closed_end} is a pre-go-live period — the manual "
                         "payroll sheet paid it, so it is not re-run from here. "
                         f"The first live period ({_live_from()} on) unlocks "
                         "the Sunday after it closes."}
    from datetime import date as _d, timedelta as _td
    stmt = _comm_reports_dir() / f"commission_statement_{closed_end}.xlsx"
    if stmt.is_file() and _d.today() > closed_end + _td(days=2):
        return {"error": f"{closed_end} was paid on {closed_end + _td(days=2)} "
                         "— locked; later changes land in the next payroll"}
    st = _payroll_run_status()
    if st.get("state") == "running":
        return {"error": "a payroll run is already in progress", "run": st}
    args = ["run_payroll.py", "--period-end", period]
    if (body or {}).get("skip_sync"):
        args.append("--skip-sync")
    if not _spawn_commissions(args, "payroll_run.log"):
        return {"error": "could not start the run — check COMMISSIONS_APP_DIR "
                         "on the server"}
    u = _user(request)
    logging.getLogger("isadash.commissions").info(
        "payroll run for %s started by %s", period,
        (u and (u.get("name") or u.get("email"))) or "admin")
    return {"ok": True, "period_end": period}


@app.get("/api/agentview")
def api_agentview(days: int = 30, start_day: str = "", end_day: str = "") -> dict:
    """Every roster agent's full metric set over a trailing window OR a
    custom Pacific date range, plus the team aggregate."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if start_day and end_day:
        from .rangestats import utc_bounds
        start, range_end = utc_bounds(start_day, end_day)
        start_date, end_date = start_day, end_day
    else:
        days = max(1, min(days, 400))
        start = (now - timedelta(days=days)).isoformat()
        range_end = now.isoformat()
        start_date, end_date = start[:10], "9999-12-31"
    conn = connect(_settings.db_path)
    try:
        stats: dict[str, dict] = {}

        def bucket(agent: str) -> dict:
            return stats.setdefault(agent, {
                "calls": 0, "leads": set(), "cf": set(), "own": set(), "anyc": set(),
                "closed": 0, "followup": 0, "unqualified": 0, "sales_calls": 0,
                "subs": 0, "pending": 0, "multi": 0, "cv_total": 0.0, "cv_n": 0,
                "audit_qual": 0, "audit_sales": 0,
            })

        for r in conn.execute(
            """SELECT c.ani_e164, c.dispo_group, c.converted_sub_id,
                      c.converted_any_sub_id, m.genesys_name AS agent
               FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
               WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
            (start, range_end)):
            for who in (r["agent"], "TEAM"):
                b = bucket(who)
                b["calls"] += 1
                ph = r["ani_e164"]
                if r["dispo_group"] in ("closed", "followup", "unqualified"):
                    b["sales_calls"] += 1
                    b[r["dispo_group"]] += 1
                    if ph:
                        b["leads"].add(ph)
                        if r["dispo_group"] != "unqualified":
                            b["cf"].add(ph)
                if ph and r["converted_sub_id"]:
                    b["own"].add(ph)
                if ph and r["converted_any_sub_id"]:
                    b["anyc"].add(ph)

        for agent, added, months, cv, status, ip, *_ in _roster_sub_rows(conn):
            if added < start or added >= range_end:
                continue
            for who in (agent, "TEAM"):
                b = bucket(who)
                b["subs"] += 1
                if status == "Pending":
                    b["pending"] += 1
                if months >= 18:
                    b["multi"] += 1
                if cv > 0:
                    b["cv_total"] += cv
                    b["cv_n"] += 1
                if ip > 0:
                    b["ip_total"] = b.get("ip_total", 0.0) + ip
                    b["ip_n"] = b.get("ip_n", 0) + 1

        # sold-to-serviced per agent (all statuses, own-name subs) — EVERY sale,
        # mosquito upsells and upsells included (Dallin, 2026-10-23); the pending
        # count under it ("…undecided") is the only pending row on this view
        fr_to_agent = {}
        for r in conn.execute(
            "SELECT genesys_name, fr_employee_id FROM agent_map "
            "WHERE on_roster=1 AND fr_employee_id IS NOT NULL"):
            for fid in r["fr_employee_id"].split(","):
                fr_to_agent[fid] = r["genesys_name"]
        sts_by: dict[str, list] = {}
        for r in conn.execute(
            "SELECT sold_by, initial_status FROM subscriptions "
            "WHERE date_added_utc >= ? AND date_added_utc < ? AND sold_by != '0'",
            (start, range_end)):
            agent = fr_to_agent.get(r["sold_by"])
            if agent:
                sts_by.setdefault(agent, []).append(r["initial_status"] or "?")
                sts_by.setdefault("TEAM", []).append(r["initial_status"] or "?")

        for r in conn.execute(
            "SELECT agent, SUM(qualified) q, SUM(sales) s FROM audit_daily "
            "WHERE date >= ? AND date <= ? GROUP BY agent",
            (start_date, end_date)):
            if r["agent"] in stats:
                stats[r["agent"]]["audit_qual"] = r["q"]
                stats[r["agent"]]["audit_sales"] = r["s"]
                stats["TEAM"]["audit_qual"] = stats["TEAM"].get("audit_qual", 0) + r["q"]
                stats["TEAM"]["audit_sales"] = stats["TEAM"].get("audit_sales", 0) + r["s"]

        from .adjust import adjustment_stats
        bound = _audit_end_utc(conn) or range_end
        adj_end = min(range_end, bound)
        adj = adjustment_stats(conn, start, adj_end) if adj_end > start else {}
        for agent, st in adj.items():
            if agent in stats and agent != "__TEAM__":
                stats[agent]["hidden"] = st["hidden"]
                stats[agent]["leaked"] = st["leaked"]
                stats[agent]["adj_conv"] = st["converted"]
        if "TEAM" in stats and "__TEAM__" in adj:  # phone-deduped team terms
            stats["TEAM"]["hidden"] = adj["__TEAM__"]["hidden"]
            stats["TEAM"]["leaked"] = adj["__TEAM__"]["leaked"]
            stats["TEAM"]["adj_conv"] = adj["__TEAM__"]["converted"]

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else None

        out = {}
        for agent, b in stats.items():
            conv_set = b["own"] | (b["cf"] & b["anyc"])
            leads = len(b["leads"] | conv_set)
            qual = len(b["cf"] | conv_set)
            conv = len(conv_set)
            out[agent] = {
                "calls": b["calls"], "leads": leads, "qual_leads": qual,
                "conversions": conv,
                "conv_qual_pct": pct(conv, qual),
                "conv_leads_pct": pct(conv, leads),
                "conv_calls_pct": pct(conv, b["calls"]),
                "closed_pct": pct(b["closed"], b["sales_calls"]),
                "followup_pct": pct(b["followup"], b["sales_calls"]),
                "unqualified_pct": pct(b["unqualified"], b["sales_calls"]),
                "subs": b["subs"], "pending": b["pending"],
                "multiyear_pct": pct(b["multi"], b["subs"]),
                "acv": round(b["cv_total"] / b["cv_n"], 2) if b["cv_n"] else None,
                "cv_total": round(b["cv_total"], 2),
                "init_avg": round(b.get("ip_total", 0) / b["ip_n"], 2)
                            if b.get("ip_n") else None,
                "sts_pct": _sts(sts_by.get(agent, []))["sts_pct"],
                "sts_pending": _sts(sts_by.get(agent, []))["pending"],
                "audit_qual": b["audit_qual"], "audit_sales": b["audit_sales"],
                "sheet_conv_pct": pct(b["audit_sales"], b["audit_qual"]),
                "hidden": b.get("hidden", 0), "leaked": b.get("leaked", 0),
                "true_conv_pct": pct(b.get("adj_conv", 0),
                                     max(b["audit_qual"] + b.get("hidden", 0)
                                         + b.get("leaked", 0),
                                         b.get("adj_conv", 0))),
            }
        agents = sorted(a for a in out if a != "TEAM")
        return {"days": days, "agents": agents, "stats": out}
    finally:
        conn.close()


def _audit_end_utc(conn) -> str | None:
    """UTC instant up to which the audit sheet has data: end of the last
    audited Pacific calendar day. Honest-conversion math never looks past
    this — call data after it has no audited denominator yet."""
    from datetime import timedelta

    from .periods import PACIFIC, UTC
    last = conn.execute("SELECT MAX(date) FROM audit_daily").fetchone()[0]
    if not last:
        return None
    end_local = (datetime.fromisoformat(last + "T00:00:00")
                 .replace(tzinfo=PACIFIC) + timedelta(days=1))
    return end_local.astimezone(UTC).isoformat()


def _week_utc_bounds(label: str) -> tuple[str, str]:
    """'YYYY-MM-DD..YYYY-MM-DD' (Pacific Sun–Sat) -> [start, end) in UTC ISO."""
    from datetime import timedelta

    from .periods import PACIFIC, UTC
    start_local = datetime.fromisoformat(label[:10] + "T00:00:00").replace(tzinfo=PACIFIC)
    end_local = start_local + timedelta(days=7)
    return (start_local.astimezone(UTC).isoformat(),
            end_local.astimezone(UTC).isoformat())


@app.get("/api/audit")
@cache.cached(copy_result=False)     # large and read-only: shared, as it always was
def api_audit() -> dict:
    """Audited (human) qualified/sales vs system dispositions/conversions,
    weekly Sun–Sat per agent, plus hidden/leaked adjustment terms."""
    from datetime import timezone as _tz

    from .adjust import adjustment_stats
    from .periods import week_label
    conn = connect(_settings.db_path)
    try:
        audit = conn.execute("SELECT date, agent, qualified, sales FROM audit_daily").fetchall()
        weeks: dict[str, dict[str, dict]] = {}
        for r in audit:
            # audit dates are Pacific calendar days; anchor at noon UTC so the
            # Sun–Sat week bucket is unambiguous
            dt = datetime.fromisoformat(r["date"] + "T12:00:00+00:00")
            w = week_label(dt)
            b = weeks.setdefault(w, {}).setdefault(r["agent"], {
                "audit_qualified": 0, "audit_sales": 0,
                "sys_qualified": 0, "sys_conversions": 0, "sys_unqualified": 0,
                "hidden": 0, "leaked": 0, "adj_conversions": 0,
            })
            b["audit_qualified"] += r["qualified"]
            b["audit_sales"] += r["sales"]
        # Only compare weeks where system call data exists — audit history
        # reaches back to 2023, Genesys pull starts 2025-06-29.
        first_call = conn.execute("SELECT MIN(start_utc) FROM calls").fetchone()[0]
        if first_call:
            dt = datetime.fromisoformat(first_call)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            min_week = week_label(dt)
            weeks = {w: v for w, v in weeks.items() if w >= min_week}
        if weeks:
            q = """
                SELECT c.start_utc, c.dispo_group, c.converted_sub_id,
                       c.converted_any_sub_id, c.ani_e164,
                       m.genesys_name AS agent
                FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
                WHERE m.on_roster = 1
            """
            cf_p: dict[tuple, set] = {}
            own_p: dict[tuple, set] = {}
            any_p: dict[tuple, set] = {}
            unq: dict[tuple, int] = {}
            for r in conn.execute(q):
                dt = datetime.fromisoformat(r["start_utc"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                w = week_label(dt)
                if w not in weeks:
                    continue
                k = (w, r["agent"])
                ph = r["ani_e164"]
                if r["dispo_group"] in ("closed", "followup") and ph:
                    cf_p.setdefault(k, set()).add(ph)
                elif r["dispo_group"] == "unqualified":
                    unq[k] = unq.get(k, 0) + 1
                if ph and r["converted_sub_id"]:
                    own_p.setdefault(k, set()).add(ph)
                if ph and r["converted_any_sub_id"]:
                    any_p.setdefault(k, set()).add(ph)
            for k in set(cf_p) | set(own_p) | set(any_p):
                w, agent = k
                if agent not in weeks.get(w, {}):
                    continue
                cf = cf_p.get(k, set())
                conv = own_p.get(k, set()) | (cf & any_p.get(k, set()))
                weeks[w][agent]["sys_qualified"] = len(cf | conv)
                weeks[w][agent]["sys_conversions"] = len(conv)
            for (w, agent), n in unq.items():
                if agent in weeks.get(w, {}):
                    weeks[w][agent]["sys_unqualified"] = n
        # hidden / leaked / bounded numerator, per week (never past the
        # last audited day)
        audit_bound = _audit_end_utc(conn)
        for w in weeks:
            a, b_ = _week_utc_bounds(w)
            if audit_bound:
                b_ = min(b_, audit_bound)
            if b_ <= a:
                continue
            adj = adjustment_stats(conn, a, b_)
            for agent, st in adj.items():
                if agent != "__TEAM__" and agent in weeks[w]:
                    weeks[w][agent]["hidden"] = st["hidden"]
                    weeks[w][agent]["leaked"] = st["leaked"]
                    weeks[w][agent]["adj_conversions"] = st["converted"]
        rows = []
        for w in sorted(weeks):
            for agent in sorted(weeks[w]):
                rows.append({"period": w, "agent": agent, **weeks[w][agent]})
        return {"rows": rows, "has_data": bool(rows)}
    finally:
        conn.close()


WEB_DIR = Path(__file__).resolve().parents[2] / "web"
# Every tab has its own address (web/router.js). They all serve the same page;
# the script then opens the tab the address names. Keep this list in step with
# the top-level words in router.js. Anything else falls through to the static
# files below (and a 404 if it isn't one). The sign-in gate above still applies.
_APP_PAGES = ("revenue", "agent-metrics", "team-metrics", "marketing", "schedule",
              "payroll", "settings", "my")


def _app_page(rest: str = "") -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


for _page in _APP_PAGES:
    app.add_api_route(f"/{_page}", _app_page, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"/{_page}/{{rest:path}}", _app_page, methods=["GET"], include_in_schema=False)

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
