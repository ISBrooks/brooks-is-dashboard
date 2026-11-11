"""Initial-service revenue report (READ-ONLY).

Answers: how much first-service ("initial") revenue did inside sales produce
per month, by sale month and by the month the first service happened, and
(optionally) how much of it has actually been PAID so far per FieldRoutes.

    .venv/bin/python scripts/report_initial_revenue.py
    .venv/bin/python scripts/report_initial_revenue.py --fr --from 2026-09-01 --to 2026-10-31

Population = the Revenue tab rule: every office-staff seller (employees
emp_type '0'), Completed/Pending, contract_value > 0, renewal sources out,
sale date in Pacific. "Initial revenue" = initial_price (FieldRoutes
initialServiceTotal) of subscriptions whose first service is Completed.

--fr  looks up each Completed first-service appointment's invoice (ticket) in
      FieldRoutes and reports total vs balance -> paid so far. Read-only API
      calls; nothing is written anywhere. Only aggregates are printed.
--pay FROM:TO  (repeatable) THE GOAL METRIC: dollars actually applied to
      initial-service invoices, bucketed by the date the payment was applied
      (a Sept 30 service paid Oct 3 counts in October). Looks up the initial
      invoices of first services dated FROM-<lookback> .. TO, then every
      payment applied to them. Write-offs and credit memos are excluded and
      reported separately. Read-only.
--lookback N  days of first services before each --pay FROM to include (90).
--pop office|mapped  office = Revenue-tab rule (default); mapped = every
      FieldRoutes id in agent_map (fallback when the employees table is thin).

Opens the SQLite DB read-only; prints to stdout only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path("/home/isdash/brooks-is-dash")
if not APP.exists():  # local checkout
    APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))

from isadash.config import load_settings  # noqa: E402

PACIFIC = ZoneInfo("America/Los_Angeles")
RENEWAL_SOURCES = ("ZOLD - Renewal", "Zoldz - Renewal", "Rate Raise Renewal",
                   "Renewal - In Contract", "Renewal - No Contract",
                   "Renewal - Out of Contract")
FIRST_MONTH = "2024-09"


def money(v: float) -> str:
    return f"${v:,.0f}"


def pac_date(iso_utc: str) -> date:
    return datetime.fromisoformat(iso_utc).astimezone(PACIFIC).date()


def week_sunday(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def seller_ids(conn: sqlite3.Connection, pop: str) -> set[str]:
    ids: set[str] = set()
    if pop == "office":
        for (fid,) in conn.execute(
                "SELECT fr_employee_id FROM employees WHERE emp_type = '0' AND name IS NOT NULL"):
            ids.add(str(fid))
    else:
        for (f,) in conn.execute(
                "SELECT fr_employee_id FROM agent_map "
                "WHERE fr_employee_id IS NOT NULL AND fr_employee_id != ''"):
            ids.update(x.strip() for x in f.split(",") if x.strip())
    return ids


def load_rows(conn: sqlite3.Connection, sellers: set[str]) -> list[dict]:
    """Counted subscriptions for the population, with Pacific sale date."""
    rq = ",".join("?" * len(RENEWAL_SOURCES))
    out = []
    for r in conn.execute(
            f"""SELECT subscription_id, sold_by, date_added_utc, contract_value,
                       initial_status, initial_price, initial_service_date,
                       initial_appointment_id, office_id
                FROM subscriptions
                WHERE date_added_utc >= '2024-08-25'
                  AND initial_status IN ('Completed', 'Pending')
                  AND contract_value IS NOT NULL AND contract_value > 0
                  AND (source IS NULL OR source NOT IN ({rq}))""", RENEWAL_SOURCES):
        if str(r["sold_by"]) not in sellers:
            continue
        d = dict(r)
        d["sale_date"] = pac_date(r["date_added_utc"])
        d["initial_price"] = float(r["initial_price"] or 0.0)
        d["contract_value"] = float(r["contract_value"] or 0.0)
        out.append(d)
    return out


def monthly_tables(rows: list[dict], today: date) -> None:
    last_month = (today.replace(day=1) + timedelta(days=40)).strftime("%Y-%m")  # next month
    by_sale: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_svc: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        m = r["sale_date"].strftime("%Y-%m")
        if m >= FIRST_MONTH:
            b = by_sale[m]
            b["n"] += 1
            b["cv"] += r["contract_value"]
            if r["initial_status"] == "Completed":
                b["cn"] += 1
                b["ci"] += r["initial_price"]
            else:
                b["pn"] += 1
                b["pi"] += r["initial_price"]
        sd = r["initial_service_date"]
        if sd and FIRST_MONTH <= sd[:7] <= last_month:
            b = by_svc[sd[:7]]
            if r["initial_status"] == "Completed":
                b["cn"] += 1
                b["ci"] += r["initial_price"]
            else:
                b["pn"] += 1
                b["pi"] += r["initial_price"]

    print("\n== BY SALE MONTH (Pacific) ==")
    print(f"{'month':8} {'sales':>6} {'contract $':>12} {'done n':>7} {'initial $ done':>15} "
          f"{'avg init':>9} {'pend n':>7} {'initial $ pend':>15}")
    for m in sorted(by_sale):
        b = by_sale[m]
        avg = b["ci"] / b["cn"] if b["cn"] else 0
        print(f"{m:8} {int(b['n']):6} {money(b['cv']):>12} {int(b['cn']):7} {money(b['ci']):>15} "
              f"{avg:9.2f} {int(b['pn']):7} {money(b['pi']):>15}")

    print("\n== BY FIRST-SERVICE MONTH (appointment date) ==")
    print(f"{'month':8} {'done n':>7} {'initial $ done':>15} {'pend n':>7} {'initial $ booked':>17}")
    for m in sorted(by_svc):
        b = by_svc[m]
        print(f"{m:8} {int(b['cn']):7} {money(b['ci']):>15} {int(b['pn']):7} {money(b['pi']):>17}")


def recent_weeks(rows: list[dict], today: date, since: date) -> None:
    wk: dict[date, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r["sale_date"] < since:
            continue
        b = wk[week_sunday(r["sale_date"])]
        b["n"] += 1
        b["cv"] += r["contract_value"]
        if r["initial_status"] == "Completed":
            b["cn"] += 1
            b["ci"] += r["initial_price"]
        else:
            b["pn"] += 1
            b["pi"] += r["initial_price"]
    print(f"\n== SALES BY WEEK (Sun-Sat, Pacific) since {since} ==")
    print(f"{'week of':10} {'sales':>6} {'contract $':>12} {'done n':>7} {'initial $ done':>15} "
          f"{'pend n':>7} {'initial $ pend':>15}")
    for w in sorted(wk):
        b = wk[w]
        tag = " (partial)" if w + timedelta(days=6) >= today else ""
        print(f"{w.isoformat():10} {int(b['n']):6} {money(b['cv']):>12} {int(b['cn']):7} "
              f"{money(b['ci']):>15} {int(b['pn']):7} {money(b['pi']):>15}{tag}")


def week_of_month(rows: list[dict], months: list[str]) -> None:
    print("\n== OCT / NOV SALES BY WEEK-OF-MONTH (days 1-7, 8-14, 15-21, 22-28, 29+) ==")
    print(f"{'month':8} " + " ".join(f"{'wk' + str(i):>14}" for i in range(1, 6)))
    for m in months:
        cnt = [0] * 5
        ini = [0.0] * 5
        for r in rows:
            if r["sale_date"].strftime("%Y-%m") != m:
                continue
            i = min((r["sale_date"].day - 1) // 7, 4)
            cnt[i] += 1
            if r["initial_status"] == "Completed":
                ini[i] += r["initial_price"]
        print(f"{m:8} " + " ".join(f"{cnt[i]:5} {money(ini[i]):>8}" for i in range(5)))


def november_pipeline(rows: list[dict], today: date) -> None:
    nov0, dec0 = date(today.year, 11, 1), date(today.year, 12, 1)
    n = i = 0
    sold_oct = 0
    for r in rows:
        sd = r["initial_service_date"]
        if not sd:
            continue
        d = date.fromisoformat(sd[:10])
        if nov0 <= d < dec0:
            n += 1
            i += r["initial_price"]
            if r["sale_date"] < nov0:
                sold_oct += 1
    print(f"\n== ALREADY ON THE BOOKS FOR {nov0:%B %Y} (first service dated in the month) ==")
    print(f"subscriptions: {n}   initial $: {money(i)}   of which sold before {nov0}: {sold_oct}")


def fr_paid(rows: list[dict], start: date, end: date, office_ids: list[str]) -> None:
    """For Completed first services dated start..end, fetch the appointment's
    ticket from FieldRoutes and report billed vs paid. Read-only."""
    from isadash.fieldroutes import FieldRoutesClient
    s = load_settings()
    picked = [r for r in rows
              if r["initial_status"] == "Completed" and r["initial_service_date"]
              and start <= date.fromisoformat(r["initial_service_date"][:10]) <= end
              and str(r["initial_appointment_id"] or "0") != "0"]
    print(f"\n== FIELDROUTES PAID-TO-DATE on first services {start}..{end} ==")
    print(f"completed first services with an appointment id: {len(picked)}")
    if not picked:
        return
    appt_ids = list(dict.fromkeys(str(r["initial_appointment_id"]) for r in picked))
    appt_ticket: dict[str, str] = {}
    appt_office: dict[str, str] = {}
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        for i in range(0, len(appt_ids), 200):
            got = fr.request("appointment", "get",
                             {"appointmentIDs": FieldRoutesClient._id_param(appt_ids[i:i + 200])})
            for ap in got.get("appointments") or []:
                tid = str(ap.get("ticketID") or "0")
                aid = str(ap.get("appointmentID"))
                appt_office[aid] = str(ap.get("officeID") or "")
                if tid != "0":
                    appt_ticket[aid] = tid
        print(f"appointments found: {len(appt_office)}   with a ticket id on the appointment: {len(appt_ticket)}")
        missing = [a for a in appt_ids if a not in appt_ticket and a in appt_office]
        if missing:
            # fallback: search tickets by appointment id, per office (search scopes to office 1 otherwise)
            by_office: dict[str, list[str]] = defaultdict(list)
            for a in missing:
                by_office[appt_office[a]].append(a)
            found = 0
            for off, ids in by_office.items():
                for i in range(0, len(ids), 200):
                    try:
                        got = fr.request("ticket", "search", {
                            "officeIDs": json.dumps([off] if off else office_ids),
                            "appointmentIDs": json.dumps(ids[i:i + 200])})
                    except Exception as e:  # noqa: BLE001
                        print(f"ticket/search fallback failed ({type(e).__name__}); skipping")
                        got = {}
                    tids = got.get("ticketIDs") or []
                    if tids:
                        tg = fr.request("ticket", "get",
                                        {"ticketIDs": FieldRoutesClient._id_param([str(t) for t in tids])})
                        for t in tg.get("tickets") or []:
                            a = str(t.get("appointmentID") or "0")
                            if a in appt_office and a not in appt_ticket:
                                appt_ticket[a] = str(t.get("ticketID"))
                                found += 1
            print(f"tickets found via search fallback: {found}")
        tickets: dict[str, dict] = {}
        tids = list(dict.fromkeys(appt_ticket.values()))
        for i in range(0, len(tids), 200):
            got = fr.request("ticket", "get",
                             {"ticketIDs": FieldRoutesClient._id_param(tids[i:i + 200])})
            for t in got.get("tickets") or []:
                tickets[str(t.get("ticketID"))] = t

    def f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    agg: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in picked:
        m = r["initial_service_date"][:7]
        b = agg[m]
        b["n"] += 1
        b["quoted"] += r["initial_price"]
        t = tickets.get(appt_ticket.get(str(r["initial_appointment_id"]), ""))
        if not t:
            b["no_ticket"] += 1
            continue
        total, bal = f(t.get("total")), f(t.get("balance"))
        b["tn"] += 1
        b["subtotal"] += f(t.get("subtotal"))
        b["total"] += total
        b["paid"] += max(0.0, total - bal)
        if bal <= 0.005:
            b["paid_full"] += 1
        elif total - bal <= 0.005:
            b["unpaid"] += 1
    print(f"\n{'svc month':9} {'done':>5} {'quoted $':>10} {'w/ticket':>8} {'billed $':>10} {'paid $':>10} "
          f"{'paid %':>6} {'paid in full':>12} {'unpaid':>6} {'no ticket':>9}")
    for m in sorted(agg):
        b = agg[m]
        pct = 100 * b["paid"] / b["total"] if b["total"] else 0
        print(f"{m:9} {int(b['n']):5} {money(b['quoted']):>10} {int(b['tn']):8} {money(b['total']):>10} "
              f"{money(b['paid']):>10} {pct:5.1f}% {int(b['paid_full']):12} {int(b['unpaid']):6} "
              f"{int(b['no_ticket']):9}")
    print("billed $ = ticket total (incl. tax); paid $ = total - balance; quoted $ = initialServiceTotal on the subscription")


def _initial_tickets(fr, picked: list[dict]) -> dict[str, tuple[str, str]]:
    """appointment id -> (ticket id, office id) for the picked subscriptions."""
    from isadash.fieldroutes import FieldRoutesClient
    appt_ids = list(dict.fromkeys(str(r["initial_appointment_id"]) for r in picked))
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(appt_ids), 200):
        got = fr.request("appointment", "get",
                         {"appointmentIDs": FieldRoutesClient._id_param(appt_ids[i:i + 200])})
        for ap in got.get("appointments") or []:
            tid = str(ap.get("ticketID") or "0")
            if tid != "0":
                out[str(ap.get("appointmentID"))] = (tid, str(ap.get("officeID") or ""))
    return out


def fr_payments(rows: list[dict], ranges: list[tuple[date, date]], lookback: int) -> None:
    """Dollars applied to initial-service invoices, by the month/week the
    payment was APPLIED. Read-only."""
    from isadash.fieldroutes import FieldRoutesClient
    s = load_settings()

    def f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    svc_windows = [(a - timedelta(days=lookback), b) for a, b in ranges]
    picked = []
    for r in rows:
        sd = r["initial_service_date"]
        if r["initial_status"] != "Completed" or not sd:
            continue
        if str(r["initial_appointment_id"] or "0") == "0":
            continue
        d = date.fromisoformat(sd[:10])
        if any(a <= d <= b for a, b in svc_windows):
            picked.append(r)
    print("\n== PAYMENTS APPLIED TO INITIAL-SERVICE INVOICES, BY DATE APPLIED ==")
    print("payment windows: " + ", ".join(f"{a}..{b}" for a, b in ranges)
          + f"   (first services from {lookback} days before each window)")
    print(f"completed first services in scope: {len(picked)}")
    if not picked:
        return
    with FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key,
                           s.pestroutes_auth_token) as fr:
        appt_ticket = _initial_tickets(fr, picked)
        svc_of: dict[str, str] = {}   # ticket id -> first-service date
        for r in picked:
            t = appt_ticket.get(str(r["initial_appointment_id"]))
            if t:
                svc_of[t[0]] = r["initial_service_date"][:10]
        print(f"initial invoices found: {len(svc_of)}")
        by_office: dict[str, list[str]] = defaultdict(list)
        for tid, off in appt_ticket.values():
            by_office[off].append(tid)
        ap_ids: list[str] = []
        for off, tids in by_office.items():
            for i in range(0, len(tids), 100):
                got = fr.request("appliedPayment", "search", {
                    "officeIDs": json.dumps([off] if off else list(s.pestroutes_office_ids)),
                    "ticketIDs": json.dumps(tids[i:i + 100])})
                ap_ids.extend(str(x) for x in got.get("appliedPaymentIDs") or [])
        ap_ids = list(dict.fromkeys(ap_ids))
        apps: list[dict] = []
        for i in range(0, len(ap_ids), 200):
            got = fr.request("appliedPayment", "get",
                             {"appliedPaymentIDs": FieldRoutesClient._id_param(ap_ids[i:i + 200])})
            apps.extend(got.get("appliedPayments") or [])
        print(f"payment applications found: {len(apps)}")
        pay_ids = list(dict.fromkeys(str(a.get("paymentID")) for a in apps if a.get("paymentID")))
        pay_kind: dict[str, str] = {}   # payment id -> real | writeoff | creditmemo | failed
        for i in range(0, len(pay_ids), 200):
            got = fr.request("payment", "get",
                             {"paymentIDs": FieldRoutesClient._id_param(pay_ids[i:i + 200])})
            for p in got.get("payments") or []:
                pid = str(p.get("paymentID"))
                if str(p.get("writeOff")) == "1":
                    pay_kind[pid] = "writeoff"
                elif str(p.get("creditMemo")) == "1":
                    pay_kind[pid] = "creditmemo"
                elif str(p.get("status")) != "1":
                    pay_kind[pid] = "failed"
                else:
                    pay_kind[pid] = "real"

    month: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    week: dict[date, dict] = defaultdict(lambda: defaultdict(float))
    wom: dict[str, list] = defaultdict(lambda: [0.0] * 5)
    for a in apps:
        da = str(a.get("dateApplied") or "")[:10]
        if not da or da.startswith("0000"):
            continue
        d = date.fromisoformat(da)
        if not any(x <= d <= y for x, y in ranges):
            continue
        amt = f(a.get("appliedAmount"))
        kind = pay_kind.get(str(a.get("paymentID")), "real")
        m = da[:7]
        b = month[m]
        if kind != "real":
            b[kind] += amt
            continue
        b["n"] += 1
        b["amt"] += amt
        b["tax"] += f(a.get("taxCollected"))
        if amt < 0:
            b["neg"] += amt
        svc = svc_of.get(str(a.get("ticketID")), "")
        if svc and svc[:7] < m:
            b["prior"] += amt
        week[week_sunday(d)]["amt"] += amt
        week[week_sunday(d)]["n"] += 1
        wom[m][min((d.day - 1) // 7, 4)] += amt

    print(f"\n{'pay month':9} {'applied $':>11} {'applications':>12} {'incl. tax $':>11} "
          f"{'refunds/reversals $':>19} {'from earlier-month svc $':>24} {'write-off $':>11} {'credit memo $':>13}")
    for m in sorted(month):
        b = month[m]
        print(f"{m:9} {money(b['amt']):>11} {int(b['n']):12} {money(b['tax']):>11} "
              f"{money(b['neg']):>19} {money(b['prior']):>24} {money(b['writeoff']):>11} {money(b['creditmemo']):>13}")
    print("applied $ = net successful payments applied to initial invoices (refunds/reversals already netted); "
          "write-offs and credit memos are NOT in applied $")

    print("\n== APPLIED $ BY WEEK APPLIED (Sun-Sat) ==")
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    for w in sorted(week):
        b = week[w]
        tag = " (partial)" if w + timedelta(days=6) >= today else ""
        print(f"{w.isoformat():10} {money(b['amt']):>11} {int(b['n']):6}{tag}")

    print("\n== APPLIED $ BY WEEK-OF-MONTH (days 1-7, 8-14, 15-21, 22-28, 29+) ==")
    for m in sorted(wom):
        print(f"{m:8} " + " ".join(f"{money(v):>10}" for v in wom[m]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fr", action="store_true", help="also query FieldRoutes for paid-to-date")
    ap.add_argument("--from", dest="start", default="2026-09-01")
    ap.add_argument("--to", dest="end", default="2026-10-31")
    ap.add_argument("--pop", choices=("office", "mapped"), default="office")
    ap.add_argument("--pay", action="append", default=[], metavar="FROM:TO",
                    help="payment-date window(s) for the applied-payments report")
    ap.add_argument("--lookback", type=int, default=90)
    a = ap.parse_args()

    s = load_settings()
    db = Path(s.db_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    try:
        sellers = seller_ids(conn, a.pop)
        rows = load_rows(conn, sellers)
        last_sale = max((r["sale_date"] for r in rows), default=None)
        print(f"db: {db}   population: {a.pop} ({len(sellers)} seller ids)   "
              f"counted subscriptions since 2024-08-25: {len(rows)}   last sale: {last_sale}   today: {today}")
        monthly_tables(rows, today)
        recent_weeks(rows, today, date(2026, 9, 1))
        week_of_month(rows, ["2024-10", "2024-11", "2025-10", "2025-11", "2026-10"])
        november_pipeline(rows, today)
    finally:
        conn.close()
    if a.fr:
        fr_paid(rows, date.fromisoformat(a.start), date.fromisoformat(a.end),
                list(s.pestroutes_office_ids))
    if a.pay:
        ranges = []
        for w in a.pay:
            x, y = w.split(":")
            ranges.append((date.fromisoformat(x), date.fromisoformat(y)))
        fr_payments(rows, ranges, a.lookback)


if __name__ == "__main__":
    main()
