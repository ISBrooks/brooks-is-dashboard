"""Backfill subscriptions.signed_agreement from the REAL FieldRoutes contract
signal (documentState=COMPLETED for that subscription), replacing the
contract_added proxy that counted unsigned WIP drafts as signed.

  python scripts/backfill_signed_agreement.py [days=400]

Scopes to Completed/Pending recurring subs sold within `days` (the universe the
Signed Doc % ever counts). Sets 1 or 0; leaves older/out-of-scope subs as-is.
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    s = load_settings()
    conn = connect(s.db_path)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = conn.execute(
        """SELECT s.subscription_id, s.customer_id, c.office_id
           FROM subscriptions s JOIN customers c ON c.customer_id = s.customer_id
           WHERE s.date_added_utc >= ?
             AND s.initial_status IN ('Completed','Pending')
             AND c.office_id IS NOT NULL AND c.office_id != ''""",
        (since,)).fetchall()
    if not rows:
        print("no subs in scope"); conn.close(); return

    cust_by_office: dict[str, list] = defaultdict(list)
    for _sub_id, cust_id, office_id in rows:
        cust_by_office[str(office_id)].append(str(cust_id))
    print(f"resolving signed status for {len(rows)} subs across "
          f"{len(cust_by_office)} offices…")

    fr = FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token)
    try:
        signed = fr.signed_subscription_ids(cust_by_office)
    finally:
        fr.close()

    updates = [(1 if str(sub_id) in signed else 0, sub_id)
               for (sub_id, _c, _o) in rows]
    conn.executemany(
        "UPDATE subscriptions SET signed_agreement=? WHERE subscription_id=?",
        updates)
    conn.commit()
    conn.close()

    n = len(updates)
    signed_n = sum(u[0] for u in updates)
    print(f"done: {signed_n}/{n} = {100*signed_n/n:.1f}% signed (real). "
          f"contract-signed set size: {len(signed)}")


if __name__ == "__main__":
    main()
