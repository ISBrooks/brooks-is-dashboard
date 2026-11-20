"""Software Management — fleet status for the Settings tab.

The dashboard reaches each Brooks droplet over SSH with a dedicated key that is
FORCED (in that host's authorized_keys) to /usr/local/bin/isdash-agent, so the
key can only ever run the agent's two verbs: `status` and `run <app.action>`.
The agent returns FACTS; this module turns them into verdicts, so every health
threshold has exactly one home.

Collection is ON DEMAND — it runs when the tab is opened, never on a timer —
because the owner does not want the boxes pinged in the background. A full
sweep is three SSH round-trips in parallel and takes a few seconds.
"""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- registry ---
HOSTS = {
    "137.184.60.159": {"name": "Sourcing-App", "note": "sourcing app + this dashboard + IS Commissions"},
    "147.182.132.161": {"name": "Podium-Pestroutes-Sync-App", "note": "Podium sync + Missed Call Texter"},
    "104.248.235.187": {"name": "Red-Note-Sync", "note": "Red Note + GCLID sync"},
}
CONSOLE_URL = "https://cloud.digitalocean.com/droplets"

APPS = [
    {"key": "sourcing", "name": "Sourcing App", "host": "137.184.60.159",
     "summary": "Ingests every inbound lead source (Genesys calls, Gmail forms, Meta, LSA) and "
                "writes the winning source onto new PestRoutes sales.",
     "schedule": "Nightly 3:00 AM PT · leads refresh every 10 min · channel health hourly",
     "actions": [{"key": "sourcing.source_now", "label": "Source sales through now",
                  "confirm": "Run the full sourcing pipeline now (7-day ingest, 14-day sourcing)? "
                             "It writes sources to PestRoutes and takes 5–10 minutes."}]},
    {"key": "gclid", "name": "Google Ads Conversion Sync", "host": "104.248.235.187",
     "summary": "Matches CallRail leads (with Google click IDs) to PestRoutes sales and sends "
                "12-month contract value to Google Ads so bidding optimizes on real revenue.",
     "schedule": "Nightly ~2:00 AM PT via Google Sheet import · emails a report each run",
     "actions": [{"key": "gclid.run_now", "label": "Run sync now",
                  "confirm": "Run tonight's Google Ads sync now? It appends conversions to the "
                             "Google Sheet that Ads imports."}]},
    {"key": "redsync", "name": "Red Note — No Signed Agreement", "host": "104.248.235.187",
     "summary": "Keeps a red \"NO SIGNED AGREEMENT\" note on accounts about to get first service "
                "without a signed agreement; removes it once signed, serviced, or cancelled.",
     "schedule": "Every 5 min, 6:00 AM – 8:00 PM PT (idle overnight by design)",
     "actions": [{"key": "redsync.restart", "label": "Restart service",
                  "confirm": "Restart the Red Note service? It resumes from its ledger; "
                             "nothing is lost."}]},
    {"key": "podium", "name": "PestRoutes → Podium Sync", "host": "147.182.132.161",
     "summary": "Pushes PestRoutes customer status, services, and autopay onto matching Podium "
                "contacts as tags and fields. One-way; never creates contacts.",
     "schedule": "Every 5 min, continuous · weekly report email Saturdays",
     "actions": [{"key": "podium.restart", "label": "Restart scheduler",
                  "confirm": "Restart the Podium sync scheduler? It resumes from its high-water "
                             "mark and catches up anything it missed."}]},
    {"key": "mct", "name": "Missed Call Texter", "host": "147.182.132.161",
     "summary": "Catches missed calls to the sales line in Genesys the moment they happen, checks "
                "PestRoutes, and if the caller isn't a current customer sends one Podium text from a "
                "sales location and marks them New Lead so a rep calls back. One text per number per week.",
     "schedule": "Every 60 s, continuous · daily summary email 7 AM MT",
     "actions": [{"key": "mct.pause", "label": "Pause texting",
                  "confirm": "Pause texting? Missed calls are still detected and recorded (as who it "
                             "WOULD have texted), but nobody gets a text until you resume."},
                 {"key": "mct.resume", "label": "Resume texting",
                  "confirm": "Resume texting? New missed calls get texted again from now on. Calls that "
                             "came in while it was paused are NOT texted automatically."},
                 {"key": "mct.restart", "label": "Restart service",
                  "confirm": "Restart the Missed Call Texter? It picks up where it left off; a call is "
                             "never texted twice."}]},
    {"key": "isdash", "name": "IS Dashboard", "host": "137.184.60.159",
     "summary": "This dashboard. Pulls Genesys, PestRoutes, VoiceOps, the audit sheet, and form "
                "leads into its own database.",
     "schedule": "Nightly 5:00 AM PT · managers may sync hourly · admins any time",
     "actions": []},   # the header Sync button already covers it
    {"key": "commissions", "name": "IS Commissions", "host": "137.184.60.159",
     "summary": "Calculates ISA commissions and deductions from PestRoutes under the rules in "
                "COMMISSION_RULES.md (rendered on Settings → Commissions). Feeds the Commission "
                "Lookup subtab and the biweekly payroll statement.",
     "schedule": "Parallel run: synced manually before each biweekly payroll · "
                 "automated Sun 11pm draft + Mon 5am final after cutover",
     "actions": [{"key": "commissions.sync_now", "label": "Sync PestRoutes now",
                  "confirm": "Pull fresh PestRoutes data into the commission engine now? "
                             "Takes 10–20 minutes; the Lookup tab keeps serving the current "
                             "data and switches over when the pull finishes."}]},
]

