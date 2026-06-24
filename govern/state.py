"""Actual-state reads, membership snapshots, and the append-only audit log."""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
from typing import Any, Callable, Optional

from .config import Config

# Fallback parallelism for per-user reads when the client carries no
# ``read_concurrency`` (e.g. a mock/stub). Real clients set it from [api].
DEFAULT_READ_CONCURRENCY = 8


def _split_role_assignments(member: dict) -> tuple[Optional[dict], dict[str, dict]]:
    """Split a member's role_assignments into (enterprise_role, {org_id: org_role}).

    An assignment is the single enterprise role when its role_type is
    "enterprise" or it carries no org_id; every other assignment is an org role
    keyed by org_id. Shared by read_actual and read_members.

    NOTE: org_ids here may reference orgs absent from the org inventory (orphaned
    memberships observed live); callers preserve them so reconcile/offboard can
    act on them.
    """
    enterprise_role = None
    org_roles: dict[str, dict] = {}
    for a in member.get("role_assignments", []):
        role = a.get("role") or {}
        entry = {"role_id": role.get("role_id"), "role_name": role.get("role_name")}
        if role.get("role_type") == "enterprise" or a.get("org_id") is None:
            enterprise_role = entry
        else:
            org_roles[a["org_id"]] = entry
    return enterprise_role, org_roles


def read_members(client) -> dict[str, dict]:
    """Return {user_id: {email, name, org_ids}} for every enterprise member.

    A lean cousin of read_actual: one call to list_enterprise_members() and NO
    per-user limit lookups (read_actual makes one get_user_limit call each). Use
    it for reports that only need identity + org membership — e.g. the login
    activity report — and don't care about ACU limits or role ids.
    """
    out: dict[str, dict] = {}
    for m in client.list_enterprise_members():
        _enterprise_role, org_roles = _split_role_assignments(m)
        out[m["user_id"]] = {
            "email": m.get("email"),
            "name": m.get("name"),
            "org_ids": sorted(org_roles.keys()),
        }
    return out


def _stderr_progress(label: str, total: int) -> Callable[[int], None]:
    """Return a progress callback that rewrites a single status line on stderr.

    Renders only when stderr is a TTY (so redirected/piped output stays clean)
    and never touches stdout — keeping the actual report pristine. The returned
    callback is meant to be driven from the MAIN thread as work completes, so the
    parallel readers below never write to the console themselves; that keeps
    output safe (no interleaving) even though the fetches run concurrently.
    """
    if not sys.stderr.isatty() or total <= 0:
        return lambda _done: None

    width = len(str(total))

    def report(done: int) -> None:
        end = "\n" if done >= total else ""
        sys.stderr.write(f"\r{label} {done:>{width}}/{total}{end}")
        sys.stderr.flush()

    return report


def _read_user_limits(client, user_ids: list[str], *, workers: int,
                      progress: Optional[Callable[[int], None]] = None,
                      ) -> dict[str, dict]:
    """Fetch each user's raw ACU-limit payload, concurrently.

    Returns {user_id: raw_limit_dict} ({} when the user has no override). The
    per-user get_user_limit calls are network-latency bound, so they run on a
    thread pool (DevinClient holds no per-request state, so it is thread-safe).
    Results are collected — and ``progress`` invoked — on the CALLING thread
    only, so no worker ever writes to stdout/stderr. Errors propagate just as the
    old serial loop did (the first failing fetch raises).
    """
    out: dict[str, dict] = {}
    total = len(user_ids)
    if total == 0:
        return out
    done = 0
    if workers <= 1:  # serial fallback (single-worker config / debugging)
        for uid in user_ids:
            out[uid] = client.get_user_limit(uid) or {}
            done += 1
            if progress:
                progress(done)
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.get_user_limit, uid): uid for uid in user_ids}
        for fut in concurrent.futures.as_completed(futures):
            uid = futures[fut]
            out[uid] = fut.result() or {}
            done += 1
            if progress:
                progress(done)
    return out


