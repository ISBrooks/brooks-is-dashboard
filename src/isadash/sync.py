"""One-click smart sync. Runs every data source in dependency order and
tracks progress for the UI:

  1. audit sheet     — re-import the Google Sheet (fast)
  2. form touches    — run the sourcing app's ingest (gmail/meta/lsa)
  3. calls + subs    — Genesys + FieldRoutes catch-up, then re-match
  4. statuses        — changed-since delta: every Pending sub + every sub whose
                       FieldRoutes dateUpdated is newer than the last sync
                       (reassignments, edits, cancellations of ANY age)
  5. customers       — changed-since delta: every customer whose dateUpdated is
                       newer than the last sync (new + changed: autopay, status…)

A watermark in app_config (sync_delta_watermark) tracks the last successful run;
a failed run doesn't advance it, so the window is retried rather than skipped.

State lives in a module-level dict the /api/sync/status endpoint reads.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

from .periods import PACIFIC as _PACIFIC

STATE: dict = {"running": False, "step": None, "steps": [], "started": None,
               "finished": None, "error": None, "summary": {}}
_LOCK = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STEPS = ["Audit sheet", "Form touches", "Calls & subscriptions",
         "Subscription statuses", "Customers"]


def _run_script(args: list[str], cwd: Path, timeout: int = 900,
                env_extra: dict | None = None) -> str:
    import os
    env = {**os.environ, **(env_extra or {})}
    r = subprocess.run([sys.executable, *args], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(r.stderr or r.stdout)[-400:]}")
    return r.stdout


def _step(name: str):
    STATE["step"] = name
    STATE["steps"].append({"name": name, "started": datetime.now(timezone.utc).isoformat()})
    log.info("sync step: %s", name)


def _finish_step(note: str = ""):
    STATE["steps"][-1]["done"] = True
    STATE["steps"][-1]["note"] = note


def _sync_worker(settings) -> None:
    from .fieldroutes import FieldRoutesClient, parse_fr_datetime_to_utc, OFFICE_TZ
    from .normalize import normalize_phone
    from .store import connect, upsert_customers, get_config, set_config
    import json as _json
    run_start = datetime.now(timezone.utc)  # watermark advances to here on success
    try:
        # record the start for the manager-sync cooldown (any trigger path:
        # nightly timer, admin button, manager button)
        _c0 = connect(settings.db_path)
        try:
            set_config(_c0, "last_sync_started", _json.dumps(run_start.isoformat()))
        finally:
            _c0.close()
        # 1. audit sheet ------------------------------------------------------
        _step("Audit sheet")
        url = getattr(settings, "audit_sheet_url", "")
        if url:
            out = _run_script(["scripts/import_audit_matrix.py", "--url", url],
                              PROJECT_ROOT, timeout=180)
            note = next((l for l in out.splitlines() if l.startswith("audit_daily")), "ok")
        else:
            note = "skipped (no AUDIT_SHEET_URL)"
        _finish_step(note)

        # 2. form touches ------------------------------------------------------
        # The sourcing app (lead-attribution) owns channel ingestion. When it's
        # co-located WITH ITS OWN venv/cron (the Sourcing-App droplet), it
        # ingests on its own 3AM schedule and writes attribution back to
        # PestRoutes LIVE — so the dashboard must NOT run a second ingest here
        # (that would double the write-back). We only read its DB. If a bare
        # copy of the scripts exists without a venv, run it ourselves. Never
        # fatal — a form-ingest hiccup must not abort the rest of the sync.
        _step("Form touches")
        app_dir = Path(settings.formleads_db).parent.parent
        ingest = app_dir / "scripts" / "ingest_touches.py"
        if not ingest.exists():
            _finish_step("skipped (sourcing app not found)")
        elif (app_dir / ".venv").exists():
            _finish_step("handled by the sourcing app's own schedule")
        else:
            try:
                out = _run_script(
                    ["scripts/ingest_touches.py", "5", "gmail,meta,lsa"],
                    app_dir, timeout=600,
                    env_extra={"DB_PATH": str(Path(settings.formleads_db).relative_to(app_dir))
                               if str(settings.formleads_db).startswith(str(app_dir))
                               else settings.formleads_db})
                added = [l.strip() for l in out.splitlines() if "added" in l]
                _finish_step("; ".join(added) or "ok")
            except Exception as e:  # non-fatal: keep syncing the rest
                _finish_step(f"skipped ({str(e)[:80]})")

        # 3. calls + subs + matcher -------------------------------------------
        _step("Calls & subscriptions")
        _run_script(["scripts/backfill.py", "--weeks", "2"], PROJECT_ROOT, timeout=900)
        _finish_step("ok")

        conn = connect(settings.db_path)
        fr = FieldRoutesClient(settings.pestroutes_base_url,
                               settings.pestroutes_auth_key,
                               settings.pestroutes_auth_token)
        try:
            # 4. changed-since delta ------------------------------------------
            # Pull every subscription whose dateUpdated is newer than our last
            # successful sync (rep reassignments, source/status/contract edits,
            # cancellations — of ANY age), plus every still-Pending sub as a
            # belt-and-suspenders. A watermark in app_config makes this a true
            # change-data delta rather than a guessed rolling window.
            _step("Subscription statuses")
            wm_raw = get_config(conn, "sync_delta_watermark")
            since_dt = (datetime.fromisoformat(_json.loads(wm_raw)) if wm_raw
                        else run_start - timedelta(days=2))  # short first-run window
            # 6h overlap + office-local (Pacific) format for the dateUpdated filter
            since_local = ((since_dt - timedelta(hours=6)).astimezone(OFFICE_TZ)
                           .strftime("%Y-%m-%d %H:%M:%S"))
            delta_sub_ids = fr.ids_updated_since(
                "subscription", since_local, settings.pestroutes_office_ids)
            pending_ids = [r[0] for r in conn.execute(
                "SELECT subscription_id FROM subscriptions "
                "WHERE initial_status = 'Pending' OR initial_status IS NULL")]
            # safety net: everything sold in the last 45 days is re-read too, so
            # a contract-value / agreement edit that doesn't bump dateUpdated
            # still lands within the pay cycle (2026-10-08 reconciliation)
            recent_ids = [r[0] for r in conn.execute(
                "SELECT subscription_id FROM subscriptions WHERE date_added_utc >= ?",
                ((run_start - timedelta(days=45)).isoformat(),))]
            ids = list(dict.fromkeys(pending_ids + delta_sub_ids + recent_ids))
            # FR employee id -> name, to keep sold_by_name in step with sold_by
            emp = {r[0]: r[1] for r in conn.execute(
                "SELECT fr_employee_id, name FROM employees")}
            done = 0
            sub_appt: dict[str, str] = {}  # sub_id -> initial appointment id
            for i in range(0, len(ids), 200):
                got = fr.request("subscription", "get", {
                    "subscriptionIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
                rows = []
                for r in got.get("subscriptions") or []:
                    try:
                        ip = float(r.get("initialServiceTotal") or 0)
                    except (TypeError, ValueError):
                        ip = 0.0
                    cancelled = parse_fr_datetime_to_utc(r.get("dateCancelled"))
                    sid = str(r.get("subscriptionID"))
                    aid = str(r.get("initialAppointmentID") or "0")
                    if aid != "0":
                        sub_appt[sid] = aid
                    sb = str(r.get("soldBy") or "")

                    def _int(v):
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            return None
                    try:
                        cv = float(r.get("contractValue") or 0)
                    except (TypeError, ValueError):
                        cv = 0.0
                    rows.append((r.get("initialStatusText"), r.get("source"), ip,
                                 1 if str(r.get("active")) == "1" else 0,
                                 cancelled.isoformat() if cancelled else None,
                                 aid, _int(r.get("billingFrequency")),
                                 _int(r.get("frequency")),
                                 # contract value + agreement length were never
                                 # refreshed by the delta before 2026-10-08, so
                                 # PestRoutes edits drifted (≈$52K over 2026 YTD)
                                 cv, _int(r.get("agreementLength")) or 0,
                                 sb, emp.get(sb), sid))
                conn.executemany(
                    "UPDATE subscriptions SET initial_status=?, source=?, "
                    "initial_price=?, active=?, date_cancelled_utc=?, "
                    "initial_appointment_id=?, "
                    # billing/service frequency feed Monthly Billing % — they
                    # were never refreshed before (NULL on most subs)
                    "billing_frequency=?, service_frequency=?, "
                    "contract_value=?, agreement_length=?, "
                    # keep attribution current when a sale is reassigned to a
                    # different rep in PestRoutes (don't wipe on a blank/0 soldBy)
                    "sold_by=COALESCE(NULLIF(NULLIF(?, ''), '0'), sold_by), "
                    "sold_by_name=COALESCE(?, sold_by_name) "
                    "WHERE subscription_id=?", rows)
                conn.commit()
                done += len(rows)
            # refresh initial service dates for these subs (pending ones can
            # reschedule; serviced ones lock in)
            appt_ids = list(dict.fromkeys(sub_appt.values()))
            appt_date: dict[str, str] = {}
            for i in range(0, len(appt_ids), 200):
                got = fr.request("appointment", "get", {
                    "appointmentIDs": FieldRoutesClient._id_param(appt_ids[i:i + 200])})
                for ap in got.get("appointments") or []:
                    dd = ap.get("date")
                    if dd and not str(dd).startswith("0000"):
                        appt_date[str(ap.get("appointmentID"))] = str(dd)[:10]
            conn.executemany(
                "UPDATE subscriptions SET initial_service_date=? WHERE subscription_id=?",
                [(appt_date.get(a), s_) for s_, a in sub_appt.items() if appt_date.get(a)])
            conn.commit()

            # real signed-agreement signal — a COMPLETED /contract record for
            # the subscription, not the draft-inflated contract_added flag.
            # SCOPE: the refreshed ids PLUS everything sold in the last 45 days.
            # A customer signing days after the sale does NOT bump the sub's
            # dateUpdated (the contract is its own record), so the delta alone
            # would miss late signatures on already-serviced subs.
            recent_cut = (run_start - timedelta(days=45)).isoformat()
            recent_ids = [r[0] for r in conn.execute(
                "SELECT subscription_id FROM subscriptions "
                "WHERE date_added_utc >= ? AND initial_status IN ('Completed','Pending')",
                (recent_cut,))]
            sa_ids = list(dict.fromkeys(ids + recent_ids))
            cust_by_office: dict[str, list] = {}
            sub_ids_scoped: list[str] = []
            for i in range(0, len(sa_ids), 500):
                batch = sa_ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                for sub_id, cust_id, office_id in conn.execute(
                    f"SELECT s.subscription_id, s.customer_id, c.office_id "
                    f"FROM subscriptions s JOIN customers c ON c.customer_id = s.customer_id "
                    f"WHERE s.subscription_id IN ({ph}) "
                    f"AND c.office_id IS NOT NULL AND c.office_id != ''", batch):
                    cust_by_office.setdefault(str(office_id), []).append(str(cust_id))
                    sub_ids_scoped.append(str(sub_id))
            signed_set = fr.signed_subscription_ids(cust_by_office) if cust_by_office else set()
            conn.executemany(
                "UPDATE subscriptions SET signed_agreement=? WHERE subscription_id=?",
                [(1 if s_ in signed_set else 0, s_) for s_ in sub_ids_scoped])
            conn.commit()
            signed_n = sum(1 for s_ in sub_ids_scoped if s_ in signed_set)

            _finish_step(f"{len(delta_sub_ids)} changed + {len(pending_ids)} pending "
                         f"= {done} subs refreshed, {len(appt_date)} service dates, "
                         f"{signed_n}/{len(sub_ids_scoped)} signed")

            # 5. changed-since customers --------------------------------------
            # Same delta for customers: every customer whose dateUpdated is newer
            # than the watermark (new customers AND changes to existing ones —
            # autopay/card, status, phone/email). dateUpdated is set on create too,
            # so this subsumes the old dateAdded-only "new customers" pull.
            _step("Customers")
            new_ids = fr.ids_updated_since(
                "customer", since_local, settings.pestroutes_office_ids)
            # ALSO refresh the customers of every sub touched this run — a
            # buyer's payment method / status can change without the customer
            # record's own dateUpdated moving, and APAY reads customer fields.
            sub_cust: list[str] = []
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                sub_cust += [r[0] for r in conn.execute(
                    f"SELECT DISTINCT customer_id FROM subscriptions "
                    f"WHERE subscription_id IN ({ph}) AND customer_id IS NOT NULL",
                    batch)]
            new_ids = list(dict.fromkeys(new_ids + sub_cust))
            added = 0
            for i in range(0, len(new_ids), 200):
                got = fr.request("customer", "get", {
                    "customerIDs": FieldRoutesClient._id_param(new_ids[i:i + 200])})
                rows = []
                for c in got.get("customers") or []:
                    dt = parse_fr_datetime_to_utc(c.get("dateAdded"))
                    rows.append({
                        "customer_id": str(c.get("customerID")),
                        "office_id": str(c.get("officeID") or ""),
                        "date_added_utc": dt.isoformat() if dt else None,
                        "status": str(c.get("status")),
                        "phone1_e164": normalize_phone(c.get("phone1")),
                        "phone2_e164": normalize_phone(c.get("phone2")),
                        "email": (c.get("email") or "").strip().lower() or None,
                        "autopay": 0 if str(c.get("aPay") or "No").strip().lower()
                                   in ("no", "", "0") else 1,
                        # APAY = "has a payment method on file" (manager's
                        # PestRoutes column P: Card/Account Number) — enrolled in
                        # autopay is NOT required. autoPayPaymentProfileID catches
                        # ACH + autopay cards; mostRecentCreditCardLastFour catches
                        # cards on file that aren't on autopay. Profile-ID alone
                        # missed those and undercounted APAY badly (76% vs ~87%).
                        "card_on_file": 1 if (
                            str(c.get("autoPayPaymentProfileID") or "0").strip() not in ("", "0")
                            or (c.get("mostRecentCreditCardLastFour") or "").strip()
                        ) else 0,
                    })
                upsert_customers(conn, rows)
                added += len(rows)
            _finish_step(f"{added} customers refreshed/added")

            # 6. employees — refresh EVERY known employee (names/types drift:
            # renames, office<->field moves) and resolve any NEW sellers, so
            # the Settings search + sold_by_name resolution stay current
            _step("Employees")
            new_sellers = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT s.sold_by FROM subscriptions s "
                "WHERE s.sold_by IS NOT NULL AND s.sold_by != '' "
                "AND NOT EXISTS (SELECT 1 FROM employees e "
                "                WHERE e.fr_employee_id = s.sold_by)")]
            known = [r[0] for r in conn.execute("SELECT fr_employee_id FROM employees")]
            emp_ids = list(dict.fromkeys(known + new_sellers))
            emp_rows = []
            for i in range(0, len(emp_ids), 200):
                got = fr.request("employee", "get", {
                    "employeeIDs": FieldRoutesClient._id_param(emp_ids[i:i + 200])})
                for e in got.get("employees") or []:
                    nm = ((e.get("fname") or "") + " " + (e.get("lname") or "")).strip()
                    emp_rows.append((str(e.get("employeeID")), nm, str(e.get("type"))))
            if emp_rows:
                conn.executemany(
                    "INSERT INTO employees(fr_employee_id, name, emp_type) VALUES(?,?,?) "
                    "ON CONFLICT(fr_employee_id) DO UPDATE SET "
                    "name=excluded.name, emp_type=excluded.emp_type", emp_rows)
                conn.commit()
            _finish_step(f"{len(emp_rows)} employees refreshed "
                         f"({len(new_sellers)} new sellers)")
            # 7. VoiceOps quality scores — refresh the recent periods the UI
            # reads. Deliberately LAST and fully non-fatal: it is an external
            # vendor, and a VoiceOps outage must never cost us the FieldRoutes
            # and Genesys work above (nor block the watermark advance).
            # Recent weeks are re-pulled rather than skipped because grading
            # trickles in for days after the calls happen.
            _step("VoiceOps quality")
            try:
                if not (settings.voiceops_client_id and settings.voiceops_client_secret):
                    _finish_step("skipped (no VoiceOps credentials)")
                else:
                    from .voiceops import (VoiceOpsClient, cached_scores,
                                           mapped_roster, pacific_weeks)
                    roster = mapped_roster(conn)
                    if not roster:
                        _finish_step("skipped (no agents mapped to VoiceOps)")
                    else:
                        today = datetime.now(timezone.utc).astimezone(_PACIFIC).date()
                        yday = today - timedelta(days=1)
                        periods = list(pacific_weeks(3, today))
                        # current AND previous window for each, so the Home
                        # bubbles have a prior period to show a delta against
                        for days, offset in ((30, 0), (7, 0), (30, 30), (7, 7)):
                            end = yday - timedelta(days=offset)
                            periods.append((
                                (end - timedelta(days=days - 1)).isoformat(),
                                end.isoformat()))
                        plan = {p: True for p in periods}
                        # the last 7 single days, for the Home daily drop-down.
                        # Only the newest 3 are re-pulled (grading may still be
                        # landing); older days come from cache, so this costs
                        # ~1 new day per night rather than 175 calls.
                        for i in range(1, 8):
                            d = (today - timedelta(days=i)).isoformat()
                            plan[(d, d)] = i <= 3
                        with VoiceOpsClient(settings.voiceops_client_id,
                                            settings.voiceops_client_secret) as vo:
                            for (sd, ed), again in plan.items():
                                cached_scores(conn, vo, sd, ed, refresh=again)
                        _finish_step(f"{len(plan)} periods ({sum(plan.values())} re-pulled) "
                                     f"x {len(roster)} agents")
            except Exception as e:                      # never fail the sync
                log.warning("voiceops step failed: %s", e)
                _finish_step(f"skipped ({str(e)[:80]})")
            # every step succeeded — advance the delta watermark to this run's
            # start so tomorrow only pulls what changed afterward (a failed run
            # never advances it, so the window is retried, not skipped)
            set_config(conn, "sync_delta_watermark", _json.dumps(run_start.isoformat()))
        finally:
            fr.close()
            conn.close()

        STATE["summary"] = {"ok": True}
    except Exception as e:  # surface, don't die silently
        log.exception("sync failed")
        STATE["error"] = str(e)[:400]
    finally:
        STATE["running"] = False
        STATE["step"] = None
        STATE["finished"] = datetime.now(timezone.utc).isoformat()
        try:        # saved results that tolerate a short lag (form leads) refresh now
            from . import cache
            cache.clear()
        except Exception:  # noqa: BLE001 - never let this break a sync's wrap-up
            log.exception("could not clear saved results after sync")
        # Persist this run's step-by-step outcome so the Software Management tab
        # can show, per data feed, what the last sync actually did — durable
        # across restarts, and covering the nightly (a separate process) as well
        # as manager-triggered syncs. Wrapped: bookkeeping must never fail a sync.
        try:
            conn2 = connect(settings.db_path)
            try:
                set_config(conn2, "last_sync_steps", _json.dumps({
                    "started": STATE.get("started"),
                    "finished": STATE.get("finished"),
                    "ok": bool((STATE.get("summary") or {}).get("ok")) and not STATE.get("error"),
                    "error": STATE.get("error"),
                    "steps": [{"name": st.get("name"), "note": st.get("note"),
                               "done": bool(st.get("done"))}
                              for st in STATE.get("steps", [])],
                }))
            finally:
                conn2.close()
        except Exception:  # noqa: BLE001
            log.warning("could not persist last_sync_steps", exc_info=True)


def start_sync(settings) -> bool:
    with _LOCK:
        if STATE["running"]:
            return False
        STATE.update({"running": True, "step": "starting", "steps": [],
                      "started": datetime.now(timezone.utc).isoformat(),
                      "finished": None, "error": None, "summary": {}})
    threading.Thread(target=_sync_worker, args=(settings,), daemon=True).start()
    return True
