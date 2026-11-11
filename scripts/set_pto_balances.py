"""ONE-OFF (timesheet go-live): set each agent's PTO balance to a known number
as of a date, e.g. the Homebase balances on the day the time clock goes live.

    python scripts/set_pto_balances.py <csv> --as-of 2026-10-25 [--write] [--replace]

The CSV needs a name column (Name / Agent / Employee) and a balance column
(PTO / Balance / Hours / PTO Balance); other columns are ignored. Names are
matched to the scheduling roster (exact, then first name / nickname).

For each agent it adds ONE ledger row dated --as-of (kind 'opening', ref
'golive:<as-of>') of whatever makes the balance at the end of that day equal
the given number — the history before it is kept, so every number can still
be explained line by line. Accrual continues from the day after --as-of;
rows already dated after it (accruals, approved PTO) stay on top. Balances
above 80 are kept as given (the 80-hour cap starts Jan 1, 2027).

Without --write it only prints current vs new. Re-running for the same date
is refused unless --replace (which swaps the earlier go-live row).
"""
import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import pto                        # noqa: E402
from isadash import schedule as sched          # noqa: E402
from isadash.config import load_settings       # noqa: E402
from isadash.store import connect              # noqa: E402

NAME_COLS = ("name", "agent", "rep", "employee", "employee name", "agent name")
BAL_COLS = ("pto", "balance", "pto balance", "hours", "pto hours", "available", "pto available")


def read_rows(src: str) -> list[tuple[str, float]]:
    with open(src, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    for i, r in enumerate(rows):                 # the header: the first row with a name column and a balance column
        low = [x.strip().lower() for x in r]
        ni = next((k for k, x in enumerate(low) if x in NAME_COLS), None)
        bi = next((k for k, x in enumerate(low) if x in BAL_COLS), None)
        if ni is not None and bi is not None:
            out = []
            for r2 in rows[i + 1:]:
                if len(r2) <= max(ni, bi) or not r2[ni].strip():
                    continue
                try:
                    out.append((r2[ni].strip(), float(r2[bi].replace(",", "").strip())))
                except ValueError:
                    print(f"  SKIP {r2[ni].strip()}: balance '{r2[bi]}' isn't a number")
            return out
    raise SystemExit("Couldn't find a header row with a name column and a PTO balance column.")


def main() -> None:
    args = sys.argv[1:]
    if not args or "--as-of" not in args:
        raise SystemExit(__doc__)
    src = args[0]
    as_of = date.fromisoformat(args[args.index("--as-of") + 1])
    write, replace = "--write" in args, "--replace" in args
    ref = f"golive:{as_of.isoformat()}"
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        pto.ensure(conn)
        from isadash.webapp import _roster_names
        roster = _roster_names(conn)
        seen, changes = set(), []
        for name, target in read_rows(src):
            full = name if name in roster else sched.match_name(name, roster)
            if not full:
                print(f"  NO MATCH  {name} ({target:g} h) - not on the roster; fix the name and re-run")
                continue
            if full in seen:
                print(f"  DUPLICATE {name} -> {full}; using the first row only")
                continue
            seen.add(full)
            prior = conn.execute("SELECT id, hours FROM pto_ledger WHERE agent = ? AND ref = ?", (full, ref)).fetchone()
            if prior and not replace:
                print(f"  ALREADY   {full}: a go-live row for {as_of} exists (add --replace to redo it)")
                continue
            now_bal = pto.balance(conn, full, as_of) - (prior["hours"] if prior else 0.0)
            delta = round(target - now_bal, 2)
            flag = "  (above 80 - kept; the cap starts Jan 1, 2027)" if target > pto.CAP_HOURS else ""
            print(f"  {full:<20} {now_bal:8.2f} -> {target:8.2f}  ({delta:+.2f}){flag}")
            changes.append((full, delta, prior))
        for a in sorted(set(roster) - seen):
            print(f"  NOT IN FILE {a}: left as is ({pto.balance(conn, a, as_of):.2f} h)")
        if not write:
            print(f"Dry run: {len(changes)} agent(s) would be set. Add --write to apply.")
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conn:
            for full, delta, prior in changes:
                if prior:
                    conn.execute("DELETE FROM pto_ledger WHERE id = ?", (prior["id"],))
                conn.execute("INSERT INTO pto_ledger (agent, day, kind, hours, note, ref, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (full, as_of.isoformat(), "opening", delta,
                              f"Go-live: balance set to the Homebase figure as of {as_of:%b} {as_of.day}, {as_of.year}", ref, "set_pto_balances", stamp))
        print(f"Wrote {len(changes)} go-live row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
