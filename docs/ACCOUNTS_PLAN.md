# User Accounts & Permissions — plan

Drafted 2026-10-01 from Dallin's direction. Replaces the two shared passwords
(APP_PASSWORD / ADMIN_PASSWORD) with invited user accounts, emailed one-time
codes, per-tab/sub-tab permissions, a Payroll tab, and a locked-down agent page.

## Decisions made

- **Sign-in:** emailed 6-digit one-time code (no stored passwords). Invite-only.
- **Email:** Resend, same pattern as GCLID Sync / IS Commissions
  (`POST https://api.resend.com/emails`, `RESEND_API_KEY` in `.env`), from the
  already-verified no-reply address at brookspest.com.
- **Permissions:** per user, down to each tab and sub-tab. Presets for speed.
- **Agents get accounts**, but only a single locked page (below).
- **Cutover:** quick — build, test, switch. No long dual-running period.

## Data model (new tables in isadash.sqlite, additive)

- `users` — email (unique, lowercased), name, status (invited/active/disabled),
  kind (`staff` | `agent`), linked agent (agent_map key, for kind=agent),
  permissions (JSON list of keys), created/invited/last_login timestamps.
- `login_codes` — email, code HASH, expires (10 min), attempts, used flag.
- `sessions` — random token HASH, user, created, last_seen, expires (30 days).
  Server-side, so disabling a user or "sign out everywhere" is immediate.
- `auth_log` — who signed in / failed / was invited / had permissions changed.

## Permission keys

    home
    revenue
    agent.scorecard   agent.power   agent.compare
    metrics.team      metrics.phone metrics.forms  metrics.multiyear  metrics.acv
    sources
    payroll.roster    payroll.rules payroll.lookup
    settings.agents   settings.config  settings.software  settings.users
    sync

Presets: Admin (all) · Manager (everything but sources/payroll/settings) ·
Agent (none of the above — the agent page only).

Every API endpoint maps to one key and is refused server-side without it; the
UI hides tabs as a convenience, not as the protection.

## Payroll tab

Top-level tab, visible only with a `payroll.*` permission. Holds Commission
Roster, Payroll Rules (both moved out of Settings) and Commission Lookup (moved
out of Agent Metrics; today it is open to anyone with the app password).

## Agent page ("My Numbers")

One page, no date pickers, no other tabs. The server works out who the agent
is from the signed-in session — never from anything the browser sends — so one
agent can never request another's data.

- Their IS #'s metrics next to the team average. Serviced Between.
- Power Score for the current pay period (where they'd sit if it ended today)
  and the last pay period. Those are the only two date options.
- Commission lookup limited to accounts where they are the rep or a split
  partner; a split shows who it is shared with.
- Download their own commission report for the last pay period.

Pay periods: 14 days Sun–Sat, payroll Mondays anchored 2026-08-31 (same maths
as IS Commissions `periods.py`).

## Ripples to handle

- **Power Score is calculated in the browser today** (`computePowerScores`).
  The agent page must not receive every agent's numbers, so the calculation
  moves to the server and the existing Power Score tab reads the same result —
  one source of truth, not two copies that can drift.
- **Commission report depends on IS Commissions go-live.** Until the app's
  statement is the payroll basis (GO_LIVE_PLAN phase 3, 10/26 payroll), agents
  would see numbers that may not match their cheque. Build last, switch on then.
- **Scripts and the nightly sync** don't go through the login, so they are
  unaffected.
- **Lock-out safety:** the owner email is set in `.env` and always has full
  access; a server-side rescue command can create a session if email is down.

## Segments

1. **Foundation + email sign-in.** Tables, Resend sender, new login page,
   sessions. Old passwords still work, so nothing changes for anyone.
2. **Users screen** (Settings → Users): invite, presets + checkboxes, link an
   agent, disable, last sign-in, resend invite.
3. **Enforcement + Payroll tab + cutover.** Every endpoint checked, tabs and
   sub-tabs hidden per user, Payroll tab moved, shared passwords switched off.
4. **Agent page — metrics and Power Score** (includes moving the Power Score
   calculation to the server).
5. **Agent page — commissions:** own-account lookup, last-period report
   download. Timed with IS Commissions go-live.

## Open questions

- Agent page: show rank only ("3rd of 11"), or the full ranked list with names?
- Agent commission report: hold until the commissions app is the payroll basis?
- Resend: a separate API key for the dashboard, and the exact from-address.
- Which email is the owner login.

## Status (2026-10-01)

Segments 1-3 are built and deployed. Shared passwords still work until
`PASSWORD_LOGIN=off` is added to the server's `.env` (the cutover). Manager
preset = every regular tab + Payroll Rules + Commission Lookup + Run Sync.
Every API path's required permission lives in `webapp._ACCESS`; a path that
is not listed there is admin-only.
