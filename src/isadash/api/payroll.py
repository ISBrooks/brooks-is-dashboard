"""Payroll tab: Commission Lookup, Submit Adjustment, rules, roster, downloads, Lock & run payroll.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from ..store import connect

from .common import (
    _allowed, _comm_db, _comm_reports_dir, _is_pre_go_live, _live_from, _lookup_why,
    _payroll_periods, _settings, _user, app,
)


@app.get("/api/commissions/roster")
def api_commissions_roster(request: Request) -> dict:
    """Settings -> Commissions: the roster the IS Commissions app calculates
    for (inbound full-rate ISAs, outbound 3% agents), plus seller candidates
    to add — same candidate pool as the Agents subtab."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        roster = [{"name": r["name"], "role": r["role"], "added": r["added_at"][:10],
                   "start_date": r["start_date"] or ""}
                  for r in conn.execute(
                      "SELECT name, role, added_at, start_date FROM commission_roster "
                      "ORDER BY role, name")]
        on = {r["name"] for r in roster}
        rev: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.name, SUM(s.contract_value) v FROM subscriptions s "
            "JOIN employees e ON e.fr_employee_id = s.sold_by "
            "WHERE e.emp_type = '0' AND s.contract_value > 0 GROUP BY e.name"):
            rev[r["name"]] = r["v"] or 0
        names = {r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM employees WHERE emp_type = '0' "
            "AND name IS NOT NULL AND name != ''")}
        # tracked dashboard agents too — covers reps (e.g. outbound) whose
        # employees rows haven't synced on this box
        names |= {r["genesys_name"] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE scorecard = 1 "
            "AND genesys_name IS NOT NULL")}
        sellers = sorted(
            [{"name": n, "revenue": round(rev.get(n, 0))}
             for n in names if n not in on],
            key=lambda s: (-s["revenue"], s["name"]))
        return {"roster": roster, "sellers": sellers}
    finally:
        conn.close()


@app.post("/api/commissions/roster")
def api_commissions_roster_save(body: dict, request: Request) -> dict:
    """Add / change role / remove one commission-roster entry."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "missing name"}
    conn = connect(_settings.db_path)
    try:
        if body.get("remove"):
            conn.execute("DELETE FROM commission_roster WHERE name = ?", (name,))
        elif "start_date" in body:
            # RULES §1a: inside-sales start date; blank clears it. Validated —
            # a malformed date would make the engine exclude every sale.
            raw = (body.get("start_date") or "").strip()
            if raw:
                try:
                    from datetime import date as _date
                    raw = _date.fromisoformat(raw).isoformat()
                except ValueError:
                    return {"error": "start date must be YYYY-MM-DD"}
            conn.execute("UPDATE commission_roster SET start_date = ? WHERE name = ?",
                         (raw or None, name))
        else:
            role = body.get("role")
            if role not in ("inbound", "outbound"):
                return {"error": "role must be inbound or outbound"}
            conn.execute(
                """INSERT INTO commission_roster (name, role) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET role = excluded.role""",
                (name, role))
        conn.commit()
        logging.getLogger("isadash.commissions").info(
            "commission roster: %s -> %s", name,
            "removed" if body.get("remove") else
            f"start {body.get('start_date') or '(none)'}" if "start_date" in body
            else body.get("role"))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/commissions/rules")
def api_commissions_rules(request: Request) -> dict:
    """The IS Commissions rulebook (COMMISSION_RULES.md), verbatim. The
    commissions app treats that file as the single source of truth — every
    rule change lands there first — so the dashboard renders it rather than
    keeping a copy that could drift."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    p = Path(_settings.commission_rules_path)
    if not p.is_file():
        return {"error": f"rules file not found at {p} — set COMMISSION_RULES_PATH"}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return {"error": f"could not read rules file: {e}"}
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return {"markdown": text, "updated": mtime.isoformat()}


