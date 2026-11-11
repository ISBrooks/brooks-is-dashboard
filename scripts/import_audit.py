"""Import the audit team's master sheet into the dashboard DB.

Accepts a CSV/XLSX file, or a Google Sheets share link ("anyone with the
link can view"), which is fetched via the sheet's CSV export URL.

  python scripts/import_audit.py --file path/to/audit.csv
  python scripts/import_audit.py --url "https://docs.google.com/spreadsheets/d/<ID>/edit..."

Expected columns (names are matched loosely, case-insensitive):
  date  |  agent (or name/rep)  |  qualified (or qualified calls/qual)  |  sales (or closed/sold)

Any extra columns are ignored. Rows with unparseable dates or numbers are
reported and skipped, never silently dropped.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.store import connect, upsert_audit_daily

COL_ALIASES = {
    "date": {"date", "day", "call date"},
    "agent": {"agent", "name", "rep", "agent name", "employee", "isa"},
    "qualified": {"qualified", "qualified calls", "qual", "qual calls",
                  "qualified leads", "# qualified"},
    "sales": {"sales", "closed", "sold", "closes", "# sales", "sales count"},
}


def match_columns(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    low = [h.strip().lower() for h in header]
    for want, aliases in COL_ALIASES.items():
        for i, h in enumerate(low):
            if h in aliases:
                out[want] = i
                break
    missing = [w for w in COL_ALIASES if w not in out]
    if missing:
        raise SystemExit(
            f"Could not find column(s) {missing} in header {header}.\n"
            f"Recognized aliases: { {k: sorted(v) for k, v in COL_ALIASES.items()} }"
        )
    return out


def parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def read_rows_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]


def read_rows_xlsx(path: Path) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise SystemExit("XLSX import needs openpyxl:  python -m pip install openpyxl")
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for r in ws.iter_rows(values_only=True):
        vals = ["" if v is None else
                (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v))
                for v in r]
        if any(v.strip() for v in vals):
            rows.append(vals)
    return rows


def fetch_google_sheet(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        raise SystemExit("That doesn't look like a Google Sheets URL.")
    gid = "0"
    g = re.search(r"[#&?]gid=(\d+)", url)
    if g:
        gid = g.group(1)
    export = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}"
    import httpx
    resp = httpx.get(export, follow_redirects=True, timeout=30)
    if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
        raise SystemExit(
            f"Could not fetch the sheet (HTTP {resp.status_code}). Make sure link "
            "sharing is set to 'Anyone with the link – Viewer'."
        )
    return resp.text


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="CSV or XLSX file")
    src.add_argument("--url", help="Google Sheets share link")
    args = ap.parse_args()

    if args.url:
        rows = read_rows_csv(fetch_google_sheet(args.url))
    else:
        p = Path(args.file)
        if not p.exists():
            raise SystemExit(f"No such file: {p}")
        rows = read_rows_xlsx(p) if p.suffix.lower() in (".xlsx", ".xlsm") \
            else read_rows_csv(p.read_text(encoding="utf-8-sig"))

    if len(rows) < 2:
        raise SystemExit("Sheet has no data rows.")
    cols = match_columns(rows[0])

    good: list[dict] = []
    bad: list[str] = []
    for i, row in enumerate(rows[1:], start=2):
        try:
            date = parse_date(row[cols["date"]])
            agent = row[cols["agent"]].strip()
            qual = int(float(row[cols["qualified"]] or 0))
            sales = int(float(row[cols["sales"]] or 0))
        except (ValueError, IndexError):
            date = agent = None  # fall through to bad
            qual = sales = 0
        if not date or not agent:
            bad.append(f"row {i}: {row[:4]}")
            continue
        good.append({"date": date, "agent": agent, "qualified": qual, "sales": sales})

    s = load_settings()
    conn = connect(s.db_path)
    upsert_audit_daily(conn, good)
    n = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM audit_daily").fetchone()
    print(f"Imported {len(good)} rows. audit_daily now has {n[0]} rows "
          f"covering {n[1]} .. {n[2]}.")
    if bad:
        print(f"SKIPPED {len(bad)} unparseable rows:")
        for b in bad[:10]:
            print("  " + b)


if __name__ == "__main__":
    main()
