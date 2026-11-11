"""User accounts: invite-only sign-in with an emailed one-time code.

No passwords are stored. A user who exists in `users` (and isn't disabled)
asks for a code, we email six digits through Resend, and a correct code within
ten minutes becomes a server-side session. Only HASHES of codes and session
tokens are kept, so a copy of the database can't be used to sign in.

Plan and permission model: docs/ACCOUNTS_PLAN.md.
"""
from __future__ import annotations

import hashlib
import html as _html
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("isadash.auth")

SESSION_COOKIE = "is_session"
CODE_TTL_MIN = 10
CODE_MAX_ATTEMPTS = 5          # wrong guesses before a code is dead
CODES_PER_EMAIL_HOUR = 5
CODES_PER_IP_HOUR = 20
CODE_MIN_GAP_S = 30            # between two sends to the same address
SESSION_DAYS = 30              # sliding: extended while the user keeps visiting
_TOUCH_EVERY_S = 600           # how often a live session's last_seen is written
_CACHE_S = 30                  # how long a session lookup is trusted in memory

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _db(db_path: Path) -> sqlite3.Connection:
    # plain connection (the tables are created by store.connect at startup) —
    # this runs on every request, so skip the schema/migration pass
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _audit(conn: sqlite3.Connection, event: str, email: str | None,
           ip: str | None, detail: str = "") -> None:
    conn.execute("INSERT INTO auth_log (at, event, email, ip, detail) VALUES (?,?,?,?,?)",
                 (_iso(_now()), event, email, ip, detail))


def norm_email(raw) -> str:
    """Lower-cased, trimmed address, or '' if it doesn't look like one."""
    e = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if len(e) > 254 or e.count("@") != 1:
        return ""
    local, _, domain = e.partition("@")
    if not local or "." not in domain or any(c.isspace() for c in e):
        return ""
    return e


def ensure_owner(conn: sqlite3.Connection, owner_email: str) -> None:
    """The owner (OWNER_EMAIL in .env) always exists with full access, so the
    dashboard can never be left without an admin."""
    email = norm_email(owner_email)
    if not email:
        return
    conn.execute(
        """INSERT INTO users (email, kind, permissions, is_owner, status)
           VALUES (?, 'staff', '["*"]', 1, 'active')
           ON CONFLICT(email) DO UPDATE SET is_owner = 1, permissions = '["*"]',
               status = CASE WHEN status = 'disabled' THEN 'active' ELSE status END""",
        (email,))
    conn.execute("UPDATE users SET is_owner = 0 WHERE email != ? AND is_owner = 1", (email,))
    conn.commit()


def _user_dict(row: sqlite3.Row) -> dict:
    try:
        perms = json.loads(row["permissions"] or "[]")
    except ValueError:
        perms = []
    return {"id": row["id"], "email": row["email"], "name": row["name"],
            "kind": row["kind"], "agent": row["agent"],
            "permissions": perms, "owner": bool(row["is_owner"])}


def new_code(conn: sqlite3.Connection, email: str) -> str:
    """Create a fresh code for `email` (retiring any earlier unused one) and
    return it. Caller commits."""
    now = _now()
    code = f"{secrets.randbelow(10 ** 6):06d}"
    conn.execute("UPDATE login_codes SET used_at = ? WHERE email = ? AND used_at IS NULL",
                 (_iso(now), email))
    conn.execute(
        "INSERT INTO login_codes (email, code_hash, created_at, expires_at) VALUES (?,?,?,?)",
        (email, _hash(email, code), _iso(now),
         _iso(now + timedelta(minutes=CODE_TTL_MIN))))
    return code