@app.get("/api/commissions/lookup")
def api_commissions_lookup(q: str = "") -> dict:
    """Agent Metrics -> Commission Lookup: account number, subscription id, or
    customer name -> every subscription's commission state, from the IS
    Commissions engine database (read-only)."""
    q = (q or "").strip()
    if not q:
        return {"results": []}
    db = Path(_settings.commissions_db)
    if not db.is_file():
        return {"error": f"commissions database not found at {db} — set COMMISSIONS_DB"}
    import sqlite3 as _sq
    conn = _sq.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    try:
        if q.isdigit():
            where, args = ("(sub.customer_id = ? OR sub.subscription_id = ?)",
                           [q, q])
        else:
            where, args = ("cu.name LIKE ?", [f"%{q}%"])
        subs = conn.execute(f"""
            SELECT sub.subscription_id, sub.customer_id, cu.name AS customer,
                   cu.card_on_file, sub.rep, sub.service_type, sub.source,
                   sub.contract_value, sub.date_added, sub.active,
                   sub.date_cancelled AS cancel_date, sub.cancel_reason,
                   st.state, st.rate, st.expected, st.first_paid_date AS first_paid,
                   st.qualifying_completed AS qualifying, st.coverage_paid,
                   st.coverage_target, st.vested_date AS vested,
                   COALESCE(st.flags,'') AS flags
            FROM subscriptions sub
            LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
            LEFT JOIN sub_status st USING (subscription_id)
            WHERE {where}
            ORDER BY sub.customer_id, sub.date_added DESC LIMIT 40""",
            args).fetchall()
        out = []
        for r in subs:
            d = dict(r)
            d["why"] = _lookup_why(d)
            d["splits"] = [dict(a) for a in conn.execute(
                "SELECT rep, share, type, note FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL",
                (r["subscription_id"],))]
            for k in ("date_added", "cancel_date", "first_paid", "vested"):
                d[k] = (d[k] or "")[:10]
            out.append(d)
        synced = conn.execute(
            "SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"results": out,
                "data_as_of": (synced["value"] if synced else None)}
    except _sq.OperationalError as e:
        return {"error": f"lookup failed: {e}"}
    finally:
        conn.close()


# --------------- Payroll -> Downloads (xlsx reports on demand) ---------------
_BRANCHES = {"1": "Riverside", "2": "Orange County", "3": "Portland",
             "4": "San Diego", "5": "Fresno", "6": "LA", "8": "Sacramento",
             "9": "San Jose", "10": "Spokane"}


# Go-live (Dallin, 2026-10-15): 10/11-10/24 (payroll 10/26) is the first pay
# period paid FROM the app. Every earlier statement is a parallel run —
# re-simulated under later rules, never the basis of pay — and says so.
PRE_GO_LIVE_BANNER = ("PRE-GO-LIVE — parallel run, not the basis of pay. "
                      "The manual payroll sheet paid this period.")


def _mark_pre_go_live(wb) -> None:
    """Stamp a pre-go-live payroll workbook so a forwarded copy can't pass
    for a statement that was paid: red banner over the Summary, red tabs,
    and the workbook title."""
    from openpyxl.styles import Alignment, Font, PatternFill
    ws = wb["Summary"]
    ws.insert_rows(1)
    ws.merge_cells("A1:D1")
    ws["A1"] = PRE_GO_LIVE_BANNER
    ws["A1"].font = Font(name="Arial", size=11, bold=True, color="9C0006")
    ws["A1"].fill = PatternFill("solid", fgColor="FFC7CE")
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A3"
    for sh in wb.worksheets:
        sh.sheet_properties.tabColor = "C00000"
    wb.properties.title = PRE_GO_LIVE_BANNER


def _tech_from_note(note: str) -> str:
    """Tech name from an upsell note: 'tech ref[erral][:] First Last' — the
    name must look like a name (capitalized word with lowercase letters), so
    'tech referral ACV +176' never matches."""
    import re as _re
    m = _re.search(r"tech ref(?:erral)?:?\s+([A-Z][a-z]+(?:\s+[A-Z][\w.'-]*)+)",
                   note or "")
    return m.group(1).strip() if m else "(tech not named on the form)"


def _xlsx_response(wb, filename: str):
    import io
    from fastapi.responses import Response
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _sheet_fill(ws, header: list, rows: list, money_cols: tuple = (),
                widths: dict | None = None) -> None:
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    HDR = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    FILL = PatternFill("solid", fgColor="1F3864")
    ws.append(header)
    for c in range(1, len(header) + 1):
        ws.cell(row=ws.max_row, column=c).font = HDR
        ws.cell(row=ws.max_row, column=c).fill = FILL
    for r in rows:
        ws.append(r)
    for c in money_cols:
        for row in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            row[0].number_format = '$#,##0.00;($#,##0.00);"-"'
    for c, w in (widths or {}).items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = ws.cell(row=2, column=1).coordinate


@app.get("/api/commissions/reports")
def api_commissions_reports() -> dict:
    """What is available to download: statement periods, D2D years, DB counts."""
    import re as _re
    periods = []
    if _comm_reports_dir().is_dir():
        for f in _comm_reports_dir().glob("commission_statement_*.xlsx"):
            m = _re.match(r"commission_statement_(\d{4}-\d{2}-\d{2})", f.name)
            if m:
                periods.append(m.group(1))
    periods = sorted(set(periods), reverse=True)
    conn = _comm_db()
    years, counts, as_of = [], {}, None
    if conn:
        try:
            tags = set()
            for (d2d,) in conn.execute(
                    "SELECT COALESCE(d2d_host, d2d_rep) FROM upsells "
                    "WHERE d2d_host IS NOT NULL OR d2d_rep IS NOT NULL"):
                m = _re.search(r"20\d\d", d2d or "")
                if m:
                    tags.add(m.group(0))
            years = sorted(tags, reverse=True)
            for state, n in conn.execute(
                    "SELECT state, COUNT(*) FROM sub_status GROUP BY state"):
                counts[state] = n
            row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
            as_of = row["value"] if row else None
        finally:
            conn.close()
    return {"periods": periods, "d2d_years": years, "states": counts,
            "data_as_of": as_of, "live_from": _live_from().isoformat(),
            "pre_go_live": [p for p in periods if _is_pre_go_live(p)]}


