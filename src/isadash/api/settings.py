"""Settings tab (agent roster, tiers and Power Score, Software Management) and the Sync button.

One of the modules webapp.py is assembled from. Split out of the original
single webapp.py on 2026-11-11: the code here was moved, not rewritten.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from ..store import connect

from .common import (
    _DEFAULT_SCORECARD_TIERS, _admin_ok, _allowed, _load_config, _num, _settings, _validate_power,
    app,
)


@app.get("/api/config")
def api_config() -> dict:
    """Scorecard tiers + power-score weights. Readable by any signed-in user
    (both the scorecard and power-score views need them to render)."""
    conn = connect(_settings.db_path)
    try:
        return _load_config(conn)
    finally:
        conn.close()


@app.post("/api/config")
def api_config_update(body: dict, request: Request):
    """Save edited tiers / power-score weights. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        if "scorecard_tiers" in body:
            _save_config(conn, "scorecard_tiers", _validate_tiers(body["scorecard_tiers"]))
        if "power_score" in body:
            _save_config(conn, "power_score", _validate_power(body["power_score"]))
        return _load_config(conn)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/config/reset")
def api_config_reset(body: dict, request: Request):
    """Restore a section to its built-in defaults. Admin only."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    key = body.get("key")
    if key not in ("scorecard_tiers", "power_score"):
        return JSONResponse({"error": "unknown key"}, status_code=400)
    conn = connect(_settings.db_path)
    try:
        conn.execute("DELETE FROM app_config WHERE key = ?", (key,))
        conn.commit()
        return _load_config(conn)
    finally:
        conn.close()


@app.post("/api/sync")
def api_sync_start(request: Request) -> dict:
    # Admins: unlimited. Managers (app password): allowed, but only when no
    # sync has STARTED in the last hour (any trigger — nightly timer, admin,
    # or manager — counts; data younger than an hour doesn't need a re-pull).
    # The FieldRoutes client self-paces at 300 req/min (1/3 of the shared
    # tenant cap) and start_sync refuses concurrent runs, so this cadence is
    # comfortably inside rate limits.
    if not _admin_ok(request):
        import json as _j
        from ..store import get_config
        conn = connect(_settings.db_path)
        try:
            last_raw = get_config(conn, "last_sync_started")
        finally:
            conn.close()
        if last_raw:
            try:
                last = datetime.fromisoformat(_j.loads(last_raw))
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            except (ValueError, TypeError):
                elapsed = 1e9
            if elapsed < 3600:
                mins = max(1, int((3600 - elapsed) // 60) + 1)
                ago = max(1, int(elapsed // 60))
                return JSONResponse(
                    {"error": "cooldown", "retry_minutes": mins,
                     "message": f"Data was synced {ago} min ago — next sync "
                                f"available in {mins} min."},
                    status_code=429)
    from ..sync import start_sync
    started = start_sync(_settings)
    return {"started": started}


@app.get("/api/sync/status")
def api_sync_status() -> dict:
    from ..sync import STATE
    return {k: v for k, v in STATE.items()}


def _validate_tiers(t: dict) -> dict:
    import copy
    out = copy.deepcopy(_DEFAULT_SCORECARD_TIERS)
    if not isinstance(t, dict):
        raise ValueError("scorecard_tiers must be an object")
    for metric, band in t.items():
        if metric not in out or not isinstance(band, dict):
            continue
        for k in ("t1", "t2", "min"):
            if band.get(k) is not None:
                out[metric][k] = _num(band[k])
    return out


def _save_config(conn, key: str, value) -> None:
    import json
    conn.execute("INSERT INTO app_config(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def _resolve_fr_ids(fr, name: str) -> set:
    """Find a person's FieldRoutes employee IDs (one per office) by name."""
    parts = name.split()
    if len(parts) < 2:
        return set()
    fname, lname = parts[0].lower(), parts[-1]
    ids = set()
    for oid in _settings.pestroutes_office_ids:
        got = fr.request("employee", "search",
                         {"lname": lname, "officeIDs": oid, "includeData": 1})
        for e in got.get("employees") or []:
            if (e.get("fname") or "").strip().lower().startswith(fname):
                ids.add(str(e.get("employeeID")))
    return ids