def request_code(settings, email: str, ip: str | None) -> str:
    """Start a sign-in. Returns 'ok' or 'rate'. The answer is the same whether
    or not the address has an account, so the form can't be used to discover
    who does."""
    conn = _db(settings.db_path)
    try:
        now = _now()
        hour_ago = _iso(now - timedelta(hours=1))
        n_email, last = conn.execute(
            "SELECT COUNT(*), MAX(at) FROM auth_log WHERE event = 'code_requested' "
            "AND email = ? AND at >= ?", (email, hour_ago)).fetchone()
        n_ip = conn.execute(
            "SELECT COUNT(*) FROM auth_log WHERE event = 'code_requested' "
            "AND ip = ? AND at >= ?", (ip, hour_ago)).fetchone()[0] if ip else 0
        if (n_email >= CODES_PER_EMAIL_HOUR or n_ip >= CODES_PER_IP_HOUR
                or (last and last > _iso(now - timedelta(seconds=CODE_MIN_GAP_S)))):
            _audit(conn, "code_rate_limited", email, ip)
            conn.commit()
            return "rate"
        _audit(conn, "code_requested", email, ip)
        user = conn.execute(
            "SELECT name FROM users WHERE email = ? AND status != 'disabled'",
            (email,)).fetchone()
        if not user:
            _audit(conn, "code_no_account", email, ip)
            conn.commit()
            return "ok"
        code = new_code(conn, email)
        conn.commit()
    finally:
        conn.close()
    # send off-thread: the response time doesn't reveal whether an email went out
    threading.Thread(target=_send_code, args=(settings, email, code, ip),
                     daemon=True).start()
    return "ok"


def _send_email(settings, to: str, subject: str, text: str, html: str) -> str:
    """Send one email through Resend. Returns '' on success, else the error."""
    if not (settings.resend_api_key and settings.resend_from):
        return "RESEND_API_KEY / RESEND_FROM not set"
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": f"Brooks IS Dashboard <{settings.resend_from}>",
                  "to": [to], "subject": subject, "text": text, "html": html},
            timeout=20)
        if r.status_code >= 300:
            return f"Resend HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _record_email_failure(settings, email: str, ip: str | None, err: str) -> None:
    log.error("email to %s failed: %s", email, err)
    try:
        conn = _db(settings.db_path)
        try:
            _audit(conn, "email_failed", email, ip, err)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("could not record email failure")


_WRAP = ("<div style=\"font:15px/1.5 'Segoe UI',system-ui,sans-serif;color:#1b2733\">"
         "{body}</div>")


def _send_code(settings, email: str, code: str, ip: str | None) -> None:
    err = _send_email(
        settings, email,
        f"{code} is your Brooks IS Dashboard sign-in code",
        (f"Your sign-in code is {code}\n\n"
         f"It expires in {CODE_TTL_MIN} minutes and works once.\n"
         "If you didn't ask for this, you can ignore this email - "
         "nobody can sign in without the code."),
        _WRAP.format(body=(
            "<p>Your Brooks IS Dashboard sign-in code:</p>"
            "<p style=\"font-size:30px;font-weight:700;letter-spacing:6px;"
            f"margin:14px 0\">{code}</p>"
            f"<p>It expires in {CODE_TTL_MIN} minutes and works once.</p>"
            "<p style=\"color:#6b7a8c;font-size:13px\">If you didn't ask for this, you "
            "can ignore this email - nobody can sign in without the code.</p>")))
    if err:
        _record_email_failure(settings, email, ip, err)


def send_invite(settings, email: str, name: str | None) -> str:
    """Email an invitation. Returns '' on success, else the error (also logged)."""
    url = settings.app_url.rstrip("/")
    hello = f"Hi {name.split()[0]}," if name and name.strip() else "Hi,"
    h_hello, h_email = _html.escape(hello), _html.escape(email)
    err = _send_email(
        settings, email,
        "You've been invited to the Brooks IS Dashboard",
        (f"{hello}\n\nYou've been given access to the Brooks IS Dashboard.\n\n"
         f"1. Go to {url}\n"
         f"2. Enter this email address ({email})\n"
         "3. We'll email you a 6-digit code - type it in and you're signed in.\n\n"
         "There is no password to remember."),
        _WRAP.format(body=(
            f"<p>{h_hello}</p><p>You've been given access to the <b>Brooks IS Dashboard</b>.</p>"
            f"<ol><li>Go to <a href=\"{url}\">{url}</a></li>"
            f"<li>Enter this email address ({h_email})</li>"
            "<li>We'll email you a 6-digit code &mdash; type it in and you're signed in.</li></ol>"
            "<p style=\"color:#6b7a8c;font-size:13px\">There is no password to remember.</p>")))
    if err:
        _record_email_failure(settings, email, None, err)
    return err


