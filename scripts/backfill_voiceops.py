"""Backfill VoiceOps quality scores into voiceops_scores.

Fills the weekly Sunday-Saturday periods the Home trend chart reads, plus the
trailing windows the Home cards use. Safe to re-run: rows upsert, and by
default already-cached periods are skipped (use --refresh to re-pull, which is
what you want for recent weeks since grading trickles in after the calls).
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import Settings                              # noqa: E402
from isadash.periods import PACIFIC                              # noqa: E402
from isadash.store import connect                                # noqa: E402
from isadash.voiceops import (VoiceOpsClient, cached_scores,     # noqa: E402
                              mapped_roster, pacific_weeks, weighted_quality)


def trailing(days: int, anchor: date, offset: int = 0) -> tuple[str, str]:
    """Whole Pacific days ending YESTERDAY, matching the Home cards exactly.

    offset shifts the window back by that many days, which is how the PREVIOUS
    period is warmed — without it the Home bubbles have nothing to compare
    against and show no delta, unlike every other card on the page.
    """
    end = anchor - timedelta(days=1 + offset)
    return ((end - timedelta(days=days - 1)).isoformat(), end.isoformat())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=13)
    ap.add_argument("--days", type=int, default=7,
                    help="single days to warm for the Home daily drop-down")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull periods already cached")
    ap.add_argument("--windows", action="store_true", default=True,
                    help="also warm the trailing 30/7-day Home windows")
    args = ap.parse_args()

    st = Settings()
    if not st.voiceops_client_id or not st.voiceops_client_secret:
        print("VOICEOPS_CLIENT_ID / VOICEOPS_CLIENT_SECRET missing from .env")
        return 2

    conn = connect(st.db_path)
    roster = mapped_roster(conn)
    if not roster:
        print("no agents have voiceops_email — run scripts/seed_voiceops_emails.py first")
        return 1
    print(f"{len(roster)} mapped agents")

    # Pacific explicitly, not date.today(): the server's local date only matches
    # the Pacific windows the sync and Home use because the host happens to be
    # set to Pacific — don't let the backfill silently depend on that setting
    today = datetime.now(PACIFIC).date()
    periods = list(pacific_weeks(args.weeks, today))
    if args.windows:
        periods += [trailing(30, today), trailing(7, today),
                    trailing(30, today, 30), trailing(7, today, 7)]
    periods += [((today - timedelta(days=i)).isoformat(),) * 2
                for i in range(args.days, 0, -1)]

    t0 = time.time()
    with VoiceOpsClient(st.voiceops_client_id, st.voiceops_client_secret) as c:
        for start_day, end_day in periods:
            have = conn.execute(
                "SELECT COUNT(*) FROM voiceops_scores WHERE start_day=? AND end_day=?",
                (start_day, end_day)).fetchone()[0]
            if have >= len(roster) and not args.refresh:
                print(f"  {start_day}..{end_day}  cached ({have} rows) — skipped")
                continue
            scores = cached_scores(conn, c, start_day, end_day, refresh=args.refresh)
            got = [s for s in scores.values() if s.get("quality_score") is not None]
            print(f"  {start_day}..{end_day}  {len(scores)} agents, "
                  f"{len(got)} scored, team quality "
                  f"{weighted_quality(list(scores.values()))}")
    print(f"done in {time.time() - t0:.0f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
