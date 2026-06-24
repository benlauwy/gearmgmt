"""Apply a plan: approval gate, resumable execution, audit logging."""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time

from . import state
from .config import REPO_ROOT, Config
from .plan import Change, Plan, load_plan
from .tui import confirm, confirm_yes

# Fallback parallelism for applying a plan's per-user groups when the client
# carries no ``apply_concurrency`` (e.g. a mock/stub). Real clients set it from
# [api].apply_concurrency.
DEFAULT_APPLY_CONCURRENCY = 8


def _user_id_from_invite(resp, email: str) -> str:
    """Pull the new user_id out of an invite response (best-effort email match)."""
    if not isinstance(resp, list):  # dry-run sentinel dict — no real id assigned
        return ""
    match = next((u for u in resp
                  if (u.get("email") or "").lower() == (email or "").lower()), None)
    user = match or (resp[0] if resp else None)
    uid = (user or {}).get("user_id")
    if not uid:
        raise RuntimeError(f"invite of {email!r} returned no user_id")
    return uid


def _apply_change(client, c: Change) -> str:
    """Dispatch one Change to the right client mutation (set-to-desired).

    Returns the resolved user_id for a ``user_invite`` (the API assigns it), so
    the caller can thread it into that invitee's org-add/limit changes; returns
    "" for every other change kind (and for dry-run invites)."""
    if c.kind == "user_invite":
        resp = client.invite_users([c.email], c.after)
        return _user_id_from_invite(resp, c.email)
    if c.field == "limit":
        client.set_user_limit(c.user_id, c.after)
    elif c.field == "enterprise_role":
        if c.after is None:
            raise RuntimeError("enterprise-role revoke is unsupported (no API to clear an enterprise role)")
        client.set_enterprise_role(c.user_id, c.after)
    elif c.field == "org_membership":
        if c.kind == "org_add":
            client.add_user_to_org(c.org_id, c.user_id, c.after)
        elif c.kind == "org_remove":
            client.remove_user_from_org(c.org_id, c.user_id)
        else:
            raise RuntimeError(f"unknown org_membership kind: {c.kind}")
    else:
        raise RuntimeError(f"unknown change field: {c.field}")
    return ""


def _persist(plan: Plan, plan_path) -> None:
    if not plan_path:
        return
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)


def _plans_dir(cfg: Config) -> str:
    return os.path.join(cfg.path("state_dir"), "plans")


def _archive_dir(cfg: Config) -> str:
    return os.path.join(_plans_dir(cfg), "archive")


def _rel(path: str) -> str:
    """Best-effort repo-relative path for tidy console output."""
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:  # different drive on Windows
        return path


def list_outstanding_plans(cfg: Config) -> list[str]:
    """Return paths of plans still awaiting work, oldest first.

    "Outstanding" == the *.json files directly under state/plans/: a non-recursive
    listing, so already-archived plans (state/plans/archive/) and the .gitkeep
    placeholder are naturally excluded.
    """
    d = _plans_dir(cfg)
    if not os.path.isdir(d):
        return []
    paths = [os.path.join(d, n) for n in os.listdir(d)
             if n.endswith(".json") and os.path.isfile(os.path.join(d, n))]
    return sorted(paths, key=os.path.getmtime)


def _is_fully_applied(plan: Plan) -> bool:
    """True once every change has landed (so nothing is held/pending/failed).

    An empty (no-op) plan counts as fully applied so it gets tidied away too."""
    return all(c.status == "applied" for c in plan.changes)


def _archive_plan(cfg: Config, plan_path: str) -> str:
    """Move a fully-applied plan into state/plans/archive/ and return its new path."""
    arch = _archive_dir(cfg)
    os.makedirs(arch, exist_ok=True)
    dest = os.path.join(arch, os.path.basename(plan_path))
    os.replace(plan_path, dest)
    return dest


def _group_by_user(changes) -> "list[tuple[str, list]]":
    """Group changes by subject, preserving first-appearance order.

    The subject is the user_id, or the email for a not-yet-created invitee (whose
    user_id is only assigned when the invite is applied)."""
    order, groups = [], {}
    for c in changes:
        key = c.subject
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    return [(key, groups[key]) for key in order]


