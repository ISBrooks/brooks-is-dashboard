#!/usr/bin/env python3
"""Put a newly generated Podium client secret into the Brooks apps on THIS server.

All three apps that talk to Podium (the IS Dashboard, the PestRoutes -> Podium
sync and the Missed Call Texter) sign in with the SAME Podium developer app.
Generating a new client secret in Podium's developer portal kills the old one
at once, and every app starts getting `401 invalid_client` (2026-11-19).

Run by deploy/podium_set_secret.ps1, as root, standard library only:

    python3 podium_set_secret.py check  <app> [<app> ...]   # changes nothing
    python3 podium_set_secret.py apply  <app> [<app> ...]

The new secret is read from the first line of standard input. It is never
printed, logged, or put on a command line.

`check` asks Podium for a token using each app's OWN client ID and saved
permission (refresh token) plus the new secret. `apply` repeats that check and
only then swaps the one PODIUM_CLIENT_SECRET line in the app's .env (owner and
mode kept, atomic replace, root-only backup in /root/podium-secret-backups)
and restarts the app's service.

Exit codes: 0 fine, 4 fine but an app must be re-authorized in Podium,
2 Podium rejected the secret (nothing changed), 3 something else went wrong.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN_URL = "https://accounts.podium.com/oauth/token"
KEY = "PODIUM_CLIENT_SECRET"
BACKUP_DIR = "/root/podium-secret-backups"
TARGETS = {
    "dashboard": {"label": "IS Dashboard", "env": "/home/isdash/brooks-is-dash/.env",
                  "service": "brooks-is-dash.service"},
    "sync": {"label": "PestRoutes -> Podium Sync", "env": "/home/frsync/fieldroutes-podium-sync/.env",
             "service": "fr-podium-sync.service"},
    "texter": {"label": "Missed Call Texter", "env": "/home/mctext/missed-call-texter/.env",
               "service": "missed-call-texter.service"},
}
# the dashboard's web process also runs the Sync button and commission jobs;
# these are the same "would a restart interrupt something?" tests as isdash-deploy
DASH_SYNC_SVC = "brooks-is-dash-sync.service"
DASH_COMM_RUN = "/home/isdash/is-commissions/data/payroll_run.json"
DASH_HEALTH_URL = "http://127.0.0.1:8377/robots.txt"
BUSY_WAIT_S = 900

_ASSIGN = re.compile(r"^(\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(.*?)\s*$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def read_env(text: str) -> dict:
    """KEY -> value for every assignment line (the last one wins, as dotenv does)."""
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            out[m.group(2)] = _unquote(m.group(3))
    return out


def rewrite_env(text: str, key: str, value: str) -> tuple:
    """Return (new_text, lines_changed): only the value of `key` changes; the
    quoting style, every other line and the file's line endings are kept."""
    out, changed = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        m = None if body.lstrip().startswith("#") else _ASSIGN.match(body)
        if m and m.group(2) == key:
            old = m.group(3).strip()
            quote = old[0] if len(old) >= 2 and old[0] == old[-1] and old[0] in "\"'" else ""
            body = m.group(1) + quote + value + quote
            changed += 1
        out.append(body + ending)
    return "".join(out), changed