@app.get("/api/commissions/report.xlsx")
def api_commissions_report_xlsx(period: str):
    """One pay period's payroll workbook: Summary, Commissions, Deductions,
    Tech Referral Upsells (for branch managers), then one sheet per rep."""
    import re as _re
    from collections import defaultdict
    from datetime import date as _date, timedelta as _td
    from openpyxl import Workbook, load_workbook
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", period or ""):
        return JSONResponse({"error": "period must be YYYY-MM-DD"}, status_code=400)
    src = _comm_reports_dir() / f"commission_statement_{period}.xlsx"
    if not src.is_file():
        return JSONResponse({"error": f"no statement for {period}"}, status_code=404)
    pre = _is_pre_go_live(period)

    stmt = load_workbook(src, read_only=True, data_only=True)
    hdr, comm_rows, ded_rows = None, [], []
    for tab, dest in (("Commissions", comm_rows), ("Deductions", ded_rows)):
        it = stmt[tab].iter_rows(values_only=True)
        h = [str(x) for x in next(it)]
        if hdr is None:
            hdr = h
        for r in it:
            if r and r[0] and r[4] is not None:
                dest.append([("" if v is None else v) for v in r[:len(hdr)]])

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    pos, neg = defaultdict(float), defaultdict(float)
    for r in comm_rows:
        pos[r[0]] += float(r[4])
    for r in ded_rows:
        neg[r[0]] += float(r[4])
    srows, tp, tn = [], 0.0, 0.0
    for rep in sorted(set(pos) | set(neg)):
        p, n = round(pos.get(rep, 0), 2), round(neg.get(rep, 0), 2)
        tp += p; tn += n
        srows.append([rep, p, n, round(p + n, 2)])
    srows.append(["TOTAL", round(tp, 2), round(tn, 2), round(tp + tn, 2)])
    _sheet_fill(summary, ["Rep", "Commissions", "Deductions", "Net"], srows,
                money_cols=(2, 3, 4), widths={1: 22, 2: 13, 3: 13, 4: 13})

    _sheet_fill(wb.create_sheet("Commissions"), hdr, comm_rows, money_cols=(5,),
                widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})
    _sheet_fill(wb.create_sheet("Deductions"), hdr, ded_rows, money_cols=(5,),
                widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})

    # tech-referral upsells credited this period, for the branch managers
    end = _date.fromisoformat(period)
    start = end - _td(days=13)
    tech = []
    conn = _comm_db()
    if conn:
        try:
            for d, cust, name, office, d2d, amount, note in conn.execute("""
                    SELECT u.date, sub.customer_id, cu.name, sub.office_id,
                           u.d2d_rep, u.amount, COALESCE(u.note,'')
                    FROM upsells u
                    LEFT JOIN subscriptions sub USING (subscription_id)
                    LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                    WHERE u.removed_at IS NULL AND u.date >= ? AND u.date <= ?
                      AND lower(COALESCE(u.note,'')) LIKE '%tech ref%'
                    ORDER BY sub.office_id, u.date""",
                    (start.isoformat(), end.isoformat())):
                t = (d2d or "").strip() or _tech_from_note(note)
                # 9% of the ACV change; 5% on commercial accounts (RULES §12)
                rate = 0.05 if "commercial 5%" in note.lower() else 0.09
                tech.append([cust or "", _BRANCHES.get(str(office), "?"),
                             t, round(float(amount) / rate, 2), name or "", d])
        finally:
            conn.close()
    _sheet_fill(wb.create_sheet("Tech Referral Upsells"),
                ["Account #", "Branch", "Tech", "ACV change", "Customer", "Date"],
                tech or [["(none this period)"]], money_cols=(4,),
                widths={1: 11, 2: 15, 3: 24, 4: 12, 5: 24, 6: 12})

    # one sheet per rep: their commissions, a spacer row, their deductions
    for rep in sorted(set(pos) | set(neg)):
        safe = _re.sub(r"[\\/*?\[\]:]", "", rep)[:31]
        ws = wb.create_sheet(safe)
        mine_c = [r for r in comm_rows if r[0] == rep]
        mine_d = [r for r in ded_rows if r[0] == rep]
        net = round(pos.get(rep, 0) + neg.get(rep, 0), 2)
        ws.append([("PRE-GO-LIVE (not the basis of pay) — " if pre else "") +
                   f"{rep} — {period} payroll: "
                   f"{len(mine_c)} commissions ${pos.get(rep, 0):,.2f}, "
                   f"{len(mine_d)} deductions ${neg.get(rep, 0):,.2f}, net ${net:,.2f}"])
        ws.append([])
        _sheet_fill(ws, hdr, mine_c + ([[]] if mine_d else []) + mine_d,
                    money_cols=(5,), widths={1: 20, 2: 12, 3: 11, 4: 24, 5: 10, 21: 50})
    # items the live close left for a human decision (RULES 8c): second tab
    if "Review" in stmt.sheetnames:
        rv = list(stmt["Review"].iter_rows(values_only=True))
        if len(rv) > 1:
            ws = wb.create_sheet("Review", 1)
            _sheet_fill(ws, [str(x) for x in rv[0]],
                        [[("" if v is None else v) for v in r] for r in rv[1:]],
                        money_cols=(), widths={1: 14, 2: 12, 3: 26, 4: 110})
    if pre:
        _mark_pre_go_live(wb)
    return _xlsx_response(
        wb, f"payroll_{period}{'_PRE-GO-LIVE' if pre else ''}.xlsx")


