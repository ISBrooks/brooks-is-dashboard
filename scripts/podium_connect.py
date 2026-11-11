"""Connect the dashboard to Podium (one time), for the Master Metric tab.

Run on your own computer, from the dashboard folder:

    python scripts\\podium_connect.py

What it does:
  1. Opens your browser on Podium's approval page. Sign in if asked, then
     click Authorize.
  2. The browser then lands on a page that says "This site can't be reached".
     That is expected. Copy the whole address from the address bar and paste
     it here.
  3. The script saves the new connection into the dashboard's .env file
     (nothing secret is shown on screen) and checks which Podium data it can
     now read.

It uses the same Podium app as the FieldRoutes->Podium sync, but stores its
own token for the dashboard. At the end it also confirms the sync's existing
connection still works.
"""
from __future__ import annotations

import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DASH_ENV = ROOT / ".env"
SYNC_ENV = ROOT.parent / "Podium_Pestroutes_Sync_Project" / ".env"

AUTH_URL = "https://accounts.podium.com/oauth/authorize"
TOKEN_URL = "https://accounts.podium.com/oauth/token"
REDIRECT_URI = "https://localhost:8765/callback"       # already registered on the Podium app
API = "https://api.podium.com/v4"
# what the sync already had, plus reading users, conversations/messages, phone calls and reporting
SCOPES = ["read_contacts", "write_contacts", "read_locations", "read_users", "read_messages",
          "read_phones", "read_reporting"]


def _set_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, ln in enumerate(lines):
        key = ln.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    extra = [f"{k}={v}" for k, v in values.items() if k not in seen]
    if extra:
        lines += ["", "# Podium (Master Metric) — written by scripts/podium_connect.py"] + extra
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    scopes = sys.argv[1:] or SCOPES
    sync = dotenv_values(SYNC_ENV) if SYNC_ENV.exists() else {}
    dash = dotenv_values(DASH_ENV)
    cid = dash.get("PODIUM_CLIENT_ID") or sync.get("PODIUM_CLIENT_ID")
    csec = dash.get("PODIUM_CLIENT_SECRET") or sync.get("PODIUM_CLIENT_SECRET")
    if not cid or not csec:
        print("Couldn't find the Podium app's client ID/secret in either .env file.")
        return 2

    state = secrets.token_urlsafe(24)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": " ".join(scopes), "state": state})
    print("\nOpening Podium in your browser. Sign in if asked, then click Authorize.")
    print("If nothing opens, copy this address into your browser:\n")
    print("  " + url + "\n")
    print("Afterwards the browser will say \"This site can't be reached\" — that's expected.")
    print("Copy the WHOLE address from the address bar and paste it below.\n")
    webbrowser.open(url)
    try:
        pasted = input("Paste the address here: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    q = dict(urllib.parse.parse_qsl(pasted.split("?", 1)[1] if "?" in pasted else pasted))
    if q.get("error"):
        print(f"\nPodium refused: {q.get('error')} — {q.get('error_description', '')}")
        print("If it mentions a scope, tell Claude which one; that permission isn't switched on for the app.")
        return 1
    if q.get("state") != state or not q.get("code"):
        print("\nThat address doesn't match this run (or has no code in it). Run the script again.")
        return 1

    r = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": q["code"],
                                    "redirect_uri": REDIRECT_URI, "client_id": cid,
                                    "client_secret": csec}, timeout=30)
    if r.status_code >= 400 or not r.json().get("refresh_token"):
        print(f"\nPodium wouldn't issue a token (HTTP {r.status_code}). Run the script again; "
              "the address can only be used once and expires quickly.")
        return 1
    body = r.json()
    _set_env(DASH_ENV, {"PODIUM_CLIENT_ID": cid, "PODIUM_CLIENT_SECRET": csec,
                        "PODIUM_REFRESH_TOKEN": body["refresh_token"]})
    print("\nSaved the new connection to the dashboard's .env file.")
    print("Permissions granted:", body.get("scope"))

    h = {"Authorization": f"Bearer {body['access_token']}"}
    print("\nWhat it can read now:")
    for label, path in (("Users", "/users?limit=1"), ("Conversations", "/conversations?limit=1"),
                        ("Phone calls", "/calls?limit=1")):
        try:
            code = httpx.get(API + path, headers=h, timeout=30).status_code
        except httpx.HTTPError as e:
            code = type(e).__name__
        print(f"  {label:14} {'yes' if code == 200 else 'NO (' + str(code) + ')'}")

    if sync.get("PODIUM_REFRESH_TOKEN"):
        old = httpx.post(TOKEN_URL, data={"grant_type": "refresh_token",
                                          "refresh_token": sync["PODIUM_REFRESH_TOKEN"],
                                          "client_id": cid, "client_secret": csec}, timeout=30)
        print("\nThe FieldRoutes->Podium sync's own connection:",
              "still works" if old.status_code == 200 else
              f"NO LONGER WORKS (HTTP {old.status_code}) — tell Claude right away")
    print("\nDone. Tell Claude it's connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
