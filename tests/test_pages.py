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
            assert r.status_code == 200 and '<script src="/js/main.js?v=' in r.text, path
            assert "no-cache" in r.headers["cache-control"], path
    assert '<script src="/js/main.js?v=' in client.get("/").text


def test_unknown_address_is_not_found(client):
    assert client.get("/no-such-page").status_code == 404
    assert client.get("/marketingg").status_code == 404


def test_assets_load_from_any_address(client):
    # relative paths would break at /marketing/spend-roas
    assert not re.search(r'(?:href|src)="(?!/|#|https?:|data:)[^"]', INDEX)
    for asset in ("/js/core.js", "/js/main.js", "/router.js", "/style.css", "/brand/mark-sky.png"):
        assert client.get(asset).status_code == 200, asset


def test_sign_in_wall_covers_tab_addresses(app, client, monkeypatch):
    monkeypatch.setattr(app, "_settings", dataclasses.replace(app._settings, app_password="secret"))
    r = client.get("/marketing/spend-roas")
    assert r.status_code == 200 and "/js/main.js" not in r.text               # the sign-in page instead
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


def test_scripts_and_styles_carry_a_fingerprint_of_their_contents(app, client, tmp_path, monkeypatch):
    """Browsers keep a script until its contents change, with no version numbers to bump."""
    import hashlib
    import shutil
    page = client.get("/").text
    found = dict(re.findall(r'(?:src|href)="(/(?:js/[\w.-]+\.js|router\.js|style\.css))\?v=(\w{10})"', page))
    listed = set(re.findall(r'<script src="(/[\w./-]+\.js)"', INDEX)) | {"/style.css"}
    assert set(found) == listed and len(found) >= 12
    for path_, tag in found.items():
        assert tag == hashlib.sha1((WEB / path_.lstrip("/")).read_bytes()).hexdigest()[:10], path_

    core = "/js/core.js"
    kept = client.get(f"{core}?v={found[core]}")
    assert "immutable" in kept.headers["cache-control"] and "max-age=31536000" in kept.headers["cache-control"]
    assert "no-cache" in client.get(core).headers["cache-control"]            # without the fingerprint: re-checked
    assert "no-cache" in client.get("/").headers["cache-control"]             # the page itself: always re-checked

    # change a file -> the page hands out a new address for it, without a restart
    copy = tmp_path / "web"
    shutil.copytree(WEB, copy)
    monkeypatch.setattr(app, "WEB_DIR", copy)
    monkeypatch.setitem(app._index_cache, "key", None)
    before = app._index_html()
    (copy / "js" / "home.js").write_text((copy / "js" / "home.js").read_text(encoding="utf-8") + "\n// changed\n",
                                         encoding="utf-8")
    after = app._index_html()
    tag = lambda html, f: re.search(rf'"{re.escape(f)}\?v=(\w+)"', html).group(1)   # noqa: E731
    assert tag(before, "/js/home.js") != tag(after, "/js/home.js")
    assert tag(before, "/js/core.js") == tag(after, "/js/core.js")
    monkeypatch.setitem(app._index_cache, "key", None)


def test_script_files_load_together_in_order():
    """Run scripts/check_web_js.js: a name declared in two files, a file missing
    from index.html, or a load-order problem would stop the page loading."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, str(WEB.parent / "scripts" / "check_web_js.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