# ------------------------------------------------------------- thresholds ---
NIGHTLY_OK_H = 26          # a daily job is fine if it succeeded within 26h
TICK_OK_MIN = 20           # a 5-minute loop is fine if it ticked within 20 min
INGEST_OK_H = 1.5          # the sourcing 10-minute ingest
UNMAPPED_DAYS = 30         # window for the unmapped-tracking-number check
MCT_TICK_OK_MIN = 5        # the missed-call texter polls every 60 s
# The Podium sync records a failed Podium lookup as "not in Podium" (its normal
# skip), so a dead Podium sign-in looks like a healthy run with 0 errors. On
# 2026-11-19 it wrote nothing for 28 hours and this page said OK. Normally it
# finds ~50 customers for every one it skips; these catch the reverse.
PODIUM_REACH_OK_H = 2      # by day it finds a customer in Podium at least this often
PODIUM_SKIP_MIN = 20       # this many skips in 2h with under a fifth as many found = not reaching Podium
AUTH_FAIL_RECENT_H = 0.5   # a sign-in refusal in the app's log counts while this fresh
PODIUM_AUTH_HELP = ("Podium is rejecting this app's sign-in. If the client secret was regenerated in "
                    "Podium's developer portal, run deploy/podium_set_secret.ps1 with the new secret.")
_AUTH_RX = re.compile(r"oauth/token.*\b40[013]\b|\b40[013]\b.*oauth/token|invalid_client|invalid_grant",
                      re.I | re.S)
# hours without a new lead before a sourcing channel is suspect — the same
# numbers the sourcing app's own hourly check uses (channel_health_check.py)
CHANNEL_FLATLINE_H = {"genesys": 18, "gmail": 18, "meta": 30, "lsa": 36}
PACIFIC = ZoneInfo("America/Los_Angeles")


