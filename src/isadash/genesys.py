"""Genesys Cloud client for the ISA dashboard.

Auth + conversation-details patterns proven in Form_Lead_DeDupe; this adds
agent + wrap-up extraction and the users / wrap-up-code lookups.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)


class GenesysError(RuntimeError):
    pass


def _strip_tel(addr: str | None) -> str | None:
    if not addr:
        return None
    val = addr.strip()
    for prefix in ("tel:", "sip:"):
        if val.lower().startswith(prefix):
            val = val[len(prefix):]
    return val.split("@", 1)[0]


class GenesysClient:
    def __init__(self, region: str, client_id: str, client_secret: str, timeout: float = 60.0):
        self.region = region.strip()
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GenesysClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _authenticate(self) -> None:
        resp = self._http.post(
            f"https://login.{self.region}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        if resp.status_code >= 400:
            raise GenesysError(f"OAuth failed: HTTP {resp.status_code}: {resp.text[:200]}")
        self._token = resp.json().get("access_token")
        if not self._token:
            raise GenesysError("OAuth response had no access_token.")

    def _request(self, method: str, path: str, **kw) -> Any:
        if not self._token:
            self._authenticate()
        url = f"https://api.{self.region}{path}"
        for attempt in (1, 2):
            resp = self._http.request(
                method, url, headers={"Authorization": f"Bearer {self._token}"}, **kw
            )
            if resp.status_code == 401 and attempt == 1:
                self._authenticate()
                continue
            if resp.status_code == 429 and attempt == 1:
                import time
                wait = int(resp.headers.get("Retry-After", "30"))
                log.warning("Genesys 429, sleeping %ss", wait)
                time.sleep(wait)
                continue
            break
        if resp.status_code >= 400:
            raise GenesysError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # --- lookups -------------------------------------------------------------
    def list_users(self) -> dict[str, str]:
        """All users (any state, so departed agents still resolve) as {id: name}."""
        out: dict[str, str] = {}
        page = 1
        while True:
            data = self._request(
                "GET", f"/api/v2/users?pageSize=100&pageNumber={page}&state=any"
            )
            for u in data.get("entities", []):
                out[u["id"]] = u.get("name") or u["id"]
            if page >= int(data.get("pageCount", 1)):
                break
            page += 1
        return out

    def list_wrapup_codes(self) -> dict[str, str]:
        """All wrap-up codes as {id: name}."""
        out: dict[str, str] = {}
        page = 1
        while True:
            data = self._request(
                "GET", f"/api/v2/routing/wrapupcodes?pageSize=100&pageNumber={page}"
            )
            for w in data.get("entities", []):
                out[w["id"]] = w.get("name") or w["id"]
            if page >= int(data.get("pageCount", 1)):
                break
            page += 1
        return out

    # --- queue operational stats ---------------------------------------------
    def queue_stats(self, start_utc: datetime, end_utc: datetime,
                    queue_ids: list[str]) -> dict:
        """Aggregate voice stats across the queues (combined): service level,
        average after-call-work (seconds), answer % and abandon %."""
        body = {
            "interval": f"{start_utc:%Y-%m-%dT%H:%M:%SZ}/{end_utc:%Y-%m-%dT%H:%M:%SZ}",
            "groupBy": ["queueId"],
            "filter": {"type": "and", "clauses": [
                {"type": "or", "predicates":
                    [{"dimension": "queueId", "value": q} for q in queue_ids]},
                {"type": "and", "predicates":
                    [{"dimension": "mediaType", "value": "voice"}]}]},
            "metrics": ["nOffered", "tAnswered", "tAbandon", "tAcw", "oServiceLevel"],
        }
        data = self._request(
            "POST", "/api/v2/analytics/conversations/aggregates/query", json=body)
        offered = answered = abandoned = acw_sum = acw_n = 0
        sl_num = sl_den = 0
        for grp in data.get("results", []):
            for d in grp.get("data", []):
                for m in d.get("metrics", []):
                    st = m.get("stats", {})
                    name = m.get("metric")
                    if name == "nOffered":
                        offered += st.get("count", 0)
                    elif name == "tAnswered":
                        answered += st.get("count", 0)
                    elif name == "tAbandon":
                        abandoned += st.get("count", 0)
                    elif name == "tAcw":
                        acw_sum += st.get("sum", 0); acw_n += st.get("count", 0)
                    elif name == "oServiceLevel":
                        sl_num += st.get("numerator", 0)
                        sl_den += st.get("denominator", 0)
        return {
            "offered": offered,
            "service_level": round(100.0 * sl_num / sl_den, 1) if sl_den else None,
            "avg_acw_sec": round(acw_sum / acw_n / 1000.0, 0) if acw_n else None,
            "answer_pct": round(100.0 * answered / offered, 1) if offered else None,
            "abandon_pct": round(100.0 * abandoned / offered, 1) if offered else None,
        }

    def service_level_series(self, start_utc: datetime, end_utc: datetime,
                             queue_ids: list[str], granularity: str = "P7D") -> list[dict]:
        """Service-level % bucketed by `granularity` (default weekly) over the
        range — ONE aggregates query, for the home trend sparkline."""
        body = {
            "interval": f"{start_utc:%Y-%m-%dT%H:%M:%SZ}/{end_utc:%Y-%m-%dT%H:%M:%SZ}",
            "granularity": granularity,
            "groupBy": ["queueId"],
            "filter": {"type": "and", "clauses": [
                {"type": "or", "predicates":
                    [{"dimension": "queueId", "value": q} for q in queue_ids]},
                {"type": "and", "predicates":
                    [{"dimension": "mediaType", "value": "voice"}]}]},
            "metrics": ["oServiceLevel"],
        }
        data = self._request(
            "POST", "/api/v2/analytics/conversations/aggregates/query", json=body)
        buckets: dict[str, list] = {}  # interval-start -> [numerator, denominator]
        for grp in data.get("results", []):
            for d in grp.get("data", []):
                start = (d.get("interval") or "").split("/")[0]
                for m in d.get("metrics", []):
                    if m.get("metric") == "oServiceLevel":
                        st = m.get("stats", {})
                        b = buckets.setdefault(start, [0, 0])
                        b[0] += st.get("numerator", 0)
                        b[1] += st.get("denominator", 0)
        out = []
        for start in sorted(buckets):
            num, den = buckets[start]
            out.append({"start": start[:10],
                        "service_level": round(100.0 * num / den, 1) if den else None})
        return out

    # --- conversations --------------------------------------------------------
    def query_conversations(self, interval: str, queue_ids: list[str],
                            max_pages: int = 400) -> list[dict]:
        """Conversation Detail records touching any of the queues, one interval
        (<=31 days). Both queues go in one query as OR predicates."""
        conversations: list[dict] = []
        page = 1
        while page <= max_pages:
            body = {
                "interval": interval,
                "order": "asc",
                "segmentFilters": [{
                    "type": "or",
                    "predicates": [
                        {"dimension": "queueId", "value": q} for q in queue_ids
                    ],
                }],
                "paging": {"pageSize": 100, "pageNumber": page},
            }
            data = self._request(
                "POST", "/api/v2/analytics/conversations/details/query", json=body
            )
            batch = data.get("conversations", [])
            conversations.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        else:
            log.warning("Hit max_pages=%s for interval %s — data may be truncated",
                        max_pages, interval)
        return conversations

    def query_conversations_range(self, start: datetime, end: datetime,
                                  queue_ids: list[str]) -> list[dict]:
        """Chunked into <=30-day windows (Genesys caps one query at 31 days).
        start/end are aware UTC datetimes."""
        out: list[dict] = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=30), end)
            interval = f"{cur:%Y-%m-%dT%H:%M:%SZ}/{nxt:%Y-%m-%dT%H:%M:%SZ}"
            batch = self.query_conversations(interval, queue_ids)
            log.info("Genesys %s -> %d conversations", interval, len(batch))
            out.extend(batch)
            cur = nxt
        # Dedup on conversationId (chunk boundaries can double-report).
        seen: dict[str, dict] = {}
        for c in out:
            seen[c["conversationId"]] = c
        return list(seen.values())


def extract_call(conv: dict) -> dict | None:
    """Flatten one conversation-detail record into the fields we keep.

    Returns None if no agent ever handled it (abandoned / IVR-only) — those
    aren't calls "taken" by anyone. The dispositioning agent is the LAST agent
    participant with a wrap-up code; if nobody dispositioned, the last agent
    with an interact (talk) segment gets credit with wrapup=None.
    """
    ani = dnis = None
    for p in conv.get("participants", []):
        if p.get("purpose") != "customer":
            continue
        for s in p.get("sessions", []):
            if s.get("mediaType") not in (None, "voice"):
                continue
            ani = ani or s.get("ani")
            dnis = dnis or s.get("dnis")

    handler_id: str | None = None       # last agent who talked
    dispo_agent: str | None = None      # last agent who set a wrap-up code
    wrapup_id: str | None = None
    for p in conv.get("participants", []):
        if p.get("purpose") != "agent" or not p.get("userId"):
            continue
        for s in p.get("sessions", []):
            if s.get("mediaType") not in (None, "voice"):
                continue
            for seg in s.get("segments", []):
                if seg.get("segmentType") == "interact":
                    handler_id = p["userId"]
                if seg.get("wrapUpCode"):
                    dispo_agent = p["userId"]
                    wrapup_id = seg["wrapUpCode"]

    agent_id = dispo_agent or handler_id
    if not agent_id:
        return None
    return {
        "conversation_id": conv["conversationId"],
        "start_utc": conv.get("conversationStart"),
        "ani": _strip_tel(ani),
        "dnis": _strip_tel(dnis),
        "agent_user_id": agent_id,
        "wrapup_id": wrapup_id,
    }
