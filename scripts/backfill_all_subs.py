"""One-time (idempotent) pull of ALL FieldRoutes subscriptions ever — the
form-conversion exclusion rule needs to know whether a contact had an active
subscription at form time, however old. Phones/emails come from the local
customers table (no extra API calls). Year-chunked searches per office.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient, parse_fr_datetime_to_utc
from isadash.store import connect, upsert_subscriptions

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    cust_phone = {}
    for r in conn.execute(
        "SELECT customer_id, phone1_e164, phone2_e164 FROM customers"):
        cust_phone[r[0]] = (r[1], r[2])
    this_year = datetime.now().year

    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        all_ids: list[str] = []
        for oid in s.pestroutes_office_ids:
            for year in range(2005, this_year + 1):
                filt = json.dumps({"operator": "BETWEEN",
                                   "value": [f"{year}-01-01", f"{year}-12-31 23:59:59"]})
                got = fr.request("subscription", "search",
                                 {"officeIDs": oid, "dateAdded": filt})
                all_ids.extend(str(i) for i in (got.get("subscriptionIDs") or []))
        all_ids = list(dict.fromkeys(all_ids))
        print(f"{len(all_ids)} total subs in FR — refreshing active/cancel + all fields")

        done = 0
        for i in range(0, len(all_ids), 200):
            got = fr.request("subscription", "get", {
                "subscriptionIDs": FieldRoutesClient._id_param(all_ids[i:i + 200])})
            rows = []
            for r in got.get("subscriptions") or []:
                added = parse_fr_datetime_to_utc(r.get("dateAdded"))
                cancelled = parse_fr_datetime_to_utc(r.get("dateCancelled"))
                ph = cust_phone.get(str(r.get("customerID")), (None, None))
                try:
                    al = int(r.get("agreementLength") or 0)
                except (TypeError, ValueError):
                    al = 0
                try:
                    cv = float(r.get("contractValue") or 0)
                except (TypeError, ValueError):
                    cv = 0.0
                try:
                    ip = float(r.get("initialServiceTotal") or 0)
                except (TypeError, ValueError):
                    ip = 0.0
                rows.append({
                    "subscription_id": str(r.get("subscriptionID")),
                    "customer_id": str(r.get("customerID")),
                    "office_id": str(r.get("officeID") or ""),
                    "date_added_utc": added.isoformat() if added else None,
                    "sold_by": str(r.get("soldBy") or "0"),
                    "sold_by_name": None,
                    "phone1_e164": ph[0], "phone2_e164": ph[1],
                    "agreement_length": al, "contract_value": cv,
                    "service_type": r.get("serviceType"),
                    "initial_status": r.get("initialStatusText"),
                    "source": r.get("source"),
                    "initial_price": ip,
                    "active": 1 if str(r.get("active")) == "1" else 0,
                    "date_cancelled_utc": cancelled.isoformat() if cancelled else None,
                })
            upsert_subscriptions(conn, rows)
            done += len(rows)
            if done % 20000 < 200:
                print(f"  {done}/{len(all_ids)}")
    print(f"DONE: {done} subscriptions refreshed (active/cancel populated)")


if __name__ == "__main__":
    main()
