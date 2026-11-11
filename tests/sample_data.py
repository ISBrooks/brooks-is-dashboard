"""A small, completely made-up dashboard database for the tests.

Nothing here comes from a real customer, agent or call. The generator is
deterministic (fixed seed, fixed "now"), so it produces the same rows on every
machine and the expected numbers in tests/snapshots/ stay valid.

It deliberately includes every kind of row the counting rules treat differently:
renewals, mosquito upsells and upsells, door-to-door sales by technicians,
office staff who aren't tracked agents, former reps, every first-service status,
customers with and without a card on file, and an audit sheet that lags a few
days behind.
"""
from __future__ import annotations

import csv
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
# The moment the tests pretend it is: Tuesday 10 Nov 2026, noon Pacific.
NOW = datetime(2026, 11, 10, 20, 0, 0, tzinfo=UTC)
SEED = 20261110

# (genesys id, name, team, on_roster, scorecard, powerscore, FieldRoutes ids)
AGENTS = [
    ("g-01", "Avery Stone", "josh", 1, 1, 1, ["9001", "9002"]),
    ("g-02", "Blake Rivers", "josh", 1, 1, 1, ["9003", "9004"]),
    ("g-03", "Casey Flint", "josh", 1, 1, 1, ["9005"]),
    ("g-04", "Devon Marsh", "josh", 1, 1, 1, ["9006", "9007"]),
    ("g-05", "Emery Lake", "kai", 1, 1, 1, ["9008"]),
    ("g-06", "Finley Shaw", "kai", 1, 1, 1, ["9009", "9010"]),
    ("g-07", "Harper Quinn", "kai", 1, 1, 1, ["9011"]),
    ("g-08", "Robin Vale", "kai", 1, 1, 0, ["9012"]),        # outbound rep: no power score
    ("g-90", "Former Rep One", None, 0, 0, 0, ["9801"]),      # left the team
    ("g-91", "Former Rep Two", None, 0, 0, 0, ["9802", "9803"]),
]
OUTBOUND_AGENT = "Robin Vale"
OFFICE_STAFF = [("9701", "Office Staff A"), ("9702", "Office Staff B"), ("9703", "Office Staff C")]
TECHS = [(str(8001 + i), f"Field Tech {i + 1}") for i in range(8)]

RENEWAL_SOURCES = ["ZOLD - Renewal", "Rate Raise Renewal"]
UPSELL_SOURCES = ["Mosquito upsell Campaign", "Upsell", "Bounced Check Fee"]
OFFICE_SOURCES = [("Source 21", 25), ("Source 54", 15), ("Source 144", 14), ("Source 11", 10),
                  (None, 8), ("ZOLD - Renewal", 6), ("Mosquito upsell Campaign", 6),
                  ("Inside Sales Renewal", 5), ("Upsell", 4), ("Renewal - Out of Contract", 4),
                  ("Rate Raise Renewal", 2), ("Bounced Check Fee", 1)]
STATUSES = [("Completed", 70), ("Cancelled", 12), ("No Appointment", 9), ("Pending", 6), ("No Show", 3)]
LENGTHS = [(12, 45), (24, 33), (18, 17), (0, 3), (36, 2)]
FREQS = [((60, 30), 55), ((30, -1), 14), ((90, 30), 12), ((60, -1), 10), ((90, -1), 8), ((-1, -1), 1)]
DISPOS = [("unqualified", 40), ("other", 22), ("closed", 20), ("followup", 13), ("none", 5)]
FORM_SOURCES = [("Source 21", 40), ("Source 54", 25), ("Source 144", 25), ("Source 22", 10)]


