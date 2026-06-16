"""Actual-state reads, membership snapshots, and the append-only audit log."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from .config import Config


def read_actual(client) -> dict[str, dict]:
    """Return {user_id: {email, name, enterprise_role, org_roles, org_ids,
    limit, limit_set}}.

    Enterprise + org roles come from client.list_enterprise_members() (one call);
    the per-user Local Agent limit from client.get_user_limit() (one call each).

    NOTE: role_assignments may reference org_ids absent from the org inventory
    (orphaned memberships observed live) — they are preserved in org_ids so
    reconcile/offboard can act on them. ``limit`` is None when no override is set (limit_set=False)
    or when the override is explicitly unlimited (limit_set=True, limit=None).
    """
    out: dict[str, dict] = {}
    for m in client.list_enterprise_members():
        uid = m["user_id"]
        enterprise_role = None
        org_roles: dict[str, dict] = {}
        for a in m.get("role_assignments", []):
            role = a.get("role") or {}
            entry = {"role_id": role.get("role_id"), "role_name": role.get("role_name")}
            if role.get("role_type") == "enterprise" or a.get("org_id") is None:
                enterprise_role = entry
            else:
                org_roles[a["org_id"]] = entry
        raw = client.get_user_limit(uid) or {}
        local_agent = raw.get("local_agent") or {}
        out[uid] = {
            "email": m.get("email"),
            "name": m.get("name"),
            "enterprise_role": enterprise_role,
            "org_roles": org_roles,
            "org_ids": sorted(org_roles.keys()),
            "limit": local_agent.get("cycle_acu_limit"),
            "limit_set": "local_agent" in raw,
        }
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