def verify_code(settings, email: str, code: str, ip: str | None,
                user_agent: str = "") -> str | None:
    """Check a code; on success create a session and return its token."""
    code = "".join(c for c in (code or "") if c.isdigit())
    conn = _db(settings.db_path)
    try:
        now = _now()
        row = conn.execute(
            "SELECT id, code_hash, attempts FROM login_codes WHERE email = ? "
            "AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (email, _iso(now))).fetchone()
        if not row:
            _audit(conn, "login_failed", email, ip, "no live code")
            conn.commit()
            return None
        if not hmac.compare_digest(row["code_hash"], _hash(email, code)):
            attempts = row["attempts"] + 1
            conn.execute(
                "UPDATE login_codes SET attempts = ?, used_at = ? WHERE id = ?",
                (attempts, _iso(now) if attempts >= CODE_MAX_ATTEMPTS else None, row["id"]))
            _audit(conn, "login_failed", email, ip, f"wrong code ({attempts})")
            conn.commit()
            return None
        conn.execute("UPDATE login_codes SET used_at = ? WHERE id = ?", (_iso(now), row["id"]))
        user = conn.execute(
            "SELECT id FROM users WHERE email = ? AND status != 'disabled'",
            (email,)).fetchone()
        if not user:
            _audit(conn, "login_failed", email, ip, "no account")
            conn.commit()
            return None
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, "
            "expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
            (_hash(token), user["id"], _iso(now), _iso(now),
             _iso(now + timedelta(days=SESSION_DAYS)), ip, (user_agent or "")[:200]))
        conn.execute("UPDATE users SET status = 'active', last_login_at = ? WHERE id = ?",
                     (_iso(now), user["id"]))
        _audit(conn, "login", email, ip)
        # housekeeping, piggy-backed on a successful sign-in
        conn.execute("DELETE FROM login_codes WHERE created_at < ?",
                     (_iso(now - timedelta(days=1)),))
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
        conn.execute("DELETE FROM auth_log WHERE at < ?", (_iso(now - timedelta(days=365)),))
        conn.commit()
        return token
    finally:
        conn.close()


def session_user(db_path: Path, token: str | None) -> dict | None:
    """The signed-in user for a session cookie, or None."""
    if not token:
        return None
    th = _hash(token)
    mono = time.monotonic()
    with _cache_lock:
        hit = _cache.get(th)
        if hit and hit[0] > mono:
            return hit[1]
    try:
        conn = _db(db_path)
    except sqlite3.Error:
        return None
    try:
        now = _now()
        row = conn.execute(
            "SELECT u.*, s.last_seen_at FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.status != 'disabled'",
            (th, _iso(now))).fetchone()
        if not row:
            with _cache_lock:
                _cache.pop(th, None)
            return None
        if row["last_seen_at"] < _iso(now - timedelta(seconds=_TOUCH_EVERY_S)):
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), th))
            conn.commit()
        user = _user_dict(row)
    except sqlite3.Error:
        log.exception("session lookup failed")
        return None
    finally:
        conn.close()
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[th] = (mono + _CACHE_S, user)
    return user


