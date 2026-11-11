# How the dashboard gets deployed

**The rule: production runs whatever is on the `main` branch of
`github.com/ISBrooks/brooks-is-dashboard`. Nothing is copied to the droplet by hand.**
A file changed directly on the server makes the next deploy refuse to run until
that change is brought into the repository.

## What happens on a push to `main`

1. **GitHub Actions checks the code** (`.github/workflows/deploy.yml`): installs the
   pinned packages, compiles every Python file, parses `web/app.js`, imports the app,
   and runs the tests in `tests/` (every data feed against a made-up sample database,
   compared with the saved numbers in `tests/snapshots/`).
2. If that passes and the repository variable `DEPLOY_ENABLED` is `true`, GitHub
   connects to the droplet with a key that can run **one command only**:
   `/usr/local/bin/isdash-deploy` (source: `deploy/isdash-deploy.sh`).
3. The droplet pulls `main` with its own read-only key and:
   - refuses if a tracked file was edited on the server outside git;
   - waits (up to 15 minutes) while a payroll run, a Sync or the 5 AM sync is in
     flight, because restarting the web service would kill them;
   - installs packages if `requirements.txt` changed;
   - compiles and imports the new code **before** restarting;
   - restarts the service only when Python code changed (a change to `web/` alone
     goes live with no restart), then checks the site answers;
   - on any failure puts the previous version back. The run shows red in GitHub
     and GitHub emails whoever pushed.

Every deploy is logged on the droplet in `/var/log/isdash-deploy.log`.

## Day to day

- Work on a branch, merge to `main` when it should go live.
- To undo a bad change: revert the commit on `main` and push; that deploys the
  previous code again.
- To re-run a deploy that was skipped because the server was busy: open the run in
  GitHub (Actions tab) and press **Re-run jobs**.
- To pause deploys: set the repository variable `DEPLOY_ENABLED` to `false`.
- A restart takes the site down for a few seconds. Avoid pushing Python changes
  while agents are clocking in or out at the top of a shift.

## Running the tests yourself

```
pip install -r requirements-dev.txt
python -m pytest
```

If a change moves a number on purpose (a rule change), regenerate the saved
numbers and review the difference in git before committing it:
`UPDATE_SNAPSHOTS=1 python -m pytest tests/test_money_numbers.py`

## If GitHub Actions is stuck

On 2026-11-11 GitHub left runs "queued" for a long time with no machine assigned.
The deploy does not depend on it: run the same checks on a clean copy of the
pushed commit, then start the droplet's deploy script directly with the root key.
It still pulls `main` from GitHub and does every safety check listed above.

```
ssh root@DROPLET /usr/local/bin/isdash-deploy deploy
ssh root@DROPLET /usr/local/bin/isdash-deploy status
```

## Web server

Caddy (`/etc/caddy/Caddyfile` on the droplet, not in git) serves only this site:
it compresses responses (`encode zstd gzip`) and passes them to the app on
127.0.0.1:8377. Backups of earlier versions sit next to it as `Caddyfile.bak-*`.

## What is NOT in git (and a deploy never touches)

`.env` (secrets), `data/` (the database), the Google key, `exports/`, `previews/`,
`reports/`, and the old `*.bak*` copies.

## Keys involved

| Key | Lives on | Can do |
|---|---|---|
| GitHub Actions deploy key (`DEPLOY_SSH_KEY` secret) | GitHub | run `isdash-deploy` on the droplet, nothing else |
| Droplet read key (`/home/isdash/.ssh/github_deploy`) | droplet | read this one repository |

`isdash-deploy` runs as root (it has to restart the service) but does all file,
git and package work as the `isdash` user. The installed copy is root-owned on
purpose; after changing `deploy/isdash-deploy.sh`, re-run the `install` phase of
`deploy/setup-git-deploy.sh` to update it.

## One-time setup / undo

`deploy/setup-git-deploy.sh` (phases `key`, `convert`, `install`, `undo`). `undo`
removes the Actions key, the installed script and the `.git` folder, leaving the
files exactly as they are, so the old copy-up routine works again.
