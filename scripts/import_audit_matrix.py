"""Import the audit team's MASTER MATRIX sheet.

Layout (confirmed 2026-07-07 against the live sheet):
  - header row: 'sa' then agent first names, one name per COLUMN PAIR
    (left cell = sales, right cell = qualified; sales <= qualified)
  - one row per day, col0 = 'M/D' with NO year
  - weekly-total rows have an empty col0 -> skipped
  - a handful of top rows have a bare day number in col0 -> month inferred
    from the next dated row
  - years are reconstructed by walking the M/D sequence and bumping the year
    whenever the month decreases, anchored so the last row lands on the most
    recent plausible date (<= today)

Usage:
  python scripts/import_audit_matrix.py --url "https://docs.google.com/spreadsheets/d/<ID>/..."
  python scripts/import_audit_matrix.py --file audit.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.store import connect, upsert_audit_daily

# sheet first-name -> Genesys/roster full name. Sheet names not listed here
# are imported verbatim (former agents etc.) — they just won't join the
# system-side comparison.
NAME_MAP = {
    "Aaron": "Aaron Darrington", "Alex": "Alex Dossett", "Andrew": "Andrew Watts", "Austin": "Austin Tandy",
    "Ben": "Benjamin Pope", "Brad": "Brad Larsen", "Briggs": "Briggs Bowman",
    "Cade C": "Cade Camac", "Colby": "Colby Westenskow", "David S": "David Stitcher",
    "Daynen": "Daynen Hinckley", "Dorian": "Dorian Cook", "Jase": "Jase Kanistanaux",
    "Jean": "Jean Buckland", "Jesse": "Jesse Rhodes", "Jodie": "Jodie Clevenger",
    "Mason": "Mason Curtis", "Micah": "Micah De Guzman", "Nate": "Nate Smith",
    "Perry": "Perry Valencia",
    "Seyj": "Seyj Dungao", "Tyler G": "Tyler Goodman", "Will": "Will Stitcher",
    "Wyatt": "Wyatt Draper", "Zach": "Zach Stoker",
}


def fetch_google_sheet(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        raise SystemExit("Not a Google Sheets URL.")
    import httpx
    resp = httpx.get(
        f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv",
        follow_redirects=True, timeout=30)
    if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
        raise SystemExit(f"Fetch failed (HTTP {resp.status_code}). Is link sharing on?")
    return resp.text


def parse_int(cell: str) -> int | None:
    cell = (cell or "").strip()
    if not cell:
        return None
    try:
        return int(float(cell))
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--file")
    args = ap.parse_args()

    text = fetch_google_sheet(args.url) if args.url \
        else Path(args.file).read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]

    # (name, sales_col, qual_col) for every named column pair
    agents = [(header[i].strip(), i, i + 1)
              for i in range(1, len(header) - 1) if header[i].strip()]
    print(f"{len(agents)} agent columns: {[a[0] for a in agents]}")

    # --- pass 1: month/day per data row (totals rows skipped) --------------
    md_rows: list[tuple[int, int, int]] = []  # (row_idx, month, day)
    pending_bare: list[tuple[int, int]] = []  # (row_idx, day) awaiting a month
    for idx, row in enumerate(rows[1:], start=1):
        c0 = (row[0] if row else "").strip()
        if not c0:
            continue  # weekly totals / spacer
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", c0)
        if m:
            mo, dy = int(m.group(1)), int(m.group(2))
            for bidx, bday in pending_bare:  # bare days belong to this month
                if bday <= dy or True:
                    md_rows.append((bidx, mo, bday))
            pending_bare.clear()
            md_rows.append((idx, mo, dy))
        elif c0.isdigit() and 1 <= int(c0) <= 31:
            pending_bare.append((idx, int(c0)))
    if pending_bare:
        print(f"WARNING: {len(pending_bare)} bare-day rows at the end had no "
              "month context — skipped.")
    md_rows.sort(key=lambda t: t[0])

    # --- pass 2: assign years (month decreasing => new year) ---------------
    # Anchor on the last row that actually CONTAINS DATA — the sheet has
    # pre-created rows for future dates, which must not drag the anchor.
    def row_has_data(row: list[str]) -> bool:
        return any(parse_int(row[c]) is not None
                   for _, si, qi in agents for c in (si, qi) if c < len(row))

    anchor_i = max((k for k, (idx, _, _) in enumerate(md_rows)
                    if row_has_data(rows[idx])), default=len(md_rows) - 1)
    boundaries_to_anchor = sum(
        1 for a, b in zip(md_rows[:anchor_i], md_rows[1:anchor_i + 1]) if b[1] < a[1])
    today = date.today()
    anc_mo, anc_dy = md_rows[anchor_i][1], md_rows[anchor_i][2]
    anchor_year = today.year if (anc_mo, anc_dy) <= (today.month, today.day) \
        else today.year - 1
    start_year = anchor_year - boundaries_to_anchor
    print(f"date span: starts {md_rows[0][1]}/{md_rows[0][2]}/{start_year}, "
          f"last data row {anc_mo}/{anc_dy}/{anchor_year}")

    dates: dict[int, str] = {}
    year, prev_mo = start_year, md_rows[0][1]
    bad_dates = 0
    for idx, mo, dy in md_rows:
        if mo < prev_mo:
            year += 1
        prev_mo = mo
        try:
            dates[idx] = date(year, mo, dy).isoformat()
        except ValueError:  # e.g. 2/29 in a non-leap year — data-entry quirk
            bad_dates += 1
    if bad_dates:
        print(f"WARNING: {bad_dates} impossible dates skipped (e.g. 2/29 off-leap-year)")

    # --- auto-map sheet names to tracked agents ---------------------------
    # NAME_MAP is the manual override; anything not in it is auto-resolved
    # against the Settings-tracked (scorecard) agents: exact full name, else
    # an unambiguous first-name match ("Kaeden" -> "Kaeden Hewitt"), else
    # first name + last initial ("Tyler G" -> "Tyler Goodman"). So a newly
    # added agent maps the moment the sheet has their column — no code edit.
    s = load_settings()
    conn = connect(s.db_path)
    tracked = [r[0] for r in conn.execute(
        "SELECT genesys_name FROM agent_map WHERE scorecard = 1 AND genesys_name IS NOT NULL")]
    full_names = {t.lower(): t for t in tracked}
    auto_hits: dict[str, str] = {}

    def resolve(name: str) -> str:
        if name in NAME_MAP:
            return NAME_MAP[name]
        low = name.strip().lower()
        if low in full_names:
            return full_names[low]
        toks = name.strip().split()
        first = toks[0].lower() if toks else ""
        initial = toks[1][0].lower() if len(toks) > 1 and len(toks[1]) <= 2 else None
        cands = [t for t in tracked
                 if t.split()[0].lower() == first
                 and (initial is None or (len(t.split()) > 1
                      and t.split()[-1][0].lower() == initial))]
        if len(cands) == 1:
            auto_hits[name] = cands[0]
            return cands[0]
        return name

    # --- pass 3: collect cells --------------------------------------------
    acc: dict[tuple[str, str], list[int]] = {}  # (date, agent) -> [sales, qual]
    partial = 0
    resolved = {name: resolve(name) for name, _si, _qi in agents}
    for idx, row in enumerate(rows[1:], start=1):
        d = dates.get(idx)
        if not d:
            continue
        for name, si, qi in agents:
            sales = parse_int(row[si]) if si < len(row) else None
            qual = parse_int(row[qi]) if qi < len(row) else None
            if sales is None and qual is None:
                continue
            if sales is None or qual is None:
                partial += 1
            agent = resolved[name]
            cur = acc.setdefault((d, agent), [0, 0])
            cur[0] += sales or 0
            cur[1] += qual or 0

    if auto_hits:
        print("Auto-mapped sheet columns:",
              ", ".join(f"{k} -> {v}" for k, v in sorted(auto_hits.items())))
    rows_out = [{"date": d, "agent": a, "sales": v[0], "qualified": v[1]}
                for (d, a), v in acc.items()]
    conn.execute("DELETE FROM audit_daily")  # full re-import, sheet is truth
    upsert_audit_daily(conn, rows_out)
    n = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM audit_daily").fetchone()
    print(f"Imported {len(rows_out)} agent-day records "
          f"({partial} half-filled cells treated as 0-for-missing).")
    print(f"audit_daily: {n[0]} rows, {n[1]} .. {n[2]}")
    known = set(tracked) | set(NAME_MAP.values())
    mapped = {a for (_, a) in acc if a in known}
    unmapped = sorted({a for (_, a) in acc} - mapped)
    print(f"Roster-matched agents: {len(mapped)}; unmatched sheet names "
          f"(kept, but no system comparison): {unmapped}")


if __name__ == "__main__":
    main()
