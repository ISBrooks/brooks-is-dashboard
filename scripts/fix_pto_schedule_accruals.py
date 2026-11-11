"""ONE-OFF (2026-10-26): remove PTO accrual rows that were earned on SCHEDULED
hours (agent_hours source 'schedule', the frozen schedule before 2026-10-05).
PTO is earned only on hours actually worked; pto.accrue no longer reads those
rows, so they never come back.

    python scripts/fix_pto_schedule_accruals.py           # dry run
    python scripts/fix_pto_schedule_accruals.py --write   # delete them
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings       # noqa: E402
from isadash.store import connect              # noqa: E402

Q = """SELECT l.id, l.agent, l.day, l.hours FROM pto_ledger l
       JOIN agent_hours a ON a.agent = l.agent AND l.ref = 'hours:' || a.day
       WHERE l.kind = 'accrual' AND a.source = 'schedule' ORDER BY l.agent, l.day"""


def main() -> None:
    write = "--write" in sys.argv
    conn = connect(load_settings().db_path)
    try:
        rows = conn.execute(Q).fetchall()
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r["agent"], []).append(r)
        for a, rs in by.items():
            print(f"  {a:20s} {len(rs)} day(s) {rs[0]['day']}..{rs[-1]['day']}  -{sum(x['hours'] for x in rs):.2f} h")
        print(f"{len(rows)} row(s), {sum(r['hours'] for r in rows):.2f} h across {len(by)} agent(s)")
        if write and rows:
            with conn:
                conn.executemany("DELETE FROM pto_ledger WHERE id = ?", [(r["id"],) for r in rows])
            print("deleted")
        elif not write:
            print("dry run - add --write to delete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
