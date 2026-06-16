"""Apply a plan: approval gate, resumable execution, audit logging."""
from __future__ import annotations

import json
import time

from . import state
from .config import Config
from .plan import Change, Plan


def _apply_change(client, c: Change):
    """Dispatch one Change to the right client mutation (set-to-desired)."""
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


def _persist(plan: Plan, plan_path) -> None:
    if not plan_path:
        return
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)


def _group_by_user(changes) -> "list[tuple[str, list]]":
    """Group changes by user_id, preserving first-appearance order."""
    order, groups = [], {}
    for c in changes:
        if c.user_id not in groups:
            groups[c.user_id] = []
            order.append(c.user_id)
        groups[c.user_id].append(c)
    return [(uid, groups[uid]) for uid in order]


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
    """
    triggered_by = triggered_by or plan.triggered_by
    counts = {"applied": 0, "would": 0, "held": 0, "already": 0, "failed": 0}
    held_users = 0

    for uid, group in _group_by_user(plan.changes):
        pending = [c for c in group if c.status != "applied"]
        counts["already"] += len(group) - len(pending)
        if not pending:
            continue

        print(f"{uid}:")
        # Atomic gate: hold the whole user if any pending change needs approval.
        if not approved and any(c.needs_approval for c in pending):
            held_users += 1
            counts["held"] += len(pending)
            blockers = sorted({c.kind for c in pending if c.needs_approval})
            for c in pending:
                print(f"  [HELD]  {c.kind:14} {c.field:16} {c.before} -> {c.after}")
            print(f"  -> entire user held pending --approved (needs: {', '.join(blockers)})")
            continue

        for c in pending:
            try:
                _apply_change(client, c)
            except Exception as e:  # noqa: BLE001 - record and continue (resumable)
                c.status, c.error = "failed", str(e)
                counts["failed"] += 1
                print(f"  [FAIL]  {c.kind:14} {c.field:16} {c.before} -> {c.after}: {e}")
                _persist(plan, plan_path)
                continue
            if client.dry_run:
                counts["would"] += 1
                print(f"  [DRY]   {c.kind:14} {c.field:16} {c.before} -> {c.after}")
            else:
                c.status, c.error = "applied", None
                counts["applied"] += 1
                print(f"  [OK]    {c.kind:14} {c.field:16} {c.before} -> {c.after}")
                state.audit(cfg, action=c.kind, user_id=uid, field=c.field,
                            before=c.before, after=c.after, reason=c.reason,
                            triggered_by=triggered_by, dry_run=False)
                _persist(plan, plan_path)
                if client.sleep:
                    time.sleep(client.sleep)

    print(f"\nApply summary: applied={counts['applied']} would(dry)={counts['would']} "
          f"held={counts['held']} already={counts['already']} failed={counts['failed']}")
    if held_users:
        print(f"{held_users} user(s) fully held pending --approved (atomic per user).")
    return plan
