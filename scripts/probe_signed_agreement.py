"""READ-ONLY probe: compare the current contract_added proxy vs the REAL
signed signal (a /contract record with documentState=COMPLETED for that
subscription) on a sample of recent subscriptions.

Learned from the Podium sync app: /contract/search MUST be given officeIDs or
FR silently scopes to office 1 and returns nothing for everyone else.

  python scripts/probe_signed_agreement.py [days]
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    s = load_settings()
    conn = connect(s.db_path)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # sample: the kind of subs the Signed Doc % counts — recent, first service
    # done/scheduled, recurring (exclude one-time treatments)
    rows = conn.execute(
        """SELECT s.subscription_id, s.customer_id, c.office_id, s.contract_added
           FROM subscriptions s JOIN customers c ON c.customer_id = s.customer_id
           WHERE s.date_added_utc >= ?
             AND s.initial_status IN ('Completed','Pending')
             AND s.service_frequency != -1
             AND c.office_id IS NOT NULL AND c.office_id != ''""",
        (since,)).fetchall()
    conn.close()

    if not rows:
        print("no sample rows found"); return
    print(f"sample: {len(rows)} recent recurring subs (last {days}d)")

    # group customer ids by office for the office-scoped contract search
    cust_by_office: dict[str, set] = defaultdict(set)
    subs_by_cust: dict[str, list] = defaultdict(list)
    proxy_signed = 0
    for sub_id, cust_id, office_id, contract_added in rows:
        cust_by_office[str(office_id)].add(str(cust_id))
        subs_by_cust[str(cust_id)].append(str(sub_id))
        if contract_added:
            proxy_signed += 1

    fr = FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token)
    # subscriptionID -> True if it has a COMPLETED (truly signed) contract
    signed_subs: set = set()
    completed_seen = wip_seen = total_contracts = 0
    try:
        for office_id, cust_ids in cust_by_office.items():
            cust_list = list(cust_ids)
            contract_ids: list[str] = []
            for i in range(0, len(cust_list), 200):
                # FR convention (per the Podium app): list filters are JSON arrays,
                # and /contract/search REQUIRES officeIDs or it silently returns 0.
                got = fr.request("contract", "search", {
                    "officeIDs": json.dumps([int(office_id)]),
                    "customerIDs": json.dumps([int(x) for x in cust_list[i:i + 200]]),
                })
                contract_ids.extend(str(x) for x in (got.get("contractIDs") or []))
            contract_ids = list(dict.fromkeys(contract_ids))
            for i in range(0, len(contract_ids), 200):
                got = fr.request("contract", "get", {
                    "contractIDs": json.dumps([int(x) for x in contract_ids[i:i + 200]])})
                contracts = got.get("contracts")
                if isinstance(contracts, dict):
                    contracts = list(contracts.values())
                for c in contracts or []:
                    total_contracts += 1
                    state = c.get("documentState")
                    signed = state == "COMPLETED" and c.get("dateSigned") not in (
                        None, "", "0000-00-00 00:00:00", "0000-00-00")
                    if state == "COMPLETED":
                        completed_seen += 1
                    elif state == "WIP":
                        wip_seen += 1
                    if signed:
                        sub_id = str(c.get("subscriptionIDs") or c.get("subscriptionID") or "").strip()
                        if sub_id:
                            signed_subs.add(sub_id)
    finally:
        fr.close()

    real_signed = sum(1 for (sub_id, *_rest) in rows if str(sub_id) in signed_subs)
    n = len(rows)
    print(f"\ncontracts pulled: {total_contracts}  (COMPLETED={completed_seen}, WIP={wip_seen})")
    print("=" * 46)
    print(f"  PROXY  (contract_added present): {proxy_signed:>4}/{n} = {100*proxy_signed/n:5.1f}%")
    print(f"  REAL   (COMPLETED contract):     {real_signed:>4}/{n} = {100*real_signed/n:5.1f}%")
    print("=" * 46)


if __name__ == "__main__":
    main()
