"""Arbitrary-date-range stats + record-level exports (the Explore tab).

Dates are Pacific calendar days, INCLUSIVE on both ends. Every metric is
computed with exactly the same rules as the bucketed tabs — this module just
frees them from the Sun–Sat / calendar-month grid.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .adjust import adjustment_stats
from .periods import PACIFIC, UTC


def utc_bounds(start_day: str, end_day: str) -> tuple[str, str]:
    a = datetime.fromisoformat(start_day + "T00:00:00").replace(tzinfo=PACIFIC)
    b = datetime.fromisoformat(end_day + "T00:00:00").replace(tzinfo=PACIFIC) + timedelta(days=1)
    return a.astimezone(UTC).isoformat(), b.astimezone(UTC).isoformat()


def _fr_to_agent(conn) -> dict[str, str]:
    out = {}
    for r in conn.execute(
        "SELECT genesys_name, fr_employee_id FROM agent_map "
        "WHERE on_roster=1 AND fr_employee_id IS NOT NULL"):
        for fid in r["fr_employee_id"].split(","):
            out[fid] = r["genesys_name"]
    return out


def range_stats(conn: sqlite3.Connection, start_day: str, end_day: str,
                excluded_sources: tuple, audit_end: str | None) -> dict:
    a, b = utc_bounds(start_day, end_day)

    stats: dict[str, dict] = {}

    def bucket(agent):
        return stats.setdefault(agent, {
            "calls": 0, "sales_calls": 0, "closed": 0, "followup": 0,
            "unqualified": 0, "leads": set(), "cf": set(), "own": set(),
            "anyc": set(),
            "subs": 0, "pending": 0, "multi": 0, "cv_sum": 0.0, "cv_n": 0,
            "ip_sum": 0.0, "ip_n": 0, "sts": [],
            "lt12": 0, "m12": 0, "m18": 0, "m24": 0,
            "audit_qual": 0, "audit_sales": 0, "hidden": 0, "leaked": 0,
            "adj_conv": 0,
        })

    def dur_bucket(months: int) -> str:
        # one-time treatments have agreementLength=0 in FieldRoutes — they
        # are "below 12 mo" contracts by policy (and count against MY%)
        if months < 12:
            return "lt12"
        if months < 18:
            return "m12"
        if months < 24:
            return "m18"
        return "m24"

    # --- Genesys calls (agent dispositions) --------------------------------
    for r in conn.execute(
        """SELECT c.ani_e164 ph, c.dispo_group, c.converted_sub_id,
                  c.converted_any_sub_id, m.genesys_name agent
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.start_utc >= ? AND c.start_utc < ?""",
        (a, b)):
        for who in (r["agent"], "TEAM"):
            k = bucket(who)
            k["calls"] += 1
            if r["dispo_group"] in ("closed", "followup", "unqualified"):
                k["sales_calls"] += 1
                k[r["dispo_group"]] += 1
                if r["ph"]:
                    k["leads"].add(r["ph"])
                    if r["dispo_group"] != "unqualified":
                        k["cf"].add(r["ph"])
            if r["ph"] and r["converted_sub_id"]:
                k["own"].add(r["ph"])
            if r["ph"] and r["converted_any_sub_id"]:
                k["anyc"].add(r["ph"])

    # --- FieldRoutes accounts (own name) ------------------------------------
    fr_map = _fr_to_agent(conn)
    excl = tuple(s.upper() for s in excluded_sources)
    for r in conn.execute(
        """SELECT sold_by, initial_status, agreement_length, contract_value,
                  initial_price, source FROM subscriptions
           WHERE date_added_utc >= ? AND date_added_utc < ? AND sold_by != '0'""",
        (a, b)):
        agent = fr_map.get(r["sold_by"])
        if not agent:
            continue
        for who in (agent, "TEAM"):
            k = bucket(who)
            k["sts"].append(r["initial_status"] or "?")
            if (r["initial_status"] in ("Completed", "Pending")
                    and (r["source"] or "").upper() not in excl):
                k["subs"] += 1
                if r["initial_status"] == "Pending":
                    k["pending"] += 1
                k[dur_bucket(r["agreement_length"] or 0)] += 1
                if (r["agreement_length"] or 0) >= 18:
                    k["multi"] += 1
                if r["contract_value"]:
                    k["cv_sum"] += r["contract_value"]
                    k["cv_n"] += 1
                if r["initial_price"]:
                    k["ip_sum"] += r["initial_price"]
                    k["ip_n"] += 1

    # --- audit sheet + adjustments (bounded to audited days) ----------------
    for r in conn.execute(
        "SELECT agent, SUM(qualified) q, SUM(sales) s FROM audit_daily "
        "WHERE date >= ? AND date <= ? GROUP BY agent",
        (start_day, end_day)):
        if r["agent"] in stats:
            stats[r["agent"]]["audit_qual"] = r["q"]
            stats[r["agent"]]["audit_sales"] = r["s"]
            stats["TEAM"]["audit_qual"] = stats.get("TEAM", bucket("TEAM"))["audit_qual"] + r["q"]
            stats["TEAM"]["audit_sales"] += r["s"]
    adj_b = min(b, audit_end) if audit_end else b
    if adj_b > a:
        for agent, st in adjustment_stats(conn, a, adj_b).items():
            tgt = "TEAM" if agent == "__TEAM__" else agent
            if tgt in stats:
                stats[tgt]["hidden"] = st["hidden"]
                stats[tgt]["leaked"] = st["leaked"]
                stats[tgt]["adj_conv"] = st["converted"]

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    rows = []
    for agent, k in stats.items():
        conv_set = k["own"] | (k["cf"] & k["anyc"])
        leads = len(k["leads"] | conv_set)
        qual = len(k["cf"] | conv_set)
        conv = len(conv_set)
        completed = sum(1 for s in k["sts"] if s == "Completed")
        pend = sum(1 for s in k["sts"] if s == "Pending")
        noappt = sum(1 for s in k["sts"] if s == "No Appointment")
        failed = len(k["sts"]) - completed - pend - noappt
        adenom = max(k["audit_qual"] + k["hidden"] + k["leaked"], k["adj_conv"])
        rows.append({
            "agent": agent,
            # Genesys
            "calls": k["calls"], "leads": leads, "qual_leads": qual,
            "closed": k["closed"], "followup": k["followup"],
            "unqualified": k["unqualified"],
            "closed_pct": pct(k["closed"], k["sales_calls"]),
            "followup_pct": pct(k["followup"], k["sales_calls"]),
            "unqualified_pct": pct(k["unqualified"], k["sales_calls"]),
            # FieldRoutes-verified conversion
            "conversions": conv, "conv_qual_pct": pct(conv, qual),
            "conv_leads_pct": pct(conv, leads),
            "conv_calls_pct": pct(conv, k["calls"]),
            # duration buckets
            "lt12": k["lt12"], "m12": k["m12"], "m18": k["m18"],
            "m24": k["m24"],
            "lt12_pct": pct(k["lt12"], k["subs"]), "m12_pct": pct(k["m12"], k["subs"]),
            "m18_pct": pct(k["m18"], k["subs"]), "m24_pct": pct(k["m24"], k["subs"]),
            # audit sheet (THE default conversion metric) + gold standard
            "audit_qual": k["audit_qual"], "audit_sales": k["audit_sales"],
            "sheet_conv_pct": pct(k["audit_sales"], k["audit_qual"]),
            "hidden": k["hidden"], "leaked": k["leaked"],
            "honest_pct": pct(k["adj_conv"], adenom) if k["audit_qual"] else None,
            # FieldRoutes accounts
            "subs": k["subs"], "pending": k["pending"],
            "multiyear_pct": pct(k["multi"], k["subs"]),
            "acv": round(k["cv_sum"] / k["cv_n"], 2) if k["cv_n"] else None,
            "init_avg": round(k["ip_sum"] / k["ip_n"], 2) if k["ip_n"] else None,
            "cv_total": round(k["cv_sum"], 2),
            "sts_pct": pct(completed, completed + failed),
            "sts_never": noappt,
        })
    rows.sort(key=lambda r: (r["agent"] == "TEAM", -(r["qual_leads"] or 0)))
    return {"start": start_day, "end": end_day, "rows": rows,
            "audit_covered_through": (audit_end or "")[:10]}


# ---------------- record-level exports ---------------------------------------

def export_leads(conn, start_day: str, end_day: str) -> list[dict]:
    """One row per (agent, caller) with everything that fed the conversion
    metrics — Genesys dispositions AND the FieldRoutes outcome."""
    a, b = utc_bounds(start_day, end_day)
    rows: dict[tuple, dict] = {}
    for r in conn.execute(
        """SELECT m.genesys_name agent, c.ani_e164 ph, c.start_utc,
                  c.wrapup_name, c.dispo_group, c.converted_sub_id,
                  c.converted_any_sub_id
           FROM calls c JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
           WHERE m.on_roster = 1 AND c.ani_e164 IS NOT NULL
             AND c.start_utc >= ? AND c.start_utc < ?""", (a, b)):
        k = rows.setdefault((r["agent"], r["ph"]), {
            "agent": r["agent"], "phone": r["ph"], "calls": 0,
            "first_call": r["start_utc"][:16], "wrapups": set(),
            "dispositioned_qualified": "no", "converted": "no",
            "own_name_sale": "no", "sub_id": "",
        })
        k["calls"] += 1
        k["wrapups"].add(r["wrapup_name"] or "(none)")
        if r["start_utc"][:16] < k["first_call"]:
            k["first_call"] = r["start_utc"][:16]
        if r["dispo_group"] in ("closed", "followup"):
            k["dispositioned_qualified"] = "yes"
        if r["converted_sub_id"]:
            k["own_name_sale"] = "yes"
            k["sub_id"] = r["converted_sub_id"]
        if r["converted_any_sub_id"] and not k["sub_id"]:
            k["sub_id"] = r["converted_any_sub_id"]
        if r["converted_sub_id"] or (
                k["dispositioned_qualified"] == "yes" and r["converted_any_sub_id"]):
            k["converted"] = "yes"

    sub_info = {}
    need = {k["sub_id"] for k in rows.values() if k["sub_id"]}
    if need:
        q = ",".join("?" * len(need))
        for s in conn.execute(
            f"""SELECT subscription_id, customer_id, sold_by_name,
                       date_added_utc, contract_value, service_type
                FROM subscriptions WHERE subscription_id IN ({q})""",
                tuple(need)):
            sub_info[s["subscription_id"]] = s
    out = []
    for k in rows.values():
        s = sub_info.get(k["sub_id"])
        out.append({
            "agent": k["agent"], "caller_phone": k["phone"],
            "first_call_utc": k["first_call"], "calls_in_range": k["calls"],
            "genesys_wrapups": " | ".join(sorted(k["wrapups"])),
            "dispositioned_qualified": k["dispositioned_qualified"],
            "counted_as_conversion": k["converted"],
            "sale_in_own_name": k["own_name_sale"],
            "fr_subscription_id": k["sub_id"],
            "fr_customer_id": s["customer_id"] if s else "",
            "fr_sold_by": s["sold_by_name"] if s else "",
            "fr_date_sold": (s["date_added_utc"] or "")[:10] if s else "",
            "fr_contract_value": s["contract_value"] if s else "",
            "fr_service_type": s["service_type"] if s else "",
        })
    out.sort(key=lambda r: (r["agent"], r["first_call_utc"]))
    return out


def export_accounts(conn, start_day: str, end_day: str,
                    excluded_sources: tuple) -> list[dict]:
    """One row per subscription sold by a roster agent in the range, with
    flags showing exactly which metrics it counts toward (or why not)."""
    a, b = utc_bounds(start_day, end_day)
    fr_map = _fr_to_agent(conn)
    excl = tuple(s.upper() for s in excluded_sources)
    out = []
    for r in conn.execute(
        """SELECT * FROM subscriptions
           WHERE date_added_utc >= ? AND date_added_utc < ? AND sold_by != '0'
           ORDER BY date_added_utc""", (a, b)):
        agent = fr_map.get(r["sold_by"])
        if not agent:
            continue
        st = r["initial_status"] or "?"
        is_excluded_src = (r["source"] or "").upper() in excl
        counted = st in ("Completed", "Pending") and not is_excluded_src
        sts_bucket = ("serviced" if st == "Completed" else
                      "pending" if st == "Pending" else
                      "never_scheduled" if st == "No Appointment" else "fell_through")
        out.append({
            "agent": agent, "fr_subscription_id": r["subscription_id"],
            "fr_customer_id": r["customer_id"],
            "date_sold": (r["date_added_utc"] or "")[:10],
            "service_type": r["service_type"], "source": r["source"],
            "initial_status": st,
            "agreement_months": r["agreement_length"],
            "is_multiyear_18mo_plus": "yes" if (r["agreement_length"] or 0) >= 18 else "no",
            "contract_value": r["contract_value"],
            "initial_service_price": r["initial_price"],
            "counted_in_multiyear_acv": "yes" if counted else
                ("no - excluded source" if is_excluded_src else f"no - status {st}"),
            "sold_to_serviced_bucket": sts_bucket,
        })
    return out


def export_adjustments(conn, start_day: str, end_day: str,
                       audit_end: str | None) -> list[dict]:
    """Hidden conversions + leaked leads, record level."""
    a, b = utc_bounds(start_day, end_day)
    if audit_end:
        b = min(b, audit_end)
    if b <= a:
        return []
    adj = adjustment_stats(conn, a, b, detail=True)
    rows = [r for ag, s in adj.items() if ag != "__TEAM__" for r in s["rows"]]
    rows.sort(key=lambda r: (r["agent"], r["call_date"]))
    return rows