@app.get("/api/commissions/database.xlsx")
def api_commissions_database_xlsx():
    """The live commission database: every account's state, plus focused
    sheets for pending-payment, paid-not-vested, splits, and upsell credits."""
    from openpyxl import Workbook
    conn = _comm_db()
    if conn is None:
        return JSONResponse({"error": "commissions database not found"}, status_code=404)
    try:
        hdr = ["Rep", "Account #", "Customer", "Subscription #", "Plan", "Source",
               "Contract Value", "Rate", "Expected $", "State", "Sold",
               "First Paid", "Services", "Coverage Paid", "Coverage Target",
               "Vested", "Active", "Cancelled", "Cancel Reason", "Card", "Flags"]
        all_rows = []
        for r in conn.execute("""
                SELECT st.rep, sub.customer_id, cu.name, sub.subscription_id,
                       sub.service_type, sub.source, sub.contract_value, st.rate,
                       st.expected, st.state, sub.date_added,
                       st.first_paid_date, st.qualifying_completed,
                       st.coverage_paid, st.coverage_target, st.vested_date,
                       sub.active, sub.date_cancelled, sub.cancel_reason,
                       cu.card_on_file, COALESCE(st.flags,'')
                FROM sub_status st
                JOIN subscriptions sub USING (subscription_id)
                LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                ORDER BY st.rep, sub.date_added DESC"""):
            row = list(r)
            for i in (10, 11, 15, 17):
                row[i] = (row[i] or "")[:10]
            row[16] = "Yes" if row[16] else "No"
            row[19] = "Yes" if row[19] else "No"
            all_rows.append(row)

        wb = Workbook()
        ws = wb.active
        ws.title = "All accounts"
        W = {1: 20, 2: 11, 3: 24, 4: 13, 5: 20, 6: 16, 7: 13, 8: 7, 9: 11,
             10: 15, 11: 11, 12: 11, 13: 9, 14: 13, 15: 14, 16: 11, 17: 7,
             18: 11, 19: 16, 20: 6, 21: 28}
        _sheet_fill(ws, hdr, all_rows, money_cols=(7, 9, 14, 15), widths=W)

        pending = [r for r in all_rows
                   if r[9] in ("pending", "not_started") and r[16] == "Yes"]
        _sheet_fill(wb.create_sheet("Pending payment"), hdr, pending,
                    money_cols=(7, 9, 14, 15), widths=W)
        paid = [r for r in all_rows if r[9] == "paid"]
        _sheet_fill(wb.create_sheet("Paid - not vested"), hdr, paid,
                    money_cols=(7, 9, 14, 15), widths=W)

        splits = []
        for a in conn.execute("SELECT * FROM assignments WHERE removed_at IS NULL "
                              "ORDER BY created_at DESC").fetchall():
            s = conn.execute(
                """SELECT sub.customer_id, cu.name, sub.contract_value, st.state
                   FROM subscriptions sub
                   LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                   LEFT JOIN sub_status st USING (subscription_id)
                   WHERE sub.subscription_id = ?""",
                (a["subscription_id"],)).fetchone()
            splits.append([a["rep"], a["share"], a["type"], a["subscription_id"],
                           s["customer_id"] if s else "", (s and s["name"]) or "",
                           s["contract_value"] if s else "",
                           (s and s["state"]) or "", (a["created_at"] or "")[:10],
                           a["note"] or ""])
        _sheet_fill(wb.create_sheet("Splits & assignments"),
                    ["Rep", "Share", "Type", "Subscription #", "Account #",
                     "Customer", "Contract Value", "State", "Booked", "Note"],
                    splits, money_cols=(7,),
                    widths={1: 20, 2: 7, 3: 10, 4: 13, 5: 11, 6: 24, 7: 13,
                            8: 14, 9: 11, 10: 50})

        cols = {r[1] for r in conn.execute("PRAGMA table_info(upsells)")}
        ups = [[u["rep"], u["subscription_id"], u["amount"], u["acv_increase"],
                u["date"], u["d2d_rep"] or "",
                (u["d2d_host"] if "d2d_host" in cols else "") or "",
                u["note"] or ""]
               for u in conn.execute(
                   "SELECT * FROM upsells WHERE removed_at IS NULL ORDER BY date DESC")]
        _sheet_fill(wb.create_sheet("Upsell credits"),
                    ["ISA Rep", "Subscription #", "Credit $", "ACV increase",
                     "Date", "D2D Rep / Tech", "D2D Host (auto)", "Note"],
                    ups, money_cols=(3, 4),
                    widths={1: 20, 2: 13, 3: 10, 4: 12, 5: 11, 6: 22, 7: 24, 8: 55})
        return _xlsx_response(
            wb, f"commission_database_{datetime.now():%Y-%m-%d}.xlsx")
    finally:
        conn.close()


