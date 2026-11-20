#!/usr/bin/env python3
"""isdash-agent — read-mostly status agent for the IS Dashboard's Software Management tab.

Installed at /usr/local/bin/isdash-agent on every Brooks droplet. The dashboard
reaches it over SSH with a dedicated key whose authorized_keys entry is
FORCED to this command (`restrict,command="/usr/local/bin/isdash-agent" ...`),
so that key can never open a shell or run anything else. The agent reads
SSH_ORIGINAL_COMMAND and accepts exactly two verbs:

    status              JSON facts for every Brooks app present on THIS host
    run <app.action>    one of the hard-coded ACTIONS below, nothing else

It returns FACTS only (timestamps, counts, service states). Deciding what is
healthy lives in the dashboard (src/isadash/software.py) so thresholds have one
home. Every section is wrapped so one unreadable file never blanks the whole
report. Standard library only — it must run on a bare Ubuntu python3.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

AGENT_VERSION = 4


# ----------------------------------------------------------------- helpers ---
def sh(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sysd(unit: str, *props: str) -> dict:
    """`systemctl show` properties -> dict (missing unit -> empty values)."""
    out = sh(["systemctl", "show", unit, "--no-pager"] + [f"-p{p}" for p in props])
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def ts_iso(s: str | None) -> str | None:
    """systemd/journal timestamp text -> ISO-8601 UTC, via GNU date (handles
    'Mon 2026-09-14 05:00:01 PDT' on a Pacific box and '... UTC' elsewhere)."""
    if not s or s in ("n/a", "0"):
        return None
    out = sh(["date", "-u", "-d", s, "+%Y-%m-%dT%H:%M:%SZ"], timeout=5).strip()
    return out or None


def q(db: str, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def q1(db: str, sql: str, params: tuple = ()):
    rows = q(db, sql, params)
    return rows[0][0] if rows else None


def journal_last(unit: str, pattern: str, lines: int = 600) -> dict | None:
    """Newest journal line for `unit` matching `pattern` -> {ts, line}."""
    out = sh(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"])
    rx = re.compile(pattern)
    for line in reversed(out.splitlines()):
        if rx.search(line):
            m = re.match(r"(\S+)\s+\S+\s+(.*)$", line)  # "<iso> <host> <rest>"
            ts = ts_iso(m.group(1)) if m else None
            return {"ts": ts, "line": (m.group(2) if m else line)[:400]}
    return None


def section(fn):
    """Run one fact-gathering function; never let it take the report down."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def env_safe(path: str, keys: tuple[str, ...]) -> dict:
    """Non-secret settings from a .env, by explicit allow-list only."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() in keys:
                        out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# -------------------------------------------------------------------- host ---
def host_facts() -> dict:
    def mem():
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                d[k] = int(v.strip().split()[0]) // 1024
        return d.get("MemTotal"), d.get("MemAvailable")

    total, avail = mem()
    du = shutil.disk_usage("/")
    ufw = sh(["ufw", "status"], timeout=5)
    return {
        "hostname": sh(["hostname"]).strip(),
        "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uptime_s": int(float(open("/proc/uptime").read().split()[0])),
        "load1": float(open("/proc/loadavg").read().split()[0]),
        "mem_total_mb": total, "mem_avail_mb": avail,
        "disk_free_pct": round(100.0 * du.free / du.total, 1),
        "ufw_active": ("Status: active" in ufw) if ufw else None,
        "tz": sh(["date", "+%Z"], timeout=5).strip(),
    }


# -------------------------------------------------------------------- apps ---
SOURCING = "/opt/lead-attribution"
GCLID = "/opt/gclid-sync"
REDSYNC = "/opt/redsync"
PODIUM = "/home/frsync/fieldroutes-podium-sync"
ISDASH = "/home/isdash/brooks-is-dash"
COMMISSIONS = "/home/isdash/is-commissions"
MCT = "/home/mctext/missed-call-texter"
MCT_UNIT = "missed-call-texter"
# the app's own pause switch (its CLAUDE.md: "To pause texting without stopping
# detection: set dry_run: true in config.yaml and make restart")
_MCT_DRY = re.compile(r"(?m)^dry_run:[ \t]*(true|false)\b", re.I)


def app_sourcing() -> dict:
    db = f"{SOURCING}/data/attribution.sqlite"

    def runs():
        return {k: {"last": v, "n": n} for k, v, n in
                q(db, "SELECT kind, MAX(ran_at), COUNT(*) FROM runs GROUP BY kind")}

    def channels():
        health = {ch: {"status": st, "last_alert": la} for ch, st, la in
                  q(db, "SELECT channel, status, last_alert FROM channel_health")}
        for ch, last_occ, last_ing, n24 in q(db,
                "SELECT channel, MAX(occurred_at), MAX(ingested_at), "
                "SUM(CASE WHEN ingested_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) "
                "FROM touches GROUP BY channel"):
            health.setdefault(ch, {}).update({"last_touch": last_occ, "last_ingest": last_ing,
                                              "ingested_24h": n24})
        return health

    def writes():
        return {"last": q1(db, "SELECT MAX(ran_at) FROM writes WHERE dry_run = 0"),
                "written_24h": q1(db, "SELECT COUNT(*) FROM writes WHERE dry_run = 0 AND "
                                      "status = 'WRITTEN' AND ran_at >= datetime('now','-1 day')")}

    def nightly_log():
        # the last run header + whether its final step (write-back) was reached
        out = sh(["tail", "-n", "60", f"{SOURCING}/logs/nightly.log"], timeout=5)
        header = None
        finished = False
        for line in out.splitlines():
            if line.startswith("=== NIGHTLY RUN"):
                header, finished = line.strip(), False
            elif header and line.strip().startswith("writes"):
                finished = True
        return {"last_header": header, "reached_writeback": finished}

    return {
        "present": True,
        "cron_installed": os.path.exists("/etc/cron.d/lead-attribution"),
        "running_now": bool(sh(["pgrep", "-f", "scripts/nightly_run.py"], timeout=5).strip()),
        "runs": section(runs),
        "channels": section(channels),
        "writes": section(writes),
        "nightly_log": section(nightly_log),
    }


def app_gclid() -> dict:
    env = env_safe("/etc/gclid-sync/env", ("SYNC_DB_PATH", "SYNC_ACCOUNT_TIMEZONE"))
    db = env.get("SYNC_DB_PATH") or f"{GCLID}/data/sync.db"
    if not os.path.isabs(db):
        db = os.path.join(GCLID, db)
    t = sysd("gclid-sync.timer", "NextElapseUSecRealtime", "LastTriggerUSec", "ActiveState")
    s = sysd("gclid-sync.service", "Result", "ActiveState", "ExecMainStartTimestamp",
             "ExecMainExitTimestamp", "ExecMainStatus")

    def uploads():
        return {"by_status": dict(q(db, "SELECT status, COUNT(*) FROM uploads GROUP BY status")),
                "last_created": q1(db, "SELECT MAX(created_at) FROM uploads"),
                "last_uploaded": q1(db, "SELECT MAX(uploaded_at) FROM uploads"),
                "customers_last_synced": q1(db, "SELECT MAX(last_synced_at) FROM customers"),
                "leads_last_seen": q1(db, "SELECT MAX(last_seen_at) FROM leads")}

    return {
        "present": True,
        "timer": {"active": t.get("ActiveState"), "next": ts_iso(t.get("NextElapseUSecRealtime")),
                  "last_trigger": ts_iso(t.get("LastTriggerUSec"))},
        "service": {"result": s.get("Result"), "state": s.get("ActiveState"),
                    "started": ts_iso(s.get("ExecMainStartTimestamp")),
                    "finished": ts_iso(s.get("ExecMainExitTimestamp")),
                    "exit_status": s.get("ExecMainStatus")},
        "uploads": section(uploads),
        "last_report": journal_last("gclid-sync", r"Uploads: "),
        "last_error": journal_last("gclid-sync", r"ERROR|Traceback", lines=300),
    }


def app_redsync() -> dict:
    db = f"{REDSYNC}/data/red_note_sync.sqlite"
    s = sysd("redsync", "ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts")

    def ledger():
        return {"last_cycle_local": q1(db, "SELECT v FROM meta WHERE k = 'last_cycle_local'"),
                "accounts": dict(q(db, "SELECT state, COUNT(*) FROM accounts GROUP BY state")),
                "last_event": q1(db, "SELECT MAX(ts) FROM events"),
                "events_24h": dict(q(db, "SELECT action, COUNT(*) FROM events "
                                       "WHERE ts >= datetime('now','-1 day') GROUP BY action"))}

    return {
        "present": True,
        "service": {"state": s.get("ActiveState"), "sub": s.get("SubState"),
                    "since": ts_iso(s.get("ActiveEnterTimestamp")),
                    "restarts": int(s.get("NRestarts") or 0)},
        "ledger": section(ledger),
        "last_cycle": journal_last("redsync", r"cycle done"),
        "loop_config": journal_last("redsync", r"loop: every", lines=3000),
        "last_error": journal_last("redsync", r"ERROR|Traceback", lines=300),
    }


def app_podium() -> dict:
    db = f"{PODIUM}/data/sync.db"
    s = sysd("fr-podium-sync", "ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts")

    def state():
        # audit_log.ts is ISO text ('2026-11-19T17:27:24.5+00:00'), so cutoffs must be
        # ISO too: SQLite's datetime('now',...) has a space where the 'T' is and
        # would count the whole of yesterday as "the last 24 hours".
        now = datetime.now(timezone.utc)
        d1, h2 = (now - timedelta(days=1)).isoformat(), (now - timedelta(hours=2)).isoformat()
        found = "error IS NULL AND action IN ('update','create','noop')"   # a customer FOUND in Podium
        return {"hwm": q1(db, "SELECT value FROM sync_state WHERE key = 'fr_high_water_mark'"),
                "audit_last": q1(db, "SELECT MAX(ts) FROM audit_log"),
                "audit_24h": dict(q(db, "SELECT action, COUNT(*) FROM audit_log "
                                      "WHERE ts >= ? GROUP BY action", (d1,))),
                # "contact not found" is the designed skip path, not a failure
                "errors_24h": q1(db, "SELECT COUNT(*) FROM audit_log WHERE ts >= ? "
                                     "AND error IS NOT NULL AND error != 'skip-no-contact'", (d1,)),
                # ...but a dead Podium sign-in is ALSO recorded as that skip (2026-11-19),
                # so report found-vs-skipped and let the dashboard judge the ratio
                "skipped_24h": q1(db, "SELECT COUNT(*) FROM audit_log WHERE ts >= ? "
                                      "AND error = 'skip-no-contact'", (d1,)),
                "last_reached": q1(db, f"SELECT MAX(ts) FROM audit_log WHERE {found}"),
                "recent_2h": {
                    "reached": q1(db, f"SELECT COUNT(*) FROM audit_log WHERE ts >= ? AND {found}", (h2,)),
                    "skipped": q1(db, "SELECT COUNT(*) FROM audit_log WHERE ts >= ? "
                                      "AND error = 'skip-no-contact'", (h2,))},
                "dead_letter": q1(db, "SELECT COUNT(*) FROM dead_letter"),
                "last_alert": (lambda r: {"category": r[0][0], "sent_at": r[0][1], "subject": r[0][2]}
                               if r else None)(
                    q(db, "SELECT category, sent_at, subject FROM alerts_sent ORDER BY id DESC LIMIT 1"))}

    return {
        "present": True,
        "service": {"state": s.get("ActiveState"), "sub": s.get("SubState"),
                    "since": ts_iso(s.get("ActiveEnterTimestamp")),
                    "restarts": int(s.get("NRestarts") or 0)},
        "state": section(state),
        "last_tick": journal_last("fr-podium-sync", r"Tick complete"),
        # Podium refusing the sign-in (a changed client secret): newest such log line, if any
        "auth_fail": journal_last("fr-podium-sync", r"40[013] \w+.*oauth/token|invalid_client|invalid_grant"),
    }


def app_isdash() -> dict:
    db = f"{ISDASH}/data/isadash.sqlite"
    web = sysd("brooks-is-dash", "ActiveState", "ActiveEnterTimestamp", "NRestarts")
    t = sysd("brooks-is-dash-sync.timer", "NextElapseUSecRealtime", "LastTriggerUSec", "ActiveState")
    s = sysd("brooks-is-dash-sync.service", "Result", "ActiveState", "ExecMainStartTimestamp",
             "ExecMainExitTimestamp", "ExecMainStatus")
    # stored timestamps are ISO-8601 text, so a text cutoff compares correctly
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

    def cfg():
        out = {}
        for k, v in q(db, "SELECT key, value FROM app_config WHERE key IN "
                          "('last_sync_started','sync_delta_watermark','last_sync_steps')"):
            try:
                out[k] = json.loads(v) if v else None
            except ValueError:
                out[k] = v
        return out

    def feeds():
        """Every data source the dashboard pulls, with the newest record it holds
        (= that feed's last verified data) and a 24h volume where one exists."""
        f = {
            "genesys_calls": {
                "last": q1(db, "SELECT MAX(start_utc) FROM calls"),
                "n24": q1(db, "SELECT COUNT(*) FROM calls WHERE start_utc >= ?", (cutoff,))},
            "pr_subscriptions": {
                "last": q1(db, "SELECT MAX(date_added_utc) FROM subscriptions"),
                "n24": q1(db, "SELECT COUNT(*) FROM subscriptions WHERE date_added_utc >= ?", (cutoff,))},
            "pr_customers": {
                "last": q1(db, "SELECT MAX(date_added_utc) FROM customers"),
                "n24": q1(db, "SELECT COUNT(*) FROM customers WHERE date_added_utc >= ?", (cutoff,))},
            "pr_employees": {"n": q1(db, "SELECT COUNT(*) FROM employees")},
            "audit_sheet": {
                "last_day": q1(db, "SELECT MAX(date) FROM audit_daily"),
                "rows": q1(db, "SELECT COUNT(*) FROM audit_daily")},
            "voiceops": {
                "last": q1(db, "SELECT MAX(fetched_at) FROM voiceops_scores"),
                "rows": q1(db, "SELECT COUNT(*) FROM voiceops_scores")},
        }
        f["audit_sheet"]["agents_on_last_day"] = q1(
            db, "SELECT COUNT(*) FROM audit_daily WHERE date = ?", (f["audit_sheet"]["last_day"],))
        # the form-lead feed is the sourcing app's own SQLite, read-only
        fl = env_safe(f"{ISDASH}/.env", ("FORMLEADS_DB",)).get("FORMLEADS_DB")
        if fl and os.path.exists(fl):
            f["form_leads"] = {
                "path": fl,
                "last_ingest": q1(fl, "SELECT MAX(ingested_at) FROM touches"),
                "last_touch": q1(fl, "SELECT MAX(occurred_at) FROM touches"),
                "n24": q1(fl, "SELECT COUNT(*) FROM touches WHERE ingested_at >= ?", (cutoff,))}
        else:
            f["form_leads"] = {"path": fl, "error": "form-leads DB not found at FORMLEADS_DB"}
        return f

    return {
        "present": True,
        "web": {"state": web.get("ActiveState"), "since": ts_iso(web.get("ActiveEnterTimestamp")),
                "restarts": int(web.get("NRestarts") or 0)},
        "timer": {"active": t.get("ActiveState"), "next": ts_iso(t.get("NextElapseUSecRealtime")),
                  "last_trigger": ts_iso(t.get("LastTriggerUSec"))},
        "sync_service": {"result": s.get("Result"), "state": s.get("ActiveState"),
                         "started": ts_iso(s.get("ExecMainStartTimestamp")),
                         "finished": ts_iso(s.get("ExecMainExitTimestamp")),
                         "exit_status": s.get("ExecMainStatus")},
        "config": section(cfg),
        "feeds": section(feeds),
    }

def app_commissions() -> dict:
    """IS Commissions engine: batch scripts + SQLite, no service. The key fact
    is meta.last_sync — the last day validated PestRoutes data was pulled."""
    db = f"{COMMISSIONS}/data/commissions.sqlite"

    def data():
        st = os.stat(db)
        states = dict(q(db, "SELECT state, COUNT(*) FROM sub_status GROUP BY state"))
        return {
            "last_sync": q1(db, "SELECT value FROM meta WHERE key='last_sync'"),
            "db_mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "db_mb": round(st.st_size / 1048576, 1),
            "subscriptions": q1(db, "SELECT COUNT(*) FROM subscriptions"),
            "tracked": q1(db, "SELECT COUNT(*) FROM sub_status"),
            "vested": states.get("vested", 0),
            "paid_unvested": states.get("paid", 0),
            "ledger_rows": q1(db, "SELECT COUNT(*) FROM ledger"),
        }

    def statement():
        rep_dir = f"{COMMISSIONS}/data/reports"
        try:
            xs = [f for f in os.listdir(rep_dir) if f.startswith("commission_statement_")]
        except OSError:
            return {}
        if not xs:
            return {}
        latest = max(xs)
        st = os.stat(f"{rep_dir}/{latest}")
        return {"file": latest,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}

    def sync_log():
        p = f"{COMMISSIONS}/logs/sync.log"
        try:
            with open(p, "rb") as f:
                f.seek(max(0, os.path.getsize(p) - 4000))
                lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
            return {"last_line": lines[-1][:200] if lines else None,
                    "mtime": datetime.fromtimestamp(os.stat(p).st_mtime,
                                                    tz=timezone.utc).isoformat()}
        except OSError:
            return {}

    return {
        "data": section(data),
        "statement": section(statement),
        "sync_log": section(sync_log),
        "venv_ok": os.path.isfile(f"{COMMISSIONS}/.venv/bin/python"),
        "sync_running": bool(sh(["pgrep", "-f", "is-commissions/scripts/sync.py"],
                                timeout=5).strip()),
    }


def _mct_config() -> dict:
    path = f"{MCT}/config.yaml"
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    m = _MCT_DRY.search(txt)

    def num(key):
        mm = re.search(rf"(?m)^{key}:[ \t]*(\d+)", txt)
        return int(mm.group(1)) if mm else None
    return {"dry_run": (m.group(1).lower() == "true") if m else None,
            "poll_interval_seconds": num("poll_interval_seconds"),
            "cooldown_days": num("cooldown_days"), "lookback_minutes": num("lookback_minutes"),
            "daily_report_hour": num("daily_report_hour"),
            "mtime": datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone.utc).isoformat()}


