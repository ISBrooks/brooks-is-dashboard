"""One-time (idempotent) pull of ALL FieldRoutes customers into SQLite.

Needed by the form-lead conversion tracker to answer "was this person already
a customer when the form arrived?" — so we need every customer ever, with
dateAdded. Searches are chunked per office per year to stay under FR's
result caps; re-running only refreshes.
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
from isadash.normalize import normalize_phone
from isadash.store import connect, upsert_customers

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_customers")

FIRST_YEAR = 2000  # anything before FR existed just returns empty


def normalize_email(raw: str | None) -> str | None:
    if not raw or "@" not in raw:
        return None
    return raw.strip().lower() or None


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    this_year = datetime.now().year
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        all_ids: list[str] = []
        for oid in s.pestroutes_office_ids:
            for year in range(FIRST_YEAR, this_year + 1):
                filt = json.dumps({"operator": "BETWEEN",
                                   "value": [f"{year}-01-01", f"{year}-12-31 23:59:59"]})
                got = fr.request("customer", "search",
                                 {"officeIDs": oid, "dateAdded": filt})
                ids = got.get("customerIDs") or []
                if ids:
                    log.info("office %s year %s: %d customers", oid, year, len(ids))
                all_ids.extend(str(i) for i in ids)
        all_ids = list(dict.fromkeys(all_ids))
        log.info("TOTAL unique customer ids: %d", len(all_ids))

        done = 0
        for i in range(0, len(all_ids), 200):
            batch = all_ids[i:i + 200]
            got = fr.request("customer", "get",
                             {"customerIDs": FieldRoutesClient._id_param(batch)})
            rows = []
            for c in got.get("customers") or []:
                added = parse_fr_datetime_to_utc(c.get("dateAdded"))
                rows.append({
                    "customer_id": str(c.get("customerID")),
                    "office_id": str(c.get("officeID") or ""),
                    "date_added_utc": added.isoformat() if added else None,
                    "status": str(c.get("status")),
                    "phone1_e164": normalize_phone(c.get("phone1")),
                    "phone2_e164": normalize_phone(c.get("phone2")),
                    "email": normalize_email(c.get("email")),
                })
            upsert_customers(conn, rows)
            done += len(rows)
            if done % 10000 < 200:
                log.info("stored %d / %d customers", done, len(all_ids))
    log.info("DONE: %d customers stored", done)


if __name__ == "__main__":
    main()