def ask_podium(client_id: str, secret: str, refresh_token: str) -> tuple:
    """('ok' | 'bad-secret' | 'reauthorize' | 'other', short text). Never returns
    or prints anything from a successful answer."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": secret}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "podium-secret-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return "other", "could not reach Podium (%s)" % type(e).__name__
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if status == 200 and body.get("access_token"):
        return "ok", "Podium accepted the new secret"
    err = str(body.get("error") or "")[:40]
    desc = str(body.get("error_description") or "")[:120]
    if err == "invalid_client":
        return "bad-secret", "Podium rejected it (%s)" % (desc or err)
    if err == "invalid_grant":
        return "reauthorize", ("the secret is accepted, but this app's saved Podium permission "
                               "is no longer valid (%s)" % (desc or err))
    return "other", "unexpected answer from Podium: HTTP %s %s %s" % (status, err, desc)


def dash_busy() -> str:
    try:
        with open(DASH_COMM_RUN, encoding="utf-8") as f:
            run = json.load(f)
        if run.get("state") == "running" and str(run.get("pid") or "").isdigit() \
                and os.path.isdir("/proc/%s" % run["pid"]):
            return "a payroll run is in progress"
    except (OSError, ValueError, AttributeError):
        pass
    state = _sh(["systemctl", "show", DASH_SYNC_SVC, "-p", "ActiveState", "--value"])
    if state in ("active", "activating", "deactivating"):
        return "the scheduled data sync is running"
    cg = _sh(["systemctl", "show", TARGETS["dashboard"]["service"], "-p", "ControlGroup", "--value"])
    try:
        with open("/sys/fs/cgroup%s/cgroup.procs" % cg, encoding="ascii") as f:
            if cg and len(f.read().split()) > 1:
                return "the dashboard is running a background job (a Sync or a commissions task)"
    except OSError:
        pass
    return ""


def _sh(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def restart(name: str, t: dict) -> tuple:
    """(ok, text). Restarts the app's service and confirms it came back."""
    if name == "dashboard":
        waited = 0
        why = dash_busy()
        while why and waited < BUSY_WAIT_S:
            if waited == 0:
                say("  wait     %s: %s - waiting up to 15 minutes before restarting" % (t["label"], why))
            time.sleep(10)
            waited += 10
            why = dash_busy()
        if why:
            return False, ("the new secret is saved but the service was NOT restarted (%s). "
                           "Run this again later." % why)
    r = subprocess.run(["systemctl", "restart", t["service"]], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return False, "restart failed: %s" % (r.stderr.strip()[:160] or "systemctl error")
    for _ in range(30):
        time.sleep(2)
        if _sh(["systemctl", "is-active", t["service"]]) != "active":
            continue
        if name != "dashboard":
            return True, "restarted and running"
        try:
            with urllib.request.urlopen(DASH_HEALTH_URL, timeout=3) as h:
                if h.status == 200:
                    return True, "restarted and answering"
        except (urllib.error.URLError, OSError):
            pass
    return False, "restarted, but it did not come back within a minute - check the service"


def save_secret(path: str, text: str, new_text: str) -> str:
    """Backup (root only) then atomic replace, keeping the file's owner and mode."""
    st = os.stat(path)
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = "%s/%s.env.%s" % (BACKUP_DIR, os.path.basename(os.path.dirname(path)), stamp)
    for target, content, uid, gid, mode in ((backup, text, 0, 0, 0o600),
                                            (path + ".new-podium-secret", new_text, st.st_uid, st.st_gid,
                                             st.st_mode & 0o777)):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chown(target, uid, gid)
        os.chmod(target, mode)
    os.replace(path + ".new-podium-secret", path)
    return backup


def say(line: str) -> None:
    print(line, flush=True)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("check", "apply") \
            or any(n not in TARGETS for n in sys.argv[2:]):
        say("usage: podium_set_secret.py check|apply %s" % " ".join(TARGETS))
        return 3
    mode, names = sys.argv[1], sys.argv[2:]
    if os.geteuid() != 0:
        say("This has to run as root.")
        return 3
    secret = sys.stdin.buffer.readline().decode("utf-8-sig", "replace").strip()
    if len(secret) < 8 or re.search(r"\s", secret) or chr(0xFFFD) in secret:
        say("  STOP     that does not look like a client secret (empty, too short, or has spaces). Nothing was changed.")
        return 3

    worst = reauth = 0                     # worst: 0 fine, 3 a problem; reauth reported separately
    for name in names:
        t = TARGETS[name]
        try:
            with open(t["env"], encoding="utf-8", newline="") as f:
                text = f.read()
        except OSError as e:
            say("  PROBLEM  %s: cannot read its settings file (%s)" % (t["label"], type(e).__name__))
            worst = max(worst, 3)
            continue
        env = read_env(text)
        if not env.get("PODIUM_CLIENT_ID") or not env.get("PODIUM_REFRESH_TOKEN") or KEY not in env:
            say("  PROBLEM  %s: its settings file has no Podium connection to update" % t["label"])
            worst = max(worst, 3)
            continue
        verdict, why = ask_podium(env["PODIUM_CLIENT_ID"], secret, env["PODIUM_REFRESH_TOKEN"])
        if verdict == "bad-secret":
            say("  REJECTED %s: %s" % (t["label"], why))
            return 2                       # the same secret goes to every app: stop at once
        if verdict == "other":
            say("  PROBLEM  %s: %s" % (t["label"], why))
            worst = max(worst, 3)
            continue
        if verdict == "reauthorize":
            reauth = 4
        if mode == "check":
            say("  %s %s: %s" % ("ok      " if verdict == "ok" else "REAUTH  ", t["label"], why))
            continue

        if env[KEY] == secret:
            saved = "already had the new secret"
        else:
            new_text, n = rewrite_env(text, KEY, secret)
            if n < 1 or read_env(new_text).get(KEY) != secret:
                say("  PROBLEM  %s: could not place the secret in its settings file; left untouched" % t["label"])
                worst = max(worst, 3)
                continue
            save_secret(t["env"], text, new_text)
            saved = "new secret saved"
        if verdict == "reauthorize":
            say("  REAUTH   %s: %s; not restarted - %s" % (t["label"], saved, why))
            continue
        ok, how = restart(name, t)
        say("  %s %s: %s; %s" % ("done    " if ok else "PROBLEM ", t["label"], saved, how))
        if not ok:
            worst = max(worst, 3)
    return worst or reauth


if __name__ == "__main__":
    sys.exit(main())
