"""Settings loaded from .env. One place; nothing else reads os.environ."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    pestroutes_base_url: str = os.environ.get("PESTROUTES_BASE_URL", "")
    pestroutes_auth_key: str = os.environ.get("PESTROUTES_AUTH_KEY", "")
    pestroutes_auth_token: str = os.environ.get("PESTROUTES_AUTH_TOKEN", "")
    pestroutes_office_ids: list[str] = field(default_factory=lambda: _csv("PESTROUTES_OFFICE_IDS"))

    genesys_region: str = os.environ.get("GENESYS_REGION", "")
    genesys_client_id: str = os.environ.get("GENESYS_CLIENT_ID", "")
    genesys_client_secret: str = os.environ.get("GENESYS_CLIENT_SECRET", "")
    genesys_queue_ids: list[str] = field(default_factory=lambda: _csv("GENESYS_INSIDE_SALES_QUEUE_IDS"))

    # VoiceOps — AI grading of every Genesys call. READ_ONLY client-credentials
    # OAuth; see voiceops.py for the two API quirks the client works around.
    # Software Management tab — SSH to the other droplets via the forced-command
    # agent (deploy/SOFTWARE_MGMT.md). Key lives in the isdash user's home.
    software_ssh_key: str = os.environ.get("SOFTWARE_SSH_KEY", "/home/isdash/.ssh/isdash_agent")
    software_known_hosts: str = os.environ.get("SOFTWARE_KNOWN_HOSTS", "/home/isdash/.ssh/known_hosts")
    software_ssh_user: str = os.environ.get("SOFTWARE_SSH_USER", "root")

    voiceops_client_id: str = os.environ.get("VOICEOPS_CLIENT_ID", "")
    voiceops_client_secret: str = os.environ.get("VOICEOPS_CLIENT_SECRET", "")

    dispo_closed: list[str] = field(default_factory=lambda: _csv("DISPO_CLOSED"))
    dispo_followup: list[str] = field(default_factory=lambda: _csv("DISPO_FOLLOWUP"))
    dispo_unqualified: list[str] = field(default_factory=lambda: _csv("DISPO_UNQUALIFIED"))

    isa_roster: list[str] = field(default_factory=lambda: _csv("ISA_ROSTER"))

    app_password: str = os.environ.get("APP_PASSWORD", "")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    # User accounts (auth.py): sign-in codes are emailed through Resend, the
    # same account the other Brooks apps use. OWNER_EMAIL always has full access.
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
    resend_from: str = os.environ.get("RESEND_FROM", "")
    owner_email: str = os.environ.get("OWNER_EMAIL", "")
    # the two shared passwords (APP_PASSWORD / ADMIN_PASSWORD) as a way in.
    # Set PASSWORD_LOGIN=off at cutover: only emailed-code accounts work.
    password_login: bool = os.environ.get("PASSWORD_LOGIN", "on").strip().lower() \
        not in ("off", "0", "false", "no")
    # pay periods: 14 days Sun-Sat, payroll the Monday after. Same anchor as
    # the IS Commissions app (its periods.py) so both agree on the dates.
    payroll_anchor_monday: str = os.environ.get("PAYROLL_ANCHOR_MONDAY", "2026-08-31")
    # Go-live: the Sunday START of the first pay period paid FROM the IS
    # Commissions app (10/11-10/24, payroll 10/26). Every statement for a
    # period ending before it is a PRE-GO-LIVE parallel run — the manual sheet
    # was the basis of pay — labelled as such, and Lock & run refuses it.
    # Same value as the IS Commissions app's PAYROLL_LIVE_FROM (Dallin, 10/15).
    payroll_live_from: str = os.environ.get("PAYROLL_LIVE_FROM", "2026-10-11")
    # Master Metric hours: the daily Homebase upload sheet (Date, Agent Name,
    # Regular Hours, OT Hours, Total Hours), read as CSV from HOURS_SHEET_FROM
    # onward; earlier days are frozen scheduled hours (scripts/freeze_schedule_hours.py)
    hours_sheet_url: str = os.environ.get(
        "HOURS_SHEET_URL",
        "https://docs.google.com/spreadsheets/d/"
        "1zYsxBSUVFKR5QQT4o6_Uzt9_N-ydMrI75HdDxHHHjZA/export?format=csv")
    hours_sheet_from: str = os.environ.get("HOURS_SHEET_FROM", "2026-10-05")
    # Marketing -> Spend & ROAS: the private spend sheet, read with the
    # dashboard's Google service account (shared to it as Viewer; key file
    # lives only on the server, mode 600). Tabs named "<year> CPA" are read.
    google_service_account_file: str = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE", str(PROJECT_ROOT / "data" / "google_service_account.json"))
    spend_sheet_id: str = os.environ.get(
        "SPEND_SHEET_ID", "166nCMpN2Vm08wzGZ-uwkszhmMrXrv9l2BPm8czqP4HM")
    # the manual weekly tracker ("Brooks Pest Weekly Data (NEW)": Call Volume/Week,
    # Forms/Week tabs) — history before the dashboard's own data, same service account
    history_sheet_id: str = os.environ.get(
        "HISTORY_SHEET_ID", "1bvCb1GaKd82uZyP-ddnBqOwHFLV2Z84QYL4GZAH2beU")
    # the agents' shift schedule ("IS Scheduling" sheet, "Website Schedule" tab),
    # read through the service account for the agent page's Schedule tab
    schedule_sheet_id: str = os.environ.get(
        "SCHEDULE_SHEET_ID", "1fPEv0XKoq2I2al_Vk_0uvocFlhGQNkzcrWM8CruMja4")
    # the agent page's Renewal Calculator: pricing and rules from the
    # "IS Renewal Calculator" sheet, its Eligibility tab found by gid
    renewal_sheet_id: str = os.environ.get(
        "RENEWAL_SHEET_ID", "12oktSciZRAoXSLI_3ngNYvFKuQ2cRRG0EwDLsVhEDlw")
    renewal_sheet_gid: str = os.environ.get("RENEWAL_SHEET_GID", "1220277383")
    spend_source_map: str = os.environ.get(
        "SPEND_SOURCE_MAP", str(PROJECT_ROOT / "config" / "spend_source_map.csv"))
    # the dashboard's own read-only Podium connection (scripts/podium_connect.py)
    podium_client_id: str = os.environ.get("PODIUM_CLIENT_ID", "")
    podium_client_secret: str = os.environ.get("PODIUM_CLIENT_SECRET", "")
    podium_refresh_token: str = os.environ.get("PODIUM_REFRESH_TOKEN", "")
    # Master Metric: the outbound reps, who are on no hours sheet and work a
    # standard week — always counted as 8 hours Mon-Fri (Dallin, 2026-10-05/06)
    mm_default_hours_agents: list[str] = field(default_factory=lambda: _csv(
        "MM_DEFAULT_HOURS_AGENTS") or ["Seyj Dungao", "Micah De Guzman", "Godfrey Vinluan"])
    mm_default_hours_per_day: float = float(os.environ.get("MM_DEFAULT_HOURS_PER_DAY", "8"))
    # the outbound reps work through Bruntwork (their own schedule, PTO and
    # time-off systems): never part of scheduling, the time clock, timesheets
    # or agent records here (Dallin, 2026-10-09)
    outbound_agents: list[str] = field(default_factory=lambda: _csv(
        "OUTBOUND_AGENTS") or ["Seyj Dungao", "Micah De Guzman", "Godfrey Vinluan"])
    # public address of the dashboard, used in invitation emails
    app_url: str = os.environ.get("APP_URL", "https://isdash.brookspest.com")
    # COMMISSION_RULES.md from the IS Commissions app, rendered on
    # Settings -> Commissions. Set explicitly on the droplet; the default
    # finds a sibling checkout during local development.
    commission_rules_path: str = os.environ.get(
        "COMMISSION_RULES_PATH",
        str(PROJECT_ROOT.parent / "IS Commissions" / "docs" / "COMMISSION_RULES.md"))
    # the IS Commissions engine's SQLite, read-only, for the Commission Lookup
    # subtab (Agent Metrics). Same default sibling-checkout convention.
    commissions_db: str = os.environ.get(
        "COMMISSIONS_DB",
        str(PROJECT_ROOT.parent / "IS Commissions" / "data" / "commissions.sqlite"))
    # the IS Commissions checkout itself — Payroll -> Submit Adjustment spawns
    # its form_intake.py --queue-only to book a submission right away
    commissions_app_dir: str = os.environ.get(
        "COMMISSIONS_APP_DIR", str(PROJECT_ROOT.parent / "IS Commissions"))
    audit_sheet_url: str = os.environ.get("AUDIT_SHEET_URL", "")
    conversion_window_days: int = int(os.environ.get("CONVERSION_WINDOW_DAYS", "14"))
    db_path: Path = PROJECT_ROOT / "data" / "isadash.sqlite"

    formleads_db: str = os.environ.get("FORMLEADS_DB", "")
    sourcing_master: str = os.environ.get("SOURCING_MASTER", "")
    sourcing_groups: str = os.environ.get("SOURCING_GROUPS", "")
    # dashboard-OWNED grouping overrides, layered on top of the sourcing app's
    # read-only dispute_groups.csv (never edit that file). Our deltas only.
    source_group_overrides: str = os.environ.get(
        "SOURCE_GROUP_OVERRIDES", str(PROJECT_ROOT / "config" / "source_group_overrides.csv"))
    # marketing channel (Paid / Organic / Affiliate Marketing) per source name:
    # generated from the IS Sourcing master sheet by scripts/build_source_channels.py,
    # with hand corrections layered on top from the overrides file
    source_channels: str = os.environ.get(
        "SOURCE_CHANNELS", str(PROJECT_ROOT / "config" / "source_channels.csv"))
    source_channel_overrides: str = os.environ.get(
        "SOURCE_CHANNEL_OVERRIDES", str(PROJECT_ROOT / "config" / "source_channel_overrides.csv"))
    form_dedup_window_hours: int = int(os.environ.get("FORM_DEDUP_WINDOW_HOURS", "72"))
    # sources whose "form" touches are NOT real forms and are dropped from the
    # form-lead pipeline entirely. Source 22 = Goodzer, whose emailed CALL-lead
    # summaries the sourcing app's Gmail parser ingests as form touches; those
    # callers are already counted via their tracking number, so counting the
    # summary emails as forms double-counts them. Dashboard-side only — the
    # sourcing app is never touched.
    form_exclude_sources: list[str] = field(
        default_factory=lambda: _csv("FORM_EXCLUDE_SOURCES") or ["Source 22"])
    # One-off garbage dumps excluded from the form pipeline, as "SOURCE|YYYY-MM-DD"
    # (Pacific day). ElectGen's feed glitched on 2026-08-07/08-10 and batch-dumped
    # ~91 international / out-of-area junk leads (0 in-area phones, 0 conversions).
    form_exclude_source_days: list[str] = field(
        default_factory=lambda: _csv("FORM_EXCLUDE_SOURCE_DAYS") or
        ["Source 25|2026-08-07", "Source 25|2026-08-10"])
    form_conversion_window_days: int = int(os.environ.get("FORM_CONVERSION_WINDOW_DAYS", "30"))

    def dispo_group(self, wrapup_name: str | None) -> str:
        """Map a wrap-up code name to closed/followup/unqualified/other/none."""
        if not wrapup_name:
            return "none"
        low = wrapup_name.strip().lower()
        if low in {n.lower() for n in self.dispo_closed}:
            return "closed"
        if low in {n.lower() for n in self.dispo_followup}:
            return "followup"
        if low in {n.lower() for n in self.dispo_unqualified}:
            return "unqualified"
        return "other"


SALES_GROUPS = ("closed", "followup", "unqualified")


def load_settings() -> Settings:
    s = Settings()
    missing = [n for n, v in (
        ("PESTROUTES_BASE_URL", s.pestroutes_base_url),
        ("PESTROUTES_AUTH_KEY", s.pestroutes_auth_key),
        ("GENESYS_CLIENT_ID", s.genesys_client_id),
        ("GENESYS_INSIDE_SALES_QUEUE_IDS", s.genesys_queue_ids),
    ) if not v]
    if missing:
        raise SystemExit(f"Missing required .env values: {', '.join(missing)}")
    return s