# ------------------------------------------------------------------- utils ---
def _parse(ts) -> datetime | None:
    """Anything the agent returns (ISO with/without zone, 'YYYY-MM-DD HH:MM:SS') -> aware UTC."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _age_h(ts, now: datetime) -> float | None:
    d = _parse(ts)
    return round((now - d).total_seconds() / 3600, 2) if d else None


def _worst(*verdicts: str) -> str:
    order = {"ok": 0, "idle": 0, "warn": 1, "down": 2}
    return max(verdicts, key=lambda v: order.get(v, 1)) if verdicts else "warn"


def _is_auth_error(text) -> bool:
    """Does this log line / stored error say Podium refused the sign-in?"""
    return bool(text and _AUTH_RX.search(str(text)))


def _conn(name: str, last, now: datetime, ok_h: float, note: str = "") -> dict:
    age = _age_h(last, now)
    if age is None:
        status = "warn"
    else:
        status = "ok" if age <= ok_h else "warn"
    return {"name": name, "last": _parse(last).isoformat() if _parse(last) else None,
            "age_h": age, "status": status, "note": note}


# ------------------------------------------------------------------- ssh -----
def _ssh(settings, host: str, command: str, timeout: int = 40) -> dict:
    """One agent call. Every failure mode becomes a dict, never an exception."""
    cmd = ["ssh", "-i", settings.software_ssh_key,
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={settings.software_known_hosts}",
           f"{settings.software_ssh_user}@{host}", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"reachable": False, "error": f"timed out after {timeout}s"}
    except OSError as e:
        return {"reachable": False, "error": f"ssh not runnable: {e}"}
    if r.returncode != 0 and not r.stdout.strip():
        return {"reachable": False, "error": (r.stderr.strip() or f"exit {r.returncode}")[:300]}
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return {"reachable": False, "error": f"agent returned non-JSON: {r.stdout[:200]!r}"}
    data["reachable"] = True
    if r.returncode != 0 and "ok" not in data:
        data["exit"] = r.returncode
    return data


# ---------------------------------------------------------------- verdicts ---
def _v_sourcing(a: dict, now: datetime) -> dict:
    runs = a.get("runs") or {}
    nightly = (runs.get("nightly") or {}).get("last")
    ingest = (runs.get("ingest") or {}).get("last")
    n_age, i_age = _age_h(nightly, now), _age_h(ingest, now)
    v = "ok"
    notes = []
    if n_age is None or n_age > NIGHTLY_OK_H:
        v, _ = "warn", notes.append("nightly run is overdue")
    if not a.get("cron_installed"):
        v, _ = "down", notes.append("cron schedule missing")
    if a.get("nightly_log", {}).get("last_header") and not a["nightly_log"].get("reached_writeback"):
        v, _ = _worst(v, "warn"), notes.append("last nightly did not reach the write-back step")
    conns = []
    for ch, lim in CHANNEL_FLATLINE_H.items():
        c = (a.get("channels") or {}).get(ch) or {}
        conn = _conn(ch.upper() if ch == "lsa" else ch.capitalize(), c.get("last_touch"), now, lim,
                     note=f"{c.get('ingested_24h', 0)} leads in 24h")
        if c.get("status") and c["status"] != "ok":
            conn["status"], conn["note"] = "warn", f"health check: {c['status']}"
        conns.append(conn)
    w = a.get("writes") or {}
    conns.append(_conn("PestRoutes (write-back)", w.get("last"), now, NIGHTLY_OK_H,
                       note=f"{w.get('written_24h', 0)} sources written in 24h"))
    v = _worst(v, *[c["status"] for c in conns])
    head = (f"Nightly ran {n_age:.0f}h ago" if n_age is not None else "No nightly run on record")
    if i_age is not None:
        head += f" · leads refreshed {int(i_age * 60)} min ago"
    if a.get("running_now"):
        head += " · a manual run is in progress"
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"nightly_last": nightly, "ingest_last": ingest,
                      "last_run_header": (a.get("nightly_log") or {}).get("last_header"),
                      "running_now": a.get("running_now")}}


def _v_gclid(a: dict, now: datetime) -> dict:
    s, t, u = a.get("service") or {}, a.get("timer") or {}, a.get("uploads") or {}
    fin_age = _age_h(s.get("finished"), now)
    notes = []
    if s.get("state") in ("active", "activating"):
        v, head = "ok", "Running right now"
    elif s.get("result") and s["result"] != "success":
        v, head = "down", f"Last run FAILED ({s['result']}, exit {s.get('exit_status')})"
    elif fin_age is None or fin_age > NIGHTLY_OK_H:
        v, head = "warn", "Last run is overdue"
    else:
        v, head = "ok", f"Last run succeeded {fin_age:.0f}h ago"
    if t.get("active") != "active":
        v, _ = "down", notes.append("nightly timer is not armed")
    rep = (a.get("last_report") or {}).get("line")
    if rep:
        m = re.search(r"Uploads: (.*)", rep)
        notes.append(("Last report — " + m.group(1)) if m else rep)
    by = u.get("by_status") or {}
    conns = [
        _conn("CallRail", u.get("leads_last_seen"), now, NIGHTLY_OK_H),
        _conn("PestRoutes", u.get("customers_last_synced"), now, NIGHTLY_OK_H),
        _conn("Google Sheet → Ads", u.get("last_uploaded"), now, NIGHTLY_OK_H,
              note=f"{by.get('uploaded', 0)} uploaded · {by.get('pending', 0)} pending · "
                   f"{by.get('failed', 0)} failed (pending/failed include the held backfill)"),
    ]
    # the upload connection only goes stale if a run actually happened and uploaded nothing
    if conns[2]["status"] == "warn" and v == "ok":
        conns[2]["status"] = "ok"
        conns[2]["note"] += " · nothing new to upload last run"
    v = _worst(v, *[c["status"] for c in conns[:2]])
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"next_run": t.get("next"), "last_started": s.get("started"),
                      "last_finished": s.get("finished"), "result": s.get("result"),
                      "uploads": by, "last_error": (a.get("last_error") or {}).get("line")}}


def _v_redsync(a: dict, now: datetime) -> dict:
    s, led = a.get("service") or {}, a.get("ledger") or {}
    poll_min, start, end, tz = 5, 6, 20, PACIFIC
    cfg = (a.get("loop_config") or {}).get("line") or ""
    m = re.search(r"every (\d+) min, (\d{2}):\d{2}-(\d{2}):\d{2} ([\w/]+)", cfg)
    if m:
        poll_min, start, end = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            tz = ZoneInfo(m.group(4))
        except Exception:  # noqa: BLE001
            pass
    local_now = now.astimezone(tz)
    in_hours = start <= local_now.hour < end
    cyc_age = _age_h((a.get("last_cycle") or {}).get("ts"), now)
    notes = []
    if s.get("state") != "active":
        v, head = "down", f"Service is {s.get('state') or 'missing'}"
    elif not in_hours:
        v, head = "idle", f"Idle — outside active hours ({start:02d}:00–{end:02d}:00 PT)"
    elif cyc_age is not None and cyc_age * 60 <= max(TICK_OK_MIN, poll_min * 4):
        v, head = "ok", f"Last cycle {int(cyc_age * 60)} min ago"
    else:
        v, head = "warn", "Service is up but no recent cycle"
    if s.get("restarts"):
        notes.append(f"{s['restarts']} automatic restart(s) since boot")
    if (a.get("last_error") or {}).get("line"):
        notes.append("Recent error in log: " + a["last_error"]["line"][:160])
    acc = led.get("accounts") or {}
    conns = [_conn("PestRoutes", (a.get("last_cycle") or {}).get("ts"), now,
                   ok_h=max(TICK_OK_MIN, poll_min * 4) / 60 if in_hours else 14,
                   note=f"{acc.get('noted', 0)} accounts currently noted · "
                        f"{acc.get('closed', 0)} closed")]
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"service_since": s.get("since"), "last_cycle_local": led.get("last_cycle_local"),
                      "accounts": acc, "events_24h": led.get("events_24h"),
                      "active_hours": f"{start:02d}:00–{end:02d}:00 {tz.key}"}}


def _v_podium(a: dict, now: datetime) -> dict:
    s, st = a.get("service") or {}, a.get("state") or {}
    hwm_age = _age_h(st.get("hwm"), now)
    notes = []
    if s.get("state") != "active":
        v, head = "down", f"Service is {s.get('state') or 'missing'}"
    elif hwm_age is not None and hwm_age * 60 <= TICK_OK_MIN:
        v, head = "ok", f"Last tick {int(hwm_age * 60)} min ago"
    else:
        v, head = "warn", "Service is up but the high-water mark is stale"
    if (st.get("errors_24h") or 0) > 0:
        v, _ = _worst(v, "warn"), notes.append(f"{st['errors_24h']} sync errors in the last 24h")
    if (st.get("dead_letter") or 0) > 0:
        v, _ = _worst(v, "warn"), notes.append(f"{st['dead_letter']} customers in the dead-letter queue")
    if s.get("restarts"):
        notes.append(f"{s['restarts']} automatic restart(s) since boot")
    a24 = st.get("audit_24h") or {}
    # Is anything actually reaching Podium? (see PODIUM_SKIP_MIN above)
    rec = st.get("recent_2h") or {}
    skipped, reached = rec.get("skipped"), rec.get("reached") or 0
    blocked = skipped is not None and skipped >= PODIUM_SKIP_MIN and reached * 5 < skipped
    auth_age = _age_h((a.get("auth_fail") or {}).get("ts"), now)
    signin_cut = auth_age is not None and auth_age <= AUTH_FAIL_RECENT_H
    if s.get("state") == "active" and (blocked or signin_cut):
        v, head = _worst(v, "warn"), "Running, but its updates are not reaching Podium"
        if blocked:
            notes.insert(0, f"In the last 2 hours {skipped} customers were skipped as \"not in Podium\" and "
                            f"only {reached} were found. Normally nearly every customer is found, so "
                            "Podium is most likely not answering this app.")
        if signin_cut:
            notes.insert(0, PODIUM_AUTH_HELP)
    if "last_reached" in st:          # agent v4+: the last time a customer was FOUND in Podium
        skipped24 = st.get("skipped_24h") or 0
        pod = _conn("Podium", st.get("last_reached"), now, PODIUM_REACH_OK_H,
                    note=f"last customer found in Podium · {a24.get('update', 0)} updated · "
                         f"{max(0, a24.get('noop', 0) - skipped24)} unchanged · {skipped24} not in Podium · "
                         f"{a24.get('create', 0)} created in 24h")
        if blocked or signin_cut:
            pod["status"] = "warn"
    else:                             # older agent: newest audit row of any kind, skips included
        pod = _conn("Podium", st.get("audit_last"), now, TICK_OK_MIN / 60,
                    note=f"{a24.get('update', 0)} updated · {a24.get('noop', 0)} unchanged · "
                         f"{a24.get('create', 0)} created in 24h")
    conns = [_conn("PestRoutes", st.get("hwm"), now, TICK_OK_MIN / 60, note="high-water mark"), pod]
    v = _worst(v, *[c["status"] for c in conns])
    la = st.get("last_alert") or {}
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"service_since": s.get("since"), "last_tick": (a.get("last_tick") or {}).get("line"),
                      "last_email": f"{la.get('subject')} ({la.get('sent_at')})" if la else None}}


def _v_isdash(a: dict, now: datetime) -> dict:
    web, s, t = a.get("web") or {}, a.get("sync_service") or {}, a.get("timer") or {}
    cfg, f = a.get("config") or {}, a.get("feeds") or {}
    last_sync = cfg.get("last_sync_started")
    ls_age = _age_h(last_sync, now)
    steps = cfg.get("last_sync_steps") or {}
    # step name -> what the last sync reported for it ("ok", "skipped (...)", "1,234 rows"…)
    note_of = {st.get("name"): (st.get("note") or ("done" if st.get("done") else "did not finish"))
               for st in (steps.get("steps") or [])}
    notes = []
    if web.get("state") != "active":
        v, head = "down", "Web service is not running"
    elif s.get("result") and s["result"] != "success":
        v, head = "warn", f"Nightly sync FAILED ({s['result']})"
    elif ls_age is None or ls_age > NIGHTLY_OK_H:
        v, head = "warn", "No sync in the last day"
    else:
        v, head = "ok", f"Last sync {ls_age:.1f}h ago"
    if steps:
        if steps.get("ok"):
            notes.append(f"Last sync completed all {len(steps.get('steps') or [])} steps")
        else:
            v, _ = _worst(v, "warn"), notes.append(
                "Last sync ended with an error: " + str(steps.get("error"))[:160])
    else:
        notes.append("Per-step sync results will appear after the next sync")
    if t.get("active") != "active":
        v, _ = _worst(v, "warn"), notes.append("nightly timer is not armed")

    def with_step(base: str, *names: str) -> str:
        extra = [f"{n}: {note_of[n]}" for n in names if n in note_of]
        return " · ".join([base] + extra) if extra else base

    g, sub = f.get("genesys_calls") or {}, f.get("pr_subscriptions") or {}
    cu, em = f.get("pr_customers") or {}, f.get("pr_employees") or {}
    au, vo, fl = f.get("audit_sheet") or {}, f.get("voiceops") or {}, f.get("form_leads") or {}
    conns = [
        _conn("Genesys — calls", g.get("last"), now, 30,
              note=with_step(f"{g.get('n24', 0)} calls in 24h", "Calls & subscriptions")),
        _conn("PestRoutes — sales (subscriptions)", sub.get("last"), now, 30,
              note=with_step(f"{sub.get('n24', 0)} sales in 24h", "Subscription statuses")),
        _conn("PestRoutes — customers", cu.get("last"), now, 48,
              note=with_step(f"{cu.get('n24', 0)} new in 24h", "Customers")),
        # employees have no record timestamp; they are refreshed by every sync
        _conn("PestRoutes — employees", last_sync, now, NIGHTLY_OK_H,
              note=with_step(f"{em.get('n', 0)} employees on file", "Employees")),
        _conn("VoiceOps — quality scores", vo.get("last"), now, NIGHTLY_OK_H,
              note=with_step(f"{vo.get('rows', 0)} cached agent-periods", "VoiceOps quality")),
        # the audit team enters days after the fact, so this feed lags by design
        _conn("Audit sheet (Google Sheet)", au.get("last_day"), now, 5 * 24,
              note=with_step(f"last audited day {au.get('last_day')} · "
                             f"{au.get('agents_on_last_day', 0)} agents that day", "Audit sheet")),
    ]
    if fl.get("error"):
        conns.append({"name": "Form leads (sourcing app DB)", "last": None, "age_h": None,
                      "status": "warn", "note": fl["error"]})
    else:
        conns.append(_conn("Form leads (sourcing app DB)", fl.get("last_ingest"), now, INGEST_OK_H,
                           note=with_step(f"{fl.get('n24', 0)} touches ingested in 24h", "Form touches")))
    v = _worst(v, *[c["status"] for c in conns])
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"web_since": web.get("since"), "next_nightly": t.get("next"),
                      "nightly_result": s.get("result"), "nightly_finished": s.get("finished"),
                      "last_sync_started": last_sync, "watermark": cfg.get("sync_delta_watermark"),
                      "last_sync_ok": steps.get("ok") if steps else None}}

def _v_commissions(a: dict, now: datetime) -> dict:
    """Batch engine, no service: health = how fresh the validated PestRoutes
    pull is (meta.last_sync) against the biweekly payroll cadence."""
    d = a.get("data") or {}
    stmt = a.get("statement") or {}
    log = a.get("sync_log") or {}
    notes = []
    last_sync = d.get("last_sync")          # a Pacific DATE ('YYYY-MM-DD')
    sync_age_h = _age_h(last_sync and f"{last_sync}T12:00:00-07:00", now)
    if d.get("error"):
        v, head = "down", "Cannot read the commission database"
        notes.append(d["error"])
    elif a.get("sync_running"):
        v = "ok"
        head = "PestRoutes sync running right now"
        if last_sync:
            head += f" · previous validated data: {last_sync}"
    elif last_sync is None:
        v, head = "warn", "No PestRoutes sync on record"
    else:
        # biweekly cadence during the parallel run: data older than a full
        # pay period (14 days + 2 days grace) means a payroll ran without it
        v = "ok" if (sync_age_h is not None and sync_age_h <= 16 * 24) else "warn"
        head = f"PestRoutes data validated through {last_sync}"
        if v == "warn":
            notes.append("last sync is older than a pay period — run one before payroll")
    if not a.get("venv_ok"):
        v, _ = _worst(v, "warn"), notes.append("python environment missing (.venv)")
    if log.get("last_line"):
        notes.append("Last sync log: " + log["last_line"])
    conns = [
        _conn("PestRoutes (full chain pull)",
              last_sync and f"{last_sync}T12:00:00-07:00", now, 16 * 24,
              note=f"{d.get('subscriptions', 0):,} ISA subscriptions · "
                   f"{d.get('vested', 0):,} vested · {d.get('paid_unvested', 0):,} paid-unvested"),
        _conn("Commission database", d.get("db_mtime"), now, 16 * 24,
              note=f"{d.get('db_mb', '?')} MB · {d.get('ledger_rows', 0):,} ledger rows"),
    ]
    if stmt.get("file"):
        conns.append(_conn("Payroll statement (xlsx)", stmt.get("mtime"), now, 16 * 24,
                           note=stmt["file"]))
    if a.get("sync_running"):
        for c in conns:
            if c["status"] == "warn":
                c["status"], c["note"] = "ok", (c["note"] + " · fresh pull in progress")
    v = _worst(v, *[c["status"] for c in conns])
    return {"status": v, "headline": head, "notes": notes, "connections": conns,
            "facts": {"last_sync": last_sync, "db_mb": d.get("db_mb"),
                      "subscriptions": d.get("subscriptions"),
                      "ledger_rows": d.get("ledger_rows"),
                      "latest_statement": stmt.get("file"),
                      "sync_running": a.get("sync_running")}}


_MCT_LABEL = {"sent": "Texted", "dry-run": "Would text (paused)", "skip-customer": "Customer — skipped",
              "skip-cooldown": "Already texted this week", "skip-opted-out": "Opted out of texts",
              "skip-bad-number": "No textable caller ID", "skip-transferred": "Forwarded after hours",
              "send-failed": "Send failed", "send-unknown": "Send status unknown", "sending": "Sending",
              "error": "Error (retrying)", "test-sent": "Test send"}
_MCT_KIND = {"abandoned-in-queue": "Hung up in queue", "hung-up-in-menu": "Hung up in menu",
             "transferred-out": "Forwarded after hours", "voicemail": "Voicemail"}


def _pt(dt: datetime) -> str:
    """'Oct 19 9:05 AM' in Pacific, portable (no %-d)."""
    x = dt.astimezone(PACIFIC)
    return f"{x:%b} {x.day} {x.hour % 12 or 12}:{x.minute:02d} {'AM' if x.hour < 12 else 'PM'}"


def _v_mct(a: dict, now: datetime) -> dict:
    s, cfg, d = a.get("service") or {}, a.get("config") or {}, a.get("data") or {}
    tick, fail = a.get("last_tick") or {}, a.get("last_tick_failed") or {}
    mode = a.get("running_mode")
    tick_age = _age_h(tick.get("ts"), now)
    a24, a7 = d.get("by_action_24h") or {}, d.get("by_action_7d") or {}
    seen24 = sum(a24.values())
    notes, pill = [], None
    if s.get("state") != "active":
        v, head = "down", f"Service is {s.get('state') or 'missing'} — no missed calls are being caught"
    elif tick_age is None or tick_age * 60 > MCT_TICK_OK_MIN:
        v, head = "warn", ("Service is up but hasn't finished a poll yet" if tick_age is None
                           else f"Service is up but the last poll was {int(tick_age * 60)} min ago")
    elif mode == "paused":
        v, pill = "idle", "Paused"
        since = _parse(d.get("paused_since"))
        head = ("Paused — still catching missed calls, texting nobody"
                + (f" (since {_pt(since)} PT)" if since else ""))
        if d.get("would_text_while_paused"):
            notes.append(f"{d['would_text_while_paused']} caller(s) would have been texted while paused. "
                         "Resuming does not text them; the app's catch-up command can, from the server.")
    else:
        v, head = "ok", (f"Live · last poll {max(0, int(tick_age * 60))} min ago · "
                         f"{a24.get('sent', 0)} texted of {seen24} missed call(s) in 24h")
    if cfg.get("dry_run") is not None and mode and cfg["dry_run"] != (mode == "paused"):
        v = _worst(v, "warn")
        notes.append(f"config.yaml says {'paused' if cfg['dry_run'] else 'live'} but the running service is "
                     f"{mode} — use Restart service to apply it.")
    # agent v4+ counts only the last 7 days, so an old incident stops flagging the card
    recent_only = "stuck_errors_7d" in d
    stuck = d.get("stuck_errors_7d") if recent_only else d.get("stuck_errors")
    if stuck:
        v = _worst(v, "warn")
        notes.append(f"{stuck} missed call(s) {'in the last 7 days ' if recent_only else ''}gave up after "
                     "5 tries (an alert email went out) — those callers were not texted.")
    # the newest error says Podium refused the sign-in, and nothing has been texted since
    le = d.get("last_error") or {}
    le_at, sent_at = _parse(le.get("at")), _parse(d.get("last_sent"))
    signin_cut = bool(le_at and (now - le_at) <= timedelta(hours=24) and _is_auth_error(le.get("text"))
                      and (sent_at is None or sent_at < le_at))
    if signin_cut:
        v = _worst(v, "warn")
        notes.insert(0, PODIUM_AUTH_HELP + " Until then nobody is being texted.")
    if d.get("retrying_errors"):
        notes.append(f"{d['retrying_errors']} missed call(s) hit an error and will be retried.")
    if d.get("send_problems_7d"):
        v = _worst(v, "warn")
        notes.append(f"{d['send_problems_7d']} text(s) in 7 days failed or may not have sent — check Podium.")
    if d.get("status_failed_7d"):
        notes.append(f"{d['status_failed_7d']} text(s) in 7 days went out but New Lead status wasn't set.")
    fail_age = _age_h(fail.get("ts"), now)
    if fail_age is not None and fail_age <= 1 and (tick_age is None or fail.get("ts", "") > (tick.get("ts") or "")):
        v = _worst(v, "warn")
        notes.append("The most recent poll failed: " + (fail.get("line") or "")[-160:])
    if s.get("restarts"):
        notes.append(f"{s['restarts']} automatic restart(s) since the service last started")

    def soft(name, last, ok_h, note, bad=False):
        c = _conn(name, last, now, ok_h, note)
        if last is None:
            c["status"] = "idle"          # nothing to check against yet (no calls of that kind)
        if bad:
            c["status"] = "warn"
        return c
    conns = [
        _conn("Genesys", tick.get("ts"), now, MCT_TICK_OK_MIN / 60,
              note=f"polled every {cfg.get('poll_interval_seconds') or 60} s · {seen24} missed sales call(s) in 24h"),
        soft("PestRoutes", d.get("last_fr_check"), 72,
             f"last customer check · {a24.get('skip-customer', 0)} customer(s) skipped in 24h"),
        soft("Podium", d.get("last_sent"), 72 if mode != "paused" else 1e9,
             f"last text sent · {a24.get('sent', 0)} in 24h, {a7.get('sent', 0)} in 7 days",
             bad=bool(d.get("send_problems_7d")) or signin_cut),
    ]
    v = _worst(v, *[c["status"] for c in conns])
    if pill is None and v == "idle":
        pill = "Paused"
    recent = []
    for r in d.get("recent") or []:
        recent.append({"when": r.get("started_at"), "kind": _MCT_KIND.get(r.get("kind"), r.get("kind")),
                       "after_hours": bool(r.get("after_hours")), "phone": f"…{r['phone_last4']}" if r.get("phone_last4") else "",
                       "outcome": _MCT_LABEL.get(r.get("action"), r.get("action")), "action": r.get("action"),
                       "detail": r.get("reason") or ("Podium rejected the sign-in" if _is_auth_error(r.get("error"))
                                                     else r.get("error") or "")})
    la = d.get("last_alert") or {}
    return {"status": v, "pill": pill, "headline": head, "notes": notes, "connections": conns,
            "recent": recent, "show_actions": ["mct.resume" if mode == "paused" else "mct.pause", "mct.restart"],
            "facts": {"mode": "Paused — detecting, not texting" if mode == "paused" else
                      "Live — texting" if mode == "live" else None,
                      "service_since": s.get("since"),
                      "last_poll": (tick.get("line") or "").split("| ")[-1] or None,
                      "texted_24h": a24.get("sent", 0), "missed_calls_24h": seen24,
                      "texted_7d": a7.get("sent", 0),
                      "customers_skipped_7d": a7.get("skip-customer", 0),
                      "already_texted_this_week_7d": a7.get("skip-cooldown", 0),
                      "cooldown": f"{cfg.get('cooldown_days')} days per number" if cfg.get("cooldown_days") else None,
                      "last_daily_report": d.get("last_report_date"),
                      "last_alert_email": f"{la.get('category')} ({la.get('sent_at')})" if la else None}}


VERDICT = {"sourcing": _v_sourcing, "gclid": _v_gclid, "redsync": _v_redsync,
           "podium": _v_podium, "isdash": _v_isdash, "commissions": _v_commissions,
           "mct": _v_mct}


# ------------------------------------------------------------------ public ---
def collect(settings) -> dict:
    """Sweep every host in parallel and return the tab's whole payload."""
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as ex:
        raw = dict(zip(HOSTS, ex.map(lambda h: _ssh(settings, h, "status"), HOSTS)))

    hosts = {}
    for ip, meta in HOSTS.items():
        r = raw[ip]
        h = (r.get("host") or {}) if r.get("reachable") else {}
        hosts[ip] = {"ip": ip, "name": meta["name"], "note": meta["note"],
                     "reachable": bool(r.get("reachable")), "error": r.get("error"),
                     "console_url": CONSOLE_URL, "hostname": h.get("hostname"),
                     "uptime_s": h.get("uptime_s"), "load1": h.get("load1"),
                     "mem_avail_mb": h.get("mem_avail_mb"), "mem_total_mb": h.get("mem_total_mb"),
                     "disk_free_pct": h.get("disk_free_pct"), "ufw_active": h.get("ufw_active"),
                     "agent_version": r.get("agent_version")}

    apps = []
    for spec in APPS:
        r = raw[spec["host"]]
        out = {k: spec[k] for k in ("key", "name", "summary", "schedule", "host", "actions")}
        out["hostname"] = HOSTS[spec["host"]]["name"]
        if not r.get("reachable"):
            out.update({"status": "down", "headline": "Host unreachable", "notes": [r.get("error")],
                        "connections": [], "facts": {}})
        else:
            a = (r.get("apps") or {}).get(spec["key"])
            if not a:
                out.update({"status": "down", "headline": "App not found on its host",
                            "notes": [], "connections": [], "facts": {}})
            elif a.get("error"):
                out.update({"status": "warn", "headline": "Agent could not read this app",
                            "notes": [a["error"]], "connections": [], "facts": {}})
            else:
                out.update(VERDICT[spec["key"]](a, now))
                show = out.pop("show_actions", None)
                if show is not None:          # only the buttons that make sense right now
                    out["actions"] = [x for x in spec["actions"] if x["key"] in show]
        if spec["key"] == "sourcing":
            # computed from the dashboard's OWN call log (the sourcing app drops
            # these calls before they reach its database, so it can't report them)
            u = unmapped_calls(settings, now)
            u["forms"] = unmapped_forms(settings, now)
            out["unmapped"] = u
            if u.get("calls"):
                out["headline"] = (f"{out.get('headline', '')} · {u['calls']} calls from "
                                   f"{len(u['numbers'])} unmapped numbers in {u['days']}d").strip(" ·")
            if u["forms"].get("forms"):
                out["headline"] = (f"{out.get('headline', '')} · {u['forms']['forms']} unmapped forms "
                                   f"in {u['forms']['days']}d").strip(" ·")
        if spec["key"] == "isdash" and out.get("connections") is not None:
            # the dashboard's own newer feeds, checked live from this process
            live = live_isdash_connections(settings, now)
            out["connections"] = list(out.get("connections") or []) + live
            out["status"] = _worst(out.get("status", "ok"), *[c["status"] for c in live])
        apps.append(out)

    return {"generated_at": now.isoformat(), "hosts": hosts, "apps": apps}