@app.get("/api/software")
def api_software(request: Request):
    """Software Management: status of every Brooks app, gathered ON DEMAND over
    SSH when the tab opens (a few seconds; nothing polls in the background).
    Admin-only — it exposes infrastructure."""
    if not _allowed(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    from .. import software
    return software.collect(_settings)


@app.post("/api/software/run")
def api_software_run(request: Request, body: dict):
    """Run one allow-listed action on the app's host. The agent on that host
    enforces the same allow-list, so nothing outside it can execute."""
    if not _allowed(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    from .. import software
    action = str((body or {}).get("action") or "")
    res = software.run_action(_settings, action)
    logging.getLogger("isadash.software").info(
        "software action %s by admin -> %s", action, res.get("message") or res.get("error"))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/settings")
def api_settings(request: Request) -> dict:
    """Agent roster config: key agents with their flags, plus candidates to add."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    conn = connect(_settings.db_path)
    try:
        agents = []
        for r in conn.execute(
            "SELECT genesys_user_id, genesys_name, fr_employee_id, active, "
            "powerscore, team FROM agent_map WHERE scorecard = 1 "
            "ORDER BY genesys_name"):
            agents.append({
                "id": r["genesys_user_id"], "name": r["genesys_name"],
                "fr_ids": len((r["fr_employee_id"] or "").split(","))
                          if r["fr_employee_id"] else 0,
                "active": bool(r["active"]), "powerscore": bool(r["powerscore"]),
                "team": r["team"] or ""})
        # candidates to ADD = every office-staff SELLER (FieldRoutes type-0
        # employee) with their lifetime revenue, so you search the people who
        # actually sell subscriptions — not the Genesys user list.
        tracked = {a["name"] for a in agents}
        rev: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.name, SUM(s.contract_value) v FROM subscriptions s "
            "JOIN employees e ON e.fr_employee_id = s.sold_by "
            "WHERE e.emp_type = '0' AND s.contract_value > 0 GROUP BY e.name"):
            rev[r["name"]] = r["v"] or 0
        fr_count: dict[str, int] = {}
        for r in conn.execute(
            "SELECT name, COUNT(*) c FROM employees WHERE emp_type = '0' "
            "AND name IS NOT NULL AND name != '' GROUP BY name"):
            fr_count[r["name"]] = r["c"]
        sellers = sorted(
            [{"name": n, "fr_count": c, "revenue": round(rev.get(n, 0)),
              "tracked": n in tracked} for n, c in fr_count.items()],
            key=lambda s: -s["revenue"])
        return {"agents": agents, "sellers": sellers}
    finally:
        conn.close()


@app.post("/api/settings/add")
def api_settings_add(body: dict, request: Request) -> dict:
    """Add an office-staff seller (by name) as a tracked key agent — resolves
    their FieldRoutes IDs from the employees cache; reuses a matching Genesys
    user id if one exists, else a synthetic key."""
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "missing name"}
    conn = connect(_settings.db_path)
    try:
        ids = sorted({r[0] for r in conn.execute(
            "SELECT fr_employee_id FROM employees WHERE name = ? AND emp_type = '0'",
            (name,)) if r[0]})
        if not ids:
            return {"error": "no FieldRoutes IDs found for that seller"}
        row = conn.execute("SELECT genesys_user_id FROM agent_map WHERE genesys_name = ? LIMIT 1",
                           (name,)).fetchone()
        gid = row[0] if row else "fr:" + name
        conn.execute(
            """INSERT INTO agent_map
                 (genesys_user_id, genesys_name, fr_employee_id, on_roster,
                  scorecard, active, powerscore, team)
               VALUES (?, ?, ?, 1, 1, ?, ?, ?)
               ON CONFLICT(genesys_user_id) DO UPDATE SET
                 genesys_name = excluded.genesys_name,
                 fr_employee_id = excluded.fr_employee_id, scorecard = 1,
                 on_roster = 1,
                 active = excluded.active, powerscore = excluded.powerscore,
                 team = excluded.team""",
            (gid, name, ",".join(ids), 1 if body.get("active", True) else 0,
             1 if body.get("powerscore") else 0, body.get("team") or None))
        conn.commit()
        return {"ok": True, "id": gid, "fr_ids": len(ids)}
    finally:
        conn.close()


@app.post("/api/settings/agent")
def api_settings_update(body: dict, request: Request) -> dict:
    if not _allowed(request):
        return JSONResponse({"error": "locked"}, status_code=403)
    gid = body.get("id")
    if not gid:
        return {"error": "missing id"}
    conn = connect(_settings.db_path)
    try:
        cur = conn.execute(
            "SELECT genesys_name, fr_employee_id FROM agent_map "
            "WHERE genesys_user_id = ?", (gid,)).fetchone()
        if not cur:
            return {"error": "unknown agent"}
        sets, args = [], []
        for f in ("scorecard", "active", "powerscore"):
            if f in body:
                sets.append(f"{f}=?"); args.append(1 if body[f] else 0)
        if "team" in body:
            sets.append("team=?"); args.append(body["team"] or None)
        resolved = None
        # promoting to key with no FR ids yet -> resolve them from FieldRoutes
        if body.get("scorecard") and not cur["fr_employee_id"]:
            from ..fieldroutes import FieldRoutesClient
            fr = FieldRoutesClient(_settings.pestroutes_base_url,
                                   _settings.pestroutes_auth_key,
                                   _settings.pestroutes_auth_token)
            try:
                ids = _resolve_fr_ids(fr, cur["genesys_name"])
            finally:
                fr.close()
            if ids:
                sets.append("fr_employee_id=?"); args.append(",".join(sorted(ids)))
                resolved = len(ids)
        if sets:
            args.append(gid)
            conn.execute(f"UPDATE agent_map SET {', '.join(sets)} "
                         "WHERE genesys_user_id = ?", args)
            conn.commit()
        return {"ok": True, "resolved_fr_ids": resolved}
    finally:
        conn.close()
