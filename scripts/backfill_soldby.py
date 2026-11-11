"""Re-fetch soldBy (+ source) for recent subscriptions and correct stale
attribution. PestRoutes lets a sale be reassigned to a different rep after the
fact; our nightly sync now keeps this current, but existing rows can be stale.
Scope: subscriptions added OR first-serviced since START (covers every window
the IS#'s / Team-Metrics / Home-YTD views can show).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect

START = "2026-01-01"  # year-to-date and then some


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    emp = {r[0]: r[1] for r in conn.execute(
        "SELECT fr_employee_id, name FROM employees")}
    ids = [r[0] for r in conn.execute(
        "SELECT subscription_id FROM subscriptions "
        "WHERE date_added_utc >= ? "
        "   OR (initial_service_date IS NOT NULL AND initial_service_date >= ?)",
        (START, START))]
    print(f"{len(ids)} subscriptions to re-check soldBy for (since {START})")
    changed = 0
    done = 0
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        for i in range(0, len(ids), 200):
            batch = ids[i:i + 200]
            got = fr.request("subscription", "get",
                             {"subscriptionIDs": FieldRoutesClient._id_param(batch)})
            rows = []
            for r in got.get("subscriptions") or []:
                sid = str(r.get("subscriptionID"))
                sb = str(r.get("soldBy") or "")
                if sb in ("", "0"):
                    continue  # never wipe a good attribution with a blank
                old = conn.execute(
                    "SELECT sold_by FROM subscriptions WHERE subscription_id=?",
                    (sid,)).fetchone()
                if old and str(old[0]) != sb:
                    changed += 1
                rows.append((sb, emp.get(sb), r.get("source"), sid))
            conn.executemany(
                "UPDATE subscriptions SET "
                "sold_by=?, sold_by_name=COALESCE(?, sold_by_name), source=? "
                "WHERE subscription_id=?", rows)
            conn.commit()
            done += len(rows)
            if done % 10000 < 200:
                print(f"  {done}/{len(ids)}  ({changed} reassigned so far)")
    print(f"DONE: {done} re-checked, {changed} had a changed sold_by")


if __name__ == "__main__":
    main()
