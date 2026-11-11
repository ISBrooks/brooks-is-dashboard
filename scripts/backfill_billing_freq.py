"""Backfill billing_frequency / service_frequency for subscriptions missing
them. These feed Monthly Billing % on Team Metrics; only a long-ago one-off
ever wrote them, so most rows are NULL (the nightly delta now keeps them
fresh going forward — this catches everything already stored). Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect

START = "2025-06-01"  # dashboard coverage starts 2025-06-29


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    ids = [r[0] for r in conn.execute(
        "SELECT subscription_id FROM subscriptions "
        "WHERE (billing_frequency IS NULL OR service_frequency IS NULL) "
        "AND date_added_utc >= ?", (START,))]
    print(f"{len(ids)} subscriptions missing billing/service frequency (since {START})")
    done = 0
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        for i in range(0, len(ids), 200):
            got = fr.request("subscription", "get", {
                "subscriptionIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
            rows = [(_int(r.get("billingFrequency")), _int(r.get("frequency")),
                     str(r.get("subscriptionID")))
                    for r in got.get("subscriptions") or []]
            conn.executemany(
                "UPDATE subscriptions SET billing_frequency=?, service_frequency=? "
                "WHERE subscription_id=?", rows)
            conn.commit()
            done += len(rows)
            if done % 10000 < 200:
                print(f"  {done}/{len(ids)}")
    print(f"DONE: {done} subscriptions updated")


if __name__ == "__main__":
    main()
