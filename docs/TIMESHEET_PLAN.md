# Timesheet, Scheduling & PTO — plan (draft 1, 2026-10-09)

Goal: replace the "IS Scheduling" Google Sheet and the old website schedule with
a scheduling + timesheet + PTO system that lives inside the IS Dashboard.
Agents clock in and out, see their schedule and PTO, and request time off on
their page. Managers build the schedule, approve requests, and run payroll
reports from a new **Agent Management** tab.

What the sheet does today (so nothing is lost):

| Sheet feature | Where it goes |
|---|---|
| One block per week: names × Mon–Sun, free-text shift cells ("9-4:30", "8-11, 1:30-7", "OFF", "-") | Schedule builder: structured shifts (start/end, 15-min steps, up to 2 segments) — no free text |
| Coverage matrix: headcount per hour per day (with .5 for half hours) | Live coverage strip under the week grid, recalculated as shifts move |
| Seating chart (who is on at each hour) | Day view: a timeline of everyone's shift, hour by hour |
| TEMPLATE WEEK copied forward each week | Saved templates; "Build next week from template / from last week" |
| Website Schedule tab (screenshot posted for agents) | The agent page Schedule tab (already live) |
| Time Off Requests tab (rep, requested on, dates, approved, changed on schedule) | PTO / time-off requests with approval workflow, applied to the schedule automatically |
| Lunch tab | Optional unpaid-break rule per shift (see open questions) |

---

## 1. Agent side (Schedule tab, three panels)