def end_session(db_path: Path, token: str | None) -> None:
    if not token:
        return
    th = _hash(token)
    with _cache_lock:
        _cache.pop(th, None)
    conn = _db(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
        conn.commit()
    finally:
        conn.close()


def forget_cached_sessions() -> None:
    """Call after changing or disabling a user so it takes effect immediately."""
    with _cache_lock:
        _cache.clear()


# --- permissions ----------------------------------------------------------
# One key per tab / sub-tab. The Users screen ticks these per person; the
# server refuses any data request the user's keys don't cover.
PERMISSION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Home", [("home", "Home")]),
    ("Revenue", [("revenue", "Revenue")]),
    ("Agent Metrics", [("agent.scorecard", "IS #'s"),
                       ("agent.power", "Power Score"),
                       ("agent.compare", "Compare Agents"),
                       ("agent.master", "Master Metric"),
                       ("agent.inbound", "Inbound vs Other")]),
    ("Team Metrics", [("metrics.team", "Team Overview"),
                      ("metrics.phone.conv", "Phone: Conversion"),
                      ("metrics.phone.trends", "Phone: Trends"),
                      ("metrics.phone.audit", "Phone: Audit"),
                      ("metrics.phone.leaked", "Phone: Leaked Leads"),
                      ("metrics.forms", "Form Conversion"),
                      ("metrics.multiyear", "Multiyear"),
                      ("metrics.acv", "Contract Value")]),
    ("Marketing", [("sources", "Sales Data"),
                   ("marketing.volume", "Lead Volume"),
                   ("marketing.bysource", "Volume By Source"),
                   ("marketing.spend", "Spend & ROAS")]),
    ("Schedule", [("managers.schedule", "Schedule builder"),
                     ("managers.timesheets", "Timesheets"),
                     ("managers.timeoff", "Time off (approve requests)"),
                     ("managers.agents", "Agent records (start date, hours, PTO)")]),
    ("Payroll", [("payroll.roster", "Commission Roster"),
                 ("payroll.rules", "Payroll Rules"),
                 ("payroll.lookup", "Commission Lookup (all accounts)"),
                 ("payroll.downloads", "Report Downloads (payroll, database, D2D)"),
                 ("payroll.adjust", "Submit Adjustments (splits, upsells, rescues)"),
                 ("payroll.run", "Lock & Run Payroll (the button)")]),
    ("Settings", [("settings.agents", "Agents, tiers & weights"),
                  ("settings.software", "Software Management"),
                  ("settings.users", "Users")]),
    ("Actions", [("sync", "Run Sync")]),
]
ALL_KEYS = [k for _, items in PERMISSION_GROUPS for k, _ in items]
# An agent login holds none of the above; instead it holds which of its own
# page's sub-tabs it may see (all of them unless a user-manager unticks one).
AGENT_TABS: list[tuple[str, str]] = [("me.numbers", "My Numbers"),
                                     ("me.schedule", "Schedule (clock in/out, PTO, time off)"),
                                     ("me.renewal", "Renewal Calculator"),
                                     ("me.payroll", "Payroll")]
AGENT_TAB_KEYS = [k for k, _ in AGENT_TABS]
# sub-tabs added after the agent logins existed, turned on for every agent
# login once (the first start with them); unticking one afterwards sticks
AGENT_TAB_ROLLOUTS = {"me.renewal": "Dallin, 2026-10-19: every agent gets the Renewal Calculator"}
# the areas the admin password guards today
ADMIN_KEYS = [k for k in ALL_KEYS
              if k.split(".")[0] in ("sources", "marketing", "payroll", "settings")]
PRESETS = {
    "Admin": list(ALL_KEYS),
    # every regular tab, plus reading the payroll rules and looking up accounts
    # IS Managers (scheduling / time clock) stays off the preset while Dallin
    # tests it; tick it per person in Users when it's ready (2026-10-09)
    "Manager": [k for k in ALL_KEYS
                if (k not in ADMIN_KEYS or k in ("payroll.rules", "payroll.lookup"))
                and not k.startswith("managers.")],
    "None": [],
}


