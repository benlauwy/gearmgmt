"""Roster intake: turn an onboarding/reassign roster into validated changes.

This layers the *value* validation (emails well-formed and unique; org names
known + governed) and the per-row change-building on top of :mod:`govern.roster`
(which only handles file *structure*). Shared by the roster-driven action
commands (onboard, reassign; offboard uses the email-only pieces).
"""
from __future__ import annotations

from typing import NamedTuple

from . import roster as roster_mod
from .config import Config
from .errors import GovernError
from .plan import Change, limit_kind
from .policy import Policy, load_policy
from .state import read_org_index
from .tui import MenuUnavailable, select_from_list


def by_email(actual: dict) -> dict:
    """Index members by lower-cased email -> (user_id, ActualState).

    Members with no email on file are skipped (a roster email can't match them).
    """
    return {(a.email or "").lower(): (uid, a)
            for uid, a in actual.items() if a.email}


def fail_roster(errors: list[str]):
    """Raise with every roster validation problem; nothing is invited or changed."""
    detail = "\n".join(f"  - {e}" for e in errors)
    raise GovernError(f"roster failed validation ({len(errors)} problem(s)); "
                      f"nothing was invited or changed:\n{detail}")


def validate_roster_emails(roster) -> list[str]:
    """Validate a roster's EMAIL column: well-formed and free of duplicates.

    Returns human-readable problems in row order (empty == clean). This is the
    email half of roster validation: ``validate_roster_values`` layers org-name
    checks on top when a file has an org column, while offboard — which ignores
    orgs and removes members from every org — uses it directly."""
    errors: list[str] = []
    seen: dict[str, int] = {}
    for i, (email, _org) in enumerate(roster.rows(), start=1):
        if not roster_mod.is_valid_email(email):
            errors.append(f"row {i}: invalid email {email!r}")
            continue
        key = email.strip().lower()
        if key in seen:
            errors.append(f"row {i}: duplicate email {email!r} (also row {seen[key]})")
        seen[key] = i
    return errors


def validate_roster_values(roster, *, is_valid_org, org_by_lower) -> list[str]:
    """Validate roster emails (+ org names, when the file has an org column).

    Returns a list of human-readable problems (empty == clean) so the caller can
    report them all at once. Shared by the roster-driven commands (onboard,
    reassign); the caller decides what to do with any errors (typically
    ``fail_roster``, before any prompt or API mutation)."""
    errors = validate_roster_emails(roster)
    if not roster.has_org_column:
        return errors
    for i, (_email, org) in enumerate(roster.rows(), start=1):
        if not (org or "").strip():
            errors.append(f"row {i}: missing group/organization name")
        elif not is_valid_org(org):
            if org.strip().lower() not in org_by_lower:
                errors.append(f"row {i}: unknown organization {org!r}")
            else:
                errors.append(f"row {i}: organization {org!r} is not governed "
                              "(no entry in limits.toml / roles.toml)")
    return errors


def onboard_row_changes(email: str, canonical: str, org_id: str, pol, existing,
                        reason: str) -> list[Change]:
    """Build the change(s) to onboard one roster row toward ``canonical`` org.

    Existing users get a minimal diff (enterprise role / org membership / limit),
    honoring overrides. New users get invite (+ enterprise role) -> org-add ->
    limit. The org-add role_id (Change.after) is filled in by the caller."""
    ent_role = pol.roles.get(canonical)
    has_limit = canonical in pol.limits
    limit = pol.limits.get(canonical)
    changes: list[Change] = []

    if existing:
        uid, a = existing
        override = pol.overrides.get(uid, {})
        cur_role = (a.enterprise_role or {}).get("role_id")
        if canonical in pol.roles and "enterprise_role" not in override and cur_role != ent_role:
            kind = "role_grant" if cur_role is None else "role_change"
            changes.append(Change(uid, kind, "enterprise_role", cur_role, ent_role,
                                  reason, email=email))
        if org_id not in a.org_ids:
            changes.append(Change(uid, "org_add", "org_membership", None, None,
                                  reason, org_id=org_id, email=email))
        if has_limit and "limit" not in override and a.limit != limit:
            changes.append(Change(uid, limit_kind(a.limit, limit), "limit",
                                  a.limit, limit, reason, email=email))
    else:
        changes.append(Change("", "user_invite", "enterprise_role", None, ent_role,
                              reason, email=email))
        changes.append(Change("", "org_add", "org_membership", None, None,
                              reason, org_id=org_id, email=email))
        if has_limit:
            changes.append(Change("", limit_kind(None, limit), "limit", None, limit,
                                  reason, email=email))
    return changes