def run_action(settings, action: str) -> dict:
    """Dispatch one allow-listed action to the host that owns the app."""
    app_key = action.split(".", 1)[0]
    spec = next((s for s in APPS if s["key"] == app_key), None)
    if not spec or action not in {x["key"] for x in spec["actions"]}:
        return {"ok": False, "error": f"unknown action {action!r}"}
    r = _ssh(settings, spec["host"], f"run {action}", timeout=90)
    if not r.get("reachable"):
        return {"ok": False, "error": r.get("error")}
    return r


# ------------------------------------------------ unmapped tracking numbers ---
def _norm_dnis(raw) -> str | None:
    """Dialed number -> E.164 the way both the call log and the master sheet mean it."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if digits else None


def _fmt_dnis(e164: str) -> str:
    d = e164.lstrip("+")
    if len(d) == 11 and d.startswith("1"):
        return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return e164


def _master_dnis(path: str) -> set[str]:
    """Every tracking number (E.164) the sourcing app's master sheet maps to a source."""
    import csv
    from pathlib import Path as _P
    out: set[str] = set()
    if not path or not _P(path).exists():
        raise FileNotFoundError(f"sourcing master sheet not found at {path or '(SOURCING_MASTER unset)'}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            e = _norm_dnis((row.get("DNIS") or "").strip())
            if e and (row.get("Pestroutes Source") or "").strip():
                out.add(e)
    return out


def unmapped_calls(settings, now: datetime, days: int = UNMAPPED_DAYS) -> dict:
    """Answered inside-sales calls in the last `days` whose dialed number is not in
    the sourcing app's master sheet — i.e. calls the sourcing app silently drops.
    Read from the dashboard's own call log (refreshed by Sync), never from the
    sourcing app. Calls with no dialed number on record are left out: they aren't
    a tracking number anyone could map."""
    from .store import connect
    res = {"days": days, "calls": 0, "answered": 0, "numbers": [], "master_count": 0,
           "calls_through": None, "error": None}
    try:
        mapped = _master_dnis(settings.sourcing_master)
    except Exception as e:                                   # noqa: BLE001 — shown on the card
        res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return res
    res["master_count"] = len(mapped)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT dnis, COUNT(*) AS n, MAX(start_utc) AS last FROM calls "
            "WHERE start_utc >= ? AND dnis IS NOT NULL AND dnis != '' GROUP BY dnis",
            (since,)).fetchall()
        res["calls_through"] = conn.execute("SELECT MAX(start_utc) FROM calls").fetchone()[0]
    finally:
        conn.close()
    by_num: dict[str, dict] = {}
    for r in rows:
        e = _norm_dnis(r["dnis"])
        if not e:
            continue
        res["answered"] += r["n"]
        if e in mapped:
            continue
        cur = by_num.setdefault(e, {"dnis": e, "display": _fmt_dnis(e), "calls": 0, "last": None})
        cur["calls"] += r["n"]
        if r["last"] and (cur["last"] is None or r["last"] > cur["last"]):
            cur["last"] = r["last"]
    nums = sorted(by_num.values(), key=lambda x: (-x["calls"], x["dnis"]))
    for x in nums:
        d = _parse(x["last"])
        x["last"] = d.isoformat() if d else None
    res["numbers"] = nums
    res["calls"] = sum(x["calls"] for x in nums)
    return res


