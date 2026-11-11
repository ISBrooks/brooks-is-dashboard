"""Scheduled sync entrypoint (systemd timer). Runs the same worker the Sync
button uses, but synchronously, and exits non-zero on failure so the timer
logs a clean success/failure in the journal.

  python scripts/nightly_sync.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.sync import STATE, _sync_worker


def podium_conversations(settings) -> str:
    """Morning-only: collect yesterday's Podium conversations for the Master
    Metric. Deliberately NOT part of the Sync button's worker — it is ~4,000
    requests, so it runs once a day here, while the FieldRoutes->Podium sync
    is in its overnight quiet hours. Never fails the nightly."""
    from isadash import podium
    from isadash.store import connect
    if not podium.configured(settings):
        return "skipped (Podium not connected)"
    # a one-off backfill is using Podium's request allowance right now; this
    # day is picked up by tomorrow's run (it re-checks the last three days)
    pid_file = Path(__file__).resolve().parents[1] / "data" / "podium_backfill.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return "skipped (a Podium backfill is running)"
        except (OSError, ValueError):
            pass                                   # stale file from a crashed run
    conn = connect(settings.db_path)
    try:
        podium.sync(conn, settings, force=True, max_seconds=600)   # users + calls first
        days = podium.missing_recent_days(conn)
        if not days:
            return "already collected"
        r = podium.collect_conversations(conn, settings, days)
        return (f"{', '.join(r['days'])}: {r['scanned']} conversations read, "
                f"{r['rows']} agent-conversation rows"
                + (f", {r['skipped']} skipped" if r["skipped"] else ""))
    finally:
        conn.close()


def homebase_hours(settings) -> str:
    """Morning: refresh worked hours for the Master Metric and PTO from the
    dashboard time clock (from 2026-10-26). The Homebase sheet is frozen
    through 2026-10-25 and no longer read."""
    from isadash import hours
    from isadash.store import connect
    conn = connect(settings.db_path)
    try:
        n = hours.sync_clock(conn)
        return f"time clock: {n} agent-days on file since {hours.CLOCK_FROM} (Homebase frozen through {hours.HOMEBASE_THROUGH})"
    finally:
        conn.close()


def pto_accrual(settings) -> str:
    """Morning: add PTO accrual rows for every day of hours now on file."""
    from isadash import pto
    from isadash.store import connect
    conn = connect(settings.db_path)
    try:
        skip = set(settings.outbound_agents)
        names = [r[0] for r in conn.execute(
            "SELECT genesys_name FROM agent_map WHERE scorecard = 1 AND genesys_name IS NOT NULL") if r[0] not in skip]
        added = pto.accrue(conn, names)
        return f"{len(added)} agents accrued {sum(added.values()):.2f} h" if added else "nothing new to accrue"
    finally:
        conn.close()


if __name__ == "__main__":
    settings = load_settings()
    _sync_worker(settings)
    for s in STATE.get("steps", []):
        print(f"  - {s['name']}: {s.get('note', '') if s.get('done') else 'DID NOT FINISH'}")
    try:
        print("  - Worked hours:", homebase_hours(settings))
    except Exception as e:  # noqa: BLE001
        print(f"  - Worked hours: FAILED ({type(e).__name__}: {str(e)[:160]})")
    try:
        print("  - PTO accrual:", pto_accrual(settings))
    except Exception as e:  # noqa: BLE001
        print(f"  - PTO accrual: FAILED ({type(e).__name__}: {str(e)[:160]})")
    try:
        print("  - Podium conversations:", podium_conversations(settings))
    except Exception as e:  # noqa: BLE001 — one tab's bookkeeping must not fail the sync
        print(f"  - Podium conversations: FAILED ({type(e).__name__}: {str(e)[:160]})")
    if STATE.get("error"):
        print(f"sync FAILED: {STATE['error']}", file=sys.stderr)
        sys.exit(1)
    print("sync ok")
