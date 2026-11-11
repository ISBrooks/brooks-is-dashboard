"""One-off catch-up of Podium conversations for the Master Metric.

The morning sync only ever collects the day before. This fills in older days
in a single pass (every conversation active since --start is read once), at a
gentle pace so the FieldRoutes->Podium sync keeps most of Podium's allowance.
Nothing is saved until the whole pass has finished.

  .venv/bin/python scripts/backfill_podium_conversations.py --start 2026-09-27 --end 2026-09-30

While it runs it holds data/podium_backfill.pid; the morning sync sees that
and leaves its own Podium step for the next morning (it catches up by itself).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from isadash import podium  # noqa: E402
from isadash.config import load_settings  # noqa: E402
from isadash.store import connect  # noqa: E402

PID_FILE = ROOT / "data" / "podium_backfill.pid"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD (Mountain)")
    ap.add_argument("--end", required=True, help="last day, YYYY-MM-DD (Mountain)")
    ap.add_argument("--gap", type=float, default=0.5, help="seconds between requests (0.5 = 120/min)")
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if end < start:
        raise SystemExit("--end is before --start")
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    settings = load_settings()
    PID_FILE.write_text(str(os.getpid()))
    t = time.time()
    try:
        conn = connect(settings.db_path)
        try:
            print(f"collecting {len(days)} days {start} .. {end} at {60 / a.gap:.0f} requests/min", flush=True)
            r = podium.collect_conversations(
                conn, settings, days, gap=a.gap,
                progress=lambda n: print(f"  {n} conversations read, {time.time() - t:.0f}s", flush=True))
        finally:
            conn.close()
        print("DONE", r, f"{time.time() - t:.0f}s", flush=True)
        return 0 if r.get("ok") else 1
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
