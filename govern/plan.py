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
from dataclasses import fields as _change_fields
from typing import Any, Optional

from .config import Config

# Increases in access require human approval; the rest auto-apply.
# role_change is conservatively treated as needing approval until a role-rank is
# defined (a change could grant access); role_revoke (-> no role) auto-applies.
# user_invite creates a brand-new enterprise user (the ultimate grant), so it is
# always gated.
NEEDS_APPROVAL = {"user_invite", "limit_increase", "role_grant", "role_upgrade",
                  "role_change", "org_add"}
AUTO_APPLY = {"limit_decrease", "role_revoke", "role_downgrade", "org_remove"}


@dataclass
class Change:
    user_id: str
    kind: str                 # one of NEEDS_APPROVAL | AUTO_APPLY
    field: str                # "limit" | "enterprise_role" | "org_role" | "org_membership"
    before: Any
    after: Any
    reason: str
    org_id: Optional[str] = None
    # For user_invite (and the org_add/limit changes that follow it) the user_id
    # does not exist yet at plan time; the invitee is keyed by email and the
    # user_id is filled in during apply, once the invite call returns it.
    email: Optional[str] = None
    status: str = "pending"   # pending | applied | failed | skipped
    error: Optional[str] = None

    @property
    def needs_approval(self) -> bool:
        return self.kind in NEEDS_APPROVAL

    @property
    def subject(self) -> str:
        """Human label for output: the user_id once known, else the email."""
        return self.user_id or self.email or "<unknown>"


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
        # Tolerate unknown keys so a plan written by a different engine version
        # (the resume ledger is meant to be durable) loads instead of raising.
        known = {f.name for f in _change_fields(Change)}
        return cls(
            workflow=d["workflow"],
            created_at=d.get("created_at", 0),
            triggered_by=d.get("triggered_by", "manual"),
            changes=[Change(**{k: v for k, v in c.items() if k in known})
                     for c in d.get("changes", [])],
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


def limit_kind(current, target) -> str:
    """Classify a limit change. None == unlimited (ranks highest)."""
    cur = float("inf") if current is None else current
    tgt = float("inf") if target is None else target
    return "limit_increase" if tgt > cur else "limit_decrease"


def _role_kind(current, target) -> str:
    """Classify a role change (enterprise or org): grant (none->real), revoke
    (real->none), or change (real->real). role_change is conservatively gated
    (we don't rank roles); grant is gated, revoke auto-applies."""
    if target is None:
        return "role_revoke"
    if current is None:
        return "role_grant"
    return "role_change"


def diff(actual: dict, desired: dict[str, object]) -> list[Change]:
    """Compute the change set between actual and desired per-user state.

    Set-to-desired, never increment. Only the dimensions each DesiredState marks
    are compared: check_limit / check_role, plus one org-role check per entry in
    org_role_by_oid. Changes are classified so the approval gate can split
    increases/grants (NEEDS_APPROVAL) from revokes/downgrades (AUTO_APPLY).
    ``actual`` maps user_id -> ActualState; a user absent from it reads back as no
    limit / no role.
    """
    changes: list[Change] = []
    for user_id, d in desired.items():
        a = actual.get(user_id)
        if getattr(d, "check_limit", False):
            cur = a.limit if a else None
            if cur != d.limit:
                changes.append(Change(user_id, limit_kind(cur, d.limit), "limit",
                                      cur, d.limit, d.source))
        if getattr(d, "check_role", False):
            cur = ((a.enterprise_role if a else None) or {}).get("role_id")
            if cur != d.enterprise_role:
                changes.append(Change(user_id, _role_kind(cur, d.enterprise_role),
                                      "enterprise_role", cur, d.enterprise_role,
                                      d.source))
        # An org role is scoped to a single org, so compare each governed
        # membership independently and stamp the Change with its org_id (so
        # plan/apply target it and the console shows which org it belongs to).
        for oid, want in (getattr(d, "org_role_by_oid", None) or {}).items():
            cur = ((a.org_roles.get(oid) if a else None) or {}).get("role_id")
            if cur != want:
                changes.append(Change(user_id, _role_kind(cur, want), "org_role",
                                      cur, want, d.source, org_id=oid))
    return changes