**Clock.** Big Clock In / Clock Out button with the current status
("Clocked in since 9:02 · shift 9–5"). One open punch at a time. The server
stamps the time (never the browser's clock). Edits to punches are manager-only
and leave an audit trail (who, when, old → new, reason).

- Auto clock-out: when a punch is still open at the scheduled shift end, a
  server job closes it at the shift end and flags the punch "auto". Manager
  sees a list of auto-closed punches to confirm or fix. Grace window
  (e.g. 10 min after shift end) so someone finishing a call isn't cut off —
  configurable.
- Early/late: clock-in before the shift start is recorded as-is but paid time
  starts at the earlier of actual/scheduled per a rounding rule (open
  question). Missed clock-in with a scheduled shift → manager alert.
- Breaks: optional "Start break / End break" (unpaid), or a flat auto-deducted
  lunch for shifts over N hours (open question: California/Oregon/Washington
  meal-break rules differ; the dashboard should at least record them).

**My schedule.** What's there now, plus: PTO days shown on the calendar,
pending requests shown as "requested", a "Swap" note on a day opens the day
roster (already built) so they can see who to ask. (A formal swap workflow —
request → other agent accepts → manager approves — is a later segment.)

**PTO.** Balance card: accrued, used, pending, available; accrual rate; the
date they became eligible. "Request time off": date range (or partial day),
type (PTO / unpaid / sick where applicable), note; shows how many PTO hours
it would use against scheduled hours. Requests list with status.

## 2. Manager side (new top-level tab **Agent Management**)

Sub-tabs:

1. **Schedule builder** (the sheet replacement)
   - Week grid: agents × Mon–Sun. Click a cell to set a shift from presets
     (9–5, 11–7, 8–3 …) or custom start/end; second segment allowed
     ("8–11, 1:30–7"). Drag a shift to another day; drag its edges to move
     start/end earlier/later; copy a day across the week.
   - Coverage strip: headcount per hour per day, colour-banded against a
     target (e.g. green ≥ 15 at 11:00) — the sheet's matrix, live.
   - Day timeline: everyone's bar on an hour axis (the seating chart).
   - Templates: save the current week as a template; "Build week from
     template"; "Copy last week". Publish/unpublish: agents only see
     published weeks; a change to a published week notifies the affected
     agent (email via Resend) — optional.
   - PTO/time-off approvals land on the grid automatically (shift cell →
     "PTO").
   - Conflicts flagged: overlapping segments, shift during approved PTO,
     unavailable days (if we add availability later).
2. **Timesheets** — punches per agent per pay period: scheduled vs worked,
   late/early, auto clock-outs to confirm, edit with reason, approve the
   period. Export (CSV) for payroll; later a feed into the commissions app.
3. **Time off** — queue of requests, approve/deny with a note, calendar of
   who's out when, balance adjustments (manual credit/debit with reason).
4. **Agents** — per-agent HR fields the rest needs: start date, employment
   status (full/part time), standard weekly hours, state (for break rules),
   PTO plan, pay-period anchor, active/terminated date. Lives on the
   existing agent record (agent_map) so the roster stays one list.
5. **Settings** — PTO rules, auto clock-out grace, rounding, break rule,
   coverage targets, shift presets.

## 3. PTO rules (to be confirmed with the policy doc)

Working assumption from Dallin: PTO starts after 6 months of full-time
employment; full-time = 30+ hours/week. So the engine needs per agent:
start date, full-time status (or a rolling check of scheduled/worked hours
≥ 30/week), and the accrual rate. Model:

- `eligible_from` = start date + 6 months, provided the full-time test holds
  (open question: measured on scheduled hours, worked hours, or a flag set by
  a manager?).
- Accrual: hours of PTO per hour worked (e.g. 1 per 30) or per pay period;
  cap on balance; carryover rule at year end; whether PTO accrues while on PTO.
- Usage: deducts on the day taken (approved request + the day passes);
  partial days allowed; negative balances not allowed (or allowed up to X).
- History: every accrual/use/adjustment is a ledger row, so a balance is
  always explainable.

## 4. Data model (SQLite, same database)

- `shifts` (id, agent, date, start, end, segment 2 start/end, kind: work|pto|unpaid|sick, published, source: manual|template|request, created_by, updated_at)
- `shift_templates` (id, name, rows JSON) 
- `punches` (id, agent, in_at, out_at, out_kind: manual|auto|edited, shift_id, break_min, note)
- `punch_audit` (punch_id, changed_by, changed_at, field, old, new, reason)
- `time_off_requests` (id, agent, start, end, partial hours, type, note, status, decided_by, decided_at, decision_note)
- `pto_ledger` (id, agent, date, kind: accrual|use|adjust, hours, note, ref)
- agent HR fields on `agent_map` (start_date, employment_type, weekly_hours, work_state, pto_plan, active_to)
- `schedule_settings` in app_config (grace, rounding, targets, presets)

Hours for the Master Metric: once punches exist, worked hours can come from
punches instead of the Homebase sheet (switchable, so nothing breaks).

## 5. Segments (each shippable on its own)

1. **Agent HR fields + PTO rules engine** — start dates and status entered;
   balances computed and shown read-only on the agent page. (Needs the PTO
   policy + accrual report from Dallin.)
2. **Clock in/out** — punches, status card, auto clock-out at shift end
   (using the schedule sheet as the shift source for now), manager
   timesheet view with edits + audit. The schedule sheet keeps running.
3. **Time-off requests** — agent request → manager approve/deny → balance
   ledger. Approved days also appear on the agent's schedule view.
4. **Schedule builder** — week grid, presets, drag to move, coverage strip,
   templates, publish. Runs alongside the sheet for a week or two, then the
   sheet is retired and the Website Schedule import is switched off.
5. **Payroll exports + Master Metric hours from punches**, swap workflow,
   notifications.

## 6. Open questions

1. PTO policy document and the current accrual report (rates, caps, carryover, who is eligible today and their balances to seed the ledger).
2. Full-time test: a manager-set flag, or computed from scheduled/worked hours?
3. Auto clock-out grace window (minutes after shift end) and whether an agent can clock back in after an auto clock-out the same day.
4. Rounding rule for punches (none / nearest 5 / nearest 15 minutes).
5. Breaks: recorded, auto-deducted, or ignored? Which states do the agents work in (meal-break rules)?
6. Who approves time off: any manager, or the agent's team lead only?
7. Can agents clock in from anywhere (phone at home), or only from the office network? (The dashboard can record IP/device for every punch either way.)
8. Coverage target per hour (the number the strip should turn green at), and the shift presets you use most.
9. Should payroll get a CSV export first, or go straight into the commissions app?
