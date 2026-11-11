"""SQLite persistence. Idempotent upserts — re-running a backfill window is
always safe."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import cache

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    conversation_id TEXT PRIMARY KEY,
    start_utc       TEXT NOT NULL,
    ani_e164        TEXT,
    dnis            TEXT,
    agent_user_id   TEXT NOT NULL,
    agent_name      TEXT,
    wrapup_id       TEXT,
    wrapup_name     TEXT,
    dispo_group     TEXT NOT NULL,          -- closed/followup/unqualified/other/none
    converted_sub_id TEXT                   -- filled by the matcher
);
CREATE INDEX IF NOT EXISTS idx_calls_start ON calls(start_utc);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent_user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    customer_id     TEXT,
    office_id       TEXT,
    date_added_utc  TEXT,
    sold_by         TEXT,
    sold_by_name    TEXT,
    phone1_e164     TEXT,
    phone2_e164     TEXT
);
CREATE INDEX IF NOT EXISTS idx_subs_phone1 ON subscriptions(phone1_e164);
CREATE INDEX IF NOT EXISTS idx_subs_phone2 ON subscriptions(phone2_e164);

CREATE TABLE IF NOT EXISTS customers (
    customer_id    TEXT PRIMARY KEY,
    office_id      TEXT,
    date_added_utc TEXT,
    status         TEXT,
    phone1_e164    TEXT,
    phone2_e164    TEXT,
    email          TEXT
);
CREATE INDEX IF NOT EXISTS idx_cust_phone1 ON customers(phone1_e164);
CREATE INDEX IF NOT EXISTS idx_cust_phone2 ON customers(phone2_e164);
CREATE INDEX IF NOT EXISTS idx_cust_email  ON customers(email);

CREATE TABLE IF NOT EXISTS audit_daily (
    date      TEXT NOT NULL,        -- YYYY-MM-DD (Pacific)
    agent     TEXT NOT NULL,
    qualified INTEGER NOT NULL,
    sales     INTEGER NOT NULL,
    PRIMARY KEY (date, agent)
);

CREATE TABLE IF NOT EXISTS agent_map (
    genesys_user_id TEXT PRIMARY KEY,
    genesys_name    TEXT,
    fr_employee_id  TEXT,
    fr_name         TEXT,
    on_roster       INTEGER NOT NULL DEFAULT 0
);

-- Editable tunables (scorecard tiers, power-score weights). key -> JSON value.
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Commission roster (Settings -> Commissions): who the IS Commissions app
-- actively calculates for, and how. Independent of agent_map on purpose —
-- outbound agents belong here without appearing on the performance tabs.
-- role: 'inbound' (full ISA commission) | 'outbound' (3%; the inbound agent
-- keeps rate minus 3% on shared accounts).
CREATE TABLE IF NOT EXISTS commission_roster (
    name     TEXT PRIMARY KEY,
    role     TEXT NOT NULL CHECK (role IN ('inbound', 'outbound')),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    start_date TEXT    -- inside-sales start (YYYY-MM-DD); earlier sales under
                       -- the agent's FR ids are not ISA sales (RULES §1a)
);

-- FieldRoutes employees who sell (per-office IDs). name groups a person across
-- their office IDs; emp_type "0" = office/inside-sales staff, "2" = field/tech.
CREATE TABLE IF NOT EXISTS employees (
    fr_employee_id TEXT PRIMARY KEY,
    name           TEXT,
    emp_type       TEXT
);

-- VoiceOps AI call-grading results, cached per (period, agent). A period is an
-- INCLUSIVE Pacific day range; the weekly rows the trend chart reads are
-- Sunday-Saturday. Cached rather than fetched live because a full-roster pull
-- is ~27 API calls, far too slow to block a page render.
CREATE TABLE IF NOT EXISTS voiceops_scores (
    start_day        TEXT NOT NULL,
    end_day          TEXT NOT NULL,
    agent            TEXT NOT NULL,
    call_count       INTEGER,
    call_score       REAL,
    quality_score    REAL,
    compliance_score REAL,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (start_day, end_day, agent)
);

-- User accounts (docs/ACCOUNTS_PLAN.md). Invite-only; sign-in is an emailed
-- one-time code, so there is no password column. permissions is a JSON list
-- of keys ("*" = everything); kind 'agent' users are tied to one agent and
-- only ever see their own page. All timestamps ISO 8601 UTC.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    kind          TEXT NOT NULL DEFAULT 'staff' CHECK (kind IN ('staff', 'agent')),
    agent         TEXT,
    permissions   TEXT NOT NULL DEFAULT '[]',
    is_owner      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'invited'
                  CHECK (status IN ('invited', 'active', 'disabled')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    invited_at    TEXT,
    last_login_at TEXT
);

-- Emailed sign-in codes: only a hash is stored; ten-minute life, single use.
CREATE TABLE IF NOT EXISTS login_codes (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS login_codes_email ON login_codes (email, id);

-- Server-side sessions (hash of the cookie token), so disabling a user or
-- signing out takes effect at once.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (id),
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT
);

-- Sign-in history: codes requested, sign-ins, failures, email errors.
CREATE TABLE IF NOT EXISTS auth_log (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    event  TEXT NOT NULL,
    email  TEXT,
    ip     TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS auth_log_email ON auth_log (event, email, at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    cache.watch(conn)       # after setup: record only what the caller's queries read
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a field existed."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    for col, decl in (("agreement_length", "INTEGER"),
                      ("contract_value", "REAL"),
                      ("service_type", "TEXT"),
                      ("initial_status", "TEXT"),
                      ("source", "TEXT"),
                      ("initial_price", "REAL"),
                      ("active", "INTEGER"),
                      ("date_cancelled_utc", "TEXT"),
                      ("initial_appointment_id", "TEXT"),
                      ("initial_service_date", "TEXT"),
                      ("billing_frequency", "INTEGER"),
                      ("service_frequency", "INTEGER"),
                      ("contract_added", "TEXT"),
                      # 1 = has a COMPLETED (truly signed) FR contract for THIS
                      # subscription; the real signed signal vs contract_added
                      # (which is true even for unsigned WIP drafts).
                      ("signed_agreement", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {decl}")
    have_cust = {r[1] for r in conn.execute("PRAGMA table_info(customers)")}
    if "autopay" not in have_cust:
        conn.execute("ALTER TABLE customers ADD COLUMN autopay INTEGER")
    if "card_on_file" not in have_cust:
        conn.execute("ALTER TABLE customers ADD COLUMN card_on_file INTEGER")
    have_calls = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    if "converted_any_sub_id" not in have_calls:
        # a sub on this caller's phone within the window, ANY seller —
        # conversion credit is seller-independent; soldBy only gates
        # multiyear / contract-value metrics
        conn.execute("ALTER TABLE calls ADD COLUMN converted_any_sub_id TEXT")
    have_cr = {r[1] for r in conn.execute("PRAGMA table_info(commission_roster)")}
    if "start_date" not in have_cr:
        # the commission engine reads this (read-only) for RULES §1a
        conn.execute("ALTER TABLE commission_roster ADD COLUMN start_date TEXT")
    have_am = {r[1] for r in conn.execute("PRAGMA table_info(agent_map)")}
    # These four were added to the live database by hand long ago and no code
    # created them, so a brand-new database (tests, a rebuild) couldn't run the
    # app. Same definitions as production.
    for col, decl in (("scorecard", "INTEGER DEFAULT 0"), ("powerscore", "INTEGER DEFAULT 0"),
                      ("active", "INTEGER DEFAULT 1"), ("team", "TEXT")):
        if col not in have_am:
            conn.execute(f"ALTER TABLE agent_map ADD COLUMN {col} {decl}")
    if "voiceops_email" not in have_am:
        # VoiceOps identifies reps by EMAIL, and they are personal addresses
        # (gmail/outlook/icloud) rather than Genesys ids or company mail — so
        # the link has to be stored per agent, seeded by name and then editable.
        conn.execute("ALTER TABLE agent_map ADD COLUMN voiceops_email TEXT")
    # No index on subscriptions(date_added_utc): measured 2026-11-11, it made
    # 30-day queries ~20 ms faster but year-to-date ones ~300 ms slower (a year
    # is ~15% of the table, where a straight scan beats index hops).
    conn.commit()


def upsert_calls(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO calls (conversation_id, start_utc, ani_e164, dnis,
                              agent_user_id, agent_name, wrapup_id, wrapup_name,
                              dispo_group)
           VALUES (:conversation_id, :start_utc, :ani_e164, :dnis,
                   :agent_user_id, :agent_name, :wrapup_id, :wrapup_name,
                   :dispo_group)
           ON CONFLICT(conversation_id) DO UPDATE SET
               start_utc=excluded.start_utc, ani_e164=excluded.ani_e164,
               dnis=excluded.dnis, agent_user_id=excluded.agent_user_id,
               agent_name=excluded.agent_name, wrapup_id=excluded.wrapup_id,
               wrapup_name=excluded.wrapup_name, dispo_group=excluded.dispo_group""",
        rows,
    )
    conn.commit()


