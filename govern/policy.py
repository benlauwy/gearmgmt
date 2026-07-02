"""Policy loading + desired-state resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
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


def match_org_role_ids(role_id: Optional[str], role_name: Optional[str],
                       org_roles: list[dict]) -> list[str]:
    """Candidate org role_ids for a configured (id, name) pair.

    An explicit ``role_id`` is the sole candidate; otherwise ``role_name`` is
    matched (case-insensitively) against the given org-type roles. Returns 0/1/
    many ids so callers pick their own strictness: ``onboard`` needs exactly one
    (and raises otherwise, listing the available roles), while reconcile treats
    "not exactly one" as "ungoverned". Shared by BOTH org-role settings — the
    member role (``[invite].org_role_*``) and the admin role
    (``[governance].admin_org_role_*``) — so they resolve identically."""
    if role_id:
        return [role_id]
    if not role_name:
        return []
    return [r["role_id"] for r in org_roles
            if (r.get("role_name") or "").lower() == role_name.lower()]


@dataclass
class DesiredState:
    user_id: str
    limit: Optional[int]
    enterprise_role: Optional[str]
    # source: "override" | "admin" | "admin-no-admin-org" | "policy:<org>"
    #         | "no-governed-org" | "violation"
    source: str
    check_limit: bool = False  # whether the limit should be reconciled for this user
    check_role: bool = False   # whether the enterprise role should be reconciled
    # Desired per-org member roles as {org_id: role_id}. An org role is scoped to
    # one org (unlike the single enterprise role), so this maps EACH governed
    # membership to its desired role. Empty = org roles ungoverned for this user:
    # a governed non-admin gets one entry (their org, -> [invite].org_role_id); an
    # admin gets one per org they're in (-> [governance].admin_org_role_id);
    # override / no-org / violation users get none.
    org_role_by_oid: dict[str, str] = field(default_factory=dict)
    note: str = ""


def resolve_desired(user_id: str, member_org_names: list[str], *,
                    is_admin: bool, policy: Policy, cfg: Config,
                    org_role_id: Optional[str] = None,
                    admin_org_role_id: Optional[str] = None,
                    name_to_org_id: Optional[dict[str, str]] = None) -> DesiredState:
    """Compute the desired (limit, enterprise_role, org roles) for one user.

    Rules (source-of-truth precedence):
      1. overrides.toml wins outright; only the fields it specifies are reconciled.
      2. admins are exempt from the single-org rule and from enterprise-ROLE
         governance, but their LIMIT is governed from the Admin Org's policy
         entry (cfg.governance.admin_org_name). An admin who is NOT a member of
         the Admin Org still gets that limit but is flagged ("admin-no-admin-org").
      3. otherwise the user's single governed org determines limit + enterprise role
         (only the dimensions that org has a policy entry for are reconciled).
      4. a non-admin in 0 governed orgs -> "no-governed-org"; in >1 -> "violation".

    ``is_admin`` is derived by the caller from the user's ACTUAL enterprise role
    via cfg.governance.admin_role_name_contains. ``member_org_names`` are the
    user's org memberships already mapped to names.

    Org-role governance fills ``org_role_by_oid`` ({org_id: desired role_id}):
      - a governed non-admin (rule 3): their role in their single org is set to
        the member ``org_role_id`` (the global ``[invite]`` role);
      - an admin (rule 2): their role in EVERY org they belong to is set to
        ``admin_org_role_id`` (the ``[governance].admin_org_role_*`` role) —
        admins may span many orgs, so all of them are governed;
      - override / no-org / violation users get none.
    Either role id being None leaves that class's org roles ungoverned (backward
    compatible). ``name_to_org_id`` maps this user's known org NAMES back to ids
    (so the governed org's id — and, for admins, every membership id — is known).
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
        # Admins keep their enterprise role and may be multi-org (no enterprise-
        # role/violation check), but their LIMIT is governed from the Admin Org's
        # policy entry. A non-member of the Admin Org still gets that limit, just
        # flagged. Their per-org ROLE is governed on EVERY org they belong to,
        # from governance.admin_org_role_id (ungoverned when that isn't set).
        admin_org = cfg.governance.get("admin_org_name", "Admin Org")
        in_admin_org = admin_org in member_org_names
        org_role_by_oid = ({oid: admin_org_role_id
                            for oid in (name_to_org_id or {}).values()}
                           if admin_org_role_id else {})
        return DesiredState(
            user_id=user_id,
            limit=policy.limits.get(admin_org),
            enterprise_role=None,
            source="admin" if in_admin_org else "admin-no-admin-org",
            check_limit=admin_org in policy.limits,
            check_role=False,
            org_role_by_oid=org_role_by_oid,
            note="" if in_admin_org
                 else f"admin not in {admin_org!r}; applying its limit anyway",
        )

    governed = [n for n in member_org_names if n in policy.roles or n in policy.limits]
    if not governed:
        return DesiredState(user_id, None, None, "no-governed-org",
                            note=f"member of no governed org: {member_org_names}")
    if len(governed) > 1:
        return DesiredState(user_id, None, None, "violation",
                            note=f"non-admin in multiple governed orgs: {governed}")

    name = governed[0]
    gov_org_id = (name_to_org_id or {}).get(name)
    return DesiredState(
        user_id=user_id,
        limit=policy.limits.get(name),
        enterprise_role=policy.roles.get(name),
        source=f"policy:{name}",
        check_limit=name in policy.limits,
        check_role=name in policy.roles,
        org_role_by_oid=({gov_org_id: org_role_id}
                         if org_role_id and gov_org_id else {}),
    )
