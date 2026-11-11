"""ONE-OFF: seed PTO opening balances + start dates from the Homebase export
"Weekly Metrics - Home base info Dallin.csv" (top block: Name, PTO, Start date).

    python scripts/seed_pto.py <csv> [--as-of 2026-09-30] [--write]

Writes agent_hr.start_date (only where empty) and one 'opening' ledger row
per agent dated the as-of day. Refuses to add a second opening row for an
agent that already has one. Names are matched to the roster (tracked agents
minus outbound); a few known spellings are mapped below.
"""
import csv
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import pto                        # noqa: E402
from isadash.config import load_settings       # noqa: E402
from isadash.store import connect              # noqa: E402

NAME_MAP = {"Jon Buckland": "Jean Buckland"}
DATE_FIX = {"Briggs Bowman": "2024-03-24"}     # the sheet says 3/24/34 (typo)


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def main() -> None:
    args = sys.argv[1:]
    src = args[0]
    as_of = date.fromisoformat(args[args.index("--as-of") + 1]) if "--as-of" in args else date(2026, 9, 30)
    write = "--write" in args
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        from isadash import timeclock
        timeclock.ensure(conn)
        pto.ensure(conn)
        skip = set(settings.outbound_agents)
        roster = [r[0] for r in conn.execute("SELECT genesys_name FROM agent_map WHERE genesys_name IS NOT NULL AND scorecard = 1")
                  if r[0] not in skip]
        rows = []
        with open(src, encoding="utf-8-sig", newline="") as f:
            for r in csv.reader(f):
                if not r or r[0].strip() in ("", "Name"):
                    if r and r[0].strip() == "":
                        break                   # the top block ends at the first blank row
                    continue
                name = NAME_MAP.get(r[0].strip(), r[0].strip())
                try:
                    bal = float(r[1])
                except (ValueError, IndexError):
                    bal = None
                start = DATE_FIX.get(name) or parse_date(r[2] if len(r) > 2 else "")
                rows.append((name, bal, start))
        for name, bal, start in rows:
            if name not in roster:
                print(f"SKIP {name}: not on the scheduling roster")
                continue
            hr = conn.execute("SELECT start_date FROM agent_hr WHERE agent = ?", (name,)).fetchone()
            has_open = conn.execute("SELECT 1 FROM pto_ledger WHERE agent = ? AND kind = 'opening'", (name,)).fetchone()
            print(f"{name:<20} balance {bal!s:>7}  start {start or '—':<10}"
                  f"{'  (start date already on file)' if hr and hr['start_date'] else ''}"
                  f"{'  (opening already seeded)' if has_open else ''}")
            if not write:
                continue
            with conn:
                if start and not (hr and hr["start_date"]):
                    conn.execute("INSERT INTO agent_hr (agent, start_date, updated_by, updated_at) VALUES (?,?,?,?) "
                                 "ON CONFLICT(agent) DO UPDATE SET start_date = excluded.start_date, updated_by = excluded.updated_by, "
                                 "updated_at = excluded.updated_at", (name, start, "seed_pto", datetime.utcnow().isoformat(timespec="seconds")))
                if bal is not None and not has_open:
                    conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (name, as_of.isoformat(), "opening", round(bal, 2),
                                  f"Opening balance from Homebase as of {as_of}", None, "seed_pto", datetime.utcnow().isoformat(timespec="seconds")))
        print("written" if write else "dry run — add --write to apply")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