def read_actual(client, *, workers: Optional[int] = None,
                progress: bool = True) -> dict[str, dict]:
    """Return {user_id: {email, name, enterprise_role, org_roles, org_ids,
    limit, limit_set}}.

    Enterprise + org roles come from client.list_enterprise_members() (one call);
    the per-user Local Agent limit from client.get_user_limit() (one call each).
    Those per-user lookups dominate wall-clock time on large populations, so they
    are fetched in parallel across ``workers`` threads (defaulting to the
    client's ``read_concurrency``); pass ``workers=1`` for a serial fetch. A
    transient progress line is shown on stderr (TTY only) unless ``progress`` is
    False.

    NOTE: role_assignments may reference org_ids absent from the org inventory
    (orphaned memberships observed live) — they are preserved in org_ids so
    reconcile/offboard can act on them. ``limit`` is None when no override is set (limit_set=False)
    or when the override is explicitly unlimited (limit_set=True, limit=None).
    """
    out: dict[str, dict] = {}
    for m in client.list_enterprise_members():
        uid = m["user_id"]
        enterprise_role, org_roles = _split_role_assignments(m)
        out[uid] = {
            "email": m.get("email"),
            "name": m.get("name"),
            "enterprise_role": enterprise_role,
            "org_roles": org_roles,
            "org_ids": sorted(org_roles.keys()),
            "limit": None,
            "limit_set": False,
        }
    if workers is None:
        workers = getattr(client, "read_concurrency", DEFAULT_READ_CONCURRENCY)
    report = _stderr_progress("fetching limits", len(out)) if progress else None
    limits = _read_user_limits(client, list(out), workers=workers, progress=report)
    for uid, raw in limits.items():
        local_agent = raw.get("local_agent") or {}
        out[uid]["limit"] = local_agent.get("cycle_acu_limit")
        out[uid]["limit_set"] = "local_agent" in raw
    return out


def read_utilizations(client, user_ids: list[str], *, time_after: int,
                      time_before: int, workers: Optional[int] = None,
                      progress: bool = True) -> dict[str, dict]:
    """Return {user_id: raw_utilization_dict} for each user, fetched in parallel.

    One get_user_utilization call per user. Like read_actual's per-user limit
    fetches, these are network-latency bound and independent, so they run on a
    thread pool (DevinClient holds no per-request state, so it is thread-safe)
    sized by the client's ``read_concurrency`` (pass ``workers=1`` for a serial
    fetch). Results are collected — and the transient stderr progress line
    (TTY only, unless ``progress`` is False) advanced — on the CALLING thread
    only, so no worker writes to the console. Errors propagate (first failure
    raises), just as the old serial loop in `usage` did.
    """
    out: dict[str, dict] = {}
    total = len(user_ids)
    if total == 0:
        return out
    if workers is None:
        workers = getattr(client, "read_concurrency", DEFAULT_READ_CONCURRENCY)
    report = _stderr_progress("fetching usage", total) if progress else None
    done = 0
    if workers <= 1:  # serial fallback (single-worker config / debugging)
        for uid in user_ids:
            out[uid] = client.get_user_utilization(
                uid, time_after=time_after, time_before=time_before) or {}
            done += 1
            if report:
                report(done)
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(client.get_user_utilization, uid, time_after, time_before): uid
            for uid in user_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            uid = futures[fut]
            out[uid] = fut.result() or {}
            done += 1
            if report:
                report(done)
    return out


# ---- membership snapshots (reactive move/offboard via snapshot-diff) ----
def snapshot_path(cfg: Config) -> str:
    return os.path.join(cfg.path("state_dir"), "membership.json")


def load_snapshot(cfg: Config) -> dict[str, list[str]]:
    p = snapshot_path(cfg)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(cfg: Config, membership: dict[str, list[str]]) -> None:
    os.makedirs(cfg.path("state_dir"), exist_ok=True)
    with open(snapshot_path(cfg), "w", encoding="utf-8") as f:
        json.dump(membership, f, indent=2, sort_keys=True)


def diff_membership(prev: dict[str, list[str]], curr: dict[str, list[str]]) -> dict:
    """Diff two {user_id: [org_id, ...]} maps.

    Returns {"joiners": [uid], "movers": [(uid, prev_orgs, curr_orgs)],
    "leavers": [uid]}, where membership "presence" means >=1 org:
      - joiner: no orgs before, some now (onboard is manual; surfaced for visibility)
      - leaver: some orgs before, none now (offboard)
      - mover:  present in both but the org set changed (move)
    """
    joiners, movers, leavers = [], [], []
    for uid in set(prev) | set(curr):
        p = set(prev.get(uid, []))
        c = set(curr.get(uid, []))
        if not p and c:
            joiners.append(uid)
        elif p and not c:
            leavers.append(uid)
        elif p != c:
            movers.append((uid, sorted(p), sorted(c)))
    return {"joiners": joiners, "movers": movers, "leavers": leavers}


# ---- audit log (append-only JSONL) ----
def audit(cfg: Config, *, action: str, user_id: str, field: str,
          before: Any, after: Any, reason: str, triggered_by: str,
          dry_run: bool = False) -> dict:
    """Append one audit record (who/what/when/why/triggered-by) to audit.jsonl."""
    rec = {
        "ts": int(time.time()),
        "action": action,
        "user_id": user_id,
        "field": field,
        "before": before,
        "after": after,
        "reason": reason,
        "triggered_by": triggered_by,
        "dry_run": dry_run,
    }
    with open(cfg.path("audit_log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
