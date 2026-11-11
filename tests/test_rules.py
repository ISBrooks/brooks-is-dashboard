"""The counting rules, checked by hand.

test_money_numbers.py proves the numbers haven't MOVED. These tests prove the
main ones are RIGHT: each recomputes a figure straight from the sample rows in
plain Python, following the rule as it is written down, and compares it with
what the dashboard reports. They share no code with the app's calculations.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

PACIFIC = ZoneInfo("America/Los_Angeles")
COUNTED = {"Completed", "Pending"}                    # first service done or scheduled
RENEWALS = {"ZOLD - Renewal", "Zoldz - Renewal", "Rate Raise Renewal", "Renewal - In Contract",
            "Renewal - No Contract", "Renewal - Out of Contract"}
UPSELLS = {"MOSQUITO UPSELL CAMPAIGN", "UPSELL", "BOUNCED CHECK FEE"}
START, END = date(2026, 1, 1), date(2026, 11, 10)


def sale_day(row) -> date:
    return datetime.fromisoformat(row["date_added_utc"]).astimezone(PACIFIC).date()


def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


@pytest.fixture()
def subs(db):
    return db.execute(
        "SELECT s.*, c.card_on_file FROM subscriptions s "
        "LEFT JOIN customers c ON c.customer_id = s.customer_id").fetchall()


# ---- Revenue tab / Home year-to-date -------------------------------------
def revenue_rows(db, subs, year):
    """Every office-staff seller; first service done or scheduled; a contract
    value; sold that Pacific year; renewals out; upsells and mosquito IN."""
    office = {r[0] for r in db.execute("SELECT fr_employee_id FROM employees WHERE emp_type = '0'")}
    return [s for s in subs
            if s["sold_by"] in office and s["initial_status"] in COUNTED
            and (s["contract_value"] or 0) > 0 and (s["source"] or "") not in RENEWALS
            and sale_day(s).year == year]


@pytest.mark.parametrize("year", [2025, 2026])
def test_revenue_tab_total(app, db, subs, year):
    rows = revenue_rows(db, subs, year)
    rev = app.api_revenue(year=year)
    assert rev["team_sales"] == len(rows)
    assert rev["team_total"] == pytest.approx(sum(s["contract_value"] for s in rows), abs=0.02)
    # the per-person lines add up to the team total, with nobody dropped
    assert sum(a["total"] for a in rev["agents"]) == pytest.approx(rev["team_total"], abs=0.5)
    assert sum(rev["team_month_totals"]) == pytest.approx(rev["team_total"], abs=0.5)


def test_revenue_excludes_technician_sales(app, db, subs):
    techs = {r[0] for r in db.execute("SELECT fr_employee_id FROM employees WHERE emp_type = '2'")}
    tech_rev = sum(s["contract_value"] or 0 for s in subs
                   if s["sold_by"] in techs and s["initial_status"] in COUNTED and sale_day(s).year == 2026)
    assert tech_rev > 100_000, "sample data should contain door-to-door sales to exclude"
    names = {a["agent"] for a in app.api_revenue(year=2026)["agents"]}
    assert not any(n.startswith("Field Tech") for n in names)


def test_home_year_to_date_equals_revenue_tab(app):
    ytd, rev = app.api_home_ytd(), app.api_revenue(year=2026)
    assert (ytd["year"], ytd["revenue"], ytd["sales"]) == (2026, rev["team_total"], rev["team_sales"])


# ---- IS #'s (scorecard), which also feeds Power Score and the agent page ---
def test_scorecard_matches_hand_count(app, db, subs):
    sc = app.api_scorecard(start_day=START.isoformat(), end_day=END.isoformat(), mode="sold")
    got = {r["agent"]: r for r in sc["rows"]}
    agents = db.execute("SELECT genesys_name, fr_employee_id FROM agent_map WHERE scorecard = 1").fetchall()
    assert set(got) == {a["genesys_name"] for a in agents}

    for a in agents:
        ids = set(a["fr_employee_id"].split(","))
        mine = [s for s in subs if s["sold_by"] in ids and START <= sale_day(s) <= END]
        counted = [s for s in mine if s["initial_status"] in COUNTED]
        # upsell-type sources are left out of every IS #'s metric
        core = [s for s in counted if (s["source"] or "").upper() not in UPSELLS]
        r = got[a["genesys_name"]]
        who = a["genesys_name"]

        assert r["subs"] == len(core), who
        with_cv = [s["contract_value"] for s in core if s["contract_value"]]
        assert r["rev"] == pytest.approx(sum(with_cv), abs=0.02), who
        assert r["acv"] == pytest.approx(round(sum(with_cv) / len(with_cv), 0), abs=1), who
        # revenue the Home table shows: upsells back in, renewals out
        rev_all = sum(s["contract_value"] for s in counted
                      if s["contract_value"] and (s["source"] or "") not in RENEWALS)
        assert r["rev_all"] == pytest.approx(rev_all, abs=0.02), who

        m24 = sum(1 for s in core if (s["agreement_length"] or 0) >= 24)
        m18 = sum(1 for s in core if 18 <= (s["agreement_length"] or 0) < 24)
        assert r["multi24_pct"] == pct(m24, len(core)), who
        assert r["multi18_pct"] == pct(m18, len(core)), who
        assert r["multi_total_pct"] == pct(m18 + m24, len(core)), who

        priced = [s["initial_price"] for s in core if s["initial_price"]]
        assert r["aip"] == pytest.approx(round(sum(priced) / len(priced), 2), abs=0.01), who

        card = [s["card_on_file"] for s in core if s["card_on_file"] is not None]
        assert r["apay_pct"] == pct(sum(card), len(card)), who
        signed = [s["signed_agreement"] for s in core if s["signed_agreement"] is not None]
        assert r["signed_pct"] == pct(sum(signed), len(signed)), who

        recurring = [s for s in core if s["service_frequency"] is not None and s["service_frequency"] != -1]
        monthly = [s for s in recurring if s["billing_frequency"] == 30
                   or (s["billing_frequency"] == -1 and s["service_frequency"] == 30)]
        assert r["monthly_pct"] == pct(len(monthly), len(recurring)), who

        # sold -> serviced: every first-service status, No Appointment and Pending outside the ratio
        st = [s["initial_status"] for s in mine if (s["source"] or "").upper() not in UPSELLS]
        done = st.count("Completed")
        failed = len(st) - done - st.count("Pending") - st.count("No Appointment")
        assert r["sts_pct"] == pct(done, done + failed), who

        q, sales = db.execute(
            "SELECT COALESCE(SUM(qualified), 0), COALESCE(SUM(sales), 0) FROM audit_daily "
            "WHERE agent = ? AND date BETWEEN ? AND ?", (who, START.isoformat(), END.isoformat())).fetchone()
        assert r["conversion_pct"] == pct(sales, q), who


def test_scorecard_serviced_mode_uses_first_service_date(app, db, subs):
    a, b = date(2026, 10, 11), date(2026, 10, 24)
    sc = app.api_scorecard(start_day=a.isoformat(), end_day=b.isoformat(), mode="serviced")
    row = next(r for r in sc["rows"] if r["agent"] == "Avery Stone")
    ids = set(db.execute("SELECT fr_employee_id FROM agent_map WHERE genesys_name = 'Avery Stone'"
                         ).fetchone()[0].split(","))
    core = [s for s in subs if s["sold_by"] in ids and s["initial_status"] in COUNTED
            and s["initial_service_date"] and a.isoformat() <= s["initial_service_date"] <= b.isoformat()
            and (s["source"] or "").upper() not in UPSELLS]
    assert row["subs"] == len(core) and len(core) > 0


# ---- Home cards ----------------------------------------------------------
def test_home_phone_conversion_is_the_audit_sheet(app, db):
    card = app.api_home()["sheet30"]
    q, sales = db.execute("SELECT SUM(qualified), SUM(sales) FROM audit_daily WHERE date BETWEEN ? AND ?",
                          (card["range"]["start"], card["range"]["end"])).fetchone()
    assert (card["qual"], card["sales"], card["pct"]) == (q, sales, pct(sales, q))
    # the sheet stops 3 days back in the sample, so the window ends there, not yesterday
    assert card["range"]["end"] == db.execute("SELECT MAX(date) FROM audit_daily").fetchone()[0]


def test_home_windows_are_whole_days_ending_yesterday(app):
    h = app.api_home()
    assert h["anchor_end"] == "2026-11-09"
    assert h["team30"]["range"] == {"start": "2026-10-11", "end": "2026-11-09"}
    assert h["team7"]["range"] == {"start": "2026-11-03", "end": "2026-11-09"}


def test_home_team_multiyear_and_contract_value(app, db, subs):
    card = app.api_home()["team30"]
    lo, hi = date.fromisoformat(card["range"]["start"]), date.fromisoformat(card["range"]["end"])
    roster = {fid for (ids,) in db.execute(
        "SELECT fr_employee_id FROM agent_map WHERE on_roster = 1") for fid in ids.split(",")}
    win = [s for s in subs if s["sold_by"] in roster and s["initial_status"] in COUNTED
           and (s["source"] or "").upper() not in UPSELLS and lo <= sale_day(s) <= hi]
    assert card["subs"] == len(win)
    assert card["multiyear_pct"] == pct(sum(1 for s in win if (s["agreement_length"] or 0) >= 18), len(win))
    cvs = [s["contract_value"] for s in win if (s["contract_value"] or 0) > 0]
    assert card["acv"] == pytest.approx(round(sum(cvs) / len(cvs), 2), abs=0.01)