def _apply_user_group(client, cfg: Config, plan: Plan, uid: str, group: list,
                      *, approved: bool, triggered_by, plan_path,
                      io_lock: threading.Lock):
    """Apply ONE user's pending changes, in order — the unit of parallelism.

    User groups are independent, so apply_plan fans them out across a thread pool;
    WITHIN a group the changes stay strictly sequential because a new invitee's
    org-add/limit can only run once the invite has assigned a user_id. To stay
    safe under concurrency this worker NEVER prints directly: it buffers every
    console line into ``lines`` (the caller prints them, in user order, so nothing
    interleaves) and serializes the file-writing side effects — plan persistence
    and the audit append — behind the shared ``io_lock``. Returns
    ``(lines, counts, held_users)`` for the caller to merge; ``counts`` mirrors
    the apply_plan tally keys and ``held_users`` is 1 when the whole user is held.
    """
    lines: list[str] = []
    counts = {"applied": 0, "would": 0, "held": 0, "already": 0, "failed": 0}

    pending = [c for c in group if c.status != "applied"]
    counts["already"] += len(group) - len(pending)
    if not pending:
        return lines, counts, 0

    lines.append(f"{uid}:")
    # Atomic gate: hold the whole user if any pending change needs approval.
    if not approved and any(c.needs_approval for c in pending):
        counts["held"] += len(pending)
        blockers = sorted({c.kind for c in pending if c.needs_approval})
        for c in pending:
            lines.append(f"  [HELD]  {c.kind:14} {c.field:16} {c.before} -> {c.after}")
        lines.append(f"  -> entire user held pending --approved (needs: {', '.join(blockers)})")
        return lines, counts, 1

    resolved_uid = ""  # set once this invitee's user_invite is applied
    for c in pending:
        # A new invitee's org-add/limit changes can only run once the invite
        # has assigned a user_id. If the invite hasn't (yet) succeeded, skip.
        if c.kind != "user_invite" and not c.user_id:
            if not resolved_uid:
                c.status, c.error = "failed", "skipped: invite did not complete"
                counts["failed"] += 1
                lines.append(f"  [SKIP]  {c.kind:14} {c.field:16} {c.before} -> {c.after}: invite incomplete")
                with io_lock:
                    _persist(plan, plan_path)
                continue
            c.user_id = resolved_uid
        try:
            new_uid = _apply_change(client, c)
        except Exception as e:  # noqa: BLE001 - record and continue (resumable)
            c.status, c.error = "failed", str(e)
            counts["failed"] += 1
            lines.append(f"  [FAIL]  {c.kind:14} {c.field:16} {c.before} -> {c.after}: {e}")
            with io_lock:
                _persist(plan, plan_path)
            continue
        if c.kind == "user_invite":
            # Thread the freshly-assigned id (or a dry-run placeholder) into
            # this invitee's remaining org-add/limit changes.
            resolved_uid = new_uid or "<dry-run-user>"
            for other in pending:
                if not other.user_id:
                    other.user_id = resolved_uid
        if client.dry_run:
            counts["would"] += 1
            lines.append(f"  [DRY]   {c.kind:14} {c.field:16} {c.before} -> {c.after}")
        else:
            c.status, c.error = "applied", None
            counts["applied"] += 1
            lines.append(f"  [OK]    {c.kind:14} {c.field:16} {c.before} -> {c.after}")
            with io_lock:
                state.audit(cfg, action=c.kind, user_id=c.user_id or uid, field=c.field,
                            before=c.before, after=c.after, reason=c.reason,
                            triggered_by=triggered_by, dry_run=False)
                _persist(plan, plan_path)
            if client.sleep:
                time.sleep(client.sleep)
    return lines, counts, 0


