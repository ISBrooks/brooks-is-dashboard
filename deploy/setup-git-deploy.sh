#!/usr/bin/env bash
# One-time switch-over of the droplet to push-to-deploy. Run from the laptop,
# one phase at a time (each is safe to repeat):
#
#   ssh root@DROPLET 'bash -s key'      < deploy/setup-git-deploy.sh
#   ssh root@DROPLET 'bash -s convert'  < deploy/setup-git-deploy.sh
#   ssh root@DROPLET "PUBKEY='ssh-ed25519 AAAA... github-actions-deploy' bash -s install" < deploy/setup-git-deploy.sh
#   ssh root@DROPLET 'bash -s undo'     < deploy/setup-git-deploy.sh     # back to how it was
#
# key      make the droplet's own read-only key for GitHub and print its public half
#          (add that to the repository as a read-only deploy key before `convert`)
# convert  turn the app folder into a git checkout of main WITHOUT changing any
#          running code: aborts if a code file on the server differs from GitHub
# install  install /usr/local/bin/isdash-deploy and authorize the GitHub Actions
#          key for that one command only
# undo     remove the Actions key, the installed script and the .git folder
#
# Nothing here restarts the web service. See deploy/DEPLOY.md.
set -euo pipefail

APP=/home/isdash/brooks-is-dash
RUN_AS=isdash
HOME_DIR=/home/isdash
REPO_SSH=git@github.com:ISBrooks/brooks-is-dashboard.git
GH_KEY=$HOME_DIR/.ssh/github_deploy
BIN=/usr/local/bin/isdash-deploy
AUTH=/root/.ssh/authorized_keys
# GitHub's published host key (https://api.github.com/meta -> ssh_keys)
GH_HOST='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'

as_app() { runuser -u "$RUN_AS" -- env HOME="$HOME_DIR" GIT_TERMINAL_PROMPT=0 "$@"; }
g() { as_app git -C "$APP" "$@"; }

phase="${1:-}"
case "$phase" in
key)
  if [ ! -f "$GH_KEY" ]; then
    as_app ssh-keygen -q -t ed25519 -N '' -C 'isdash-droplet-read' -f "$GH_KEY"
    echo "created $GH_KEY"
  else
    echo "already exists: $GH_KEY"
  fi
  KH=$HOME_DIR/.ssh/known_hosts
  if ! grep -qF "$GH_HOST" "$KH" 2>/dev/null; then
    printf '%s\n' "$GH_HOST" | as_app tee -a "$KH" >/dev/null
    echo "added github.com to $KH"
  fi
  echo "PUBLIC KEY (add to the repository as a read-only deploy key):"
  cat "$GH_KEY.pub"
  ;;

convert)
  if [ -d "$APP/.git" ]; then
    echo "already a git checkout: $(g rev-parse --short HEAD)"; exit 0
  fi
  g init --quiet -b main
  g config core.autocrlf false
  g config core.sshCommand "ssh -i $GH_KEY -o IdentitiesOnly=yes -o BatchMode=yes"
  g remote add origin "$REPO_SSH"
  if ! g fetch --quiet origin main; then
    rm -rf "$APP/.git"
    echo "ABORTED: could not read the repository from GitHub (is the deploy key added?). Nothing changed."; exit 1
  fi
  # point git at main without touching a single file, then see what differs
  g reset --quiet --mixed origin/main
  diffs=$(g status --porcelain --untracked-files=no || true)
  code=$(printf '%s\n' "$diffs" | grep -E '^.. (src/|web/|scripts/|config/)' | grep -vE '^ D ' || true)
  echo "files on the server that differ from GitHub main:"
  printf '%s\n' "${diffs:-  (none)}"
  if [ -n "$code" ]; then
    rm -rf "$APP/.git"
    echo "ABORTED: running code on the server differs from GitHub (listed above)."
    echo "Bring those changes into the repository first. Nothing changed."; exit 1
  fi
  # only non-code files differ (or files GitHub has that the server lacks): take main's
  g reset --quiet --hard origin/main
  g branch --quiet --set-upstream-to=origin/main main
  echo "converted: $APP is now on $(g rev-parse --short HEAD), service untouched"
  ;;

install)
  : "${PUBKEY:?set PUBKEY to the GitHub Actions public key line}"
  case "$PUBKEY" in ssh-ed25519\ AAAA*) ;; *) echo "PUBKEY does not look like an ed25519 public key"; exit 1 ;; esac
  [ -f "$APP/deploy/isdash-deploy.sh" ] || { echo "run 'convert' first"; exit 1; }
  # root-owned copy: the script runs as root, so the app's user must not be able to edit it
  install -o root -g root -m 755 "$APP/deploy/isdash-deploy.sh" "$BIN"
  touch /var/log/isdash-deploy.log
  L="restrict,command=\"$BIN\" $PUBKEY"
  cp -p "$AUTH" "$AUTH.bak-$(date +%Y%m%d-%H%M)"
  if grep -qF "$L" "$AUTH"; then echo "key already authorized"; else printf '%s\n' "$L" >> "$AUTH"; echo "key authorized"; fi
  chmod 600 "$AUTH"
  echo "deploy lines: $(grep -cF "command=\"$BIN\"" "$AUTH")  (should be 1)"
  echo "total keys:   $(grep -c 'ssh-' "$AUTH")"
  "$BIN" status
  ;;

undo)
  if [ -f "$AUTH" ] && grep -qF "command=\"$BIN\"" "$AUTH"; then
    cp -p "$AUTH" "$AUTH.bak-$(date +%Y%m%d-%H%M)"
    grep -vF "command=\"$BIN\"" "$AUTH" > "$AUTH.tmp" && cat "$AUTH.tmp" > "$AUTH" && rm -f "$AUTH.tmp"
    chmod 600 "$AUTH"; echo "removed the deploy key"
  fi
  rm -f "$BIN" && echo "removed $BIN"
  rm -rf "$APP/.git" && echo "removed $APP/.git (files left exactly as they are)"
  ;;

*)
  echo "usage: bash -s {key|convert|install|undo}"; exit 2 ;;
esac