def upsert_subscriptions(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO subscriptions (subscription_id, customer_id, office_id,
                                      date_added_utc, sold_by, sold_by_name,
                                      phone1_e164, phone2_e164,
                                      agreement_length, contract_value, service_type,
                                      initial_status, source, initial_price,
                                      active, date_cancelled_utc)
           VALUES (:subscription_id, :customer_id, :office_id, :date_added_utc,
                   :sold_by, :sold_by_name, :phone1_e164, :phone2_e164,
                   :agreement_length, :contract_value, :service_type,
                   :initial_status, :source, :initial_price,
                   :active, :date_cancelled_utc)
           ON CONFLICT(subscription_id) DO UPDATE SET
               customer_id=excluded.customer_id, office_id=excluded.office_id,
               date_added_utc=excluded.date_added_utc, sold_by=excluded.sold_by,
               -- keep an existing sold_by_name if this caller didn't resolve one
               sold_by_name=COALESCE(excluded.sold_by_name, subscriptions.sold_by_name),
               phone1_e164=excluded.phone1_e164, phone2_e164=excluded.phone2_e164,
               agreement_length=excluded.agreement_length,
               contract_value=excluded.contract_value,
               service_type=excluded.service_type,
               initial_status=excluded.initial_status,
               source=excluded.source,
               initial_price=excluded.initial_price,
               active=excluded.active,
               date_cancelled_utc=excluded.date_cancelled_utc""",
        rows,
    )
    conn.commit()


def upsert_customers(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for r in rows:
        r.setdefault("autopay", None)
        r.setdefault("card_on_file", None)
    conn.executemany(
        """INSERT INTO customers (customer_id, office_id, date_added_utc, status,
                                  phone1_e164, phone2_e164, email, autopay, card_on_file)
           VALUES (:customer_id, :office_id, :date_added_utc, :status,
                   :phone1_e164, :phone2_e164, :email, :autopay, :card_on_file)
           ON CONFLICT(customer_id) DO UPDATE SET
               office_id=excluded.office_id, date_added_utc=excluded.date_added_utc,
               status=excluded.status, phone1_e164=excluded.phone1_e164,
               phone2_e164=excluded.phone2_e164, email=excluded.email,
               autopay=COALESCE(excluded.autopay, customers.autopay),
               card_on_file=COALESCE(excluded.card_on_file, customers.card_on_file)""",
        rows,
    )
    conn.commit()


def upsert_audit_daily(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO audit_daily (date, agent, qualified, sales)
           VALUES (:date, :agent, :qualified, :sales)
           ON CONFLICT(date, agent) DO UPDATE SET
               qualified=excluded.qualified, sales=excluded.sales""",
        rows,
    )
    conn.commit()


def upsert_agent_map(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO agent_map (genesys_user_id, genesys_name, fr_employee_id,
                                  fr_name, on_roster)
           VALUES (:genesys_user_id, :genesys_name, :fr_employee_id, :fr_name,
                   :on_roster)
           ON CONFLICT(genesys_user_id) DO UPDATE SET
               genesys_name=excluded.genesys_name,
               fr_employee_id=excluded.fr_employee_id,
               fr_name=excluded.fr_name,
               -- a Settings-added scorecard agent is ALWAYS on roster, even if
               -- the ISA_ROSTER env list (which drives excluded.on_roster via
               -- the nightly backfill) hasn't been updated for them
               on_roster=CASE WHEN agent_map.scorecard=1 THEN 1
                              ELSE excluded.on_roster END""",
        rows,
    )
    conn.commit()


def get_config(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Raw value from app_config (or default). Callers json-decode if needed."""
    row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_config(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