def _mct_running_mode() -> tuple[str | None, dict | None]:
    """('live' | 'paused' | None, the newest start line) — what the running
    process actually loaded, which can differ from config.yaml until a restart."""
    start = journal_last(MCT_UNIT, r"Missed Call Texter starting", lines=20000)
    line = (start or {}).get("line") or ""
    return ("paused" if "DRY RUN" in line else "live" if "LIVE" in line else None), start


def app_mct() -> dict:
    """Missed Call Texter: Genesys missed sales calls -> FieldRoutes check ->
    one Podium text. Facts only; phone numbers leave this box as last-4."""
    db = f"{MCT}/data/mct.db"
    s = sysd(MCT_UNIT, "ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts")
    mode, start = _mct_running_mode()
    utc = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    d1, d7 = utc(now - timedelta(days=1)), utc(now - timedelta(days=7))

    def data():
        paused_since = (start or {}).get("ts") if mode == "paused" else None
        def err_text(s):
            """A stored error, short, with any phone number cut to its last 4 digits."""
            return re.sub(r"\+?\d{6,}(\d{4})\b", r"...\1", s or "")[:200] or None
        recent = [{"started_at": r[0], "kind": r[1], "after_hours": r[2], "action": r[3],
                   "reason": (r[4] or "")[:140], "phone_last4": (r[5] or "")[-4:] or None,
                   "location_how": r[6], "attempts": r[7], "processed_at": r[8],
                   "error": err_text(r[9]) if r[3] in ("error", "send-failed", "send-unknown") else None}
                  for r in q(db, "SELECT started_at, kind, after_hours, action, reason, phone, location_how, "
                                 "attempts, processed_at, last_error FROM calls ORDER BY started_at DESC LIMIT 12")]
        last_alert = q(db, "SELECT category, sent_at FROM alerts_sent ORDER BY sent_at DESC LIMIT 1")
        last_err = q(db, "SELECT processed_at, last_error FROM calls WHERE action = 'error' "
                         "ORDER BY processed_at DESC LIMIT 1")
        return {
            "by_action_24h": dict(q(db, "SELECT action, COUNT(*) FROM calls WHERE processed_at >= ? GROUP BY action", (d1,))),
            "by_action_7d": dict(q(db, "SELECT action, COUNT(*) FROM calls WHERE processed_at >= ? GROUP BY action", (d7,))),
            "last_processed": q1(db, "SELECT MAX(processed_at) FROM calls"),
            "last_sent": q1(db, "SELECT MAX(processed_at) FROM calls WHERE action = 'sent'"),
            # every outcome past the cooldown check means FieldRoutes answered
            "last_fr_check": q1(db, "SELECT MAX(processed_at) FROM calls WHERE action IN "
                                    "('sent','sending','dry-run','skip-customer','skip-opted-out','send-failed','send-unknown')"),
            "stuck_errors": q1(db, "SELECT COUNT(*) FROM calls WHERE action = 'error' AND attempts >= 5"),
            # the card flags only recent ones, so an old incident doesn't warn forever
            "stuck_errors_7d": q1(db, "SELECT COUNT(*) FROM calls WHERE action = 'error' AND attempts >= 5 "
                                      "AND processed_at >= ?", (d7,)),
            # why the newest failed call failed (e.g. Podium refusing the sign-in)
            "last_error": {"at": last_err[0][0], "text": err_text(last_err[0][1])} if last_err else None,
            "retrying_errors": q1(db, "SELECT COUNT(*) FROM calls WHERE action = 'error' AND attempts < 5"),
            "send_problems_7d": q1(db, "SELECT COUNT(*) FROM calls WHERE action IN ('send-failed','send-unknown') "
                                       "AND processed_at >= ?", (d7,)),
            "status_failed_7d": q1(db, "SELECT COUNT(*) FROM calls WHERE action = 'sent' AND status_set = 0 "
                                       "AND processed_at >= ?", (d7,)),
            "paused_since": paused_since,
            "would_text_while_paused": q1(db, "SELECT COUNT(*) FROM calls WHERE action = 'dry-run' AND processed_at >= ?",
                                          (utc(datetime.fromisoformat(paused_since.replace("Z", "+00:00"))),))
                                       if paused_since else None,
            "start_after": q1(db, "SELECT value FROM state WHERE key = 'start_after'"),
            "last_report_date": q1(db, "SELECT value FROM state WHERE key = 'last_report_date'"),
            "last_alert": {"category": last_alert[0][0], "sent_at": last_alert[0][1]} if last_alert else None,
            "recent": recent,
        }

    return {
        "present": True,
        "service": {"state": s.get("ActiveState"), "sub": s.get("SubState"),
                    "since": ts_iso(s.get("ActiveEnterTimestamp")),
                    "restarts": int(s.get("NRestarts") or 0)},
        "running_mode": mode, "last_start": start,
        "config": section(_mct_config),
        "data": section(data),
        "last_tick": journal_last(MCT_UNIT, r"Tick complete"),
        "last_tick_failed": journal_last(MCT_UNIT, r"Tick failed"),
    }