class _Rng:
    """Only random() and randrange(): their output is stable across Python versions."""

    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def f(self) -> float:
        return self._r.random()

    def n(self, a: int, b: int) -> int:
        """Whole number from a to b-1."""
        return self._r.randrange(a, b)

    def pick(self, seq):
        return seq[self._r.randrange(len(seq))]

    def weighted(self, pairs):
        total = sum(w for _, w in pairs)
        x = self._r.random() * total
        for value, w in pairs:
            x -= w
            if x < 0:
                return value
        return pairs[-1][0]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def build(db_path: Path, formleads_path: Path, sourcing_master: Path) -> None:
    """Create the dashboard DB, the sourcing app's form-lead DB and its master sheet."""
    from isadash import match, store

    rng = _Rng(SEED)
    conn = store.connect(db_path)

    conn.executemany(
        "INSERT INTO agent_map (genesys_user_id, genesys_name, fr_employee_id, fr_name, on_roster,"
        " scorecard, powerscore, active, team) VALUES (?,?,?,?,?,?,?,1,?)",
        [(gid, name, ",".join(ids), name, roster, sc, ps, team)
         for gid, name, team, roster, sc, ps, ids in AGENTS])
    emp = [(fid, name, "0") for _g, name, _t, _r, _s, _p, ids in AGENTS for fid in ids]
    emp += [(fid, name, "0") for fid, name in OFFICE_STAFF]
    emp += [(fid, name, "2") for fid, name in TECHS]
    conn.executemany("INSERT INTO employees (fr_employee_id, name, emp_type) VALUES (?,?,?)", emp)

    # ---- customers
    customers = []
    for i in range(2500):
        phone = f"+1555{2000000 + i * 7:07d}"
        added = NOW - timedelta(days=rng.n(1, 1050), seconds=rng.n(0, 86400))
        card = None if rng.f() < 0.10 else (1 if rng.f() < 0.72 else 0)
        customers.append({
            "customer_id": f"C{i + 1}", "office_id": "1", "date_added_utc": _iso(added),
            "status": "1", "phone1_e164": phone,
            "phone2_e164": f"+1555{9000000 + i:07d}" if rng.f() < 0.2 else None,
            "email": f"c{i + 1}@example.test" if rng.f() < 0.7 else None,
            "autopay": card, "card_on_file": card})
    store.upsert_customers(conn, customers)

    # ---- subscriptions
    start = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    span = int((NOW - start).total_seconds())
    roster_ids = [(gid, name, fid) for gid, name, _t, roster, _s, _p, ids in AGENTS if roster for fid in ids]
    former_ids = [(gid, name, fid) for gid, name, _t, roster, _s, _p, ids in AGENTS if not roster for fid in ids]
    subs, agent_sales = [], []
    for i in range(4000):
        if rng.f() < 0.25:                       # plenty of recent rows for the 7 / 30 day windows
            added = NOW - timedelta(seconds=rng.n(0, 60 * 86400))
        else:
            added = start + timedelta(seconds=rng.n(0, span))
        x = rng.f()
        if x < 0.60:
            gid, name, fid = rng.pick(roster_ids)
            source = rng.weighted(OFFICE_SOURCES)
        elif x < 0.63:
            gid, name, fid = rng.pick(former_ids)
            source = rng.weighted(OFFICE_SOURCES)
        elif x < 0.70:
            gid = None
            fid, name = rng.pick(OFFICE_STAFF)
            source = rng.weighted(OFFICE_SOURCES)
        else:
            gid = None
            fid, name = rng.pick(TECHS)
            source = "Door to Door"
        status = rng.weighted(STATUSES)
        if status == "Pending" and added < NOW - timedelta(days=21):
            status = "Completed"                 # an old sale isn't still waiting on its first visit
        cust = customers[rng.n(0, len(customers))]
        service_freq, billing_freq = rng.weighted(FREQS)
        cancelled = None
        if status == "Cancelled" or (status == "Completed" and rng.f() < 0.10):
            c = added + timedelta(days=rng.n(2, 300))
            cancelled = _iso(c) if c < NOW else None
        sid = f"S{i + 1}"
        subs.append({
            "subscription_id": sid, "customer_id": cust["customer_id"], "office_id": "1",
            "date_added_utc": _iso(added), "sold_by": fid, "sold_by_name": name,
            "phone1_e164": cust["phone1_e164"], "phone2_e164": cust["phone2_e164"],
            "agreement_length": rng.weighted(LENGTHS),
            "contract_value": None if rng.f() < 0.03 else round(300 + rng.f() * 1500, 2),
            "service_type": "General Pest", "initial_status": status, "source": source,
            "initial_price": None if rng.f() < 0.04 else round(49 + rng.f() * 250, 2),
            "active": 0 if cancelled else 1, "date_cancelled_utc": cancelled,
            "_isd": None if status == "No Appointment" else (added + timedelta(days=rng.n(1, 15))).date().isoformat(),
            "_bf": billing_freq, "_sf": service_freq,
            "_signed": None if rng.f() < 0.15 else (1 if rng.f() < 0.65 else 0)})
        if gid and gid in {g for g, _n, _f in roster_ids}:
            agent_sales.append((gid, added, cust["phone1_e164"]))
    store.upsert_subscriptions(conn, subs)
    conn.executemany(
        "UPDATE subscriptions SET initial_service_date = ?, billing_frequency = ?, service_frequency = ?,"
        " signed_agreement = ?, initial_appointment_id = ?, contract_added = ? WHERE subscription_id = ?",
        [(s["_isd"], s["_bf"], s["_sf"], s["_signed"], "A" + s["subscription_id"][1:],
          "1" if s["_signed"] is not None else None, s["subscription_id"]) for s in subs])

    # ---- calls: some lead to a sale (same caller, a few days earlier), most are noise
    roster_gids = [gid for gid, _n, _t, roster, _s, _p, _ids in AGENTS if roster]
    names = {gid: name for gid, name, *_ in AGENTS}
    calls = []

    def add_call(when, gid, ani, dispo):
        calls.append({
            "conversation_id": f"conv-{len(calls) + 1:06d}",
            "start_utc": when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{rng.n(0, 1000):03d}Z",
            "ani_e164": ani, "dnis": rng.pick(["+18005550101", "+18005550102", "+18005550103"]),
            "agent_user_id": gid, "agent_name": names.get(gid, "Unknown Agent"),
            "wrapup_id": "w-" + dispo, "wrapup_name": dispo.title(), "dispo_group": dispo})

    for gid, added, phone in agent_sales:
        if rng.f() < 0.40 and added > NOW - timedelta(days=500):
            taker = gid if rng.f() < 0.7 else rng.pick(roster_gids)
            when = added - timedelta(days=rng.n(0, 12), seconds=rng.n(60, 20000))
            add_call(when, taker, phone, "closed" if rng.f() < 0.75 else "followup")
    for _ in range(4200):
        when = NOW - timedelta(seconds=rng.n(3600, 500 * 86400))
        gid = "g-zz" if rng.f() < 0.05 else rng.pick(roster_gids)
        x = rng.f()
        ani = None if x < 0.03 else (customers[rng.n(0, len(customers))]["phone1_e164"] if x < 0.40
                                     else f"+1444{rng.n(0, 10 ** 7):07d}")
        add_call(when, gid, ani, rng.weighted(DISPOS))
    store.upsert_calls(conn, calls)
    match.run_matcher(conn, 14)

    # ---- audit sheet: one row per agent per working day, stopping 3 days ago
    audit = []
    first = (NOW - timedelta(days=420)).date()
    last = (NOW - timedelta(days=3)).date()
    day = first
    scorecard_names = [name for _g, name, _t, _r, sc, _p, _i in AGENTS if sc]
    while day <= last:
        if day.weekday() != 6:                   # nobody audits Sundays
            for name in scorecard_names:
                q = rng.n(3, 15)
                audit.append({"date": day.isoformat(), "agent": name, "qualified": q,
                              "sales": rng.n(0, q // 2 + 1)})
        day += timedelta(days=1)
    store.upsert_audit_daily(conn, audit)
    conn.commit()
    conn.close()

    # ---- the sourcing app's form-lead database
    fl = sqlite3.connect(formleads_path)
    fl.execute("CREATE TABLE touches (dedup_key TEXT PRIMARY KEY, channel TEXT, source TEXT,"
               " occurred_at TEXT, phone_e164 TEXT, email TEXT, raw_ref TEXT, ingested_at TEXT)")
    touches = []
    for i in range(1500):
        when = NOW - timedelta(seconds=rng.n(600, 200 * 86400))
        x = rng.f()
        if x < 0.30:                             # someone already (or later) in the customer base
            c = customers[rng.n(0, len(customers))]
            phone, email = c["phone1_e164"], c["email"]
        elif x < 0.45 and touches:               # the same person filling in a form again
            phone, email = touches[rng.n(0, len(touches))][4:6]
        else:
            phone = f"+1333{rng.n(0, 10 ** 7):07d}"
            email = f"lead{i}@example.test" if rng.f() < 0.5 else None
        channel = rng.weighted([("gmail", 60), ("meta", 20), ("lsa", 12), ("call", 8)])
        touches.append((f"t-{i + 1}", channel, rng.weighted(FORM_SOURCES), _iso(when), phone, email,
                        f"ref-{i + 1}", _iso(when)))
    fl.executemany("INSERT INTO touches VALUES (?,?,?,?,?,?,?,?)", touches)
    fl.commit()
    fl.close()

    with sourcing_master.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Pestroutes Source", "Provider / Channel"])
        w.writerows([["Source 21", "Sample Search Ads"], ["Source 54", "Sample Directory"],
                     ["Source 144", "Sample Affiliate"], ["Source 11", "Sample Organic"]])