@app.get("/api/commissions/d2d.xlsx")
def api_commissions_d2d_xlsx(year: str):
    """Door-to-door year-end report: every D2D account upsold by inside sales,
    for reps carrying the given year tag (plus date-matched rows with no tag)."""
    import re as _re
    from openpyxl import Workbook
    if not _re.fullmatch(r"20\d\d", year or ""):
        return JSONResponse({"error": "year must be like 2026"}, status_code=400)
    conn = _comm_db()
    if conn is None:
        return JSONResponse({"error": "commissions database not found"}, status_code=404)
    try:
        hdr = ["D2D Rep (as tagged)", "Account #", "Customer", "Subscription #",
               "Plan", "Branch", "ISA Rep (upseller)", "Upsell Date",
               "Upsell Credit", "ACV Increase", "Note"]
        tagged, untagged = [], []
        for u in conn.execute("""
                SELECT u.rep AS isa, u.subscription_id, u.amount, u.acv_increase,
                       u.date, u.d2d_rep, u.d2d_host, COALESCE(u.note,'') AS note,
                       sub.customer_id, cu.name, sub.service_type, sub.office_id
                FROM upsells u
                LEFT JOIN subscriptions sub USING (subscription_id)
                LEFT JOIN customers cu ON cu.customer_id = sub.customer_id
                WHERE u.removed_at IS NULL
                  AND (u.d2d_host IS NOT NULL OR u.d2d_rep IS NOT NULL)
                ORDER BY COALESCE(u.d2d_host, u.d2d_rep), u.date"""):
            # d2d_host is the auto-detected year-tagged seller from the host
            # sub's soldBy; d2d_rep is the form era's free text (or, on tech
            # referrals, the TECH's name — branch-manager payroll, not D2D)
            label = u["d2d_host"] or u["d2d_rep"]
            row = [label, u["customer_id"] or "", u["name"] or "",
                   u["subscription_id"], u["service_type"] or "",
                   _BRANCHES.get(str(u["office_id"]), "?"),
                   u["isa"], u["date"], u["amount"], u["acv_increase"], u["note"]]
            m = _re.search(r"20\d\d", label or "")
            if m and m.group(0) == year:
                tagged.append(row)
            elif (not m and str(u["date"] or "").startswith(year)
                  and not u["d2d_host"]
                  and "tech ref" not in (u["note"] or "").lower()):
                untagged.append(row)
        wb = Workbook()
        ws = wb.active
        ws.title = f"D2D {year}"
        W = {1: 30, 2: 11, 3: 24, 4: 13, 5: 20, 6: 14, 7: 20, 8: 11, 9: 12,
             10: 12, 11: 55}
        _sheet_fill(ws, hdr, tagged or [["(no tagged rows)"]],
                    money_cols=(9, 10), widths=W)
        _sheet_fill(wb.create_sheet(f"No year tag ({year} dates)"), hdr,
                    untagged or [["(none)"]], money_cols=(9, 10), widths=W)
        return _xlsx_response(wb, f"d2d_upsells_{year}.xlsx")
    finally:
        conn.close()


# --------------- Payroll -> Submit Adjustment (structured intake) -----------
# The form writes a structured row into the commission engine's intake_queue
# and immediately spawns the engine's form_intake.py --queue-only, which
# verifies against the synced PestRoutes data and books it (or HOLDs it) —
# the same brain that processes the Google Form, minus all the free-text
# parsing. Nothing books without that verification step.
_INTAKE_TYPES = ("split", "outbound", "rescue", "upsell", "tech_upsell")


_INTAKE_DDL = """CREATE TABLE IF NOT EXISTS intake_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT DEFAULT (datetime('now','localtime')),
    submitted_by TEXT,
    type TEXT NOT NULL,
    account TEXT NOT NULL,
    agent1 TEXT, agent2 TEXT, tech TEXT, branch TEXT, acv REAL,
    sub_id_hint TEXT, note TEXT,
    status TEXT DEFAULT 'NEW',
    detail TEXT, est_amount REAL, rep TEXT, sub_id TEXT,
    processed_at TEXT, booked_ref TEXT)"""


