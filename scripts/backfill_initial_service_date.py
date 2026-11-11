"""Backfill subscriptions.initial_service_date (Pacific YYYY-MM-DD, the
scheduled/serviced date of the INITIAL appointment) for every Completed/
Pending subscription. Two phases: read each sub's initialAppointmentID, then
fetch those appointments' `date`. Idempotent — skips subs already filled.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    sub_ids = [r[0] for r in conn.execute(
        "SELECT subscription_id FROM subscriptions "
        "WHERE initial_status IN ('Completed','Pending') "
        "AND initial_service_date IS NULL")]
    print(f"{len(sub_ids)} counted subs need an initial service date")

    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        # phase 1: sub -> initialAppointmentID
        sub_appt: dict[str, str] = {}
        for batch in _chunks(sub_ids, 200):
            got = fr.request("subscription", "get",
                             {"subscriptionIDs": FieldRoutesClient._id_param(batch)})
            for r in got.get("subscriptions") or []:
                aid = str(r.get("initialAppointmentID") or "0")
                if aid != "0":
                    sub_appt[str(r.get("subscriptionID"))] = aid
        conn.executemany(
            "UPDATE subscriptions SET initial_appointment_id=? WHERE subscription_id=?",
            [(a, s_) for s_, a in sub_appt.items()])
        conn.commit()
        print(f"phase 1 done: {len(sub_appt)} appointment ids collected")

        # phase 2: appointment -> date
        appt_ids = list(dict.fromkeys(sub_appt.values()))
        appt_date: dict[str, str] = {}
        done = 0
        for batch in _chunks(appt_ids, 200):
            got = fr.request("appointment", "get",
                             {"appointmentIDs": FieldRoutesClient._id_param(batch)})
            for a in got.get("appointments") or []:
                d = a.get("date")
                if d and not str(d).startswith("0000"):
                    appt_date[str(a.get("appointmentID"))] = str(d)[:10]
            done += len(batch)
            if done % 20000 < 200:
                print(f"  appts {done}/{len(appt_ids)}")

        rows = [(appt_date.get(aid), sid) for sid, aid in sub_appt.items()
                if appt_date.get(aid)]
        conn.executemany(
            "UPDATE subscriptions SET initial_service_date=? WHERE subscription_id=?",
            rows)
        conn.commit()
        print(f"DONE: {len(rows)} initial service dates set")


if __name__ == "__main__":
    main()
