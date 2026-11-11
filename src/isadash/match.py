"""Conversion matching: a call converts if the caller's number shows up as a
FieldRoutes customer with a subscription SOLD BY THE SAME AGENT within the
conversion window after the call."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


def build_agent_map(genesys_users: dict[str, str], fr_employees: dict[str, str],
                    roster: list[str], overrides: dict | None = None) -> list[dict]:
    """Join Genesys users to FR employees by exact (case-insensitive) full name.

    FieldRoutes keeps a SEPARATE employee record per office for the same
    person, so one agent maps to a comma-joined SET of FR employee IDs.
    `overrides` maps a Genesys display name -> extra FR employee id(s)
    (str or list) for names the two systems spell differently."""
    overrides = overrides or {}
    fr_by_name: dict[str, set[str]] = {}
    for eid, name in fr_employees.items():
        fr_by_name.setdefault(name.lower(), set()).add(str(eid))
    roster_low = {r.lower() for r in roster}
    rows = []
    for gid, gname in genesys_users.items():
        key = gname.lower()
        ids = set(fr_by_name.get(key, set()))
        extra = overrides.get(gname)
        if extra:
            ids.update([extra] if isinstance(extra, str) else [str(x) for x in extra])
        rows.append({
            "genesys_user_id": gid,
            "genesys_name": gname,
            "fr_employee_id": ",".join(sorted(ids)) if ids else None,
            "fr_name": gname if ids else None,
            "on_roster": 1 if key in roster_low else 0,
        })
    return rows


def run_matcher(conn: sqlite3.Connection, window_days: int) -> int:
    """Mark calls converted. Idempotent: recomputes both columns every run.

    converted_sub_id     — sub in the AGENT'S OWN name (soldBy), credits the
                           earliest qualifying call. Gates nothing but proof
                           of a self-closed sale (the "hidden" term).
    converted_any_sub_id — a sub on the caller's phone within the window from
                           ANY seller. Conversion credit is seller-independent
                           (double-credit with door-to-door is deliberate);
                           only multiyear / contract value stay soldBy-gated.
    """
    conn.execute("UPDATE calls SET converted_sub_id = NULL, converted_any_sub_id = NULL")

    # phone -> [(sub_id, sold_by, date_added_utc)] — any seller. A sub only
    # counts as a sale if its INITIAL appointment is Serviced (Completed) or
    # still Pending — the company standard. No-Appointment / No-Show /
    # Cancelled-initial all mean the first service never got done, so they
    # aren't conversions. Later account cancellation is IGNORED (retention,
    # not sales); a serviced-then-cancelled sub stays a conversion.
    subs_by_phone: dict[str, list[tuple[str, str, str]]] = {}
    for r in conn.execute(
        "SELECT subscription_id, sold_by, date_added_utc, phone1_e164, phone2_e164 "
        "FROM subscriptions WHERE date_added_utc IS NOT NULL "
        "AND initial_status IN ('Completed', 'Pending')"
    ):
        for ph in (r["phone1_e164"], r["phone2_e164"]):
            if ph:
                subs_by_phone.setdefault(ph, []).append(
                    (r["subscription_id"], r["sold_by"], r["date_added_utc"])
                )

    fr_by_agent = {
        r["genesys_user_id"]: set((r["fr_employee_id"] or "").split(","))
        for r in conn.execute(
            "SELECT genesys_user_id, fr_employee_id FROM agent_map "
            "WHERE fr_employee_id IS NOT NULL")
    }

    window = timedelta(days=window_days)
    # earliest qualifying call wins each subscription (own-name credit)
    best: dict[str, tuple[str, str]] = {}  # sub_id -> (call_start, conversation_id)
    any_updates: list[tuple[str, str]] = []  # (sub_id, conversation_id)
    for c in conn.execute(
        "SELECT conversation_id, start_utc, ani_e164, agent_user_id FROM calls "
        "WHERE ani_e164 IS NOT NULL"
    ):
        call_dt = datetime.fromisoformat(c["start_utc"])
        fr_ids = fr_by_agent.get(c["agent_user_id"], set())
        any_hit = None
        for sub_id, sold_by, added in subs_by_phone.get(c["ani_e164"], []):
            added_dt = datetime.fromisoformat(added)
            if not (timedelta(0) <= added_dt - call_dt <= window):
                continue
            if any_hit is None:
                any_hit = sub_id
            if sold_by in fr_ids:
                cur = best.get(sub_id)
                if cur is None or c["start_utc"] < cur[0]:
                    best[sub_id] = (c["start_utc"], c["conversation_id"])
        if any_hit:
            any_updates.append((any_hit, c["conversation_id"]))

    conn.executemany(
        "UPDATE calls SET converted_any_sub_id = ? WHERE conversation_id = ?",
        any_updates)
    for sub_id, (_, conv_id) in best.items():
        conn.execute("UPDATE calls SET converted_sub_id = ? WHERE conversation_id = ?",
                     (sub_id, conv_id))
    conn.commit()
    log.info("Matcher: %d own-name conversions, %d any-seller call matches",
             len(best), len(any_updates))
    return len(best)
