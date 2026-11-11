#!/usr/bin/env bash
# Authorize the IS Dashboard's agent key on every Brooks droplet.
# Run from the laptop (needs the root key ~/.ssh/id_ed25519). Idempotent:
# backs up authorized_keys, removes any earlier broken attempt, appends the
# line only if it isn't there yet.
# What the line does: `restrict` disables shell/pty/forwarding for this key and
# `command=` pins it to the agent, so the key can ONLY return status JSON and
# run the agent's allow-listed actions. See deploy/SOFTWARE_MGMT.md.
#
# The line is defined INSIDE the remote script on purpose: anything passed to
# ssh as an argument gets re-parsed by the remote shell, which strips the
# quotes and splits the key off — that is exactly the bug the first version had.
set -u
for h in 137.184.60.159 147.182.132.161 104.248.235.187; do
  echo "== $h =="
  ssh -i ~/.ssh/id_ed25519 -o ConnectTimeout=10 "root@$h" bash -s <<'EOF'
F=/root/.ssh/authorized_keys
L='restrict,command="/usr/local/bin/isdash-agent" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMsjshZ0gHdEsNoJbx9IAS+NLvdFnJPBQfFbH+XxUIdB isdash-agent'
cp -p "$F" "$F.bak-$(date +%Y%m%d-%H%M)"
# drop any earlier broken attempt: a restrict line with no key material on it
grep -vE '^restrict,command=[^ ]*$' "$F" > "$F.tmp" && cat "$F.tmp" > "$F" && rm -f "$F.tmp"
if grep -qF "$L" "$F"; then echo "already installed"; else printf '%s\n' "$L" >> "$F"; echo "installed"; fi
chmod 600 "$F"
echo "agent lines: $(grep -cF 'command="/usr/local/bin/isdash-agent"' "$F")  (should be 1)"
echo "total keys:  $(grep -c 'ssh-' "$F")"
EOF
done
