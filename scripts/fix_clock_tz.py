"""ONE-OFF (2026-10-20): re-anchor punches recorded while the time clock read
shift times as Pacific. The team works Mountain time, so "9-5" now means
9 AM-5 PM Mountain (periods.CLOCK_TZ).

For every punch with a shift: recompute shift_start / shift_end from the shift
text on the punch's Mountain day. An AUTO clock-out that was closed at the old
(Pacific) shift end is moved to the new shift end, with a punch_audit row.
Manual / edited punches keep their in/out times (those were real clicks).

    python scripts/fix_clock_tz.py            # dry run: prints what would change
    python scripts/fix_clock_tz.py --write    # apply
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import schedule as sched          # noqa: E402
from isadash import timeclock                  # noqa: E402
from isadash.config import load_settings       # noqa: E402
from isadash.periods import CLOCK_TZ           # noqa: E402
from isadash.store import connect              # noqa: E402

WHO = "system:timezone-fix"
REASON = "Shift times are Mountain, not Pacific: auto clock-out moved to the real shift end"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    write = "--write" in sys.argv
    conn = connect(load_settings().db_path)
    try:
        timeclock.ensure(conn)
        now = iso(datetime.now(timezone.utc))
        rows = conn.execute("SELECT * FROM punches WHERE shift IS NOT NULL ORDER BY in_at").fetchall()
        changes = 0
        for r in rows:
            in_at = datetime.fromisoformat(r["in_at"])
            day = in_at.astimezone(CLOCK_TZ).date()
            segs = sched.shift_segments(r["shift"])
            if not segs:
                continue
            midnight = datetime(day.year, day.month, day.day, tzinfo=CLOCK_TZ)
            new_start = iso(midnight + timedelta(minutes=segs[0][0]))
            new_end = iso(midnight + timedelta(minutes=segs[-1][1]))
            moved_out = None
            if r["out_kind"] == "auto" and r["out_at"] and r["shift_end"] and r["out_at"] == r["shift_end"] and new_end != r["out_at"]:
                moved_out = max(new_end, r["in_at"])
            if new_start == r["shift_start"] and new_end == r["shift_end"] and not moved_out:
                continue
            changes += 1
            print(f"punch {r['id']} ({r['agent']}, {day}, shift {r['shift']}): "
                  f"shift_end {r['shift_end']} -> {new_end}"
                  + (f"; auto clock-out {r['out_at']} -> {moved_out}" if moved_out else ""))
            if write:
                with conn:
                    conn.execute("UPDATE punches SET shift_start = ?, shift_end = ? WHERE id = ?", (new_start, new_end, r["id"]))
                    if moved_out:
                        conn.execute("UPDATE punches SET out_at = ? WHERE id = ?", (moved_out, r["id"]))
                        conn.execute("INSERT INTO punch_audit (punch_id, changed_by, changed_at, field, old, new, reason) "
                                     "VALUES (?,?,?,?,?,?,?)", (r["id"], WHO, now, "out_at", r["out_at"], moved_out, REASON))
        print(f"{changes} punch(es) {'updated' if write else 'would change (dry run; add --write)'}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