def reassign_row_changes(email: str, canonical: str, org_id: str, pol, existing,
                         org_index: dict, governed: set, reason: str) -> list[Change]:
    """Build the change(s) to MOVE one existing member into ``canonical`` org.

    A move = materialize the destination first (add to ``canonical`` + set its
    enterprise role and ACU limit, honoring overrides — exactly the existing-user
    onboard diff) and THEN remove the member from their OTHER governed orgs. The
    add-then-remove order means an interrupted apply can leave the user briefly in
    both orgs (a detectable, resumable state) but never org-less. Ungoverned /
    orphaned memberships are left untouched (the single-GOVERNED-org invariant)."""
    uid, a = existing
    changes = list(onboard_row_changes(email, canonical, org_id, pol, existing, reason))
    for oid, r in a.org_roles.items():
        name = org_index.get(oid)
        if name in governed and name != canonical:
            changes.append(Change(uid, "org_remove", "org_membership",
                                  r.get("role_id"), None, reason, org_id=oid, email=email))
    return changes


class RosterCtx(NamedTuple):
    """The shared context onboard/reassign build before computing their plans."""
    pol: Policy
    org_index: dict
    org_by_lower: dict
    governed: set
    policy_name_by_lower: dict
    roster: "roster_mod.Roster"


def load_and_validate_roster(cfg: Config, client, file: str) -> RosterCtx:
    """Load policy + org inventory, parse the roster, and validate it up front.

    Shared by onboard and reassign: builds the governed-org lookups, parses the
    file (RosterError propagates on a structural problem), prints any parse
    warnings, then runs the email(+org) value validation — raising via
    fail_roster on any problem BEFORE any prompt or API mutation."""
    pol = load_policy(cfg)
    org_index = read_org_index(client)
    org_by_lower = {name.lower(): oid for oid, name in org_index.items()}
    governed = set(pol.roles) | set(pol.limits)
    policy_name_by_lower = {n.lower(): n for n in governed}

    def is_valid_org(name: str) -> bool:
        low = (name or "").strip().lower()
        return low in policy_name_by_lower and low in org_by_lower

    # --- parse + structurally validate the file (shape, header, columns) ---
    # parse_roster raises RosterError (a GovernError) on a structural problem;
    # let it propagate to the CLI boundary.
    roster = roster_mod.parse_roster(file, is_valid_org=is_valid_org)
    for w in roster.warnings:
        print(f"WARNING: {w}")

    # --- validate emails (+ org names, when present) up front ---
    errors = validate_roster_values(roster, is_valid_org=is_valid_org,
                                    org_by_lower=org_by_lower)
    if errors:
        fail_roster(errors)
    return RosterCtx(pol, org_index, org_by_lower, governed,
                     policy_name_by_lower, roster)


def resolve_single_column_org(roster, pol, org_by_lower, *, no_orgs_msg: str,
                              prompt: str, selected_label: str) -> None:
    """For a single-column roster, pick ONE org interactively for everyone.

    No-op when the roster already has an org column. Only orgs that can accept
    the operation (an enterprise role in roles.toml AND existing in the
    enterprise) are offered; the chosen org is written to every row. Raises
    cleanly when there's nothing to choose or no interactive terminal."""
    if roster.has_org_column:
        return
    selectable = sorted(n for n in pol.roles if n.lower() in org_by_lower)
    if not selectable:
        raise GovernError(no_orgs_msg)
    try:
        chosen = select_from_list(prompt, selectable)
    except MenuUnavailable as e:
        raise GovernError(f"{e}. Provide a 2-column file (email, group name) instead.") from e
    if not chosen:
        raise GovernError("cancelled — no organization selected.")
    print(f"{selected_label}: {chosen}\n")
    roster.orgs = [chosen] * len(roster.emails)