def _comm_db_rw():
    """Read-write handle on the commission engine's DB (WAL, short writes)."""
    import sqlite3 as _sq
    db = Path(_settings.commissions_db)
    if not db.is_file():
        return None
    conn = _sq.connect(str(db), timeout=15)
    conn.row_factory = _sq.Row
    return conn


def _intake_roster() -> tuple[list[str], list[str]]:
    conn = connect(_settings.db_path)
    try:
        rows = conn.execute(
            "SELECT name, role FROM commission_roster ORDER BY name").fetchall()
        return ([r["name"] for r in rows if r["role"] == "inbound"],
                [r["name"] for r in rows if r["role"] == "outbound"])
    finally:
        conn.close()


def _comm_python() -> Path:
    """The commissions app's interpreter (its venv, else ours)."""
    import sys as _sys
    d = Path(_settings.commissions_app_dir)
    for py in (d / ".venv" / "bin" / "python3", d / ".venv" / "bin" / "python",
               d / "venv" / "bin" / "python3", d / "venv" / "bin" / "python",
               d / ".venv" / "Scripts" / "python.exe"):
        if py.exists():
            return py
    return Path(_sys.executable)


def _spawn_commissions(script_args: list[str], logname: str) -> bool:
    """Run a commissions-app script detached, output appended to logs/<name>.
    Best effort: the service may run with ProtectHome=read-only — if the logs
    dir isn't writable, spawn without a log rather than not at all."""
    import os as _os
    import subprocess as _sp
    d = Path(_settings.commissions_app_dir)
    script = d / "scripts" / script_args[0]
    if not script.is_file():
        return False
    kw = ({"start_new_session": True} if _os.name == "posix"
          else {"creationflags": 0x08000000})              # CREATE_NO_WINDOW
    try:
        try:
            logd = d / "logs"
            logd.mkdir(exist_ok=True)
            lf = open(logd / logname, "ab")
        except OSError:
            lf = _sp.DEVNULL
        try:
            _sp.Popen([str(_comm_python()), str(script)] + script_args[1:],
                      cwd=str(d), stdout=lf, stderr=lf, **kw)
        finally:
            if lf is not _sp.DEVNULL:
                lf.close()
        return True
    except OSError:
        logging.getLogger("isadash.commissions").exception(
            "spawn failed: %s", script_args)
        return False


def _run_intake_processor() -> bool:
    """Spawn form_intake.py --queue-only (detached) so a dashboard submission
    books within seconds. If it can't run, the row waits as NEW for the next
    pipeline run."""
    return _spawn_commissions(["form_intake.py", "--queue-only"], "intake.log")


@app.get("/api/commissions/intake/meta")
def api_commissions_intake_meta() -> dict:
    """Dropdown data for the Submit Adjustment form."""
    inbound, outbound = _intake_roster()
    as_of = None
    conn = _comm_db()
    if conn:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_sync'").fetchone()
            as_of = row["value"] if row else None
        finally:
            conn.close()
    return {"inbound": inbound, "outbound": outbound,
            "branches": sorted(_BRANCHES.values()), "data_as_of": as_of}


@app.get("/api/commissions/intake/account")
def api_commissions_intake_account(acct: str = "") -> dict:
    """Live account check for the form: the customer + every subscription,
    so the submitter confirms the right account (and can pick the sub)."""
    acct = (acct or "").strip()
    if not acct.isdigit():
        return {"error": "enter the PestRoutes account number (digits only)"}
    conn = _comm_db()
    if conn is None:
        return {"error": "commissions database not found"}
    try:
        cust = conn.execute(
            "SELECT customer_id, name, card_on_file, office_id, status "
            "FROM customers WHERE customer_id = ?", (acct,)).fetchone()
        # soldBy employee names live in the dashboard's own DB; a year-tagged
        # name (e.g. 'R- Max Barker 2026') marks a D2D-sold subscription
        import re as _re
        emp: dict[str, str] = {}
        dconn = connect(_settings.db_path)
        try:
            for r in dconn.execute(
                    "SELECT fr_employee_id, name FROM employees "
                    "WHERE name IS NOT NULL"):
                emp[str(r["fr_employee_id"])] = r["name"]
        finally:
            dconn.close()
        subs = []
        for r in conn.execute("""
                SELECT sub.subscription_id, sub.rep, sub.service_type,
                       sub.source, sub.contract_value, sub.date_added,
                       sub.active, sub.office_id, st.state, sub.sold_by
                FROM subscriptions sub
                LEFT JOIN sub_status st USING (subscription_id)
                WHERE sub.customer_id = ?
                ORDER BY sub.active DESC, sub.date_added DESC""", (acct,)):
            asg = conn.execute(
                "SELECT rep, share, type FROM assignments "
                "WHERE subscription_id = ? AND removed_at IS NULL",
                (r["subscription_id"],)).fetchall()
            ups = conn.execute(
                "SELECT COUNT(*) FROM upsells WHERE subscription_id = ? "
                "AND removed_at IS NULL", (r["subscription_id"],)).fetchone()[0]
            seller = emp.get(str(r["sold_by"] or ""), "")
            subs.append({
                "sub_id": r["subscription_id"], "rep": r["rep"],
                "service_type": r["service_type"], "source": r["source"],
                "cv": r["contract_value"],
                "sold": (r["date_added"] or "")[:10],
                "active": bool(r["active"]), "state": r["state"],
                "branch": _BRANCHES.get(str(r["office_id"]), ""),
                "d2d": seller if _re.search(r"\b20\d\d\b", seller) else "",
                "assignments": [f'{a["rep"]} {a["share"]:.0%} ({a["type"]})'
                                for a in asg],
                "upsells": ups})
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return {"tracked": bool(subs),
                "customer": ({"id": cust["customer_id"], "name": cust["name"],
                              "card": bool(cust["card_on_file"]),
                              "status": cust["status"],
                              "branch": _BRANCHES.get(str(cust["office_id"]), "")}
                             if cust else None),
                "subs": subs,
                "data_as_of": row["value"] if row else None}
    finally:
        conn.close()


