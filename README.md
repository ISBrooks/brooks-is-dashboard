# Brooks ISA Dashboard

Inside-sales-agent performance dashboard for Brooks Pest Control, built on
the data access proven in two sibling projects:

- **Form_Lead_DeDupe** — Genesys Cloud (calls, queues) + FieldRoutes
  (subscriptions, sources) + Meta/Google Ads/Gmail
- **Podium_Pestroutes_Sync_Project** — FieldRoutes (customers) + Podium

## Phase 1 metrics (per ISA; weekly Sun–Sat Pacific, monthly, WTD)

A **lead** is a UNIQUE caller (ANI) the agent dispositioned
closed/followup/unqualified in the period. A **qualified lead** is one ever
dispositioned Closed or Follow Up (or who converted). A **conversion** is a
lead whose phone matches a FR customer (phone1/phone2) holding a
subscription with `soldBy` = any of that agent's FR employee IDs and
`dateAdded` within `CONVERSION_WINDOW_DAYS` (14) after the call. Each
subscription credits one call (the earliest qualifying one).

Three conversion rates shown side by side:
- **Cv/Q%** = conversions / qualified leads — the headline (~75% team-wide)
- **Cv/L%** = conversions / all unique leads incl. unqualified (~38%)
- **Cv/C%** = conversions / total calls taken (~29%)

Disposition mix (Cl%/FU%/Uq%) stays per-CALL: share of the agent's
sales-dispositioned calls wrapped each way.

Verified against live data 2026-07-06: 91.3% of leads dispositioned Closed
show a matching subscription sold by that same agent.

## Running it

```bash
# refresh data (pull new calls/subs since backfill, re-match, re-export CSVs)
python scripts/backfill.py --weeks 3        # cheap incremental catch-up
python scripts/backfill.py --trailing-12mo  # full rebuild (idempotent)

# dashboard
python scripts/serve.py                     # -> http://127.0.0.1:8377
```

The web app reads `data/isadash.sqlite` and auto-picks up new data on the
next page load after any backfill run (mtime-based cache).

## Form-lead team conversion (Form Leads tab)

Reads normalized form touches (gmail/meta/lsa) from the sourcing app's live
DB (`FORMLEADS_DB` in .env → Form_Lead_DeDupe/attribution_live.sqlite,
refreshed by its nightly ingest). Touches sharing a phone OR email are one
CONTACT (union-find); touches within `FORM_DEDUP_WINDOW_HOURS` (72) are one
LEAD EVENT. Events matching a FR customer that existed BEFORE the form are
excluded ("Already Cust"). Conversion = a FR subscription on that
phone/email within `FORM_CONVERSION_WINDOW_DAYS` (30). Requires the full
customers table: `python scripts/backfill_customers.py` (207k rows, ~30 min,
idempotent). Coverage starts 2026-01-01 (Meta from 2026-03-12).

## Audit sheet (Audit tab)

`python scripts/import_audit.py --file <csv/xlsx>` or
`--url <Google Sheets share link>` loads the audit team's per-day per-agent
qualified/sales counts into `audit_daily`. The tab compares human-audited
qualified calls + sales against system dispositions + matched conversions,
per agent per week — Dispo Acc% under 80 flags in red (agent likely
under-dispositioning qualified calls).

## Data plumbing (all verified live 2026-07-06)

| Need | Source | Endpoint |
|------|--------|----------|
| Calls + agent + wrap-up per call | Genesys | `POST /api/v2/analytics/conversations/details/query` (queue filter; agent participant has `userId`, segments carry `wrapUpCode`) |
| Agent id → name | Genesys | `GET /api/v2/users` (142 users) |
| Wrap-up code id → label | Genesys | `GET /api/v2/routing/wrapupcodes` (68 codes — snapshot in `docs/genesys_wrapup_codes.md`) |
| Sold subscriptions + rep | FieldRoutes | `subscription/search` per office (`dateAdded` filter, **must pass officeIDs**) + `subscription/get` → `soldBy` employee ID |
| Rep id → name | FieldRoutes | `employee/get` (`fname`/`lname`) |
| Customer phones for ANI join | FieldRoutes | `customer/get` → `phone1`/`phone2` |

Identity join between systems: Genesys user display name ↔ FR employee
`fname lname`, seeded by the `ISA_ROSTER` list in `.env`. **FieldRoutes
keeps a separate employee record per office for the same person** — an
agent maps to a SET of FR employee IDs (Aaron Darrington alone has 4+).
Spelling mismatches go in `config/name_overrides.json`.
`soldBy` is FR's Sales Rep field; `soldBy2`/`soldBy3` are never used
(0 of 400 checked) and the `lead*` fields are empty in this tenant.

## Reused patterns (don't re-derive)

- Genesys analytics queries cap at 31 days per interval — chunk long ranges
  (see `Form_Lead_DeDupe/src/leadsource/readers/genesys.py`).
- FR `subscription/search` is per-office; `get` is global. A `get` with
  exactly one ID returns nothing — pad to two.
- FR returns datetimes in office-local (Pacific) time, not UTC.
- FR rate limit 900 reads/min shared with the nightly dedupe job and the
  5-min Podium sync — pace the dashboard's pulls.
