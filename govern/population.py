"""Population resolution: actual state + policy -> desired state for everyone.

The shared bridge between :mod:`govern.state` (what *is*) and
:mod:`govern.policy` (what *should be*), used by both the action commands
(``reconcile``) and the read-only reports (``coverage``).
"""
from __future__ import annotations

from .config import Config
from .errors import GovernError
from .policy import load_policy, match_org_role_ids, resolve_desired
from .state import read_actual, read_org_index


def is_admin(actual_user, admin_subs: list[str]) -> bool:
    """True if the user's ACTUAL enterprise-role name contains an admin keyword."""
    name = ((actual_user.enterprise_role or {}).get("role_name") or "").lower()
    return any(s in name for s in admin_subs)


def configured_org_role_ids(cfg: Config, client) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(member_org_role_id, admin_org_role_id)`` from config, tolerantly.

    The member role governs non-admins (``[invite].org_role_id`` / ``org_role_name``,
    the same role ``onboard`` grants); the admin role governs admins on every org
    (``[governance].admin_org_role_id`` / ``admin_org_role_name``). Each reuses the
    onboard resolution rules but NEVER raises — an unset or ambiguous entry just
    leaves that class's org roles ungoverned (returns None). The live org roles are
    fetched at most once, and only when some NAME (not an id) actually needs it."""
    inv, gov = cfg.invite or {}, cfg.governance or {}
    member = (inv.get("org_role_id"), inv.get("org_role_name"))
    admin = (gov.get("admin_org_role_id"), gov.get("admin_org_role_name"))
    needs_lookup = any(name and not rid for rid, name in (member, admin))
    org_roles = ([r for r in client.list_roles() if r.get("role_type") == "org"]
                 if needs_lookup else [])

    def one(rid, name):
        ids = match_org_role_ids(rid, name, org_roles)
        return ids[0] if len(ids) == 1 else None

    return one(*member), one(*admin)


def org_id_by_name(org_index: dict, name: str) -> str:
    """Resolve an org name to its id (case-insensitive) or raise GovernError."""
    matches = [oid for oid, n in org_index.items() if n.lower() == name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise GovernError(f"no org named {name!r}. Known: {sorted(org_index.values())}")
    raise GovernError(f"multiple orgs named {name!r}")


def resolve_population(cfg: Config, client):
    """Read actual state + org index and resolve desired state for every user.

    Returns (actual, desired_map, org_index, policy).
    """
    pol = load_policy(cfg)
    org_index = read_org_index(client)
    admin_subs = [s.lower() for s in cfg.governance.get("admin_role_name_contains", [])]
    org_role_id, admin_org_role_id = configured_org_role_ids(cfg, client)
    actual = read_actual(client)
    desired = {}
    for uid, a in actual.items():
        names = [org_index.get(oid, f"<unknown:{oid}>") for oid in a.org_ids]
        name_to_org_id = {org_index[oid]: oid for oid in a.org_ids if oid in org_index}
        desired[uid] = resolve_desired(uid, names, is_admin=is_admin(a, admin_subs),
                                       policy=pol, cfg=cfg, org_role_id=org_role_id,
                                       admin_org_role_id=admin_org_role_id,
                                       name_to_org_id=name_to_org_id)
    return actual, desired, org_index, pol