@app.post("/api/commissions/intake")
def api_commissions_intake(body: dict, request: Request) -> dict:
    """Queue one structured adjustment and kick the booking processor."""
    typ = (body.get("type") or "").strip()
    acct = str(body.get("account") or "").strip()
    a1 = (body.get("agent1") or "").strip()
    a2 = (body.get("agent2") or "").strip()
    tech = (body.get("tech") or "").strip()
    branch = (body.get("branch") or "").strip()
    note = (body.get("note") or "").strip()
    hint = str(body.get("sub_id") or "").strip()
    raw_acv = body.get("acv")
    try:
        acv = float(raw_acv) if raw_acv not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "ACV change must be a number"}

    if typ not in _INTAKE_TYPES:
        return {"error": f"unknown adjustment type '{typ}'"}
    if not acct.isdigit():
        return {"error": "account # must be the numeric PestRoutes account number"}
    inbound, outbound = _intake_roster()
    if a1 not in inbound:
        return {"error": "pick the ISA rep from the roster dropdown"}
    if typ == "split" and (a2 not in inbound or a2 == a1):
        return {"error": "a split needs a second, different ISA rep"}
    if typ == "outbound" and a2 not in outbound:
        return {"error": "pick the outbound agent from the roster dropdown"}
    if typ in ("upsell", "tech_upsell") and (acv is None or acv <= 0):
        return {"error": "enter the ACV change (a positive dollar amount)"}
    if typ == "tech_upsell" and len(tech) < 2:
        return {"error": "tech referral needs the technician's name"}

    u = _user(request)
    by = (u and (u.get("name") or u.get("email"))) or "admin"
    conn = _comm_db_rw()
    if conn is None:
        return {"error": "commissions database not found"}
    try:
        conn.execute(_INTAKE_DDL)
        cur = conn.execute(
            """INSERT INTO intake_queue(submitted_by, type, account, agent1,
               agent2, tech, branch, acv, sub_id_hint, note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (by, typ, acct, a1, a2, tech, branch, acv, hint, note))
        conn.commit()
        qid = cur.lastrowid
    finally:
        conn.close()
    processing = _run_intake_processor()
    logging.getLogger("isadash.commissions").info(
        "intake #%s queued by %s: %s acct %s (%s%s)%s", qid, by, typ, acct, a1,
        f" + {a2}" if a2 else "", "" if processing else " — processor NOT started")
    return {"ok": True, "id": qid, "processing": processing}


@app.get("/api/commissions/intake/list")
def api_commissions_intake_list(limit: int = 30) -> dict:
    """Recent submissions with their booking outcome, newest first."""
    import sqlite3 as _sq
    conn = _comm_db()
    if conn is None:
        return {"rows": []}
    try:
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT id, submitted_at, submitted_by, type, account, agent1,
                   agent2, tech, branch, acv, status, detail, est_amount, rep,
                   sub_id FROM intake_queue ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit), 200)),))]
        except _sq.OperationalError:          # no submissions yet
            rows = []
        return {"rows": rows}
    finally:
        conn.close()


@app.post("/api/commissions/intake/delete")
def api_commissions_intake_delete(body: dict, request: Request) -> dict:
    """Delete one submission and take off whatever it booked. Runs the
    engine's form_intake.py --delete, which holds the same lock as booking,
    so a delete never races a submission being booked."""
    import json as _json
    import os as _os
    import subprocess as _sp
    try:
        qid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"error": "missing submission id"}
    u = _user(request)
    by = (u and (u.get("name") or u.get("email"))) or "admin"
    d = Path(_settings.commissions_app_dir)
    script = d / "scripts" / "form_intake.py"
    if not script.is_file():
        return {"error": "commissions app not found"}
    kw = {} if _os.name == "posix" else {"creationflags": 0x08000000}
    log = logging.getLogger("isadash.commissions")
    try:
        p = _sp.run([str(_comm_python()), str(script), "--delete", str(qid),
                     "--by", by], cwd=str(d), capture_output=True, text=True,
                    timeout=120, **kw)
    except _sp.TimeoutExpired:
        return {"error": "the adjustment processor is busy — try again in a minute"}
    out = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            out = _json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict):
        log.error("intake delete #%s failed (exit %s): %s", qid, p.returncode,
                  (p.stderr or p.stdout or "")[-800:])
        return {"error": "delete failed — nothing was changed"}
    log.info("intake #%s deleted by %s: %s", qid, by, out.get("message"))
    return out if out.get("ok") else {"error": out.get("message") or "delete failed"}


def _pid_alive(pid) -> bool:
    import os as _os
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if _os.name == "posix":
        return Path(f"/proc/{pid}").exists()
    import ctypes                              # NEVER os.kill(pid, 0) on
    h = ctypes.windll.kernel32.OpenProcess(    # Windows — that terminates it
        0x1000, False, pid)                    # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def _payroll_run_status() -> dict:
    import json as _json
    st = {}
    f = Path(_settings.commissions_app_dir) / "data" / "payroll_run.json"
    if f.is_file():
        try:
            st = _json.loads(f.read_text())
        except (OSError, ValueError):
            st = {}
    # a stale "running" (box rebooted mid-run) must not wedge the button
    if st.get("state") == "running" and not _pid_alive(st.get("pid")):
        st["state"] = "stale"
    return st


@app.get("/api/commissions/runpayroll/status")
def api_commissions_runpayroll_status() -> dict:
    """Button state: the latest closed period, whether its statement exists,
    and the live run status."""
    from datetime import date as _d, timedelta as _td
    closed_end, closed_start, next_end = _payroll_periods()
    stmt = _comm_reports_dir() / f"commission_statement_{closed_end}.xlsx"
    ran_at = None
    if stmt.is_file():
        ran_at = datetime.fromtimestamp(stmt.stat().st_mtime).strftime(
            "%a %m/%d %I:%M %p")
    return {"period_end": closed_end.isoformat(),
            "period_start": closed_start.isoformat(),
            "next_period_end": next_end.isoformat(),
            "statement_exists": stmt.is_file(), "ran_at": ran_at,
            "pre_go_live": _is_pre_go_live(closed_end),
            "live_from": _live_from().isoformat(),
            "payroll_monday": (closed_end + _td(days=2)).isoformat(),
            # its payroll Monday has passed: paid, locked (the commissions app
            # refuses the re-run too; later changes land in the next period)
            "paid_locked": stmt.is_file() and _d.today() > closed_end + _td(days=2),
            "run": _payroll_run_status()}


@app.post("/api/commissions/runpayroll")
def api_commissions_runpayroll(body: dict, request: Request) -> dict:
    """Lock adjustments & run payroll for the latest closed period: books
    every pending submission, pulls fresh PestRoutes data, and generates the
    statement. Detached; progress via the status endpoint."""
    closed_end, _, _ = _payroll_periods()
    period = (body or {}).get("period_end") or closed_end.isoformat()
    if period != closed_end.isoformat():
        return {"error": f"only the latest closed period ({closed_end}) can "
                         "be run from here"}
    if _is_pre_go_live(closed_end):
        return {"error": f"{closed_end} is a pre-go-live period — the manual "
                         "payroll sheet paid it, so it is not re-run from here. "
                         f"The first live period ({_live_from()} on) unlocks "
                         "the Sunday after it closes."}
    from datetime import date as _d, timedelta as _td
    stmt = _comm_reports_dir() / f"commission_statement_{closed_end}.xlsx"
    if stmt.is_file() and _d.today() > closed_end + _td(days=2):
        return {"error": f"{closed_end} was paid on {closed_end + _td(days=2)} "
                         "— locked; later changes land in the next payroll"}
    st = _payroll_run_status()
    if st.get("state") == "running":
        return {"error": "a payroll run is already in progress", "run": st}
    args = ["run_payroll.py", "--period-end", period]
    if (body or {}).get("skip_sync"):
        args.append("--skip-sync")
    if not _spawn_commissions(args, "payroll_run.log"):
        return {"error": "could not start the run — check COMMISSIONS_APP_DIR "
                         "on the server"}
    u = _user(request)
    logging.getLogger("isadash.commissions").info(
        "payroll run for %s started by %s", period,
        (u and (u.get("name") or u.get("email"))) or "admin")
    return {"ok": True, "period_end": period}
