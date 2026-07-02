#!/usr/bin/env python3
"""Disable Windsurf (SCIM ``active: false``) for EVERY user — never deletes.

Standalone: talks to the Windsurf / Codeium SCIM 2.0 API directly (standard
library only), independent of the ``govern/`` package and the Devin enterprise
API it drives. This is a different service and a different credential.

What it does
    1. GET   /Users            list every SCIM user (paginated)
    2. PATCH /Users/<id>       set ``active: false`` on each still-active user
It NEVER issues DELETE, so no user is removed — only deactivated (their seat is
freed and access revoked). Re-enable later by PATCHing ``active: true``.

Pagination note
    The Windsurf SCIM server currently reports ``totalResults`` as the size of
    the *current page* (capped at ``count``), not the total number of users. So
    a client that trusts ``totalResults`` stops after the first page (e.g. at
    exactly 100 users) even when more exist. This script therefore IGNORES
    ``totalResults`` for termination: it keeps requesting pages, advancing
    ``startIndex`` by the number of results actually returned, and stops only
    when a page comes back empty. It also de-duplicates by user id and bails out
    if a page adds no new users, so a server that ignores ``startIndex`` can't
    make it loop forever.

Credentials come from the environment, a local ``.env`` (real env vars win), or
``--token`` / ``--base-url``. NOTE this is a Windsurf *service key* ("api secret
key" from windsurf.com/team/settings, with Team User Read/Update), NOT the
``cog_`` Devin token used by govern.py:

    WINDSURF_SCIM_TOKEN=<service key>
    WINDSURF_SCIM_BASE_URL=https://server.codeium.com/scim/v2   # default; for a
        # self-hosted portal use https://<portal>/_route/api_server/scim/v2

Usage:
    python disable_windsurf_scim.py --dry-run     # preview only, change nothing
    python disable_windsurf_scim.py               # confirm, then disable everyone
    python disable_windsurf_scim.py --yes         # skip the prompt (automation)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://server.codeium.com/scim/v2"
CONTENT_TYPE = "application/scim+json"
PATCHOP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
# The body that deactivates one user (and only that — no delete, no other field).
DISABLE_OP = {"schemas": [PATCHOP_SCHEMA],
              "Operations": [{"op": "replace", "path": "active", "value": False}]}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path: Optional[str] = None) -> None:
    """Minimal .env loader (KEY=VALUE, optional quotes / ``export``); real env
    vars always win. Mirrors govern.config.load_dotenv so both share a format."""
    if path is None:
        for cand in (os.path.join(os.getcwd(), ".env"), os.path.join(SCRIPT_DIR, ".env")):
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
                val = val[1:-1]
            if key not in os.environ:
                os.environ[key] = val


class ScimClient:
    """Tiny SCIM client: list users + disable one, with 429/5xx retry+backoff."""

    def __init__(self, token: str, base_url: str, *, max_retries: int = 5,
                 backoff: float = 2.0, page_size: int = 100):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff = backoff
        self.page_size = page_size

    def _request(self, method: str, path: str, *, body=None, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": CONTENT_TYPE, "Accept": CONTENT_TYPE}
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
                    time.sleep(self._retry_wait(e, attempt))
                    attempt += 1
                    continue
                detail = e.read().decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e
            except URLError as e:
                # Transient DNS/connection blip: retry with backoff like a 5xx.
                if attempt < self.max_retries:
                    time.sleep(self.backoff * (attempt + 1))
                    attempt += 1
                    continue
                raise RuntimeError(f"Network error {method} {url}: {e.reason}") from e

    def _retry_wait(self, err: HTTPError, attempt: int) -> float:
        retry_after = err.headers.get("Retry-After")
        try:
            return float(retry_after) if retry_after else self.backoff * (attempt + 1)
        except (TypeError, ValueError):  # Retry-After as an HTTP-date, not seconds
            return self.backoff * (attempt + 1)

    def list_users(self) -> list[dict]:
        """Every SCIM user, paged by ``startIndex``/``count``.

        Deliberately does NOT trust the response's ``totalResults`` to decide
        when to stop — the Windsurf SCIM server reports it as the current page
        size, which would cut listing off after the first page. Instead we keep
        fetching, advancing ``startIndex`` by the number of rows actually
        returned, and stop only when a page is empty. De-duplication by ``id``
        (plus a "no new users this page" guard) protects against a server that
        ignores ``startIndex`` and keeps returning the same page."""
        users: list[dict] = []
        seen: set[str] = set()
        start_index = 1
        for _ in range(1_000_000):  # hard stop against a misbehaving pager
            resp = self._request("GET", "/Users",
                                  params={"startIndex": start_index, "count": self.page_size}) or {}
            page = resp.get("Resources") or []
            got = len(page)
            if got == 0:
                break  # empty page => we've seen everyone

            new_this_page = 0
            for u in page:
                uid = u.get("id")
                # Fall back to userName if the server omits id, so de-dup still works.
                key = uid if uid is not None else u.get("userName")
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                users.append(u)
                new_this_page += 1

            if new_this_page == 0:
                # Server returned only users we've already seen (e.g. it ignored
                # startIndex). Advancing further would loop forever — stop here.
                break

            # Advance by the page length so we cope with servers that cap count.
            start_index += got
        return users

    def disable_user(self, user_id: str):
        return self._request(
            "PATCH", f"/Users/{quote(user_id, safe='')}", body=DISABLE_OP)


def is_active(user: dict) -> bool:
    """SCIM ``active`` as a bool (default True when absent; tolerate string form)."""
    val = user.get("active", True)
    if isinstance(val, str):
        return val.strip().lower() != "false"
    return bool(val)


def label_of(user: dict) -> str:
    return user.get("userName") or user.get("id") or "<unknown>"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="disable_windsurf_scim",
        description="Disable Windsurf (SCIM active=false) for every user. Never "
                    "deletes anyone.")
    p.add_argument("--token", help="Windsurf SCIM service key "
                                   "(default: $WINDSURF_SCIM_TOKEN / .env)")
    p.add_argument("--base-url", dest="base_url",
                   help=f"SCIM base URL (default: $WINDSURF_SCIM_BASE_URL "
                        f"or {DEFAULT_BASE_URL})")
    p.add_argument("--dry-run", action="store_true",
                   help="list who WOULD be disabled and exit; change nothing")
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompt (required for non-interactive runs)")
    p.add_argument("--concurrency", type=int, default=8,
                   help="parallel PATCH workers (default: 8)")
    p.add_argument("--page-size", dest="page_size", type=int, default=100,
                   help="SCIM list page size (default: 100)")
    p.add_argument("--max-retries", dest="max_retries", type=int, default=5,
                   help="retries on 429/5xx per request (default: 5)")
    args = p.parse_args(argv)

    if args.concurrency < 1:
        p.error("--concurrency must be >= 1")
    if args.page_size < 1:
        p.error("--page-size must be >= 1")

    load_dotenv()
    token = args.token or os.environ.get("WINDSURF_SCIM_TOKEN", "")
    base_url = args.base_url or os.environ.get("WINDSURF_SCIM_BASE_URL", DEFAULT_BASE_URL)
    if not token:
        p.error("no SCIM token: set WINDSURF_SCIM_TOKEN (see .env) or pass --token")

    client = ScimClient(token, base_url, max_retries=args.max_retries,
                        page_size=args.page_size)

    print(f"SCIM host: {client.base_url}")
    print("Listing users ...")
    try:
        users = client.list_users()
    except RuntimeError as e:
        print(f"ERROR: could not list users: {e}", file=sys.stderr)
        return 1

    to_disable = [u for u in users if is_active(u)]
    already = len(users) - len(to_disable)
    print(f"Found {len(users)} user(s): {len(to_disable)} active to disable, "
          f"{already} already disabled.")

    if not to_disable:
        print("Nothing to do — no active users.")
        return 0

    if args.dry_run:
        print(f"\n[dry-run] Would disable {len(to_disable)} user(s):")
        for u in to_disable:
            print(f"  - {label_of(u)}")
        print("\n[dry-run] No changes made.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("ERROR: refusing to disable everyone non-interactively without "
                  "--yes (or use --dry-run to preview).", file=sys.stderr)
            return 1
        print(f"\nThis will DISABLE Windsurf access for {len(to_disable)} user(s) "
              f"on {client.base_url}.")
        print("Users are only deactivated (active=false), never deleted.")
        if input("Type DISABLE to proceed: ").strip() != "DISABLE":
            print("Aborted — no changes made.")
            return 1

    print(f"\nDisabling {len(to_disable)} user(s) ...")
    ok, failures = 0, []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(client.disable_user, u["id"]): u for u in to_disable}
        for i, fut in enumerate(as_completed(futures), 1):
            u = futures[fut]
            try:
                fut.result()
                ok += 1
                print(f"  [{i}/{len(to_disable)}] disabled {label_of(u)}")
            except Exception as e:  # noqa: BLE001 — collect every failure, keep going
                failures.append((u, e))
                print(f"  [{i}/{len(to_disable)}] FAILED   {label_of(u)}: {e}",
                      file=sys.stderr)

    print(f"\nDone: {ok} disabled, {already} already disabled, "
          f"{len(failures)} failed.")
    if failures:
        print("Failed users (re-run to retry):", file=sys.stderr)
        for u, e in failures:
            print(f"  - {label_of(u)}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
