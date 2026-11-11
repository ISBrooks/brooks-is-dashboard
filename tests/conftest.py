"""Shared test setup: a made-up database, a frozen clock and no network.

Everything runs against tests/sample_data.py's database in a temp folder, with
"now" pinned to sample_data.NOW, so the numbers are the same on every machine
and on every day. A developer's real .env is ignored on purpose: the tests must
behave the same on a laptop and on GitHub.
"""
from __future__ import annotations

import dataclasses
import os
import re
import socket
import sys
from pathlib import Path

import pytest
from freezegun import freeze_time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False            # never read the real .env

# Drop every setting the app reads from the environment, then set the few the
# tests rely on, so nothing leaks in from the machine running them.
_cfg = (ROOT / "src" / "isadash" / "config.py").read_text(encoding="utf-8")
for _name in set(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', _cfg)):
    os.environ.pop(_name, None)
os.environ.update({
    "PESTROUTES_BASE_URL": "https://placeholder.invalid",
    "PESTROUTES_AUTH_KEY": "placeholder",
    "GENESYS_CLIENT_ID": "placeholder",
    "GENESYS_INSIDE_SALES_QUEUE_IDS": "placeholder",
    "OUTBOUND_AGENTS": "Robin Vale",
    "MM_DEFAULT_HOURS_AGENTS": "Robin Vale",
    "FORM_DEDUP_WINDOW_HOURS": "336",                 # production's value (14 days)
    "FORM_CONVERSION_WINDOW_DAYS": "90",              # production's value
    "FORM_EXCLUDE_SOURCES": "Source 22",
})
os.environ.pop("ISDASH_CACHE", None)

import sample_data  # noqa: E402
# imported before the clock is frozen: FastAPI's own setup breaks under a fake clock
from isadash import cache, webapp  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def frozen_clock():
    with freeze_time(sample_data.NOW, tick=False):
        yield


@pytest.fixture(scope="session", autouse=True)
def no_network():
    """Any attempt to reach the internet fails the test instead of hanging or
    silently depending on an outside service."""
    real = socket.socket.connect

    def refuse(self, address, *a, **k):
        # the in-process test client wires itself up over the machine's own
        # loopback address; only connections that leave the machine are refused
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real(self, address, *a, **k)
        raise AssertionError(f"test tried to open a network connection to {address!r}")

    socket.socket.connect = refuse
    yield
    socket.socket.connect = real


@pytest.fixture(scope="session")
def app(tmp_path_factory, frozen_clock, no_network):
    """The web app module, pointed at a freshly built sample database."""
    d = tmp_path_factory.mktemp("isadash")
    db, fl, master = d / "isadash.sqlite", d / "attribution.sqlite", d / "sourcing_master.csv"
    sample_data.build(db, fl, master)

    webapp._settings = dataclasses.replace(
        webapp._settings, db_path=db, formleads_db=str(fl), sourcing_master=str(master),
        app_password="", admin_password="")
    cache.configure(db)
    cache.clear()
    return webapp


@pytest.fixture()
def db(app):
    """A read connection to the sample database, for checking numbers by hand."""
    import sqlite3
    conn = sqlite3.connect(app._settings.db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
