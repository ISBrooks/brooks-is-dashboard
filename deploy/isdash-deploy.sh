#!/usr/bin/env bash
# isdash-deploy - put the latest `main` from GitHub live on this droplet.
#
# Installed (root-owned) at /usr/local/bin/isdash-deploy by
# deploy/setup-git-deploy.sh. GitHub Actions reaches it through an SSH key that
# is pinned to this one command (restrict,command=... in root's authorized_keys),
# so that key can do nothing else on the server. See deploy/DEPLOY.md.
#
# What a deploy does, in order:
#   1. refuses if a tracked file was edited on the server outside git
#   2. waits while a payroll run or a data sync is in flight (a restart would
#      kill them: they are child processes of the web service)
#   3. checks the new code out, installs packages if requirements.txt changed
#   4. compiles + imports the app BEFORE restarting, so broken code never goes live
#   5. restarts the service (only if Python code changed) and checks it answers
#   6. on any failure puts the previous version back and exits non-zero
#
# The whole script is one function called on the last line, so bash has read
# all of it before a checkout can replace this file's source in the repo.
set -euo pipefail

main() {
  local APP="${ISDASH_APP:-/home/isdash/brooks-is-dash}"
  local RUN_AS="${ISDASH_USER:-isdash}"
  local SVC="${ISDASH_SVC:-brooks-is-dash.service}"
  local SYNC_SVC="${ISDASH_SYNC_SVC:-brooks-is-dash-sync.service}"
  local BRANCH="${ISDASH_BRANCH:-main}"
  local HEALTH_URL="${ISDASH_HEALTH_URL:-http://127.0.0.1:8377/robots.txt}"
  local LOG="${ISDASH_LOG:-/var/log/isdash-deploy.log}"
  local LOCK="${ISDASH_LOCK:-/run/lock/isdash-deploy.lock}"
  local COMM_RUN="${ISDASH_COMM_RUN:-/home/isdash/is-commissions/data/payroll_run.json}"
  local CGROUP_ROOT="${ISDASH_CGROUP_ROOT:-/sys/fs/cgroup}"
  local BUSY_WAIT_S="${ISDASH_BUSY_WAIT_S:-900}"
  local HEALTH_WAIT_S="${ISDASH_HEALTH_WAIT_S:-60}"
  local PY="$APP/.venv/bin/python"

  local cmd="${SSH_ORIGINAL_COMMAND:-${1:-deploy}}"
  case "$cmd" in
    deploy|status) ;;
    *) echo "isdash-deploy: only 'deploy' and 'status' are allowed"; exit 2 ;;
  esac

  say() { printf '%s\n' "$*"; printf '%s  %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true; }
  # everything that touches the app's files runs as the app's own user
  as_app() { runuser -u "$RUN_AS" -- env HOME="/home/$RUN_AS" GIT_TERMINAL_PROMPT=0 "$@"; }
  g() { as_app git -C "$APP" "$@"; }
  short() { g rev-parse --short "$1"; }

  # ---- what would a restart interrupt right now? (empty = nothing) ----
  busy_reason() {
    if [ -f "$COMM_RUN" ]; then
      # same test the dashboard's own button uses: state "running" AND that process still exists
      local run state pid
      run=$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); print(d.get("state") or "-", d.get("pid") or "-")' "$COMM_RUN" 2>/dev/null | tr -d '\r' || true)
      state=${run%% *}; pid=${run##* }
      if [ "$state" = "running" ] && [[ "$pid" =~ ^[0-9]+$ ]] && [ -d "/proc/$pid" ]; then
        echo "a payroll run is in progress"; return
      fi
    fi
    case "$(systemctl show "$SYNC_SVC" -p ActiveState --value 2>/dev/null || true)" in
      active|activating|deactivating) echo "the scheduled data sync is running"; return ;;
    esac
    local cg procs
    cg=$(systemctl show "$SVC" -p ControlGroup --value 2>/dev/null || true)
    if [ -n "$cg" ] && [ -r "$CGROUP_ROOT$cg/cgroup.procs" ]; then
      procs=$(wc -l < "$CGROUP_ROOT$cg/cgroup.procs")
      if [ "$procs" -gt 1 ]; then echo "the app is running a background job (a Sync or a commissions task)"; return; fi
    fi
    echo ""
  }

  healthy() {
    local waited=0 ok=0
    while [ "$waited" -lt "$HEALTH_WAIT_S" ]; do
      if systemctl is-active --quiet "$SVC" && curl -fsS -m 3 -o /dev/null "$HEALTH_URL" 2>/dev/null; then
        ok=$((ok + 1))
        # two good answers in a row: it came up and stayed up
        if [ "$ok" -ge 2 ]; then return 0; fi
      else
        ok=0
      fi
      sleep 2; waited=$((waited + 2))
    done
    return 1
  }

  # compile every file, then import the app the way the service does
  preflight() {
    as_app "$PY" -m compileall -q "$APP/src" "$APP/scripts" >/dev/null || return 1
    (cd "$APP" && as_app "$PY" -c 'import sys; sys.path.insert(0, "src"); import isadash.webapp') || return 1
  }

  if [ "$cmd" = "status" ]; then
    echo "version: $(short HEAD) ($(g log -1 --format=%s HEAD | cut -c1-70))"
    echo "service: $(systemctl is-active "$SVC" 2>/dev/null || true)"
    local why; why=$(busy_reason)
    echo "busy:    ${why:-no}"
    exit 0
  fi

  exec 9>"$LOCK"
  if ! flock -n 9; then echo "another deploy is already running"; exit 75; fi

  g fetch --quiet origin "$BRANCH"
  local PREV TARGET
  PREV=$(g rev-parse HEAD)
  TARGET=$(g rev-parse "origin/$BRANCH")

  if [ "$PREV" = "$TARGET" ]; then
    say "already on $(short HEAD) - nothing to deploy"
    exit 0
  fi

  local dirty
  dirty=$(g status --porcelain --untracked-files=no)
  if [ -n "$dirty" ]; then
    say "REFUSED: these files were changed on the server outside git:"
    printf '%s\n' "$dirty"
    echo "Copy those changes into the repository (or discard them), then deploy again."
    exit 3
  fi

  local changed code_changed=0 pip_changed=0
  changed=$(g diff --name-only "$PREV" "$TARGET")
  if printf '%s\n' "$changed" | grep -qE '^(src/|scripts/|requirements\.txt$)'; then code_changed=1; fi
  if printf '%s\n' "$changed" | grep -qx 'requirements.txt'; then pip_changed=1; fi

  say "deploying $(short "$PREV") -> $(short "$TARGET") ($(printf '%s\n' "$changed" | grep -c . || true) files; restart: $([ "$code_changed" = 1 ] && echo yes || echo no))"

  if [ "$code_changed" = 1 ]; then
    local waited=0 why
    while :; do
      why=$(busy_reason)
      [ -z "$why" ] && break
      if [ "$waited" -ge "$BUSY_WAIT_S" ]; then
        say "NOT DEPLOYED: $why and it did not finish within $((BUSY_WAIT_S / 60)) minutes. Nothing was changed - run the deploy again later."
        exit 75
      fi
      [ "$waited" = 0 ] && echo "waiting: $why"
      sleep 10; waited=$((waited + 10))
    done
  fi

  put_back() {
    g checkout --quiet -B "$BRANCH" "$PREV"
    if [ "$pip_changed" = 1 ]; then as_app "$PY" -m pip install --quiet -r "$APP/requirements.txt" || true; fi
  }

  g checkout --quiet -B "$BRANCH" "$TARGET"

  if [ "$code_changed" = 1 ]; then
    if [ "$pip_changed" = 1 ]; then
      if ! as_app "$PY" -m pip install --quiet -r "$APP/requirements.txt"; then
        put_back
        say "FAILED: installing the Python packages failed. Previous version $(short "$PREV") kept; the site was not restarted."
        exit 1
      fi
    fi
    if ! preflight; then
      put_back
      say "FAILED: the new code does not compile or import (error above). Previous version $(short "$PREV") kept; the site was not restarted."
      exit 1
    fi

    systemctl restart "$SVC"
    if ! healthy; then
      echo "---- last lines from the service log ----"
      journalctl -u "$SVC" -n 25 --no-pager 2>/dev/null || true
      echo "-----------------------------------------"
      put_back
      systemctl restart "$SVC"
      if healthy; then
        say "ROLLED BACK: the new version did not come up healthy. $(short "$PREV") is live again."
        exit 1
      fi
      say "ROLLBACK FAILED: the site is NOT answering on $(short "$PREV") either. It needs attention now."
      exit 4
    fi
  fi

  say "DEPLOYED $(short "$TARGET"): $(g log -1 --format=%s "$TARGET" | cut -c1-80)"
}

main "$@"
