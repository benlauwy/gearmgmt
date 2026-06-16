"""Change-set (plan) data model, classification, and (de)serialization.

A plan is the unit of the diff-first / plan->apply approval gate and also the
resume ledger: each Change carries its own status so an interrupted apply can be
re-run safely.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .config import Config

# Increases in access require human approval; the rest auto-apply.
# role_change is conservatively treated as needing approval until a role-rank is
# defined (a change could grant access); role_revoke (-> no role) auto-applies.
NEEDS_APPROVAL = {"limit_increase", "role_grant", "role_upgrade", "role_change", "org_add"}
AUTO_APPLY = {"limit_decrease", "role_revoke", "role_downgrade", "org_remove"}


@dataclass
class Change:
    user_id: str
    kind: str                 # one of NEEDS_APPROVAL | AUTO_APPLY
    field: str                # "limit" | "enterprise_role" | "org_membership"
    before: Any
    after: Any
    reason: str
    org_id: Optional[str] = None
    status: str = "pending"   # pending | applied | failed | skipped
    error: Optional[str] = None

    @property
    def needs_approval(self) -> bool:
        return self.kind in NEEDS_APPROVAL


@dataclass
class Plan:
    workflow: str
    created_at: int = field(default_factory=lambda: int(time.time()))
    triggered_by: str = "manual"
    changes: list[Change] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            workflow=d["workflow"],
            created_at=d.get("created_at", 0),
            triggered_by=d.get("triggered_by", "manual"),
            changes=[Change(**c) for c in d.get("changes", [])],
        )


def save_plan(cfg: Config, plan: Plan) -> str:
    d = os.path.join(cfg.path("state_dir"), "plans")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{plan.workflow}-{plan.created_at}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)
    return path


def load_plan(path: str) -> Plan:
    with open(path, encoding="utf-8") as f:
        return Plan.from_dict(json.load(f))


def _limit_kind(current, target) -> str:
    """Classify a limit change. None == unlimited (ranks highest)."""
    cur = float("inf") if current is None else current
    tgt = float("inf") if target is None else target
    return "limit_increase" if tgt > cur else "limit_decrease"


def diff(actual: dict[str, dict], desired: dict[str, object]) -> list[Change]:
    """Compute the change set between actual and desired per-user state.

    Set-to-desired, never increment. Only the dimensions each DesiredState marks
    with check_limit / check_role are compared. Changes are classified so the
    approval gate can split increases/grants (NEEDS_APPROVAL) from
    revokes/downgrades (AUTO_APPLY).
    """
    changes: list[Change] = []
    for user_id, d in desired.items():
        a = actual.get(user_id, {})
        if getattr(d, "check_limit", False):
            cur = a.get("limit")
            if cur != d.limit:
                changes.append(Change(user_id, _limit_kind(cur, d.limit), "limit",
                                      cur, d.limit, d.source))
        if getattr(d, "check_role", False):
            cur = (a.get("enterprise_role") or {}).get("role_id")
            if cur != d.enterprise_role:
                if d.enterprise_role is None:
                    kind = "role_revoke"
                elif cur is None:
                    kind = "role_grant"
                else:
                    kind = "role_change"
                changes.append(Change(user_id, kind, "enterprise_role",
                                      cur, d.enterprise_role, d.source))
    return changes
