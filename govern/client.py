"""Thin Devin enterprise API client (verified endpoints) with retry + dry-run.

All endpoints here were confirmed live against a dedicated enterprise deployment
except ``remove_user_from_org`` (see its docstring).
"""
from __future__ import annotations

import json
import time
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

from .config import Config


class DevinClient:
    def __init__(self, token: str, base_url: str, *, dry_run: bool = False,
                 max_retries: int = 5, backoff: float = 2.0, sleep: float = 0.1,
                 read_concurrency: int = 8, apply_concurrency: int = 8):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep  # inter-mutation delay used by the applier
        # Parallel workers for network-latency-bound per-user reads (read_actual).
        # Each request is independent, so concurrent calls are safe; 429s are
        # still retried with backoff in _request.
        self.read_concurrency = read_concurrency
        # Parallel workers for applying a plan: the per-USER change groups in
        # apply_plan run concurrently (changes WITHIN a user stay sequential).
        # Like reads, the mutations are network-latency bound and independent
        # across users; 429s are retried with backoff in _request.
        self.apply_concurrency = apply_concurrency

    @classmethod
    def from_config(cls, cfg: Config, *, dry_run: bool = False) -> "DevinClient":
        """Build a client from a Config's [api] settings (retry + concurrency).

        The single place the resilience/concurrency knobs are read, shared by the
        govern.py CLI and report.py. ``dry_run`` is plumbed through for the
        plan/apply engine; read-only callers (report.py) leave it False."""
        api = cfg.api
        return cls(
            cfg.token, cfg.base_url, dry_run=dry_run,
            max_retries=int(api.get("max_retries", 5)),
            backoff=float(api.get("retry_backoff_seconds", 2.0)),
            sleep=float(api.get("rate_limit_sleep_seconds", 0.1)),
            read_concurrency=int(api.get("read_concurrency", 8)),
            apply_concurrency=int(api.get("apply_concurrency", 8)),
        )

    # ---- low-level ----
    def _request(self, method: str, path: str, *, body=None, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        attempt = 0
        while True:
            try:
                req = Request(url, data=data, headers=headers, method=method)
                with urlopen(req) as resp:
                    if resp.status == 204:
                        return None
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else None
            except HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else self.backoff * (attempt + 1)
                    time.sleep(wait)
                    attempt += 1
                    continue
                detail = e.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e
            except URLError as e:
                raise RuntimeError(f"Network error {method} {url}: {e.reason}") from e

    def _paginate(self, path: str, params=None) -> list[dict]:
        out: list[dict] = []
        params = dict(params or {})
        params.setdefault("first", 200)
        cursor = None
        while True:
            if cursor:
                params["after"] = cursor
            r = self._request("GET", path, params=params)
            out.extend(r.get("items", []))
            if not r.get("has_next_page"):
                break
            cursor = r.get("end_cursor")
            if not cursor:
                break
        return out

    @staticmethod
    def _uid(user_id: str) -> str:
        return quote(user_id, safe="")

    # ---- reads (verified) ----
    def list_organizations(self) -> list[dict]:
        return self._paginate("/v3/enterprise/organizations")

    def list_org_members(self, org_id: str) -> list[dict]:
        return self._paginate(f"/v3/enterprise/organizations/{org_id}/members/users")

    def list_enterprise_members(self) -> list[dict]:
        # Returns each user with full role_assignments (1 enterprise + N org roles).
        return self._paginate("/v3/enterprise/members/users")

    def list_roles(self) -> list[dict]:
        return self._paginate("/v3/enterprise/roles")

    def get_user_limit(self, user_id: str) -> dict:
        # {} when no override; {"local_agent": {"cycle_acu_limit": <int>}} otherwise.
        return self._request("GET", f"/v3beta1/enterprise/users/{self._uid(user_id)}/consumption/acu-limits")

    def get_user_utilization(self, user_id: str, time_after: Optional[int] = None,
                             time_before: Optional[int] = None) -> dict:
        return self._request(
            "GET", f"/v3/enterprise/consumption/daily/users/{self._uid(user_id)}",
            params={"time_after": time_after, "time_before": time_before},
        )

    def list_audit_logs(self, *, after=None, time_after=None, action=None, order="asc") -> dict:
        return self._request(
            "GET", "/v3/enterprise/audit-logs",
            params={"after": after, "time_after": time_after, "action": action,
                    "order": order, "first": 200},
        )

    def list_all_audit_logs(self, *, action=None, time_after=None, order="asc") -> list[dict]:
        """Paginate ALL audit-log entries, optionally filtered to one ``action``.

        The paginating cousin of ``list_audit_logs`` (single page + cursor): it
        follows ``end_cursor`` to the end and returns the flat list of entries,
        like the other ``list_*`` reads. With no ``time_after`` the API returns
        the full available history — so ``action="login"`` yields every login
        event ever recorded for the enterprise."""
        return self._paginate(
            "/v3/enterprise/audit-logs",
            {"action": action, "time_after": time_after, "order": order},
        )

    # ---- writes (verified) — respect dry_run ----
    def invite_users(self, emails: list[str], enterprise_role_id: str):
        """Bulk-invite new enterprise users by email, granting them an enterprise
        role (POST /v3/enterprise/members/users). Returns the created User
        objects (each with its new ``user_id``). Does NOT place anyone in an org;
        follow with add_user_to_org. The API accepts up to 100 emails per call."""
        return self._mutate(
            "POST", "/v3/enterprise/members/users",
            body={"emails": emails, "enterprise_role_id": enterprise_role_id},
        )

    def set_user_limit(self, user_id: str, acu_limit: Optional[int]):
        return self._mutate(
            "PATCH", f"/v3beta1/enterprise/users/{self._uid(user_id)}/consumption/acu-limits",
            body={"local_agent": {"cycle_acu_limit": acu_limit}},
        )

    def set_enterprise_role(self, user_id: str, role_id: str):
        return self._mutate("PATCH", f"/v3/enterprise/members/users/{self._uid(user_id)}",
                            body={"role_id": role_id})

    def set_org_role(self, org_id: str, user_id: str, role_id: str):
        return self._mutate(
            "PATCH", f"/v3/enterprise/organizations/{org_id}/members/users/{self._uid(user_id)}",
            body={"role_id": role_id},
        )

    def add_user_to_org(self, org_id: str, user_id: str, role_id: str):
        # Assign Organization User: POST to the item path with the role (verified
        # live; POST to the collection path returns 405).
        return self._mutate(
            "POST", f"/v3/enterprise/organizations/{org_id}/members/users/{self._uid(user_id)}",
            body={"role_id": role_id},
        )

    def remove_user_from_org(self, org_id: str, user_id: str):
        """Delete a user's DIRECT org role (verified live). IDP-group-derived
        memberships cannot be removed this way — manage those via IDP config."""
        return self._mutate(
            "DELETE", f"/v3/enterprise/organizations/{org_id}/members/users/{self._uid(user_id)}")

    def _mutate(self, method: str, path: str, *, body=None):
        if self.dry_run:
            return {"dry_run": True, "method": method, "path": path, "body": body}
        return self._request(method, path, body=body)
