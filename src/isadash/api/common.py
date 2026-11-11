"""Shared by every area: the app object, settings, sign-in and access rules, and the calculations more than one tab uses.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .. import auth, cache
from ..config import load_settings
from ..store import connect


app = FastAPI(title="Brooks IS Dashboard")


log = logging.getLogger("isadash.webapp")


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
        # A script or style requested with its fingerprint (?v=..., added by
        # _index_html) can be kept for good: the address changes when the file
        # does. Everything else is re-checked with the server on every load.
        if (resp.status_code == 200 and request.query_params.get("v")
                and path.endswith((".js", ".css"))):
            resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        else:
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


def _roster_names(conn) -> list[str]:
    """The inside-sales roster for scheduling / time clock / agent records:
    tracked agents minus the Bruntwork outbound reps."""
    skip = set(_settings.outbound_agents)
    return [r[0] for r in conn.execute(
        "SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL AND scorecard = 1")
        if r[0] not in skip]


@app.on_event("startup")
def _timeclock_startup() -> None:
    from .. import timeclock
    timeclock.start_sweeper(_settings, connect, _roster_names)


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


@cache.cached(copy_result=False)     # tens of thousands of events, read-only for every caller
def _get_form_events():
    """Build + classify form-lead events. Saved until the dashboard tables it
    reads change, or the sourcing app's database changed and the saved copy is
    older than _FORM_LAG_S."""
    from ..formleads import (aggregate_events, build_lead_events,
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

    from ..periods import PACIFIC, week_start
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

    from ..formleads import load_source_names
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


def _scorecard_calls(start_day: str, end_day: str) -> dict:
    """Genesys call handling per IS #'s agent for a Pacific day range: inbound
    calls taken, calls that rang them and weren't answered, average talk and
    average after-call work. Its own feed (like quality) so the sheet renders
    from the database first and these columns fill in a moment later."""
    from datetime import date

    from .. import mastermetric
    from ..periods import PACIFIC
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

    from ..rangestats import utc_bounds
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


def _mm_only(start_day: str, end_day: str) -> dict:
    from datetime import date

    from .. import mastermetric
    conn = connect(_settings.db_path)
    try:
        return mastermetric.mm_only(conn, _settings, date.fromisoformat(start_day), date.fromisoformat(end_day))
    finally:
        conn.close()


def _valid_range(start_day: str, end_day: str) -> bool:
    from datetime import date
    try:
        a, b = date.fromisoformat(start_day), date.fromisoformat(end_day)
    except ValueError:
        return False
    return a <= b and (b - a).days <= 400


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


def _audit_end_utc(conn) -> str | None:
    """UTC instant up to which the audit sheet has data: end of the last
    audited Pacific calendar day. Honest-conversion math never looks past
    this — call data after it has no audited denominator yet."""
    from datetime import timedelta

    from ..periods import PACIFIC, UTC
    last = conn.execute("SELECT MAX(date) FROM audit_daily").fetchone()[0]
    if not last:
        return None
    end_local = (datetime.fromisoformat(last + "T00:00:00")
                 .replace(tzinfo=PACIFIC) + timedelta(days=1))
    return end_local.astimezone(UTC).isoformat()


WEB_DIR = Path(__file__).resolve().parents[3] / "web"    # src/isadash/api/common.py -> the repo