def apply_plan(cfg: Config, client, plan: Plan, *, approved: bool = False,
               triggered_by=None, plan_path=None) -> Plan:
    """Execute a plan (the second half of the plan -> apply approval gate).

    The approval gate is ATOMIC PER USER: if any of a user's pending changes
    needs approval and ``approved`` is False, NONE of that user's changes are
    applied — so a move never lands half-materialized (e.g. destination limit but
    source role). When approved (or when all of a user's pending changes
    auto-apply), they are applied in order.

    - Changes already "applied" are skipped (resume after partial failure).
    - Each real mutation is recorded to the audit log; client.dry_run is honored
      (dry-run neither mutates nor audits) and client.sleep paces the calls.
    - Per-USER groups run concurrently (client.apply_concurrency workers; 1 =
      serial); changes within a user stay sequential. Output stays in roster
      order and audit/plan writes are serialized, so the result is identical to a
      serial apply — just faster on large rosters.
    """
    triggered_by = triggered_by or plan.triggered_by
    counts = {"applied": 0, "would": 0, "held": 0, "already": 0, "failed": 0}
    held_users = 0

    # Inviting members requires the enterprise's "Require SSO for member access"
    # setting to be OFF. Invites need approval (so they only run with --approved)
    # and don't touch the server in dry-run, so only gate on a real, approved run
    # that still has invites left to send. If we ask the operator to turn it off,
    # we must also ask them to turn it back on afterwards.
    pending_invites = [c for c in plan.changes
                       if c.kind == "user_invite" and c.status != "applied"]
    gate_sso = approved and not client.dry_run and bool(pending_invites)
    if gate_sso:
        confirm_yes(
            f"\nThis plan invites {len(pending_invites)} new member(s) — action required first\n"
            "  Open Settings > Enterprise > General and UNCHECK\n"
            '  "Require SSO for member access" (invites only work while it is off).\n',
            prompt="Press y once it's unchecked to start applying: ",
        )

    # Apply each user's changes as an independent unit. Users have no ordering
    # dependency on one another, so the groups fan out across ``apply_concurrency``
    # workers (the per-user mutations are network-latency bound); WITHIN a user the
    # changes stay strictly sequential (see _apply_user_group). Each worker buffers
    # its output and returns it so the MAIN thread prints whole user blocks in
    # roster order — concurrent calls never interleave on the console — while file
    # writes are serialized behind ``io_lock``.
    groups = _group_by_user(plan.changes)
    workers = getattr(client, "apply_concurrency", DEFAULT_APPLY_CONCURRENCY)
    io_lock = threading.Lock()

    def run(item):
        uid, group = item
        return _apply_user_group(
            client, cfg, plan, uid, group, approved=approved,
            triggered_by=triggered_by, plan_path=plan_path, io_lock=io_lock)

    pool = None
    if workers <= 1 or len(groups) <= 1:  # serial (single worker / nothing to fan out)
        results = (run(item) for item in groups)
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(run, item) for item in groups]
        results = (f.result() for f in futures)  # consumed in submission order
    try:
        for lines, deltas, held in results:
            for line in lines:
                print(line)
            for key, val in deltas.items():
                counts[key] += val
            held_users += held
    finally:
        if pool is not None:
            pool.shutdown()

    print(f"\nApply summary: applied={counts['applied']} would(dry)={counts['would']} "
          f"held={counts['held']} already={counts['already']} failed={counts['failed']}")
    if held_users:
        print(f"{held_users} user(s) fully held pending --approved (atomic per user).")

    # Restore the setting we asked the operator to disable for the invites.
    if gate_sso:
        confirm_yes(
            "\nApply finished — action required\n"
            "  Open Settings > Enterprise > General and re-CHECK\n"
            '  "Require SSO for member access".\n',
            prompt="Press y once it's re-checked to finish: ",
        )

    # Tidy away a finished plan: once every change has landed (and this is a real
    # run with a file to move), retire it to state/plans/archive/ so it no longer
    # shows up as outstanding. Plans with held/failed/pending changes stay put so
    # they remain resumable (e.g. re-run with --approved). Dry-runs never move it.
    if plan_path and not client.dry_run and _is_fully_applied(plan):
        dest = _archive_plan(cfg, plan_path)
        print(f"Plan fully applied — archived to {_rel(dest)}")
    return plan


def apply_outstanding(cfg: Config, client, *, approved: bool = False) -> None:
    """Apply every outstanding plan in state/plans/ after one y/N confirmation.

    Used when ``apply`` is invoked with no plan path. Lists what will run, asks
    once, then applies each plan oldest-first (each is archived as it completes).
    """
    plans = list_outstanding_plans(cfg)
    if not plans:
        print(f"No outstanding plans in {_rel(_plans_dir(cfg))}.")
        return

    listing = "\n".join(f"  {_rel(p)}" for p in plans)
    mode = " with --approved" if approved else ""
    if not confirm(
        f"\n{len(plans)} outstanding plan(s) in {_rel(_plans_dir(cfg))}:\n{listing}\n",
        prompt=f"Apply all {len(plans)} plan(s){mode}? [y/N]: ",
    ):
        print("Aborted: no plans applied.")
        return

    for p in plans:
        print(f"\n=== applying {_rel(p)} ===")
        apply_plan(cfg, client, load_plan(p), approved=approved, plan_path=p)