FORMS_PASS_OK_H = 36       # the daily Gmail pass (scripts/unmapped_forms_watch.py) is late after this


def unmapped_forms(settings, now: datetime, days: int = UNMAPPED_DAYS) -> dict:
    """Form-lead emails the sourcing app could not place on a source (dropped leads),
    summed over the last `days` Pacific days from the per-day file that
    scripts/unmapped_forms_watch.py writes once a day (a read-only Gmail pass that
    replays the sourcing app's own parsing). Nothing here touches Gmail."""
    from pathlib import Path as _P
    path = _P(settings.db_path).parent / "unmapped_forms.json"
    res = {"days": days, "forms": 0, "total_forms": 0, "checked_days": 0, "last_run": None, "error": None}
    if not path.exists():
        res["error"] = "no count on file yet — the daily Gmail pass (unmapped_forms_watch.py) has not run"
        return res
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001 — shown on the card
        res["error"] = f"count file unreadable: {type(e).__name__}: {str(e)[:120]}"
        return res
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date()
    per = data.get("days") or {}
    for k in range(1, days + 1):
        d = per.get((today - timedelta(days=k)).isoformat())
        if d:
            res["checked_days"] += 1
            res["forms"] += int(d.get("unmapped") or 0)
            res["total_forms"] += int(d.get("forms") or 0)
    lr = _parse(data.get("last_run"))
    res["last_run"] = lr.isoformat() if lr else None
    age = _age_h(data.get("last_run"), now)
    if data.get("last_error"):
        res["error"] = f"last pass failed: {data['last_error']}"
    elif age is None or age > FORMS_PASS_OK_H:
        res["error"] = "the daily Gmail pass has not run in the last 36 hours"
    return res


