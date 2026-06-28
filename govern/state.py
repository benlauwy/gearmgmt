"""Actual-state reads and the append-only audit log."""
from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import Config
from .errors import GovernError

# Fallback parallelism for per-user reads when the client carries no
# ``read_concurrency`` (e.g. a mock/stub). Real clients set it from [api].
DEFAULT_READ_CONCURRENCY = 8


@dataclass
class ActualState:
    """One member's observed state (the counterpart to policy.DesiredState).

    ``read_actual`` populates every field; ``read_members`` fills only identity
    (user_id/email/name/org_ids) and leaves the rest at their defaults. ``limit``
    is None for unlimited *or* unset; ``limit_set`` is False only when no override
    exists. ``enterprise_role`` is ``{role_id, role_name}`` (or None) and
    ``org_roles`` maps org_id -> that same shape.
    """
    user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    enterprise_role: Optional[dict] = None
    org_roles: dict = field(default_factory=dict)
    org_ids: list = field(default_factory=list)
    limit: Optional[int] = None
    limit_set: bool = False


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


def parse_limit_payload(raw: Optional[dict]) -> tuple[Optional[int], bool]:
    """(limit, limit_set) from a get_user_limit payload.

    ``limit`` is the numeric Local Agent cycle cap, or None for unlimited/unset;
    ``limit_set`` is False only when no override exists at all (no ``local_agent``
    key). Shared by read_actual and the usage/lookup single-user reads.
    """
    raw = raw or {}
    local_agent = raw.get("local_agent") or {}
    return local_agent.get("cycle_acu_limit"), "local_agent" in raw


def read_org_index(client) -> dict[str, str]:
    """Return {org_id: name} for every enterprise organization (one call)."""
    return {o["org_id"]: o["name"] for o in client.list_organizations()}


def read_members(client) -> dict[str, ActualState]:
    """Return {user_id: ActualState} (identity + org_ids only) for every member.

    A lean cousin of read_actual: one call to list_enterprise_members() and NO
    per-user limit lookups (read_actual makes one get_user_limit call each). Use
    it for reports that only need identity + org membership — e.g. the login
    activity report — and don't care about ACU limits or role ids (those fields
    stay at their ActualState defaults).
    """
    out: dict[str, ActualState] = {}
    for m in client.list_enterprise_members():
        _enterprise_role, org_roles = _split_role_assignments(m)
        out[m["user_id"]] = ActualState(
            user_id=m["user_id"],
            email=m.get("email"),
            name=m.get("name"),
            org_ids=sorted(org_roles.keys()),
        )
    return out


def _email_matches(index: dict, value: str) -> list[str]:
    """user_ids in ``index`` whose email equals ``value`` (case-insensitive), sorted."""
    low = value.lower()
    return sorted(uid for uid, m in index.items()
                  if (m.email or "").lower() == low)


def resolve_identities(index: dict, value: str) -> list[str]:
    """Resolve a value (a user_id or an email) to ALL matching user_ids.

    A known user_id resolves to itself; otherwise every member whose email
    matches case-insensitively — an email can map to several identities (e.g. a
    pending ``email|...`` invite alongside an authenticated ``okta|...`` id), so
    every match is returned (sorted). Exits cleanly when nothing matches.
    ``index`` is any {user_id: {... "email" ...}} map (read_members/read_actual).
    """
    if value in index:
        return [value]
    matches = _email_matches(index, value)
    if not matches:
        raise GovernError(f"no user matching {value!r} "
                          f"(give an email or the user_id)")
    return matches


def resolve_one(index: dict, value: str) -> str:
    """Resolve a value (a user_id or an email) to EXACTLY one user_id.

    The strict resolver the action commands use so they never touch the wrong
    identity: a known user_id passes through; an email must match exactly one
    member. Exits cleanly on no match or (defensively) an ambiguous email.
    """
    if value in index:
        return value
    matches = _email_matches(index, value)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise GovernError(f"no user matching {value!r} "
                          f"(give an email or the user_id)")
    raise GovernError(f"email {value!r} matches multiple users: {matches}")


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


def _parallel_map(fn: Callable[[str], Any], keys: list[str], *, workers: int,
                  progress: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """Map ``fn`` over ``keys``, returning {key: fn(key)}.

    The per-key calls are assumed network-latency bound and independent, so they
    run on a thread pool (DevinClient holds no per-request state, so it is
    thread-safe); ``workers<=1`` falls back to a serial loop. Results are
    collected — and ``progress(done)`` invoked — on the CALLING thread only, so no
    worker writes to stdout/stderr. Errors propagate (the first failing call
    raises). Shared by the per-user limit and utilization reads.
    """
    out: dict[str, Any] = {}
    if not keys:
        return out
    done = 0
    if workers <= 1:  # serial fallback (single-worker config / debugging)
        for k in keys:
            out[k] = fn(k)
            done += 1
            if progress:
                progress(done)
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, k): k for k in keys}
        for fut in concurrent.futures.as_completed(futures):
            out[futures[fut]] = fut.result()
            done += 1
            if progress:
                progress(done)
    return out


def read_actual(client, *, workers: Optional[int] = None,
                progress: bool = True) -> dict[str, ActualState]:
    """Return {user_id: ActualState} (identity, roles, org_ids, limit, limit_set).

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
    out: dict[str, ActualState] = {}
    for m in client.list_enterprise_members():
        uid = m["user_id"]
        enterprise_role, org_roles = _split_role_assignments(m)
        out[uid] = ActualState(
            user_id=uid,
            email=m.get("email"),
            name=m.get("name"),
            enterprise_role=enterprise_role,
            org_roles=org_roles,
            org_ids=sorted(org_roles.keys()),
        )
    if workers is None:
        workers = getattr(client, "read_concurrency", DEFAULT_READ_CONCURRENCY)
    report = _stderr_progress("fetching limits", len(out)) if progress else None
    limits = _parallel_map(client.get_user_limit, list(out),
                           workers=workers, progress=report)
    for uid, raw in limits.items():
        out[uid].limit, out[uid].limit_set = parse_limit_payload(raw)
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
    if workers is None:
        workers = getattr(client, "read_concurrency", DEFAULT_READ_CONCURRENCY)
    report = _stderr_progress("fetching usage", len(user_ids)) if progress else None
    return _parallel_map(
        lambda uid: client.get_user_utilization(
            uid, time_after=time_after, time_before=time_before) or {},
        user_ids, workers=workers, progress=report)


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
