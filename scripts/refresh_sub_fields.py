"""Re-read agreement_length / contract_value / service_type / status / source
etc. from PestRoutes onto already-stored subscriptions. Idempotent.

    python scripts/refresh_sub_fields.py                  # every subscription
    python scripts/refresh_sub_fields.py --since 2026-01-01   # sold on/after a day
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient, parse_fr_datetime_to_utc
from isadash.store import connect

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("refresh_sub_fields")


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None
    if since:
        ids = [r[0] for r in conn.execute(
            "SELECT subscription_id FROM subscriptions WHERE date_added_utc >= ?", (since,))]
    else:
        ids = [r[0] for r in conn.execute("SELECT subscription_id FROM subscriptions")]
    print(f"refreshing {len(ids)} subscriptions" + (f" sold since {since}" if since else ""))
    done = 0
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        for i in range(0, len(ids), 200):
            batch = ids[i:i + 200]
            got = fr.request("subscription", "get",
                             {"subscriptionIDs": FieldRoutesClient._id_param(batch)})
            rows = []
            for r in got.get("subscriptions") or []:
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
                cancelled = parse_fr_datetime_to_utc(r.get("dateCancelled"))
                def _int(v):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return None
                ca = r.get("contractAdded")
                ca = ca[:19] if ca and not str(ca).startswith("0000") else None
                rows.append((al, cv, r.get("serviceType"), r.get("initialStatusText"),
                             r.get("source"), ip,
                             1 if str(r.get("active")) == "1" else 0,
                             cancelled.isoformat() if cancelled else None,
                             _int(r.get("billingFrequency")), _int(r.get("frequency")), ca,
                             str(r.get("subscriptionID"))))
            conn.executemany(
                "UPDATE subscriptions SET agreement_length=?, contract_value=?, "
                "service_type=?, initial_status=?, source=?, initial_price=?, "
                "active=?, date_cancelled_utc=?, "
                "billing_frequency=?, service_frequency=?, contract_added=? "
                "WHERE subscription_id=?", rows)
            conn.commit()
            done += len(rows)
            if done % 10000 < 200:
                print(f"  {done}/{len(ids)}")
    print(f"DONE: {done} updated")


if __name__ == "__main__":
    main()
