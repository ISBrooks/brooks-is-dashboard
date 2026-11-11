"""ONE-OFF: import Saturday history from the IS Scheduling sheet's "SATRDAYS" tab
(downloaded as CSV) into the Saturday tracker (saturday_log, source 'sheet').

    python scripts/import_saturdays.py <csv> [--through 2026-10-17] [--map "Jake=Jake Smith"] [--write]

Reads the table whose header row starts with "Date" (6/14, 6/21, ... one column
per Saturday; the year is worked out so every column is a Saturday). Any filled
cell counts as worked ("1", "0.5"), except holiday / off markers ("4th", "x",
"off"). Only Saturdays on or before --through (default: today) are imported —
upcoming Saturdays count once their week is published in the builder.

Names are matched to the roster (exact, then first name / nickname); anything
left over must be mapped with --map or it is skipped. Without --write it only
prints what it would do. Re-running is harmless (existing rows are kept).
"""
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import saturdays, schedule        # noqa: E402
from isadash.config import load_settings       # noqa: E402
from isadash.store import connect              # noqa: E402

SKIP_VALUES = {"4th", "x", "off", "-"}


def header_dates(cells: list[str], today: date) -> list[date | None]:
    """'6/14', '6/21', ... -> dates, choosing the start year that makes every column a Saturday."""
    md = []
    for c in cells:
        c = c.strip()
        md.append(tuple(int(x) for x in c.split("/")[:2]) if c and "/" in c else None)
    for y0 in range(today.year, today.year - 4, -1):
        y, prev, out, ok = y0, 0, [], True
        for v in md:
            if v is None:
                out.append(None)
                continue
            m, d = v
            if m < prev:
                y += 1
            prev = m
            try:
                dt = date(y, m, d)
            except ValueError:
                ok = False
                break
            if dt.weekday() != 5:
                ok = False
                break
            out.append(dt)
        if ok:
            return out
    raise SystemExit("Couldn't find a year where every header date is a Saturday.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    src = args[0]
    through = date.fromisoformat(args[args.index("--through") + 1]) if "--through" in args else saturdays._today()
    manual = {}
    for i, a in enumerate(args):
        if a == "--map":
            k, v = args[i + 1].split("=", 1)
            manual[k.strip()] = v.strip()
    write = "--write" in args

    rows = list(csv.reader(open(src, encoding="utf-8-sig")))
    hi = next((i for i, r in enumerate(rows) if len(r) > 1 and r[1].strip().lower() == "date"), None)
    if hi is None:
        raise SystemExit("No header row starting with 'Date' found.")
    dates = header_dates(rows[hi][2:], saturdays._today())
    print(f"Columns: {min(d for d in dates if d)} .. {max(d for d in dates if d)}; importing through {through}")

    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        from isadash.webapp import _roster_names
        roster = _roster_names(conn)
        entries, unmatched = [], {}
        for r in rows[hi + 1:]:
            name = (r[1] if len(r) > 1 else "").strip()
            if name.lower().startswith("weekly total"):
                break
            vals = [(dates[i], v.strip()) for i, v in enumerate(r[2:]) if i < len(dates) and dates[i] and v.strip()]
            worked = [d for d, v in vals if v.lower() not in SKIP_VALUES and d <= through]
            if not worked:
                continue
            if not name:
                print(f"  SKIP  (row {rows.index(r) + 1} has no name) - {len(worked)} Saturday(s)")
                continue
            full = manual.get(name) or (name if name in roster else None) or schedule.match_name(name, roster)
            if not full:
                unmatched[name] = len(worked)
                continue
            last = max(worked)
            print(f"  {name:20s} -> {full:22s} {len(worked):3d} Saturdays, last {last}")
            entries += [(full, d.isoformat()) for d in worked]
        for name, n in unmatched.items():
            print(f"  NO MATCH  {name:20s} {n:3d} Saturdays - add --map \"{name}=Full Name\" to keep them")
        if write:
            n = saturdays.import_history(conn, "import:sheet", entries)
            print(f"Wrote {n} new row(s) ({len(entries) - n} already there).")
        else:
            print(f"Dry run: {len(entries)} row(s) would be imported. Add --write to save.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
