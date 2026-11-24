"""Payroll -> Downloads payroll workbook: the Summary's week layout, the
pre-go-live banner and how hours are lined up with money.

Found 2026-11-24: the banner row moved the week headers' values down but not
their merged cells, so pre-go-live workbooks had overlapping merges; the hours
column called "Hours" held regular hours; and a rep whose Commission Roster
name is their FieldRoutes spelling got one row for money and another for hours.
"""
from __future__ import annotations

import sqlite3

from openpyxl import Workbook

from isadash.api import payroll

LABELS = ["Week 1", "Week 2", "Pay period"]


def _summary(hours=True):
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    pos = {"Avery Stone": 100.0}
    neg = {"Avery Stone": -20.0}
    wpos = {1: {"Avery Stone": 60.0}, 2: {"Avery Stone": 40.0}}
    wneg = {1: {}, 2: {"Avery Stone": -20.0}}
    h = {1: {"Avery Stone": (38.0, 0.0, 2.0)}, 2: {"Avery Stone": (40.0, 3.5, 0.0)}} if hours else None
    payroll._week_summary(ws, ["Avery Stone"], pos, neg, wpos, wneg, LABELS, hours=h)
    return wb, ws


def _overlaps(ws) -> list:
    rs = [m for m in ws.merged_cells.ranges]
    bad = []
    for i, a in enumerate(rs):
        for b in rs[i + 1:]:
            if not (a.max_row < b.min_row or b.max_row < a.min_row
                    or a.max_col < b.min_col or b.max_col < a.min_col):
                bad.append((str(a), str(b)))
    return bad


def test_hours_columns_say_regular_hours():
    _wb, ws = _summary()
    heads = [c.value for c in ws[2]]
    assert "Hours" not in heads
    assert heads[4:7] == ["Regular Hours", "OT Hours", "PTO Hours"]
    # the period's hours are the two weeks added up: 78 regular, 3.5 OT, 2 PTO
    assert [c.value for c in ws[3]][-3:] == [78.0, 3.5, 2.0]


def test_pre_go_live_banner_moves_the_week_headers_with_it():
    wb, ws = _summary()
    before = sorted(str(m) for m in ws.merged_cells.ranges)
    assert before == ["B1:G1", "H1:M1", "N1:S1"]
    payroll._mark_pre_go_live(wb)
    assert _overlaps(ws) == []
    assert ws["A1"].value == payroll.PRE_GO_LIVE_BANNER
    # every week-header merge is now one row lower, still holding its label
    assert sorted(str(m) for m in ws.merged_cells.ranges) == ["A1:S1", "B2:G2", "H2:M2", "N2:S2"]
    assert [ws.cell(row=2, column=c).value for c in (2, 8, 14)] == LABELS
    assert ws.freeze_panes == "B4"


def test_pre_go_live_banner_on_an_old_four_column_summary():
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    payroll._sheet_fill(ws, ["Rep", "Commissions", "Deductions", "Net"],
                        [["Avery Stone", 1.0, -1.0, 0.0]])
    payroll._mark_pre_go_live(wb)
    assert _overlaps(ws) == []
    assert ws["A2"].value == "Rep" and ws.freeze_panes == "A3"


def test_clock_names_become_commission_roster_names():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE commission_roster (name TEXT, role TEXT)")
    c.execute("CREATE TABLE agent_map (genesys_name TEXT, fr_name TEXT)")
    c.executemany("INSERT INTO commission_roster VALUES (?, ?)",
                  [("Casey  Flint", "inbound"), ("Blake Rivers", "inbound")])
    c.executemany("INSERT INTO agent_map VALUES (?, ?)",
                  [("Casey Flint-Moss", "casey flint"),     # roster uses the FieldRoutes spelling
                   ("Blake Rivers", "Blake R"),             # roster uses the Genesys spelling
                   ("Robin Vale", "Robin Vale")])           # not on the roster: left alone
    assert payroll._clock_to_roster(c) == {"Casey Flint-Moss": "Casey Flint",
                                           "Blake Rivers": "Blake Rivers"}


def test_clock_names_without_a_roster_table_stay_as_they_are():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE agent_map (genesys_name TEXT, fr_name TEXT)")
    assert payroll._clock_to_roster(c) == {}
