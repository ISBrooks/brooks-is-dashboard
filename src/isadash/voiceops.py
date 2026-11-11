"""VoiceOps client — AI call-grading scores for the ISA dashboard.

VoiceOps sits on top of Genesys, transcribes every call and grades it against
models we configure. We pull the per-rep aggregate from their Reporting API
(`GET /external/v1/reporting/reps`), whose `quality_score` is the weighted-mean
adherence across the skills classified as Quality.

TWO API QUIRKS, BOTH VERIFIED LIVE 2026-09-08. They are the whole reason this
module exists instead of a few inline httpx calls:

1. `end_date` is EXCLUSIVE, even though the published docs say "inclusive".
   Proof: any range with start == end returns NO rows at all (a zero-width
   window), and start=Sun end=Mon returns only Sunday's calls. So requesting
   Sun..Sat the "documented" way silently DROPS Saturday — and Saturday is a
   real working day here (~480 calls org-wide). `_window()` always sends
   end = last_day + 1 day.

2. Bare `YYYY-MM-DD` parameters are interpreted as UTC midnight, but full ISO
   timestamps ARE honoured to the second (a 12:00:00Z start returned its first
   call at 12:00:19Z). A Pacific Sunday-Saturday week is
   [Sun 00:00 PT, next Sun 00:00 PT) = 07:00Z in PDT / 08:00Z in PST, so we
   always send explicit UTC instants derived from the Pacific calendar. Sending
   bare dates would shift every window 7-8 hours and bleed the previous
   Saturday evening into the week — exactly what this metric must not do.

Rate limit is 180 req/min ORG-WIDE across every OAuth endpoint, shared with the
customer's other VoiceOps integrations, so we self-pace well under it.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .periods import PACIFIC, UTC

log = logging.getLogger(__name__)

BASE_URL = "https://app.voiceops.com/voiceops-api"


def _retry_after(resp) -> float | None:
    """Seconds from a Retry-After header, when the server tells us to wait."""
    raw = resp.headers.get("Retry-After") or resp.headers.get("X-Rate-Limit-Reset")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if 0 < val <= 120 else None


class VoiceOpsError(RuntimeError):
    pass


def _window(start_day: str, end_day: str) -> tuple[str, str]:
    """Pacific INCLUSIVE day range -> the UTC instants the API wants.

    Callers speak the dashboard's language everywhere else: an inclusive
    Pacific 'YYYY-MM-DD' pair. Here that becomes
    [start 00:00 Pacific, end+1day 00:00 Pacific) expressed in UTC, which
    handles both the exclusive-end quirk and DST in one place.
    """
    s = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=PACIFIC)
    e = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=PACIFIC) + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (s.astimezone(UTC).strftime(fmt), e.astimezone(UTC).strftime(fmt))


class VoiceOpsClient:
    def __init__(self, client_id: str, client_secret: str, timeout: float = 90.0,
                 max_per_min: int = 60):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._http = httpx.Client(timeout=timeout)
        # a third of the org-wide 180/min ceiling, matching how the FieldRoutes
        # client leaves headroom for the other apps sharing the tenant
        self._min_gap = 60.0 / max(1, max_per_min)
        self._last_call = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "VoiceOpsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- auth -----------------------------------------------------------
    def _authenticate(self) -> None:
        resp = self._http.post(
            f"{BASE_URL}/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": self._client_id,
                  "client_secret": self._client_secret,
                  "scope": "READ_ONLY"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise VoiceOpsError(f"OAuth failed: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        self._token = body.get("accessToken")
        if not self._token:
            raise VoiceOpsError(f"OAuth response had no accessToken: {str(body)[:200]}")
        exp = body.get("accessTokenExpiresAt")
        try:
            self._token_exp = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            self._token_exp = datetime.now(UTC) + timedelta(minutes=10)

    def _ensure_token(self) -> str:
        # refresh a minute early so a long sync never trips over an expiry
        if (not self._token or not self._token_exp
                or datetime.now(UTC) >= self._token_exp - timedelta(seconds=60)):
            self._authenticate()
        return self._token or ""

    # Transient conditions worth another go. The vendor 504s on heavy pages and
    # occasionally just stalls for longer than the read timeout; a backfill makes
    # hundreds of calls, so one bad minute must not abort a ten-minute run.
    _RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def _get(self, path: str, params: dict[str, Any], attempts: int = 4) -> dict:
        """GET with bounded retry and backoff.

        Anything still failing after `attempts` is raised as a VoiceOpsError,
        which every caller already treats as "skip this row and carry on" —
        so a persistent failure costs one agent-period, never the whole job.
        """
        delay = 2.0
        last = "no attempt made"
        for attempt in range(1, attempts + 1):
            gap = self._min_gap - (time.monotonic() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            try:
                resp = self._http.get(
                    f"{BASE_URL}{path}", params=params,
                    headers={"Authorization": f"Bearer {self._ensure_token()}"})
                self._last_call = time.monotonic()
            except httpx.HTTPError as e:          # read timeout, reset, DNS…
                self._last_call = time.monotonic()
                last = f"{type(e).__name__}: {str(e)[:120]}"
            else:
                if resp.status_code == 401:       # expired / rejected token
                    self._token = None
                    last = "HTTP 401 (re-authenticating)"
                elif resp.status_code in self._RETRY_STATUS:
                    last = f"HTTP {resp.status_code}"
                    after = _retry_after(resp)
                    if after is not None:
                        delay = max(delay, after)
                elif resp.status_code >= 400:     # 4xx: our bug, retrying won't help
                    raise VoiceOpsError(
                        f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    return resp.json()
            if attempt < attempts:
                log.warning("voiceops %s: %s - retry %d/%d in %.0fs",
                            path, last, attempt, attempts - 1, delay)
                time.sleep(delay)
                delay = min(delay * 2.5, 30.0)
        raise VoiceOpsError(f"GET {path} failed after {attempts} attempts: {last}")

    # ---- reporting ------------------------------------------------------
    def rep_summary(self, email: str, start_day: str, end_day: str) -> dict | None:
        """One rep's aggregate scores over an inclusive Pacific day range.

        Returns None when the rep has no scorable calls in the window — the API
        omits such reps entirely, which is normal (PTO, outbound-only reps).
        """
        start, end = _window(start_day, end_day)
        body = self._get("/external/v1/reporting/reps",
                         {"start_date": start, "end_date": end,
                          "rep_email": email, "limit": 5})
        rows = body.get("data") or []
        if not rows:
            return None
        s = rows[0].get("summary") or {}
        return {"call_count": s.get("call_count"),
                "call_score": s.get("call_score"),
                "quality_score": s.get("quality_score"),
                "compliance_score": s.get("compliance_score")}

    def all_reps(self, start_day: str, end_day: str, page_size: int = 25) -> list[dict]:
        """Every rep with scorable calls in the window (paginated).

        page_size stays small on purpose: limit=250 makes their backend exceed
        CloudFront's timeout and return a 504. 25 is comfortably fast.
        """
        start, end = _window(start_day, end_day)
        out: list[dict] = []
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"start_date": start, "end_date": end,
                                      "limit": max(1, min(page_size, 100))}
            if cursor is not None:
                params["cursor"] = cursor
            body = self._get("/external/v1/reporting/reps", params)
            out.extend(body.get("data") or [])
            pg = body.get("pagination") or {}
            if not pg.get("has_more"):
                return out
            cursor = pg.get("next_cursor")
            if cursor is None:
                return out


# ---- roster + cache -----------------------------------------------------
# Scores are cached per (period, agent) in voiceops_scores. A full-roster pull
# is ~27 sequential API calls, so nothing user-facing may fetch inline; the
# nightly sync warms the periods the UI asks for by default, and anything else
# (a custom picker range) is fetched once and then served from cache forever.

def mapped_roster(conn) -> list[tuple[str, str]]:
    """(agent_name, voiceops_email) for scorecard agents that have an email.

    Agents without a mapped email are simply absent — outbound-only reps are
    never graded by VoiceOps and should read blank rather than zero.
    """
    return [(r["genesys_name"], r["voiceops_email"]) for r in conn.execute(
        "SELECT genesys_name, voiceops_email FROM agent_map "
        "WHERE scorecard = 1 AND voiceops_email IS NOT NULL AND voiceops_email <> '' "
        "ORDER BY genesys_name")]


def needs_refresh(conn, start_day: str, end_day: str, *, settle_days: int = 3,
                  max_age_h: float = 1.0) -> bool:
    """True when the period is still live (ends within settle_days of today, or
    later) and a saved score for it is older than max_age_h - the same test
    cached_scores applies before re-pulling."""
    today_pac = datetime.now(PACIFIC).date()
    if end_day < (today_pac - timedelta(days=settle_days)).isoformat():
        return False
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_h)).isoformat()
    row = conn.execute("SELECT MIN(fetched_at) FROM voiceops_scores "
                       "WHERE start_day = ? AND end_day = ?", (start_day, end_day)).fetchone()
    return bool(row and row[0] and row[0] < cutoff)


def cached_scores(conn, client: "VoiceOpsClient | None", start_day: str, end_day: str,
                  *, refresh: bool = False, settle_days: int = 3,
                  max_age_h: float = 1.0) -> dict[str, dict]:
    """Per-agent scores for an inclusive Pacific day range, cache-first.

    Returns {agent_name: {call_count, call_score, quality_score, compliance_score}}.
    Agents the API omits (no scorable calls — PTO, or never graded) are cached
    with call_count 0 and NULL scores so they are not re-requested every night;
    a NULL quality_score keeps them out of every average, which is exactly the
    "counts if they have data, blank if not" rule.

    client=None serves whatever is cached and fetches nothing, so read paths can
    stay fast and never block on VoiceOps being up.
    """
    roster = mapped_roster(conn)
    out: dict[str, dict] = {}
    if not roster:
        return out
    # A period is SETTLED once its last day is a few days old (VoiceOps grading
    # trickles in for ~3 days) and a settled row is good forever. An UNSETTLED
    # period — ending recently, today, or in the future, e.g. a custom picker
    # range — must never be frozen at first view: its rows are re-pulled once
    # they are older than max_age_h, when a client is available. Cache-only
    # readers (client=None, e.g. Home) serve whatever is cached.
    today_pac = datetime.now(PACIFIC).date()
    unsettled = end_day >= (today_pac - timedelta(days=settle_days)).isoformat()
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_h)).isoformat()
    if not refresh:
        for r in conn.execute(
                "SELECT agent, call_count, call_score, quality_score, compliance_score, fetched_at "
                "FROM voiceops_scores WHERE start_day = ? AND end_day = ?",
                (start_day, end_day)):
            if unsettled and client is not None and (r["fetched_at"] or "") < cutoff:
                continue                      # stale for a live period -> re-fetched below
            out[r["agent"]] = {"call_count": r["call_count"], "call_score": r["call_score"],
                               "quality_score": r["quality_score"],
                               "compliance_score": r["compliance_score"]}
    missing = [(n, e) for n, e in roster if refresh or n not in out]
    if not missing or client is None:
        return out

    now = datetime.now(UTC).isoformat()
    # Circuit breaker: each failed agent has already burned its own retries, so
    # a vendor outage would otherwise cost ~20s x every agent x every period.
    # Three failures in a row means VoiceOps is down, not that one rep is odd —
    # abandon the period and let the next run pick it up.
    consecutive = 0
    for name, email in missing:
        try:
            got = client.rep_summary(email, start_day, end_day)
        except VoiceOpsError as e:
            log.warning("voiceops %s %s..%s: %s", name, start_day, end_day, e)
            consecutive += 1
            if consecutive >= 3:
                log.warning("voiceops: 3 consecutive failures - abandoning %s..%s",
                            start_day, end_day)
                break
            continue
        consecutive = 0
        row = got or {"call_count": 0, "call_score": None,
                      "quality_score": None, "compliance_score": None}
        conn.execute(
            """INSERT INTO voiceops_scores (start_day, end_day, agent, call_count,
                                            call_score, quality_score, compliance_score, fetched_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(start_day, end_day, agent) DO UPDATE SET
                   call_count=excluded.call_count, call_score=excluded.call_score,
                   quality_score=excluded.quality_score,
                   compliance_score=excluded.compliance_score,
                   fetched_at=excluded.fetched_at""",
            (start_day, end_day, name, row["call_count"], row["call_score"],
             row["quality_score"], row["compliance_score"], now))
        # Commit per row. The write lock must NEVER be held across the next
        # agent's HTTP call: a whole period is ~40s (longer with retries), which
        # outlasts the 30s busy_timeout and fails any concurrent sync with
        # "database is locked". Per-row commits hold it for milliseconds.
        conn.commit()
        out[name] = row
    return out


def weighted_quality(rows) -> float | None:
    """Call-weighted mean quality score over rows that actually have one.

    Weighting by call_count (not a flat average) is what reproduces VoiceOps'
    own published team number — their score is a weighted-mean adherence, so a
    rep with 6 calls must not move the team as much as one with 160.
    """
    num = den = 0.0
    for r in rows:
        q, n = r.get("quality_score"), r.get("call_count") or 0
        if q is None or n <= 0:
            continue
        num += q * n
        den += n
    return round(num / den, 1) if den else None


def pacific_weeks(n: int, anchor: "date | None" = None) -> list[tuple[str, str]]:
    """The last n COMPLETE Sunday-Saturday Pacific weeks, oldest first.

    'Complete' means the week ended before today, so a half-finished current
    week never lands on a trend chart.
    """
    from datetime import date as _date
    today = anchor or datetime.now(PACIFIC).date()
    # most recent Sunday that has already started this week's *next* week:
    # step back to this week's Sunday, then one more week for the last complete one
    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    out: list[tuple[str, str]] = []
    for i in range(n, 0, -1):
        s = this_sunday - timedelta(days=7 * i)
        out.append((s.isoformat(), (s + timedelta(days=6)).isoformat()))
    return out
