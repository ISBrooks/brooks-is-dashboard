"""Aggregation + text/CSV reports.

Metrics per agent per period (weeks Sun–Sat Pacific; months calendar Pacific):

  total_calls   — every call handled on the ISA queues (any wrap-up)
  leads         — UNIQUE callers dispositioned closed/followup/unqualified
  qual_leads    — unique callers ever dispositioned closed or followup
                  (unqualified-only callers excluded — not real opportunities)
  closed/followup/unqualified — per-CALL disposition counts (+% of sales calls)
  conversions   — unique leads that became a FR subscription sold by this agent
  conv_qual_pct — conversions / qual_leads        (the headline rate)
  conv_leads_pct— conversions / leads             (incl. unqualified)
  conv_calls_pct— conversions / total_calls       (raw volume efficiency)

A lead who calls in two periods counts as a lead in both; their conversion
lands in the period of the credited (earliest qualifying) call.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import SALES_GROUPS
from .periods import month_label, week_label, week_start


@dataclass
class Bucket:
    total_calls: int = 0
    sales_calls: int = 0
    closed: int = 0
    followup: int = 0
    unqualified: int = 0
    lead_phones: set = field(default_factory=set)
    cf_phones: set = field(default_factory=set)    # pitched (closed/followup)
    own_phones: set = field(default_factory=set)   # sub in agent's own name
    any_phones: set = field(default_factory=set)   # sub from any seller
    # honest-conversion inputs (filled post-aggregation when audit data
    # exists; ALL are computed over the audit-covered part of the period so
    # numerator and denominator always describe the same days)
    audit_qual: int = 0
    audit_sales: int = 0
    hidden: int = 0
    leaked: int = 0
    honest_conv: int = 0

    def merge_counts(self, other: "Bucket") -> None:
        self.total_calls += other.total_calls
        self.sales_calls += other.sales_calls
        self.closed += other.closed
        self.followup += other.followup
        self.unqualified += other.unqualified
        self.lead_phones |= other.lead_phones
        self.cf_phones |= other.cf_phones
        self.own_phones |= other.own_phones
        self.any_phones |= other.any_phones
        self.audit_qual += other.audit_qual
        self.audit_sales += other.audit_sales
        self.hidden += other.hidden
        self.leaked += other.leaked
        self.honest_conv += other.honest_conv

    @property
    def conv_phones(self) -> set:
        """Converted = own-name sale, OR pitched and the customer bought
        from anyone (deliberate double-credit with other channels)."""
        return self.own_phones | (self.cf_phones & self.any_phones)

    def row(self) -> dict:
        def pct(n, d):
            return round(100.0 * n / d, 1) if d else 0.0
        conv_set = self.conv_phones
        leads = len(self.lead_phones | conv_set)
        qual = len(self.cf_phones | conv_set)
        conv = len(conv_set)
        # every converted caller is by definition a qualified opportunity —
        # floor the denominator at the conversion count so weeks where the
        # audit sheet under-logged an agent's pitches cap at 100%
        denom = max(self.audit_qual + self.hidden + self.leaked, self.honest_conv)
        return {
            "total_calls": self.total_calls,
            "leads": leads,
            "qual_leads": qual,
            "closed": self.closed,
            "closed_pct": pct(self.closed, self.sales_calls),
            "followup": self.followup,
            "followup_pct": pct(self.followup, self.sales_calls),
            "unqualified": self.unqualified,
            "unqualified_pct": pct(self.unqualified, self.sales_calls),
            "conversions": conv,
            "conv_qual_pct": pct(conv, qual),
            "conv_leads_pct": pct(conv, leads),
            "conv_calls_pct": pct(conv, self.total_calls),
            "sheet_pct": round(100.0 * self.audit_sales / self.audit_qual, 1)
                         if self.audit_qual else None,
            "honest_pct": round(100.0 * self.honest_conv / denom, 1)
                          if self.audit_qual else None,
        }


@dataclass
class Aggregates:
    weekly: dict[str, dict[str, Bucket]] = field(default_factory=dict)
    monthly: dict[str, dict[str, Bucket]] = field(default_factory=dict)


def aggregate(conn: sqlite3.Connection, roster_only: bool = True) -> Aggregates:
    agg = Aggregates()
    q = """
        SELECT c.start_utc, c.dispo_group, c.converted_sub_id,
               c.converted_any_sub_id, c.ani_e164,
               COALESCE(m.genesys_name, c.agent_name, c.agent_user_id) AS agent,
               COALESCE(m.on_roster, 0) AS on_roster
        FROM calls c LEFT JOIN agent_map m ON m.genesys_user_id = c.agent_user_id
    """
    for r in conn.execute(q):
        if roster_only and not r["on_roster"]:
            continue
        dt = datetime.fromisoformat(r["start_utc"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        phone = r["ani_e164"]
        for label, table in ((week_label(dt), agg.weekly), (month_label(dt), agg.monthly)):
            b = table.setdefault(label, {}).setdefault(r["agent"], Bucket())
            b.total_calls += 1
            if r["dispo_group"] in SALES_GROUPS:
                b.sales_calls += 1
                setattr(b, r["dispo_group"], getattr(b, r["dispo_group"]) + 1)
                if phone:
                    b.lead_phones.add(phone)
                    if r["dispo_group"] in ("closed", "followup"):
                        b.cf_phones.add(phone)
            if phone and r["converted_sub_id"]:
                b.own_phones.add(phone)
            if phone and r["converted_any_sub_id"]:
                b.any_phones.add(phone)
    return agg


_COLS = ["total_calls", "leads", "qual_leads", "closed", "closed_pct",
         "followup", "followup_pct", "unqualified", "unqualified_pct",
         "conversions", "conv_qual_pct", "conv_leads_pct", "conv_calls_pct",
         "honest_pct"]

_HDR = (f"{'Agent':<22}{'Calls':>6}{'Leads':>6}{'Qual':>6}{'Clsd':>6}{'Cl%':>6}"
        f"{'FlUp':>6}{'FU%':>6}{'Unq':>6}{'Uq%':>6}{'Conv':>6}"
        f"{'Cv/Q%':>7}{'Cv/L%':>7}{'Cv/C%':>7}")


def _fmt_row(name: str, r: dict) -> str:
    return (f"{name:<22}{r['total_calls']:>6}{r['leads']:>6}{r['qual_leads']:>6}"
            f"{r['closed']:>6}{r['closed_pct']:>6}{r['followup']:>6}"
            f"{r['followup_pct']:>6}{r['unqualified']:>6}{r['unqualified_pct']:>6}"
            f"{r['conversions']:>6}{r['conv_qual_pct']:>7}{r['conv_leads_pct']:>7}"
            f"{r['conv_calls_pct']:>7}")


def render_period(label: str, agents: dict[str, Bucket]) -> str:
    lines = [f"\n=== {label} ===", _HDR]
    total = Bucket()
    for agent in sorted(agents,
                        key=lambda a: -len(agents[a].cf_phones | agents[a].conv_phones)):
        lines.append(_fmt_row(agent, agents[agent].row()))
        total.merge_counts(agents[agent])
    lines.append(_fmt_row("TOTAL", total.row()))
    return "\n".join(lines)


def write_csv(agg: Aggregates, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind, table in (("weekly", agg.weekly), ("monthly", agg.monthly)):
        path = out_dir / f"isa_stats_{kind}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["period", "agent"] + _COLS)
            for label in sorted(table):
                for agent in sorted(table[label]):
                    r = table[label][agent].row()
                    w.writerow([label, agent] + [r[c] for c in _COLS])
        paths.append(path)
    return paths


def week_to_date_label(now_utc: datetime) -> str:
    return week_label(week_start(now_utc))
