"""Policy loading + desired-state resolution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import Config, load_toml


def coerce_limit(value: Any, *, allow_zero: bool = False) -> Optional[int]:
    """Positive int, or None for unlimited (accepts "null"/"none").

    ``allow_zero`` permits 0 as well — used for the offboard leaver limit, which
    zeroes (reclaims) a member's cap. Policy limits stay strictly positive.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("null", "none"):
        return None
    n = int(value)
    if n < (0 if allow_zero else 1):
        kind = "non-negative" if allow_zero else "positive"
        raise ValueError(f"limit must be a {kind} integer or null")
    return n


@dataclass
class Policy:
    limits: dict[str, Optional[int]]      # org name -> limit
    roles: dict[str, str]                 # org name -> enterprise role_id
    overrides: dict[str, dict[str, Any]]  # user_id -> {reason, limit?, enterprise_role?}


def load_policy(cfg: Config) -> Policy:
    limits_raw = load_toml(cfg.path("limits_policy"))
    roles = load_toml(cfg.path("roles_policy"))
    overrides = load_toml(cfg.path("overrides"))
    limits = {k: coerce_limit(v) for k, v in limits_raw.items()}
    return Policy(limits=limits, roles=roles,
                 overrides={k: dict(v) for k, v in overrides.items()})


@dataclass
class DesiredState:
    user_id: str
    limit: Optional[int]
    enterprise_role: Optional[str]
    # source: "override" | "admin-exempt" | "policy:<org>" | "no-governed-org" | "violation"
    source: str
    check_limit: bool = False  # whether the limit should be reconciled for this user
    check_role: bool = False   # whether the enterprise role should be reconciled
    note: str = ""


def resolve_desired(user_id: str, member_org_names: list[str], *,
                    is_admin: bool, policy: Policy, cfg: Config) -> DesiredState:
    """Compute the desired (limit, enterprise_role) for one user.

    Rules (source-of-truth precedence):
      1. overrides.toml wins outright; only the fields it specifies are reconciled.
      2. admins are exempt from member governance and may be multi-org -> not governed.
      3. otherwise the user's single governed org determines limit + enterprise role
         (only the dimensions that org has a policy entry for are reconciled).
      4. a non-admin in 0 governed orgs -> "no-governed-org"; in >1 -> "violation".

    ``is_admin`` is derived by the caller from the user's ACTUAL enterprise role
    via cfg.governance.admin_role_name_contains. ``member_org_names`` are the
    user's org memberships already mapped to names.
    """
    override = policy.overrides.get(user_id)
    if override is not None:
        has_limit = "limit" in override
        has_role = "enterprise_role" in override
        return DesiredState(
            user_id=user_id,
            limit=coerce_limit(override.get("limit")) if has_limit else None,
            enterprise_role=override.get("enterprise_role") if has_role else None,
            source="override",
            check_limit=has_limit,
            check_role=has_role,
            note=override.get("reason", ""),
        )

    if is_admin:
        return DesiredState(user_id, None, None, "admin-exempt",
                            note="admin: multi-org allowed, not governed")

    governed = [n for n in member_org_names if n in policy.roles or n in policy.limits]
    if not governed:
        return DesiredState(user_id, None, None, "no-governed-org",
                            note=f"member of no governed org: {member_org_names}")
    if len(governed) > 1:
        return DesiredState(user_id, None, None, "violation",
                            note=f"non-admin in multiple governed orgs: {governed}")

    name = governed[0]
    return DesiredState(
        user_id=user_id,
        limit=policy.limits.get(name),
        enterprise_role=policy.roles.get(name),
        source=f"policy:{name}",
        check_limit=name in policy.limits,
        check_role=name in policy.roles,
    )