def clean_permissions(raw) -> list[str]:
    """Only known keys, in catalogue order."""
    want = {str(k) for k in raw} if isinstance(raw, (list, tuple, set)) else set()
    return [k for k in ALL_KEYS if k in want]


def clean_agent_tabs(raw) -> list[str]:
    want = {str(k) for k in raw} if isinstance(raw, (list, tuple, set)) else set()
    return [k for k in AGENT_TAB_KEYS if k in want]


def grantable(actor: dict | None) -> set[str] | None:
    """The staff keys this user-manager may give or take away: None = all of
    them (the owner, a '*' account, or the admin password); otherwise exactly
    the keys they hold themselves (Dallin, 2026-10-12: anyone with the Users
    screen can only hand out what they already have)."""
    if actor is None or actor.get("owner") or "*" in actor.get("permissions", []):
        return None
    return set(actor.get("permissions", []))


def outranks(actor: dict | None, target_perms: list[str], target_owner: bool) -> bool:
    """True when the target account holds something the actor doesn't."""
    g = grantable(actor)
    if g is None:
        return False
    return target_owner or any(k not in g for k in target_perms if k in ALL_KEYS)


def migrate_agent_tabs(conn: sqlite3.Connection) -> int:
    """Agent logins created before tab visibility existed hold no me.* keys;
    give them all three so nothing disappears for anyone."""
    n = 0
    for r in conn.execute("SELECT id, permissions FROM users WHERE kind = 'agent'").fetchall():
        try:
            perms = json.loads(r["permissions"] or "[]")
        except ValueError:
            perms = []
        if not any(k in AGENT_TAB_KEYS for k in perms):
            conn.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(list(AGENT_TAB_KEYS)), r["id"]))
            n += 1
    if n:
        conn.commit()
        forget_cached_sessions()
    return n


def rollout_agent_tabs(conn: sqlite3.Connection) -> int:
    """Give each AGENT_TAB_ROLLOUTS tab to every agent login, once per tab
    (a flag in app_config remembers it was done)."""
    n = 0
    for key in AGENT_TAB_ROLLOUTS:
        flag = "agent_tab_rollout:" + key
        if conn.execute("SELECT 1 FROM app_config WHERE key = ?", (flag,)).fetchone():
            continue
        for r in conn.execute("SELECT id, permissions FROM users WHERE kind = 'agent'").fetchall():
            try:
                perms = json.loads(r["permissions"] or "[]")
            except ValueError:
                perms = []
            if key not in perms:
                conn.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(perms + [key]), r["id"]))
                n += 1
        conn.execute("INSERT INTO app_config(key, value) VALUES(?, ?)", (flag, json.dumps(_iso(_now()))))
        conn.commit()
    if n:
        forget_cached_sessions()
    return n


def is_admin(user: dict | None) -> bool:
    """Full admin: the owner, or someone holding every admin-area key."""
    if not user:
        return False
    perms = user["permissions"]
    return bool(user["owner"] or "*" in perms or all(k in perms for k in ADMIN_KEYS))


# --- user management (Settings -> Users) ----------------------------------
def list_users(conn: sqlite3.Connection) -> list[dict]:
    now = _iso(_now())
    live = dict(conn.execute(
        "SELECT user_id, COUNT(*) FROM sessions WHERE expires_at > ? GROUP BY user_id", (now,)))
    out = []
    for r in conn.execute("SELECT * FROM users ORDER BY is_owner DESC, kind DESC, email"):
        u = _user_dict(r)
        if u["owner"]:
            u["permissions"] = list(ALL_KEYS)
        u.update(status=r["status"], created=r["created_at"], invited=r["invited_at"],
                 last_login=r["last_login_at"], sessions=live.get(r["id"], 0))
        out.append(u)
    return out


