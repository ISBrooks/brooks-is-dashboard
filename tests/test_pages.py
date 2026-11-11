"""Every tab has its own address (web/router.js). The server must serve the
page at each of them, keep the sign-in wall in front, and stay in step with the
router's list."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parents[1] / "web"
ROUTER = (WEB / "router.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def client(app):
    return TestClient(app.app)          # not used as a context manager: no background threads


def test_every_tab_address_serves_the_page(app, client):
    for page in app._APP_PAGES:
        for path in (f"/{page}", f"/{page}/anything/below"):
            r = client.get(path)
            assert r.status_code == 200 and '<script src="/app.js' in r.text, path
            assert "no-cache" in r.headers["cache-control"], path
    assert '<script src="/app.js' in client.get("/").text


def test_unknown_address_is_not_found(client):
    assert client.get("/no-such-page").status_code == 404
    assert client.get("/marketingg").status_code == 404


def test_assets_load_from_any_address(client):
    # relative paths would break at /marketing/spend-roas
    assert not re.search(r'(?:href|src)="(?!/|#|https?:|data:)[^"]', INDEX)
    for asset in ("/app.js", "/router.js", "/style.css", "/brand/mark-sky.png"):
        assert client.get(asset).status_code == 200, asset


def test_sign_in_wall_covers_tab_addresses(app, client, monkeypatch):
    monkeypatch.setattr(app, "_settings", dataclasses.replace(app._settings, app_password="secret"))
    r = client.get("/marketing/spend-roas")
    assert r.status_code == 200 and '<script src="/app.js' not in r.text      # the sign-in page instead
    assert "location.reload()" in r.text                                       # ...which returns here after signing in
    assert client.get("/api/home").status_code == 401


def test_server_and_router_agree_on_top_level_words(app):
    slugs = set(re.findall(r'slug: "([a-z-]*)"', ROUTER)) - {""}
    assert slugs == set(app._APP_PAGES)


def test_every_tab_and_sub_tab_has_an_address():
    """Adding a button to index.html without a line in router.js fails here."""
    for key in re.findall(r'<button data-tab="(\w+)"', INDEX):
        assert re.search(rf"\b{key}:\s+\{{ slug:", ROUTER), f"tab {key} has no address"
    for attr in ("asub", "msub", "sub", "gsub", "psub", "mksub", "ssub", "mesub"):
        for key in re.findall(rf'data-{attr}="(\w+)"', INDEX):
            assert re.search(rf'\b{key}: "[a-z-]+"', ROUTER), f"sub-tab {attr}={key} has no address"
