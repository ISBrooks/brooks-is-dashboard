"""Seed agent_map.voiceops_email by matching VoiceOps reps to our roster.

VoiceOps identifies reps by EMAIL, and they are personal addresses (gmail /
outlook / icloud) with no relationship to our Genesys ids or FieldRoutes rows,
so the only bridge is the rep's display name. We therefore match on name ONCE
here and store the email; everything afterwards keys on the stored email.

Run over a WIDE window (default 60 days): the reps endpoint omits anyone with
zero scorable calls in the period, so a one-week window silently misses anybody
who was on PTO that week.

Matching is deliberately conservative — an over-eager match would attribute one
rep's grades to another agent. Exact normalised full name wins; failing that a
UNIQUE first-name + last-initial match; anything ambiguous is reported, never
guessed.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import Settings                     # noqa: E402
from isadash.store import connect                       # noqa: E402
from isadash.voiceops import VoiceOpsClient, VoiceOpsError  # noqa: E402


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def key_fi(s: str | None) -> str:
    """first name + last initial, e.g. 'Tyler Goodman' -> 'tylerg'."""
    parts = [p for p in re.split(r"\s+", (s or "").strip()) if p]
    if not parts:
        return ""
    return norm(parts[0]) + (norm(parts[-1])[:1] if len(parts) > 1 else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--chunk-days", type=int, default=14,
                    help="window size per request batch; long windows can 504")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    st = Settings()
    if not st.voiceops_client_id or not st.voiceops_client_secret:
        print("VOICEOPS_CLIENT_ID / VOICEOPS_CLIENT_SECRET missing from .env")
        return 2

    end = date.today()
    start = end - timedelta(days=args.days)

    reps: dict[str, dict] = {}          # email -> rep object
    with VoiceOpsClient(st.voiceops_client_id, st.voiceops_client_secret) as c:
        cur = start
        while cur <= end:
            chunk_end = min(cur + timedelta(days=args.chunk_days - 1), end)
            try:
                rows = c.all_reps(cur.isoformat(), chunk_end.isoformat())
            except VoiceOpsError as e:
                print(f"  ! {cur}..{chunk_end}: {e}")
                cur = chunk_end + timedelta(days=1)
                continue
            for r in rows:
                rep = r.get("rep") or {}
                if rep.get("email"):
                    reps.setdefault(rep["email"], rep)
            print(f"  {cur}..{chunk_end}: {len(rows)} rows, {len(reps)} distinct reps so far")
            cur = chunk_end + timedelta(days=1)

    if not reps:
        print("no reps returned — aborting without writing")
        return 1

    by_norm: dict[str, list[dict]] = defaultdict(list)
    by_fi: dict[str, list[dict]] = defaultdict(list)
    for rep in reps.values():
        by_norm[norm(rep.get("name"))].append(rep)
        by_fi[key_fi(rep.get("name"))].append(rep)

    conn = connect(st.db_path)
    agents = list(conn.execute(
        "SELECT genesys_user_id, genesys_name, voiceops_email FROM agent_map "
        "WHERE scorecard = 1 ORDER BY genesys_name"))

    matched, already, unmatched, ambiguous = [], [], [], []
    for a in agents:
        name = a["genesys_name"]
        hit = by_norm.get(norm(name)) or []
        how = "exact"
        if len(hit) != 1:
            hit = by_fi.get(key_fi(name)) or []
            how = "first+initial"
        if len(hit) > 1:
            ambiguous.append((name, [h.get("email") for h in hit]))
            continue
        if not hit:
            unmatched.append(name)
            continue
        email = hit[0].get("email")
        if a["voiceops_email"] == email:
            already.append(name)
            continue
        matched.append((name, email, how))
        if not args.dry_run:
            conn.execute("UPDATE agent_map SET voiceops_email = ? WHERE genesys_user_id = ?",
                         (email, a["genesys_user_id"]))
    if not args.dry_run:
        conn.commit()

    print(f"\ndistinct VoiceOps reps seen: {len(reps)}   scorecard agents: {len(agents)}")
    print(f"newly mapped: {len(matched)}   already correct: {len(already)}")
    for n, e, how in matched:
        print(f"   + {n:<26} {e:<34} ({how})")
    if ambiguous:
        print(f"AMBIGUOUS (left unmapped, resolve by hand): {len(ambiguous)}")
        for n, es in ambiguous:
            print(f"   ? {n:<26} -> {es}")
    if unmatched:
        print(f"NO VOICEOPS REP FOUND ({len(unmatched)}): {', '.join(unmatched)}")
        print("   (expected for outbound-only reps who are never graded)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