def save_user(conn: sqlite3.Connection, body: dict, by: str, actor: dict | None = None) -> dict:
    """Create (no id) or update a user. Returns {'user_id', 'created'} or
    {'error'}. Caller commits nothing — this commits. `actor` is the
    user-manager doing it (None = the admin password): a limited actor can
    only tick or untick keys they hold; anything else on the account is
    left exactly as it was."""
    uid = body.get("id")
    name = (body.get("name") or "").strip()[:80] or None
    kind = body.get("kind") if body.get("kind") in ("staff", "agent") else "staff"
    agent = (body.get("agent") or "").strip() or None
    perms = clean_permissions(body.get("permissions"))
    if kind == "agent":
        # agents hold only their page's sub-tabs; all three unless told otherwise
        perms = clean_agent_tabs(body["agent_tabs"]) if "agent_tabs" in body else list(AGENT_TAB_KEYS)
        if not agent:
            return {"error": "Pick which agent this login belongs to."}
    else:
        agent = None
    existing = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid is not None else None
    g = grantable(actor)
    if g is not None:
        try:
            had = json.loads(existing["permissions"] or "[]") if existing else []
        except ValueError:
            had = []
        if existing and outranks(actor, had, bool(existing["is_owner"])) and (kind != existing["kind"] or agent != existing["agent"]):
            return {"error": "That account has access you don't have, so you can't change its type."}
        if kind == "staff":
            # keys you hold: as ticked; keys you don't: exactly as they were
            perms = [k for k in ALL_KEYS if (k in g and k in perms) or (k not in g and k in had)]
    if uid is None:
        email = norm_email(body.get("email"))
        if not email:
            return {"error": "Enter a valid email address."}
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return {"error": f"{email} already has an account."}
        cur = conn.execute(
            "INSERT INTO users (email, name, kind, agent, permissions, status, invited_at) "
            "VALUES (?,?,?,?,?, 'invited', ?)",
            (email, name, kind, agent, json.dumps(perms), _iso(_now())))
        _audit(conn, "user_invited", email, None, f"by {by}; {kind}; {len(perms)} permissions")
        conn.commit()
        return {"user_id": cur.lastrowid, "created": True, "email": email, "name": name}
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return {"error": "No such user."}
    if row["is_owner"]:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
    else:
        conn.execute("UPDATE users SET name = ?, kind = ?, agent = ?, permissions = ? WHERE id = ?",
                     (name, kind, agent, json.dumps(perms), uid))
    _audit(conn, "user_updated", row["email"], None, f"by {by}; {kind}; {len(perms)} permissions")
    conn.commit()
    forget_cached_sessions()
    return {"user_id": uid, "created": False, "email": row["email"], "name": name}


def user_action(conn: sqlite3.Connection, settings, uid, action: str, by: str, actor: dict | None = None) -> dict:
    """disable | enable | signout | resend | remove"""
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return {"error": "No such user."}
    email = row["email"]
    if row["is_owner"] and action in ("disable", "remove"):
        return {"error": "The owner account can't be disabled or removed."}
    if action != "resend":
        try:
            had = json.loads(row["permissions"] or "[]")
        except ValueError:
            had = []
        if outranks(actor, had, bool(row["is_owner"])):
            return {"error": "That account has access you don't have, so you can't disable, remove or sign it out."}
    if action == "resend":
        conn.execute("UPDATE users SET invited_at = ? WHERE id = ?", (_iso(_now()), uid))
        _audit(conn, "user_reinvited", email, None, f"by {by}")
        conn.commit()
        err = send_invite(settings, email, row["name"])
        return {"ok": not err, "error": f"Email failed: {err}"} if err else {"ok": True}
    if action in ("disable", "signout", "remove"):
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    if action == "disable":
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (uid,))
    elif action == "enable":
        conn.execute("UPDATE users SET status = ? WHERE id = ?",
                     ("active" if row["last_login_at"] else "invited", uid))
    elif action == "remove":
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    elif action != "signout":
        return {"error": "Unknown action."}
    _audit(conn, "user_" + action, email, None, f"by {by}")
    conn.commit()
    forget_cached_sessions()
    return {"ok": True}