APPS = {
    "sourcing": (SOURCING, app_sourcing),
    "gclid": (GCLID, app_gclid),
    "redsync": (REDSYNC, app_redsync),
    "podium": (PODIUM, app_podium),
    "isdash": (ISDASH, app_isdash),
    "commissions": (COMMISSIONS, app_commissions),
    "mct": (MCT, app_mct),
}


def status() -> dict:
    return {
        "agent_version": AGENT_VERSION,
        "host": section(host_facts),
        "apps": {key: section(fn) for key, (path, fn) in APPS.items() if os.path.isdir(path)},
    }


# ----------------------------------------------------------------- actions ---
def act_sourcing_source_now() -> dict:
    """Run the nightly pipeline now (7d ingest, 14d sourcing) — detached, because
    it takes minutes; the next `status` shows the new run in `runs`."""
    if sh(["pgrep", "-f", "scripts/nightly_run.py"], timeout=5).strip():
        return {"ok": False, "message": "a nightly_run.py is already running"}
    subprocess.Popen(
        ["bash", "-c", "cd /opt/lead-attribution && nohup .venv/bin/python scripts/nightly_run.py 7 14 "
                       ">> logs/manual_run.log 2>&1 &"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "message": "started nightly_run.py 7 14 (detached; ~5-10 min). "
                                   "Output: /opt/lead-attribution/logs/manual_run.log"}


def act_gclid_run_now() -> dict:
    st = sysd("gclid-sync.service", "ActiveState").get("ActiveState")
    if st in ("active", "activating"):
        return {"ok": False, "message": "gclid-sync is already running"}
    r = subprocess.run(["systemctl", "start", "--no-block", "gclid-sync.service"],
                       capture_output=True, text=True, timeout=15)
    return {"ok": r.returncode == 0,
            "message": "started gclid-sync.service (the nightly --live run)" if r.returncode == 0
                       else r.stderr.strip()[:300]}


def act_restart(unit: str) -> dict:
    r = subprocess.run(["systemctl", "restart", unit], capture_output=True, text=True, timeout=60)
    st = sysd(unit, "ActiveState").get("ActiveState")
    return {"ok": r.returncode == 0 and st == "active",
            "message": f"{unit} restarted; state now {st}" if r.returncode == 0
                       else r.stderr.strip()[:300]}


# The complete list of things the dashboard may ask this host to do. Adding an
# action means editing this file and redeploying it — deliberately.
def act_commissions_sync_now() -> dict:
    """Fresh PestRoutes pull for the commission engine — detached (10–20 min);
    the next `status` shows sync_running and then the new last_sync date."""
    if sh(["pgrep", "-f", "is-commissions/scripts/sync.py"], timeout=5).strip():
        return {"ok": False, "message": "a commissions sync is already running"}
    os.makedirs(f"{COMMISSIONS}/logs", exist_ok=True)
    sh(["chown", "isdash:isdash", f"{COMMISSIONS}/logs"], timeout=5)
    subprocess.Popen(
        ["runuser", "-u", "isdash", "--", "bash", "-c",
         f"cd {COMMISSIONS} && nohup .venv/bin/python "
         f"{COMMISSIONS}/scripts/sync.py >> logs/sync.log 2>&1 &"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "message": "started scripts/sync.py as isdash (detached; 10-20 min). "
                                   f"Output: {COMMISSIONS}/logs/sync.log"}


def act_mct_set_paused(pause: bool) -> dict:
    """Pause = dry_run: true (keeps detecting and recording who it WOULD text,
    sends nothing); resume = dry_run: false. Edits only that one line of
    config.yaml (owner/mode kept, atomic replace), restarts the service, then
    confirms the mode from the app's own start line."""
    path = f"{MCT}/config.yaml"
    st = os.stat(path)
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    m = _MCT_DRY.search(txt)
    if not m:
        return {"ok": False, "message": "couldn't find a 'dry_run:' line in config.yaml — nothing changed"}
    want = "paused" if pause else "live"
    running, _ = _mct_running_mode()
    if (m.group(1).lower() == "true") == pause:
        if running == want:
            return {"ok": False, "message": f"already {'paused' if pause else 'live'} — nothing changed"}
    else:
        new = txt[:m.start()] + f"dry_run: {'true' if pause else 'false'}" + txt[m.end():]
        tmp = f"{path}.isdash-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    r = act_restart(MCT_UNIT)
    if not r.get("ok"):
        return r
    for _ in range(10):                      # the start line follows within a few seconds
        time.sleep(1.5)
        running, start = _mct_running_mode()
        if start and start.get("ts") and running == want:
            break
    if running != want:
        return {"ok": False, "message": f"config set, service restarted, but the app reports "
                                        f"{running or 'no start line yet'} — check the journal"}
    return {"ok": True, "message": ("Paused: still detecting missed calls and recording who it would "
                                    "text, but sending nothing." if pause else
                                    "Live again: texting missed callers. Calls recorded while paused "
                                    "were not texted.")}


ACTIONS = {
    "sourcing.source_now": act_sourcing_source_now,
    "gclid.run_now": act_gclid_run_now,
    "redsync.restart": lambda: act_restart("redsync"),
    "podium.restart": lambda: act_restart("fr-podium-sync"),
    "commissions.sync_now": act_commissions_sync_now,
    "mct.pause": lambda: act_mct_set_paused(True),
    "mct.resume": lambda: act_mct_set_paused(False),
    "mct.restart": lambda: act_restart(MCT_UNIT),
}


def main() -> int:
    raw = os.environ.get("SSH_ORIGINAL_COMMAND") or " ".join(sys.argv[1:])
    parts = raw.strip().split()
    if parts == ["status"]:
        print(json.dumps(status(), default=str))
        return 0
    if len(parts) == 2 and parts[0] == "run":
        fn = ACTIONS.get(parts[1])
        if fn is None:
            print(json.dumps({"ok": False, "error": f"unknown action {parts[1]!r}"}))
            return 2
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        res["action"] = parts[1]
        print(json.dumps(res, default=str))
        return 0 if res.get("ok") else 1
    print(json.dumps({"error": "usage: status | run <app.action>"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
