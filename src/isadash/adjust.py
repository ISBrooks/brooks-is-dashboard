"""Adjusted ("gold standard") conversion terms.

Per (agent, caller-phone) in a window, four flags:
  cf   — agent dispositioned some call closed/follow-up (pitched; the call
         is in the audit team's qualified base)
  own  — a subscription in the AGENT'S name landed within the window
  any  — a subscription from ANY seller landed within the window
  unq  — agent dispositioned the caller unqualified

Derived, MUTUALLY EXCLUSIVE per (agent, phone) — a caller lands in at most
ONE adjustment bucket, so nothing is double counted:
  CONVERTED  = own OR (cf AND any)      -> numerator credit (seller-agnostic
               when pitched; own-name sales always count)
  HIDDEN     = own AND NOT cf           -> +1 denominator (pitch missing from
               the audit base, sale proves it was qualified)
  LEAKED     = unq AND NOT cf AND NOT own AND any
                                        -> +1 denominator (provably real
               prospect, bought elsewhere)
  CC_LEAKED  = NOT unq AND NOT cf AND NOT own AND any  (report only)

adjusted_conv = CONVERTED / (audited qualified + HIDDEN + LEAKED)

The "__TEAM__" entry dedupes by PHONE across agents: each unique caller
contributes at most one hidden/leaked unit to the team denominator, and a
caller who converted anywhere is never simultaneously counted as leaked.
"""
from __future__ import annotations

import sqlite3


def _flags(conn: sqlite3.Connection, start_utc: str, end_utc: str) -> dict:
    """(agent, phone) -> {cf, own, any, unq, first, wrapup}"""
    flags: dict[tuple, dict] = {}
    for r in conn.execute(
        """SELECT m.genesys_name agent, c.ani_e164 ph, c.dispo_group,
                  c.wrapup_name, c.converted_sub_id, c.converted_any_sub_id,
                  c.start_utc
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
             AND c.start_utc >= ? AND c.start_utc < ?""",
        (start_utc, end_utc),
    ):
        f = flags.setdefault((r["agent"], r["ph"]), {
            "cf": False, "own": False, "any": False, "unq": False,
            "first": r["start_utc"], "wrapup": r["wrapup_name"],
            "any_sub": None,
        })
        if r["dispo_group"] in ("closed", "followup"):
            f["cf"] = True
        if r["dispo_group"] == "unqualified":
            f["unq"] = True
        if r["converted_sub_id"]:
            f["own"] = True
        if r["converted_any_sub_id"]:
            f["any"] = True
            f["any_sub"] = f["any_sub"] or r["converted_any_sub_id"]
        if r["start_utc"] < f["first"]:
            f["first"] = r["start_utc"]
            f["wrapup"] = r["wrapup_name"]
    return flags


def classify(f: dict) -> str:
    """One bucket per (agent, phone): converted / hidden / leaked /
    cc_leaked / none. hidden is also a conversion (own-name sale)."""
    if f["own"]:
        return "hidden" if not f["cf"] else "converted"
    if f["cf"]:
        return "converted" if f["any"] else "none"
    if f["any"]:
        return "leaked" if f["unq"] else "cc_leaked"
    return "none"


def adjustment_stats(conn: sqlite3.Connection, start_utc: str, end_utc: str,
                     phone_subs=None, detail: bool = False) -> dict:
    """Per-agent {hidden, leaked, cc_leaked, converted, rows} for roster
    calls in [start_utc, end_utc), plus a phone-deduped '__TEAM__' entry.
    (phone_subs param kept for call-site compatibility; unused.)"""
    flags = _flags(conn, start_utc, end_utc)

    sub_seller: dict[str, tuple] = {}
    if detail:
        need = {f["any_sub"] for f in flags.values() if f["any_sub"]}
        for r in conn.execute(
            "SELECT subscription_id, sold_by_name, date_added_utc FROM subscriptions"):
            if r["subscription_id"] in need:
                sub_seller[r["subscription_id"]] = (r["sold_by_name"], r["date_added_utc"])

    stats: dict[str, dict] = {}

    def bucket(agent):
        return stats.setdefault(agent, {"hidden": 0, "leaked": 0,
                                        "cc_leaked": 0, "converted": 0,
                                        "rows": []})

    phone_roll: dict[str, set] = {}  # phone -> set of classifications
    for (agent, ph), f in flags.items():
        k = classify(f)
        phone_roll.setdefault(ph, set()).add(k)
        if k == "none":
            continue
        b = bucket(agent)
        if k == "hidden":
            b["hidden"] += 1
            b["converted"] += 1  # own-name sale counts in the numerator
            if detail:
                b["rows"].append({
                    "agent": agent, "phone": ph,
                    "call_date": f["first"][:10], "wrapup": f["wrapup"],
                    "kind": "hidden (bought from this agent, never dispositioned qualified)",
                    "sold_by": agent, "sub_added": "",
                })
        elif k == "converted":
            b["converted"] += 1
        else:
            b[k] += 1
            if detail:
                seller, added = sub_seller.get(f["any_sub"], ("?", ""))
                b["rows"].append({
                    "agent": agent, "phone": ph,
                    "call_date": f["first"][:10], "wrapup": f["wrapup"],
                    "kind": "unqualified" if k == "leaked" else "other",
                    "sold_by": seller or "(no rep)",
                    "sub_added": (added or "")[:10],
                })

    # team entry: unique phones, converted trumps hidden trumps leaked
    team = {"hidden": 0, "leaked": 0, "cc_leaked": 0, "converted": 0, "rows": []}
    for ph, kinds in phone_roll.items():
        if "converted" in kinds and ("hidden" not in kinds):
            team["converted"] += 1
        elif "hidden" in kinds:
            team["hidden"] += 1
            team["converted"] += 1
        elif "leaked" in kinds:
            team["leaked"] += 1
        elif "cc_leaked" in kinds:
            team["cc_leaked"] += 1
    stats["__TEAM__"] = team
    return stats
