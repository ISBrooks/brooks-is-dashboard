"""Locks in the dashboard's numbers.

Each case runs one data feed against the made-up sample database and compares
the full result with the copy saved in tests/snapshots/. If a change to the
code moves ANY number, the matching test fails and shows what moved.

When a rule is changed on purpose, regenerate the expected results and review
the differences in git before committing them:

    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_money_numbers.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SNAP = Path(__file__).parent / "snapshots"
YTD = ("2026-01-01", "2026-11-10")
PERIOD = ("2026-10-11", "2026-10-24")          # one two-week pay period
MONTH = ("2026-10-01", "2026-10-31")

# name -> how to get the numbers from the app module
CASES = {
    # Revenue tab, Home year-to-date
    "revenue_2026": lambda w: w.api_revenue(year=2026),
    "revenue_2025": lambda w: w.api_revenue(year=2025),
    "revenue_overview": lambda w: w.api_revenue_overview(),
    "home_ytd": lambda w: w.api_home_ytd(),
    # Home
    "home_cards": lambda w: w.api_home(),
    "home_trends": lambda w: w.api_home_trends(),
    "home_daily": lambda w: w.api_home_daily(),
    # Agent Metrics: IS #'s (also feeds Power Score and the agent page)
    "scorecard_ytd_sold": lambda w: w.api_scorecard(start_day=YTD[0], end_day=YTD[1], mode="sold"),
    "scorecard_period_sold": lambda w: w.api_scorecard(start_day=PERIOD[0], end_day=PERIOD[1], mode="sold"),
    "scorecard_period_serviced": lambda w: w.api_scorecard(start_day=PERIOD[0], end_day=PERIOD[1], mode="serviced"),
    "agentview_month": lambda w: w.api_agentview(start_day=MONTH[0], end_day=MONTH[1]),
    "inbound_split_month": lambda w: w.api_inbound_split(start_day=MONTH[0], end_day=MONTH[1]),
    # Team Metrics
    "teammetrics_month_sold": lambda w: w.api_teammetrics(start_day=MONTH[0], end_day=MONTH[1], mode="sold", phone=False),
    "teammetrics_month_serviced": lambda w: w.api_teammetrics(start_day=MONTH[0], end_day=MONTH[1], mode="serviced", phone=False),
    "range_month": lambda w: w.api_range(start=MONTH[0], end=MONTH[1]),
    "leaked_month": lambda w: w.api_leaked(start_day=MONTH[0], end_day=MONTH[1]),
    "audit": lambda w: w.api_audit(),
    "team_overview_db": lambda w: w._team_overview_db(),
    "home_quality": lambda w: w.api_home_quality(),
    "phone_weekly_monthly": lambda w: w._build_payload(),
    # Form leads
    "formleads": lambda w: w.api_formleads(),
    # Marketing (the spend sheet is tests/sample_data.spend_sheet)
    "spend_windows": lambda w: w.api_marketing_spend_windows(),
    "spend_chart_ytd": lambda w: w.api_marketing_spend_chart(start_day=YTD[0], end_day=YTD[1]),
    "spend_month": lambda w: w.api_marketing_spend(start_day=MONTH[0], end_day=MONTH[1]),
    "volume_by_source_month": lambda w: w.api_marketing_bysource(start_day=MONTH[0], end_day=MONTH[1]),
    "source_revenue_2026": lambda w: w.api_source_revenue(year=2026, level="channel"),
    "source_revenue_overview": lambda w: w.api_source_revenue_overview(level="source"),
}


def _plain(result):
    """The JSON a browser would receive, in a stable, readable layout."""
    body = getattr(result, "body", None)
    if body is not None:                      # a ready-made HTTP response
        result = json.loads(body)
    return json.dumps(result, indent=1, sort_keys=True, default=str, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", sorted(CASES))
def test_numbers_unchanged(app, name):
    got = _plain(CASES[name](app))
    path = SNAP / f"{name}.json"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAP.mkdir(exist_ok=True)
        path.write_text(got, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"no saved result for {name}: run with UPDATE_SNAPSHOTS=1 and review it"
    want = path.read_text(encoding="utf-8")
    if got != want:
        import difflib
        diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(),
                                         "saved", "now", lineterm="", n=2))
        shown = "\n".join(diff[:60]) + ("\n... (more)" if len(diff) > 60 else "")
        pytest.fail(f"{name}: numbers changed\n{shown}", pytrace=False)


def test_saved_results_have_real_numbers(app):
    """Guard against a sample database so thin that the snapshots lock in zeros."""
    rev = app.api_revenue(year=2026)
    assert rev["team_total"] > 100_000 and rev["team_sales"] > 200
    sc = app.api_scorecard(start_day=YTD[0], end_day=YTD[1], mode="sold")
    assert len(sc["rows"]) == 8 and all(r["subs"] > 20 for r in sc["rows"])
    home = app.api_home()
    assert home["sheet30"]["qual"] > 500 and home["form30"]["net"] > 50 and home["team30"]["subs"] > 100