# ---------------------------------------------------- live dashboard feeds -----
# These are the connections the dashboard reads itself (not via the status
# agent): each check runs on demand when the tab opens, with a short timeout,
# and a failure only marks that one row. Nothing here is polled.
def _check(name: str, fn, timeout_s: float = 25.0) -> dict:
    """Run fn() -> (status, last_iso_or_None, note) in a thread with a deadline."""
    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TO
    with _TPE(max_workers=1) as ex:
        try:
            status, last, note = ex.submit(fn).result(timeout=timeout_s)
        except _TO:
            return {"name": name, "last": None, "age_h": None, "status": "warn", "note": f"no answer in {timeout_s:.0f}s"}
        except Exception as e:                          # noqa: BLE001 — reported on the row
            return {"name": name, "last": None, "age_h": None, "status": "down",
                    "note": f"{type(e).__name__}: {str(e)[:140]}"}
    d = _parse(last) if last else None
    now = datetime.now(timezone.utc)
    return {"name": name, "last": d.isoformat() if d else None,
            "age_h": round((now - d).total_seconds() / 3600, 2) if d else None,
            "status": status, "note": note}


def live_isdash_connections(settings, now: datetime) -> list[dict]:
    from datetime import date as _date

    from . import marketing, podium
    from .genesys import GenesysClient
    from .store import connect, get_config

    def c_genesys():
        y = (now.astimezone(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1))
        with GenesysClient(settings.genesys_region, settings.genesys_client_id, settings.genesys_client_secret) as g:
            agg = marketing._queue_agg(g, marketing._utc(y), marketing._utc(y, end=True), settings.genesys_queue_ids)
        offered = sum(v["offered"] for v in agg.values())
        return "ok", now.isoformat(), f"queue aggregates answered live · {offered} calls offered yesterday"

    def c_podium():
        if not podium.configured(settings):
            return "warn", None, "not connected (PODIUM_* missing)"
        conn = connect(settings.db_path)
        try:
            calls = conn.execute("SELECT MAX(created_at) FROM podium_calls").fetchone()[0]
            day = conn.execute("SELECT day, collected_at FROM podium_conv_days ORDER BY day DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        age = _age_h(day["collected_at"], now) if day else None
        status = "ok" if (age is not None and age <= 36) else "warn"
        # sign in for real: stored data stays "fresh" for a day after Podium cuts the dashboard off
        problem = podium.auth_problem(settings)
        if problem:
            return "warn", (day["collected_at"] if day else None), (
                f"{problem}. If the client secret was regenerated in Podium's developer portal, run "
                f"deploy/podium_set_secret.ps1 with the new secret. Calls mirrored through {str(calls)[:16]}.")
        return status, (day["collected_at"] if day else None), (
            f"calls mirrored through {str(calls)[:16]} · conversations collected each morning for the day before "
            f"(latest day {day['day'] if day else '—'})")

    def c_hours():
        from . import hours as hours_mod
        conn = connect(settings.db_path)
        try:
            n = hours_mod.sync_clock(conn)
            row = conn.execute("SELECT MAX(day), MAX(loaded_at) FROM agent_hours WHERE source = 'clock'").fetchone()
        finally:
            conn.close()
        return "ok", now.isoformat(), (f"worked hours from the dashboard time clock since {hours_mod.CLOCK_FROM} · latest day {row[0] or '—'} "
                                      f"({n} agent-days) · Homebase sheet frozen through {hours_mod.HOMEBASE_THROUGH}")

    def c_spend():
        sh = marketing.spend_sheet(settings)
        latest = max(sh["weeks"]) if sh["weeks"] else None
        age_d = (now.date() - _date.fromisoformat(latest)).days if latest else None
        status = "ok" if (age_d is not None and age_d <= 14) else "warn"
        return status, now.isoformat(), (f"service account opened the sheet · tabs {', '.join(sh['tabs'])} · "
                                         f"latest week ends {latest or '—'} · read every 10 minutes")

    def c_history():
        h = marketing.history_sheet(settings)
        yrs = sorted(set(h["calls"]) | set(h["forms"]))
        return "ok" if yrs else "warn", now.isoformat(), (
            f"weekly tracker sheet · calls {min(h['calls'] or [0])}–{max(h['calls'] or [0])}, "
            f"forms {min(h['forms'] or [0])}–{max(h['forms'] or [0])} · read every 10 minutes")

    checks = [("Genesys — queue volume (live)", c_genesys), ("Podium — calls & conversations", c_podium),
              ("Homebase hours sheet (Google Sheet)", c_hours), ("Spend sheet (Google service account)", c_spend),
              ("Weekly tracker sheet (Google service account)", c_history)]
    with ThreadPoolExecutor(max_workers=len(checks)) as ex:
        return list(ex.map(lambda nc: _check(nc[0], nc[1]), checks))
