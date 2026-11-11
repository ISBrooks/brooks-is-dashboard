# Software Management tab — how the dashboard reaches the other droplets

The Settings → Software Management tab shows the status of every Brooks app and
runs a few allow-listed actions. It works by SSH from the dashboard box to each
droplet, **on demand only** (when the tab is opened — nothing runs on a timer).

## Pieces

| Where | What |
|---|---|
| every droplet | `/usr/local/bin/isdash-agent` (this repo: `deploy/isdash-agent.py`) — returns JSON facts; runs allow-listed actions |
| every droplet | a line in `/root/.ssh/authorized_keys` that **forces** the dashboard's key to that agent |
| dashboard box | `/home/isdash/.ssh/isdash_agent` (private key, owned by `isdash`, mode 600) + `known_hosts` |
| dashboard | `src/isadash/software.py` (registry, verdicts, SSH), `/api/software`, `/api/software/run` |

The forced-command line looks like this — `restrict` turns off pty, forwarding
and rc files, and `command=` means the key can run the agent and nothing else:

```
restrict,command="/usr/local/bin/isdash-agent" ssh-ed25519 AAAA... isdash-agent
```

## Provisioning (done 2026-09-14; repeat for a new droplet)

```bash
# 1. dashboard box, as isdash — the private key never leaves this host
sudo -u isdash mkdir -p /home/isdash/.ssh && chmod 700 /home/isdash/.ssh
sudo -u isdash ssh-keygen -t ed25519 -N "" -C isdash-agent -f /home/isdash/.ssh/isdash_agent
PUB=$(cat /home/isdash/.ssh/isdash_agent.pub)

# 2. each droplet (including the dashboard's own, for the sourcing app)
scp deploy/isdash-agent.py root@<ip>:/usr/local/bin/isdash-agent
ssh root@<ip> 'chmod 755 /usr/local/bin/isdash-agent; grep -q isdash-agent /root/.ssh/authorized_keys || echo "restrict,command=\"/usr/local/bin/isdash-agent\" '"$PUB"'" >> /root/.ssh/authorized_keys'

# 3. dashboard box: pin every host key so the client never prompts or writes
sudo -u isdash bash -c 'ssh-keyscan -t ed25519 137.184.60.159 147.182.132.161 104.248.235.187 > /home/isdash/.ssh/known_hosts'
chmod 600 /home/isdash/.ssh/known_hosts

# 4. test from the dashboard box
sudo -u isdash ssh -i /home/isdash/.ssh/isdash_agent root@<ip> status | head -c 300
```

Optional `.env` overrides (defaults shown): `SOFTWARE_SSH_KEY=/home/isdash/.ssh/isdash_agent`,
`SOFTWARE_KNOWN_HOSTS=/home/isdash/.ssh/known_hosts`, `SOFTWARE_SSH_USER=root`.

## Updating the agent

Edit `deploy/isdash-agent.py`, then `scp` it to every droplet again. Adding an
action = adding it to `ACTIONS` in the agent **and** to that app's `actions`
in `software.py`; the dashboard refuses anything not in both lists.

## Things the verdict logic deliberately knows

- Red Note only runs 06:00–20:00 PT; overnight it reads **idle**, not down.
- The Podium sync logs `ERROR` for every "contact not found" — that is its
  designed skip path, so health reads the DB counters, not the log.
- GCLID's pending/failed upload counts include the deliberately held backfill.
- Sourcing channel thresholds (18/18/30/36 h) mirror its own hourly check.


## Agent v3 — Missed Call Texter (2026-10-19)

`app_mct()` reports `/home/mctext/missed-call-texter` on 147.182.132.161: service state,
the running mode from the app's own start line (`starting — LIVE` / `DRY RUN`),
config.yaml's `dry_run`, last poll ("Tick complete"), outcome counts for 24h / 7d,
stuck/retrying errors, send problems, last alert, and the 12 newest calls with the
phone cut to the last 4 digits.

Actions: `mct.pause` / `mct.resume` flip only the `dry_run:` line of config.yaml
(owner/mode kept, atomic replace), restart the service and confirm the mode from
the new start line — the app's own documented pause. Paused = still detecting and
recording who it WOULD text; resuming never texts those (the app's `catch-up`
command can, from the server). `mct.restart` restarts the unit.

GOTCHA: the agent must have LF line endings — a CRLF shebang
(`/usr/bin/env: 'python3'`) silently kills `status` on every box.
