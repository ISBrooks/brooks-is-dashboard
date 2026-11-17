"""The Brooks IS Dashboard web app, assembled.

The endpoints live in the isadash/api/ package, one module per area of the
site (home, revenue, agent_metrics, team_metrics, marketing, managers, payroll,
settings, agent_page, accounts) on top of api/common.py. Importing them below
registers their endpoints on the one FastAPI `app`. What remains in this file
is the wiring that needs all of them at once: the background warmer, the page
and its script files, and the static-file mount (which must come last).

Run it with scripts/serve.py (the service does exactly that).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import cache

# Importing an area registers its endpoints on `app`. Every name that used to be
# defined in this file is re-exported, so `from isadash.webapp import X` still works.
from .api.common import (  # noqa: F401
    ADMIN_COOKIE, APP_COOKIE, PS_LOWER_BETTER, PS_METRIC_KEYS, WEB_DIR, _ACCESS, _ADMIN_PERMS,
    _AGENT_OPEN, _AGENT_PERMS, _ALL_PERMS, _CONFIG_DEFAULTS, _COUNTED_STATUSES,
    _DEFAULT_POWER_SCORE, _DEFAULT_SCORECARD_TIERS, _EXCLUDED_SOURCES, _FORM_LAG_S,
    _LEGACY_POWER_SCORE, _LOGIN_HTML, _MANAGER_PERMS, _NOINDEX, _OPEN_PATHS, _PW_LINK, _RANGE_TABS,
    _RENEWAL_SOURCES, _ROBOTS_TXT, _SLOW_MS, _admin_ok, _allowed, _app_ok, _audit_end_utc,
    _auth_startup, _brand_uri, _channel_for, _client_ip, _comm_db, _comm_reports_dir, _gate,
    _get_form_events, _inbound_split, _is_pre_go_live, _live_from, _load_config,
    _load_source_channels, _load_source_maps, _login_html, _lookup_why, _mm_only, _num,
    _own_inbound_subs, _payroll_periods, _perf_log, _perms, _ps_from_legacy, _pw_cookie,
    _revenue_data, _roster_names, _roster_sub_rows, _scorecard_calls, _settings, _sts,
    _timeclock_startup, _token, _user, _valid_range, _validate_power, app, log,
)
from .api.common import (  # noqa: F401
    _FORM_FILE_BACKSTOP_S, _FORM_SETTLED_LAG_S, _form_touch_fingerprint, _settled_forms,
)
from .api.team_metrics import _phone_queue_stats, _teammetrics_db  # noqa: F401
from .api.accounts import (  # noqa: F401
    _PREFS_SCHEMA, _PREF_KEYS, _actor, _is_https, api_admin_login, api_admin_status,
    api_auth_logout, api_auth_request, api_auth_verify, api_login, api_me, api_prefs_get,
    api_prefs_put, api_users, api_users_action, api_users_save,
)
from .api.revenue import (  # noqa: F401
    api_revenue, api_revenue_overview, api_source_revenue, api_source_revenue_overview,
)
from .api.agent_metrics import (  # noqa: F401
    _bucket, _quality_refresh_lock, _quality_refreshing, _refresh_quality_in_background,
    api_agentsubs, api_agentview, api_inbound_split, api_mastermetric, api_powerscore_export,
    api_quality, api_scorecard, api_scorecard_calls, api_scorecard_mm,
)
from .api.team_metrics import (  # noqa: F401
    _TEAM_OV_CALLS, _TEAM_OV_METRICS, _build_payload, _data_json, _json_bytes, _rows_for,
    _team_overview_db, _week_utc_bounds, api_audit, api_data, api_export, api_formleads,
    api_formleads_export, api_leaked, api_range, api_team_overview, api_teammetrics,
)
from .api.home import (  # noqa: F401
    _phone_window_stats, api_home, api_home_daily, api_home_quality, api_home_servicelevel,
    api_home_trends, api_home_ytd,
)
from .api.marketing import (  # noqa: F401
    _dispo_counts, _norm_dnis, _source_dnis, api_marketing_bysource, api_marketing_forms,
    api_marketing_forms_history, api_marketing_hourly, api_marketing_spend,
    api_marketing_spend_chart, api_marketing_spend_windows, api_marketing_volume,
    api_marketing_volume_history, api_source_detail, api_source_dispositions, api_sources,
)
from .api.settings import (  # noqa: F401
    _resolve_fr_ids, _save_config, _validate_tiers, api_config, api_config_reset,
    api_config_update, api_settings, api_settings_add, api_settings_update, api_software,
    api_software_run, api_sync_start, api_sync_status,
)
from .api.payroll import (  # noqa: F401
    PRE_GO_LIVE_BANNER, _BRANCHES, _INTAKE_DDL, _INTAKE_TYPES, _comm_db_rw, _comm_python,
    _intake_roster, _mark_pre_go_live, _payroll_run_status, _pid_alive, _run_intake_processor,
    _sheet_fill, _spawn_commissions, _tech_from_note, _xlsx_response, api_commissions_d2d_xlsx,
    api_commissions_database_xlsx, api_commissions_intake, api_commissions_intake_account,
    api_commissions_intake_delete, api_commissions_intake_list, api_commissions_intake_meta,
    api_commissions_lookup, api_commissions_report_xlsx, api_commissions_reports,
    api_commissions_roster, api_commissions_roster_save, api_commissions_rules,
    api_commissions_runpayroll, api_commissions_runpayroll_status,
)
from .api.managers import (  # noqa: F401
    _hours_report, _who, api_managers_adherence, api_managers_agents, api_managers_agents_save,
    api_managers_auto_clockout, api_managers_clock_override, api_managers_clock_rules,
    api_managers_clock_rules_save, api_managers_hours_report, api_managers_hours_report_csv,
    api_managers_live, api_managers_pto, api_managers_pto_adjust, api_managers_pto_ledger,
    api_managers_punch, api_managers_punch_history, api_managers_saturdays,
    api_managers_saturdays_post, api_managers_schedule, api_managers_schedule_move,
    api_managers_schedule_publish, api_managers_schedule_shift, api_managers_schedule_targets,
    api_managers_schedule_template, api_managers_schedule_week, api_managers_template_one,
    api_managers_templates, api_managers_templates_post, api_managers_timeoff,
    api_managers_timeoff_decide, api_managers_timeoff_preview, api_managers_timeoff_revoke,
    api_managers_timeoff_save, api_managers_timesheet, api_managers_timesheet_csv,
)
from .api.agent_page import (  # noqa: F401
    _AGENT_STATE_LABEL, _AGENT_TYPE_LABEL, _NO_AGENT, _PS_CALL_KEYS, _agent_final_periods,
    _agent_line_text, _agent_self, _agent_totals, _data_updated, _my_agent, _pay_period,
    _stmt_cache, _stmt_lines, _us_date, api_my_calls, api_my_clock, api_my_clock_in,
    api_my_clock_out, api_my_inbound, api_my_mm, api_my_payroll_lookup, api_my_payroll_periods,
    api_my_payroll_report_csv, api_my_power, api_my_pto, api_my_quality, api_my_renewal,
    api_my_schedule, api_my_schedule_day, api_my_scorecard, api_my_tabs, api_my_timeoff,
    api_my_timeoff_cancel, api_my_timeoff_create, api_my_timeoff_preview, api_my_ytd,
)


def _warm_calls() -> list:
    """What the Home tab asks for on load, with the same arguments the page
    sends (web/app.js: loadHome, loadHomeYtd) so a visitor finds them ready."""
    from .periods import PACIFIC
    now = datetime.now(timezone.utc)
    today = now.astimezone(PACIFIC).date()
    year = today.year
    period = _pay_period("current")
    return [
        (_get_form_events, {}),
        (_build_payload, {}),
        (api_home, {}),
        (api_home_trends, {}),
        # the page builds this range in the browser: Jan 1 .. today's UTC date
        (api_scorecard, {"start_day": f"{year}-01-01", "end_day": now.date().isoformat(), "mode": "sold"}),
        (api_revenue, {"year": year}),      # behind /api/home_ytd
        (api_home_quality, {}),
        (api_home_servicelevel, {}),
        (_team_overview_db, {}),            # Team Metrics -> Team Overview
        # Marketing -> Spend & ROAS: the table, and the chart's default range (this year)
        (api_marketing_spend_windows, {}),
        (api_marketing_spend_chart, {"start_day": f"{year}-01-01", "end_day": today.isoformat()}),
        # Marketing -> Sales Data and Revenue -> Overview, as the page first asks for them
        (api_source_revenue, {"year": 0, "level": "channel"}),
        (api_revenue_overview, {"by": "agent"}),
        # Agent Metrics -> IS #'s opens on the current pay period, through its last day
        (api_scorecard, {"start_day": period[0].isoformat(), "end_day": period[1].isoformat(), "mode": "sold"}),
    ]


@app.on_event("startup")
def _cache_startup() -> None:
    cache.sync_triggers()               # bring the change counters in line with cache.ignore_rows
    cache.start_warmer(_warm_calls)


# Every tab has its own address (web/router.js). They all serve the same page;
# the script then opens the tab the address names. Keep this list in step with
# the top-level words in router.js. Anything else falls through to the static
# files below (and a 404 if it isn't one). The sign-in gate above still applies.
_APP_PAGES = ("revenue", "agent-metrics", "team-metrics", "marketing", "schedule",
              "payroll", "settings", "my")


_ASSET_RE = re.compile(r'(?P<attr>src|href)="(?P<path>/(?:js/[\w.-]+\.js|router\.js|style\.css))(?:\?v=\w*)?"')


_index_cache: dict = {"key": None, "assets": [], "html": ""}


def _index_html() -> str:
    """index.html with every script and style address stamped with a
    fingerprint of that file's contents (/js/home.js?v=3f2a9c1d07). Browsers may
    then keep each file until it actually changes, and nobody has to remember
    to bump a version number. Rebuilt whenever the page or one of the files
    changes on disk, so a deploy that only touches web/ needs no restart."""
    index = WEB_DIR / "index.html"

    def stamp(p: Path) -> tuple:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)

    def key_for(assets: list) -> tuple:
        return (stamp(index),) + tuple(stamp(WEB_DIR / a.lstrip("/")) for a in assets)

    try:
        key = key_for(_index_cache["assets"])
    except OSError:
        key = None
    if key is None or key != _index_cache["key"]:
        raw = index.read_text(encoding="utf-8")
        assets = sorted({m.group("path") for m in _ASSET_RE.finditer(raw)})
        tag = {a: hashlib.sha1((WEB_DIR / a.lstrip("/")).read_bytes()).hexdigest()[:10] for a in assets}
        html = _ASSET_RE.sub(lambda m: f'{m.group("attr")}="{m.group("path")}?v={tag[m.group("path")]}"', raw)
        _index_cache.update(key=key_for(assets), assets=assets, html=html)
    return _index_cache["html"]


def _app_page(rest: str = "") -> HTMLResponse:
    return HTMLResponse(_index_html())


app.add_api_route("/", _app_page, methods=["GET"], include_in_schema=False)


for _page in _APP_PAGES:
    app.add_api_route(f"/{_page}", _app_page, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"/{_page}/{{rest:path}}", _app_page, methods=["GET"], include_in_schema=False)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
