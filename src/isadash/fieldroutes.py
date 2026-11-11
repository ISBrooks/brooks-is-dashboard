"""FieldRoutes client — subscription/customer/employee reads only.

Quirks inherited from the sibling projects:
- search is per-office (officeIDs required or results silently drop); get is global
- a get with exactly one ID returns nothing — pad to two
- datetimes are office-local (Pacific), not UTC
- shared rate limit 900 reads/min with the dedupe + Podium sync jobs
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
OFFICE_TZ = ZoneInfo("America/Los_Angeles")


class FieldRoutesError(RuntimeError):
    pass


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class FieldRoutesClient:
    def __init__(self, base_url: str, auth_key: str, auth_token: str,
                 timeout: float = 60.0, max_reads_per_min: int = 300):
        self.base_url = base_url.rstrip("/")
        self._auth = {"authenticationKey": auth_key, "authenticationToken": auth_token}
        self._http = httpx.Client(timeout=timeout)
        # Self-imposed pace well under the 900/min tenant cap, since the
        # nightly dedupe and 5-min Podium sync share the same tenant limit.
        self._min_interval = 60.0 / max_reads_per_min
        self._last_call = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FieldRoutesClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, entity: str, action: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{entity}/{action}"
        last_err: Exception | None = None
        for attempt in range(5):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self._http.get(url, params={**self._auth, **(params or {})})
            except httpx.HTTPError as e:  # transport blip — retry
                last_err = e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):  # transient
                last_err = FieldRoutesError(
                    f"{entity}/{action} -> HTTP {resp.status_code}")
                log.warning("FR %s/%s HTTP %s, retry %d/4",
                            entity, action, resp.status_code, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise FieldRoutesError(
                    f"{entity}/{action} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise FieldRoutesError(f"{entity}/{action} failed after 5 attempts: {last_err}")

    @staticmethod
    def _id_param(ids: list[str]) -> str:
        items = [str(i) for i in ids]
        if len(items) == 1:
            items = items * 2
        return ",".join(items)

    def pull_subscriptions(self, since: str, until: str, office_ids: list[str]) -> list[dict]:
        """All subscriptions with dateAdded in [since, until] across offices."""
        date_filter = json.dumps({"operator": "BETWEEN", "value": [since, until]})
        sub_ids: list[str] = []
        for oid in office_ids:
            got = self.request("subscription", "search",
                               {"dateAdded": date_filter, "officeIDs": oid})
            ids = got.get("subscriptionIDs") or []
            log.info("FR office %s: %d subscriptions %s..%s", oid, len(ids), since, until)
            sub_ids.extend(str(i) for i in ids)
        sub_ids = list(dict.fromkeys(sub_ids))
        records: list[dict] = []
        for batch in _chunks(sub_ids, 200):
            got = self.request("subscription", "get",
                               {"subscriptionIDs": self._id_param(batch)})
            records.extend(got.get("subscriptions") or [])
        return records

    def ids_updated_since(self, entity: str, since_local: str,
                          office_ids: list[str]) -> list[str]:
        """IDs of `entity` records with dateUpdated >= since_local, deduped across
        offices. since_local is office-local Pacific ('YYYY-MM-DD HH:MM:SS').
        entity is 'subscription' or 'customer'. Powers the nightly changed-since
        delta so rep reassignments / edits / cancellations of ANY age flow in."""
        id_key = {"subscription": "subscriptionIDs", "customer": "customerIDs"}[entity]
        filt = json.dumps({"operator": ">=", "value": since_local})
        seen: set[str] = set()
        out: list[str] = []
        for oid in office_ids:
            got = self.request(entity, "search",
                               {"officeIDs": oid, "dateUpdated": filt})
            for i in got.get(id_key) or []:
                s = str(i)
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return out

    def signed_subscription_ids(self, cust_ids_by_office: dict[str, list[str]]) -> set[str]:
        """Subscription IDs that have a COMPLETED (truly signed) FR contract.

        The real signed-agreement signal, learned from the Podium sync app:
        `/contract/search` MUST be given officeIDs (else FR silently scopes to
        office 1 and returns 0 for everyone else), and list filters must be
        JSON arrays — not the comma form the /get endpoints accept.
        """
        signed: set[str] = set()
        for office_id, cust_ids in cust_ids_by_office.items():
            cust_ids = list(dict.fromkeys(str(c) for c in cust_ids))
            contract_ids: list[str] = []
            for batch in _chunks(cust_ids, 200):
                got = self.request("contract", "search", {
                    "officeIDs": json.dumps([int(office_id)]),
                    "customerIDs": json.dumps([int(x) for x in batch])})
                contract_ids.extend(str(x) for x in (got.get("contractIDs") or []))
            contract_ids = list(dict.fromkeys(contract_ids))
            for batch in _chunks(contract_ids, 200):
                got = self.request("contract", "get", {
                    "contractIDs": json.dumps([int(x) for x in batch])})
                contracts = got.get("contracts")
                if isinstance(contracts, dict):
                    contracts = list(contracts.values())
                for c in contracts or []:
                    if c.get("documentState") == "COMPLETED" and c.get("dateSigned") \
                            not in (None, "", "0000-00-00", "0000-00-00 00:00:00"):
                        sid = str(c.get("subscriptionIDs") or c.get("subscriptionID") or "").strip()
                        if sid:
                            signed.add(sid)
        return signed

    def get_customers(self, customer_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for batch in _chunks(customer_ids, 200):
            got = self.request("customer", "get", {"customerIDs": self._id_param(batch)})
            out.extend(got.get("customers") or [])
        return out

    def get_employees(self, employee_ids: list[str]) -> dict[str, str]:
        """{employeeID: 'Fname Lname'}"""
        out: dict[str, str] = {}
        for batch in _chunks(employee_ids, 200):
            got = self.request("employee", "get", {"employeeIDs": self._id_param(batch)})
            for e in got.get("employees") or []:
                name = f"{(e.get('fname') or '').strip()} {(e.get('lname') or '').strip()}".strip()
                out[str(e.get("employeeID"))] = name
        return out


def parse_fr_datetime_to_utc(value: str | None) -> datetime | None:
    """FR datetimes are office-local Pacific; return aware UTC."""
    if not value or value.startswith("0000"):
        return None
    try:
        local = datetime.fromisoformat(value).replace(tzinfo=OFFICE_TZ)
    except ValueError:
        return None
    return local.astimezone(ZoneInfo("UTC"))
