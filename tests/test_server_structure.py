"""A safety net under the server code as a whole (it is about to be split into
one file per area): no name used without being defined, no endpoint lost or
renamed by accident, and the read-only feeds still answer."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SNAP = Path(__file__).parent / "snapshots"

# Feeds that reach outside this app (other servers over SSH, the commissions app's
# folder, Google sheets, the sourcing app's files): what they answer depends on
# the machine, so they are left out of the status check below.
OUTSIDE = ("/api/software", "/api/commissions", "/api/payroll", "/api/my/payroll", "/api/my/renewal",
           "/api/marketing", "/api/sources", "/api/source_", "/api/managers/hours", "/api/hours",
           "/api/admin/status", "/api/sync")


def _compare(name: str, got: str) -> None:
    path = SNAP / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.write_text(got, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"no saved copy of {name}: run with UPDATE_SNAPSHOTS=1 and review it"
    want = path.read_text(encoding="utf-8")
    if got != want:
        import difflib
        diff = "\n".join(difflib.unified_diff(want.splitlines(), got.splitlines(), "saved", "now", lineterm="", n=0))
        pytest.fail(f"{name} changed:\n{diff}", pytrace=False)


def test_no_name_is_used_without_being_defined():
    """pyflakes reads every file without running it. An undefined name is a
    crash waiting for the one request that reaches that line (four were found
    this way on 2026-11-11, one of which broke adding a new agent in Settings)."""
    r = subprocess.run([sys.executable, "-m", "pyflakes", "src/isadash", "scripts"],
                       cwd=ROOT, capture_output=True, text=True)
    bad = [line for line in r.stdout.splitlines() if "undefined name" in line or "syntax" in line.lower()]
    assert not bad, "\n".join(bad)


def _routes(app):
    return sorted((m, r.path) for r in app.app.routes if isinstance(r, APIRoute) for m in r.methods if m != "HEAD")


def test_no_endpoint_lost_or_renamed(app):
    _compare("routes.txt", "".join(f"{m} {p}\n" for m, p in _routes(app)))


def test_every_endpoint_has_an_access_decision(app):
    """Anything not listed in _ACCESS is admin-only. Saved here so a new endpoint
    shows up in review as an explicit choice rather than by accident."""
    listed = set(app._ACCESS) | {f"{m} {p}" for p in app._OPEN_PATHS for m in ("GET", "POST")}
    admin_only = [f"{m} {p}" for m, p in _routes(app) if p.startswith("/api") and f"{m} {p}" not in listed]
    _compare("admin_only_endpoints.txt", "".join(a + "\n" for a in admin_only))


def test_read_only_feeds_still_answer(app, monkeypatch):
    """Call every GET feed against the sample database and compare the status
    codes with the saved ones. A split that drops an import shows up here as a
    200 turning into a 500. (Feeds that need Genesys answer 502 here: the
    network is blocked in tests.)"""
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)     # don't wait out retry pauses
    client = TestClient(app.app, raise_server_exceptions=False)
    # called twice: bare, and with the arguments most feeds take (a month, a year,
    # and "as this agent" for the agent-page feeds, which refuse without one)
    args = ("?start_day=2026-10-01&end_day=2026-10-31&start=2026-10-01&end=2026-10-31"
            "&year=2026&as_agent=Avery%20Stone&day=2026-10-15")
    lines = []
    for m, p in _routes(app):
        if m != "GET" or not p.startswith("/api") or "{" in p or p.startswith(OUTSIDE):
            continue
        lines.append(f"{client.get(p).status_code} {client.get(p + args).status_code} {p}\n")
    assert sum(1 for line in lines if " 200 " in line) > 45
    assert not [line for line in lines if "500" in line.split("/")[0]], "a feed is failing outright"
    _compare("feed_status.txt", "".join(lines))
