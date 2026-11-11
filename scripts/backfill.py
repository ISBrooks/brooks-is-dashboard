"""Pull Genesys calls + FR subscriptions for a window, match conversions,
print the weekly/monthly/WTD report.

  python scripts/backfill.py --weeks 1          # sample: last full week + WTD
  python scripts/backfill.py --trailing-12mo    # full backfill
  python scripts/backfill.py --report-only      # re-aggregate from SQLite
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient, parse_fr_datetime_to_utc
from isadash.genesys import GenesysClient, extract_call
from isadash.match import build_agent_map, run_matcher
from isadash.normalize import normalize_phone
from isadash.periods import (PACIFIC, current_week_start_utc,
                             trailing_12mo_start_utc, week_start)
from isadash.report import aggregate, render_period, week_to_date_label, write_csv
from isadash.store import (connect, upsert_agent_map, upsert_calls,
                           upsert_subscriptions)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill")

OVERRIDES_PATH = Path(__file__).resolve().parents[1] / "config" / "name_overrides.json"


def contract_initial(r: dict) -> float:
    try:
        return float(r.get("initialServiceTotal") or 0)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, help="pull N full weeks back + week-to-date")
    ap.add_argument("--trailing-12mo", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    s = load_settings()
    conn = connect(s.db_path)
    now = datetime.now(timezone.utc)

    if not args.report_only:
        if args.trailing_12mo:
            start = trailing_12mo_start_utc(now)
        elif args.weeks:
            start = current_week_start_utc(now) - timedelta(weeks=args.weeks)
        else:
            ap.error("choose --weeks N, --trailing-12mo, or --report-only")
        log.info("Window: %s .. %s (Pacific weeks start %s)",
                 start, now, week_start(start))

        # --- Genesys ---
        with GenesysClient(s.genesys_region, s.genesys_client_id,
                           s.genesys_client_secret) as g:
            users = g.list_users()
            wrapups = g.list_wrapup_codes()
            convs = g.query_conversations_range(start, now, s.genesys_queue_ids)
        log.info("Genesys: %d users, %d wrapup codes, %d conversations",
                 len(users), len(wrapups), len(convs))
        call_rows = []
        for conv in convs:
            c = extract_call(conv)
            if not c:
                continue
            wrapup_name = wrapups.get(c["wrapup_id"]) if c["wrapup_id"] else None
            call_rows.append({
                "conversation_id": c["conversation_id"],
                "start_utc": c["start_utc"],
                "ani_e164": normalize_phone(c["ani"]),
                "dnis": c["dnis"],
                "agent_user_id": c["agent_user_id"],
                "agent_name": users.get(c["agent_user_id"]),
                "wrapup_id": c["wrapup_id"],
                "wrapup_name": wrapup_name,
                "dispo_group": s.dispo_group(wrapup_name),
            })
        upsert_calls(conn, call_rows)
        log.info("Stored %d agent-handled calls", len(call_rows))

        # --- FieldRoutes --- (extend window so late conversions still match)
        fr_until = now + timedelta(days=1)
        fr_since = start
        since_str = fr_since.astimezone(PACIFIC).strftime("%Y-%m-%d")
        until_str = fr_until.astimezone(PACIFIC).strftime("%Y-%m-%d")
        with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                               s.pestroutes_auth_token) as fr:
            subs = fr.pull_subscriptions(since_str, until_str, s.pestroutes_office_ids)
            cust_ids = sorted({str(r.get("customerID")) for r in subs if r.get("customerID")})
            customers = {str(c.get("customerID")): c for c in fr.get_customers(cust_ids)}
            # Employee map must cover sellers across ALL stored history, not
            # just this window — otherwise a short refresh shrinks each
            # agent's FR-id set and the matcher loses old conversions.
            emp_ids = {str(r.get("soldBy")) for r in subs
                       if r.get("soldBy") not in (None, 0, "0")}
            emp_ids.update(r[0] for r in conn.execute(
                "SELECT DISTINCT sold_by FROM subscriptions WHERE sold_by != '0'"))
            employees = fr.get_employees(sorted(emp_ids)) if emp_ids else {}
        sub_rows = []
        for r in subs:
            cust = customers.get(str(r.get("customerID")), {})
            added = parse_fr_datetime_to_utc(r.get("dateAdded"))
            try:
                agreement = int(r.get("agreementLength") or 0)
            except (TypeError, ValueError):
                agreement = 0
            try:
                contract = float(r.get("contractValue") or 0)
            except (TypeError, ValueError):
                contract = 0.0
            sub_rows.append({
                "subscription_id": str(r.get("subscriptionID")),
                "customer_id": str(r.get("customerID")),
                "office_id": str(r.get("officeID") or ""),
                "date_added_utc": added.isoformat() if added else None,
                "sold_by": str(r.get("soldBy") or "0"),
                "sold_by_name": employees.get(str(r.get("soldBy"))),
                "phone1_e164": normalize_phone(cust.get("phone1")),
                "phone2_e164": normalize_phone(cust.get("phone2")),
                "agreement_length": agreement,
                "contract_value": contract,
                "service_type": r.get("serviceType"),
                "initial_status": r.get("initialStatusText"),
                "source": r.get("source"),
                "initial_price": contract_initial(r),
                "active": 1 if str(r.get("active")) == "1" else 0,
                "date_cancelled_utc": (parse_fr_datetime_to_utc(r.get("dateCancelled")).isoformat()
                                       if parse_fr_datetime_to_utc(r.get("dateCancelled")) else None),
            })
        upsert_subscriptions(conn, sub_rows)
        log.info("Stored %d subscriptions (%d sold-by employees)",
                 len(sub_rows), len(employees))

        # --- agent identity map ---
        overrides = {}
        if OVERRIDES_PATH.exists():
            overrides = json.loads(OVERRIDES_PATH.read_text())
        upsert_agent_map(conn, build_agent_map(users, employees, s.isa_roster, overrides))
        unmatched = [r["genesys_name"] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE on_roster=1 AND fr_employee_id IS NULL")]
        if unmatched:
            log.warning("Roster agents with NO FieldRoutes employee match "
                        "(fix via config/name_overrides.json): %s", unmatched)

        run_matcher(conn, s.conversion_window_days)

    # --- report ---
    agg = aggregate(conn)
    wtd = week_to_date_label(now)
    print("\n################ WEEK TO DATE ################")
    if wtd in agg.weekly:
        print(render_period(wtd + " (WTD)", agg.weekly[wtd]))
    else:
        print("(no calls yet this week)")
    print("\n################ WEEKLY (Sun-Sat) ################")
    for label in sorted(agg.weekly):
        if label != wtd:
            print(render_period(label, agg.weekly[label]))
    print("\n################ MONTHLY ################")
    for label in sorted(agg.monthly):
        print(render_period(label, agg.monthly[label]))
    paths = write_csv(agg, Path(__file__).resolve().parents[1] / "data")
    print("\nCSV exports:", *paths, sep="\n  ")


if __name__ == "__main__":
    main()
