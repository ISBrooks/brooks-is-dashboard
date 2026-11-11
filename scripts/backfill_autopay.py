"""Backfill customers.autopay (0/1) from FieldRoutes customer.aPay for every
customer who has a Completed/Pending subscription in the last N days — the set
that can appear in any agent scorecard. aPay != 'No' means on autopay.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect

DAYS = 400


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    cut = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT customer_id FROM subscriptions "
        "WHERE date_added_utc >= ? AND initial_status IN ('Completed','Pending')",
        (cut,))]
    print(f"{len(ids)} customers to refresh autopay for (last {DAYS}d sales)")
    done = 0
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        for i in range(0, len(ids), 200):
            got = fr.request("customer", "get",
                             {"customerIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
            rows = []
            for c in got.get("customers") or []:
                apay = str(c.get("aPay") or "No").strip().lower()
                # "has a payment method on file" (PestRoutes column P) — autopay
                # enrollment NOT required. autoPayPaymentProfileID catches ACH +
                # autopay cards; mostRecentCreditCardLastFour catches cards on file
                # that aren't on autopay (profile-ID alone missed those → undercount).
                card = 1 if (
                    str(c.get("autoPayPaymentProfileID") or "0").strip() not in ("", "0")
                    or (c.get("mostRecentCreditCardLastFour") or "").strip()
                ) else 0
                rows.append((0 if apay in ("no", "", "0") else 1, card,
                             str(c.get("customerID"))))
            conn.executemany(
                "UPDATE customers SET autopay=?, card_on_file=? WHERE customer_id=?", rows)
            conn.commit()
            done += len(rows)
            if done % 10000 < 200:
                print(f"  {done}/{len(ids)}")
    print(f"DONE: {done} customers' autopay + card_on_file refreshed")


if __name__ == "__main__":
    main()
