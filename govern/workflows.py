"""Workflow orchestration.

Action commands build a Plan (diff-first) applied through the apply gate:
  onboard · move · reassign · offboard · reconcile
Read-only reports (mutate nothing; usage/coverage/logins also write no plan):
  reconcile · usage · coverage · logins
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import NamedTuple, Optional

from .config import Config
from .plan import Change, Plan, _limit_kind, diff, save_plan
from .policy import Policy, load_policy, resolve_desired
from . import roster as roster_mod
from .state import (diff_membership, load_snapshot, read_actual, read_members,
                    read_org_index, read_utilizations, resolve_identities,
                    resolve_one, save_snapshot, snapshot_path)
from .tui import MenuUnavailable, select_from_list


def _is_admin(actual_user: dict, admin_subs: list[str]) -> bool:
    name = ((actual_user.get("enterprise_role") or {}).get("role_name") or "").lower()
    return any(s in name for s in admin_subs)


def _fmt_limit(value, is_set: bool = True) -> str:
    if value is None:
        return "unlimited" if is_set else "unset"
    return str(value)


def _emailer(actual: dict):
    """Return a uid -> display-email lookup (falling back to the uid itself)."""
    return lambda uid: actual.get(uid, {}).get("email") or uid


def _by_email(actual: dict) -> dict:
    """Index members by lower-cased email -> (user_id, actual_entry).

    Members with no email on file are skipped (a roster email can't match them).
    """
    return {(a.get("email") or "").lower(): (uid, a)
            for uid, a in actual.items() if a.get("email")}


def _tag(c: Change) -> str:
    """The approval-gate label shown at the start of a change line."""
    return "APPROVAL" if c.needs_approval else "auto"


def _where(org_index: dict, c: Change) -> str:
    """A ``  [Org Name]`` suffix for org-scoped changes (empty when no org)."""
    return f"  [{org_index.get(c.org_id, c.org_id)}]" if c.org_id else ""


def _render_change(c: Change, *, label: str, show_field: bool = True,
                   where: str = "", suffix: str = "") -> str:
    """One formatted change line for a plan listing.

    Unifies the columns shared by the action commands: the approval tag, the
    change kind, an optional field column, a left-justified ``label`` (the
    subject — user_id/email), the ``before -> after``, and optional ``where``
    (org) / ``suffix`` (e.g. the drift reason)."""
    field = f"{c.field:16} " if show_field else ""
    return (f"  [{_tag(c):8}] {c.kind:14} {field}{label:34} "
            f"{c.before} -> {c.after}{where}{suffix}")


def _org_id_by_name(org_index: dict, name: str) -> str:
    """Resolve an org name to its id (case-insensitive) or exit with a message."""
    matches = [oid for oid, n in org_index.items() if n.lower() == name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"ERROR: no org named {name!r}. Known: {sorted(org_index.values())}")
    raise SystemExit(f"ERROR: multiple orgs named {name!r}")


def _resolve_population(cfg: Config, client):
    """Read actual state + org index and resolve desired state for every user.

    Returns (actual, desired_map, org_index, policy).
    """
    pol = load_policy(cfg)
    org_index = read_org_index(client)
    admin_subs = [s.lower() for s in cfg.governance.get("admin_role_name_contains", [])]
    actual = read_actual(client)
    desired = {}
    for uid, a in actual.items():
        names = [org_index.get(oid, f"<unknown:{oid}>") for oid in a["org_ids"]]
        desired[uid] = resolve_desired(uid, names, is_admin=_is_admin(a, admin_subs),
                                       policy=pol, cfg=cfg)
    return actual, desired, org_index, pol


def _resolve_org_role_id(cfg: Config, client) -> str:
    """Resolve the per-org role granted to invitees when added to their org.

    Prefers ``[invite].org_role_id``; else resolves ``[invite].org_role_name``
    against the live org-type roles. Exits with the available org roles listed if
    neither is configured (or the name doesn't match) so the operator can fix it
    BEFORE anything is mutated."""
    inv = cfg.invite or {}
    rid = inv.get("org_role_id")
    if rid:
        return rid
    org_roles = [r for r in client.list_roles() if r.get("role_type") == "org"]
    available = "; ".join(f"{r.get('role_name')} = {r.get('role_id')}"
                          for r in org_roles) or "(none found)"
    name = inv.get("org_role_name")
    if name:
        match = [r for r in org_roles
                 if (r.get("role_name") or "").lower() == name.lower()]
        if len(match) == 1:
            return match[0]["role_id"]
        if not match:
            raise SystemExit(f"ERROR: no org role named {name!r}. "
                             f"Available org roles: {available}")
        raise SystemExit(f"ERROR: multiple org roles named {name!r}")
    raise SystemExit(
        "ERROR: onboarding needs an org-level role for new members. Set "
        "[invite].org_role_id or [invite].org_role_name in config.toml. "
        f"Available org roles: {available}")


def _fill_org_add_roles(cfg: Config, client, changes: list[Change]) -> None:
    """Fill in the org-level role for every org-add (resolved once, lazily)."""
    org_adds = [c for c in changes if c.kind == "org_add"]
    if org_adds:
        org_role_id = _resolve_org_role_id(cfg, client)
        for c in org_adds:
            c.after = org_role_id


def _fail_roster(errors: list[str]):
    """Print all roster validation problems and exit without changing anything."""
    print(f"\nERROR: roster failed validation ({len(errors)} problem(s)); "
          "nothing was invited or changed:")
    for e in errors:
        print(f"  - {e}")
    raise SystemExit(1)


def _validate_roster_emails(roster) -> list[str]:
    """Validate a roster's EMAIL column: well-formed and free of duplicates.

    Returns human-readable problems in row order (empty == clean). This is the
    email half of roster validation: ``_validate_roster_values`` layers org-name
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


def _validate_roster_values(roster, *, is_valid_org, org_by_lower) -> list[str]:
    """Validate roster emails (+ org names, when the file has an org column).

    Returns a list of human-readable problems (empty == clean) so the caller can
    report them all at once. Shared by the roster-driven commands (onboard,
    reassign); the caller decides what to do with any errors (typically
    ``_fail_roster``, before any prompt or API mutation)."""
    errors = _validate_roster_emails(roster)
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


def _onboard_row_changes(email: str, canonical: str, org_id: str, pol, existing,
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
        cur_role = (a.get("enterprise_role") or {}).get("role_id")
        if canonical in pol.roles and "enterprise_role" not in override and cur_role != ent_role:
            kind = "role_grant" if cur_role is None else "role_change"
            changes.append(Change(uid, kind, "enterprise_role", cur_role, ent_role,
                                  reason, email=email))
        if org_id not in a["org_ids"]:
            changes.append(Change(uid, "org_add", "org_membership", None, None,
                                  reason, org_id=org_id, email=email))
        if has_limit and "limit" not in override and a.get("limit") != limit:
            changes.append(Change(uid, _limit_kind(a.get("limit"), limit), "limit",
                                  a.get("limit"), limit, reason, email=email))
    else:
        changes.append(Change("", "user_invite", "enterprise_role", None, ent_role,
                              reason, email=email))
        changes.append(Change("", "org_add", "org_membership", None, None,
                              reason, org_id=org_id, email=email))
        if has_limit:
            changes.append(Change("", _limit_kind(None, limit), "limit", None, limit,
                                  reason, email=email))
    return changes


def _reassign_row_changes(email: str, canonical: str, org_id: str, pol, existing,
                          org_index: dict, governed: set, reason: str) -> list[Change]:
    """Build the change(s) to MOVE one existing member into ``canonical`` org.

    A move = materialize the destination first (add to ``canonical`` + set its
    enterprise role and ACU limit, honoring overrides — exactly the existing-user
    onboard diff) and THEN remove the member from their OTHER governed orgs. The
    add-then-remove order means an interrupted apply can leave the user briefly in
    both orgs (a detectable, resumable state) but never org-less. Ungoverned /
    orphaned memberships are left untouched (the single-GOVERNED-org invariant)."""
    uid, a = existing
    changes = list(_onboard_row_changes(email, canonical, org_id, pol, existing, reason))
    for oid, r in a.get("org_roles", {}).items():
        name = org_index.get(oid)
        if name in governed and name != canonical:
            changes.append(Change(uid, "org_remove", "org_membership",
                                  r.get("role_id"), None, reason, org_id=oid, email=email))
    return changes


class _RosterCtx(NamedTuple):
    """The shared context onboard/reassign build before computing their plans."""
    pol: Policy
    org_index: dict
    org_by_lower: dict
    governed: set
    policy_name_by_lower: dict
    roster: "roster_mod.Roster"


def _load_and_validate_roster(cfg: Config, client, file: str) -> _RosterCtx:
    """Load policy + org inventory, parse the roster, and validate it up front.

    Shared by onboard and reassign: builds the governed-org lookups, parses the
    file (failing cleanly on a structural RosterError), prints any parse
    warnings, then runs the email(+org) value validation — exiting via
    _fail_roster on any problem BEFORE any prompt or API mutation."""
    pol = load_policy(cfg)
    org_index = read_org_index(client)
    org_by_lower = {name.lower(): oid for oid, name in org_index.items()}
    governed = set(pol.roles) | set(pol.limits)
    policy_name_by_lower = {n.lower(): n for n in governed}

    def is_valid_org(name: str) -> bool:
        low = (name or "").strip().lower()
        return low in policy_name_by_lower and low in org_by_lower

    # --- parse + structurally validate the file (shape, header, columns) ---
    try:
        roster = roster_mod.parse_roster(file, is_valid_org=is_valid_org)
    except roster_mod.RosterError as e:
        raise SystemExit(f"ERROR: {e}")
    for w in roster.warnings:
        print(f"WARNING: {w}")

    # --- validate emails (+ org names, when present) up front ---
    errors = _validate_roster_values(roster, is_valid_org=is_valid_org,
                                     org_by_lower=org_by_lower)
    if errors:
        _fail_roster(errors)
    return _RosterCtx(pol, org_index, org_by_lower, governed,
                      policy_name_by_lower, roster)


def _resolve_single_column_org(roster, pol, org_by_lower, *, no_orgs_msg: str,
                               prompt: str, selected_label: str) -> None:
    """For a single-column roster, pick ONE org interactively for everyone.

    No-op when the roster already has an org column. Only orgs that can accept
    the operation (an enterprise role in roles.toml AND existing in the
    enterprise) are offered; the chosen org is written to every row. Exits
    cleanly when there's nothing to choose or no interactive terminal."""
    if roster.has_org_column:
        return
    selectable = sorted(n for n in pol.roles if n.lower() in org_by_lower)
    if not selectable:
        raise SystemExit(no_orgs_msg)
    try:
        chosen = select_from_list(prompt, selectable)
    except MenuUnavailable as e:
        raise SystemExit(f"ERROR: {e}. Provide a 2-column file (email, group name) instead.")
    if not chosen:
        raise SystemExit("Cancelled — no organization selected.")
    print(f"{selected_label}: {chosen}\n")
    roster.orgs = [chosen] * len(roster.emails)


def onboard(cfg: Config, client, *, file: Optional[str] = None):
    """Invite users from a roster file and materialize their org membership,
    enterprise role, and ACU limit from policy.

    The roster is a CSV or .xlsx with a header row and one or two columns: an
    email column (required) and a group/organization-name column (optional). With
    one column you pick the target org interactively; with two we auto-detect
    which is which. Emails and org names are validated up front — any problem
    fails before anything is invited. Diff-first: writes a plan; apply it with
    ``govern.py apply <plan> [--approved]`` (invites are gated)."""
    if not file:
        raise SystemExit("ERROR: onboard requires --file PATH (a CSV or .xlsx roster)")

    ctx = _load_and_validate_roster(cfg, client, file)
    pol, org_index, org_by_lower = ctx.pol, ctx.org_index, ctx.org_by_lower
    governed, policy_name_by_lower, roster = (
        ctx.governed, ctx.policy_name_by_lower, ctx.roster)

    # --- single-column file: choose ONE org interactively for everyone (only
    #     orgs that can accept invites — i.e. have an enterprise role — are shown) ---
    _resolve_single_column_org(
        roster, pol, org_by_lower,
        no_orgs_msg="ERROR: no organizations available to invite into "
                    "(roles.toml has no org that exists in the enterprise).",
        prompt="Select the organization to add these users to:",
        selected_label="Selected organization")

    # --- read existing members (to split new vs existing) ---
    actual = read_actual(client)
    actual_by_email = _by_email(actual)

    # --- new users must be invitable: their target org needs an enterprise role ---
    role_errors: list[str] = []
    for i, (email, org) in enumerate(roster.rows(), start=1):
        if email.strip().lower() in actual_by_email:
            continue
        canonical = policy_name_by_lower[org.strip().lower()]
        if pol.roles.get(canonical) is None:
            role_errors.append(f"row {i}: cannot invite {email} into {org!r} — it has no "
                               "enterprise role in roles.toml")
    if role_errors:
        _fail_roster(role_errors)

    # --- build the plan ---
    changes: list[Change] = []
    warnings: list[str] = []
    n_new = n_existing = 0
    for email, org in roster.rows():
        low = org.strip().lower()
        canonical = policy_name_by_lower[low]
        org_id = org_by_lower[low]
        existing = actual_by_email.get(email.strip().lower())
        if existing:
            n_existing += 1
            other_governed = [org_index.get(o) for o in existing[1]["org_ids"]
                              if org_index.get(o) in governed and org_index.get(o) != canonical]
            if other_governed:
                warnings.append(f"{email}: already in governed org(s) {other_governed}; "
                                f"adding to {canonical!r} creates a multi-org situation")
        else:
            n_new += 1
        changes.extend(_onboard_row_changes(email, canonical, org_id, pol, existing,
                                            f"onboard:{canonical}"))

    _fill_org_add_roles(cfg, client, changes)

    scope = os.path.basename(file)
    plan = Plan(workflow="onboard", triggered_by=f"onboard:file:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    n_invite = sum(c.kind == "user_invite" for c in changes)
    n_org = sum(c.kind == "org_add" for c in changes)
    n_limit = sum(c.field == "limit" for c in changes)
    n_role = sum(c.field == "enterprise_role" and c.kind != "user_invite" for c in changes)
    print(f"=== onboard (file: {scope}) ===")
    print(f"Roster rows: {len(roster.emails)} ({n_new} new invite(s), "
          f"{n_existing} existing)  |  changes: {len(changes)} "
          f"({n_invite} invite, {n_org} org-add, {n_role} role, {n_limit} limit)\n")
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings:
        print()
    for c in changes:
        print(_render_change(c, label=c.subject, where=_where(org_index, c)))
    if not changes:
        print("  (no changes — everyone in the roster already matches policy)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path} --approved   # invites/grants are gated")
    return plan


def move_members(cfg: Config, client):
    """Re-materialize members who changed orgs since the last run.

    Detects users whose org set changed (membership snapshot-diff) and re-resolves
    their desired limit + enterprise role from the destination org. Because each
    org is its own kind and roles are computed as a diff, same-role moves (e.g.
    IDE Light -> IDE Standard) naturally yield limit-only changes, while cross-role
    moves also produce the minimal role delta. Joiners (onboard, manual) and
    leavers (offboard) are surfaced but not acted on here. First run just baselines;
    the snapshot advances unless --dry-run."""
    actual, desired, org_index, _pol = _resolve_population(cfg, client)
    curr = {uid: a["org_ids"] for uid, a in actual.items()}
    prev = load_snapshot(cfg)

    email = _emailer(actual)

    def names(ids):
        return [org_index.get(o, f"<unknown:{o}>") for o in ids]

    print("=== move (membership snapshot-diff) ===")
    if not prev:
        save_snapshot(cfg, curr)
        print(f"No prior snapshot — baseline established for {len(curr)} user(s).")
        print(f"Snapshot: {snapshot_path(cfg)}")
        print("Re-run after membership changes to detect movers.")
        return None

    delta = diff_membership(prev, curr)
    movers = delta["movers"]
    print(f"Since last snapshot: {len(movers)} mover(s), "
          f"{len(delta['joiners'])} joiner(s) [onboard/manual], "
          f"{len(delta['leavers'])} leaver(s) [offboard]\n")

    for uid, p, c in movers:
        d = desired.get(uid)
        src = f"  [{d.source}]" if d else ""
        print(f"  {email(uid)}: {names(p)} -> {names(c)}{src}")
    if not movers:
        print("  (no movers)")

    mover_ids = {uid for uid, _p, _c in movers}
    subset = {uid: desired[uid] for uid in mover_ids if uid in desired}
    changes = diff(actual, subset)
    plan = Plan(workflow="move", triggered_by="move:snapshot-diff", changes=changes)
    path = save_plan(cfg, plan)

    if changes:
        print("\nPlanned changes for movers:")
        for c in changes:
            print(_render_change(c, label=email(c.user_id)))

    violations = [uid for uid in mover_ids
                  if desired.get(uid) and desired[uid].source == "violation"]
    if violations:
        print("\nMovers now in multiple governed orgs (single-org violation):")
        for uid in violations:
            print(f"  - {email(uid)}: {desired[uid].note}")

    if client.dry_run:
        print("\n(dry-run: snapshot NOT advanced)")
    else:
        save_snapshot(cfg, curr)
        print(f"\nSnapshot advanced: {len(curr)} user(s).")
    print(f"Plan saved: {path}")
    if changes:
        print(f"Apply with:  python govern.py apply {path} [--approved]")
    return plan


def reassign(cfg: Config, client, *, file: Optional[str] = None):
    """Bulk-move existing members to a new org from a roster file.

    The roster is the same shape as onboard's: a CSV/.xlsx with a header row and
    an email column plus an optional DESTINATION group/org-name column (one column
    => pick a single destination interactively). Unlike onboard (which only ADDS),
    reassign MOVES: each member is added to their destination org with that org's
    enterprise role + ACU limit (from policy, honoring overrides) and then removed
    from their OTHER governed orgs (ungoverned memberships are left intact). Every
    roster email must already be an enterprise user — unknown emails fail up front,
    before anything changes (use onboard to invite). Diff-first: writes a plan;
    apply it with ``govern.py apply <plan> [--approved]`` — org removals and limit
    decreases auto-apply, while the org add and any role/limit increase are gated."""
    if not file:
        raise SystemExit("ERROR: reassign requires --file PATH (a CSV or .xlsx roster)")

    ctx = _load_and_validate_roster(cfg, client, file)
    pol, org_index, org_by_lower = ctx.pol, ctx.org_index, ctx.org_by_lower
    governed, policy_name_by_lower, roster = (
        ctx.governed, ctx.policy_name_by_lower, ctx.roster)

    # --- read existing members. reassign MOVES existing users; it never invites,
    #     so every roster email must already exist. Check before any prompt so an
    #     unknown email fails up front rather than after picking a destination. ---
    actual = read_actual(client)
    actual_by_email = _by_email(actual)
    unknown = [f"row {i}: {email} is not an enterprise user "
               "(reassign moves existing users; use onboard to invite new ones)"
               for i, (email, _org) in enumerate(roster.rows(), start=1)
               if email.strip().lower() not in actual_by_email]
    if unknown:
        _fail_roster(unknown)

    # --- single-column file: choose ONE destination org for everyone (only orgs
    #     with an enterprise role that exist in the enterprise are shown) ---
    _resolve_single_column_org(
        roster, pol, org_by_lower,
        no_orgs_msg="ERROR: no organizations available to move into "
                    "(roles.toml has no org that exists in the enterprise).",
        prompt="Select the destination organization to move these users to:",
        selected_label="Selected destination organization")

    # --- build the plan ---
    changes: list[Change] = []
    warnings: list[str] = []
    n_moved = n_noop = 0
    for email, org in roster.rows():
        low = org.strip().lower()
        canonical = policy_name_by_lower[low]
        org_id = org_by_lower[low]
        existing = actual_by_email[email.strip().lower()]
        row_changes = _reassign_row_changes(email, canonical, org_id, pol, existing,
                                            org_index, governed, f"reassign:{canonical}")
        if row_changes:
            n_moved += 1
        else:
            n_noop += 1
            warnings.append(f"{email}: already in {canonical!r} at its role/limit — nothing to move")
        changes.extend(row_changes)

    _fill_org_add_roles(cfg, client, changes)

    scope = os.path.basename(file)
    plan = Plan(workflow="reassign", triggered_by=f"reassign:file:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    n_remove = sum(c.kind == "org_remove" for c in changes)
    n_add = sum(c.kind == "org_add" for c in changes)
    n_limit = sum(c.field == "limit" for c in changes)
    n_role = sum(c.field == "enterprise_role" for c in changes)
    print(f"=== reassign (file: {scope}) ===")
    print(f"Roster rows: {len(roster.emails)} ({n_moved} to move, {n_noop} already in place)"
          f"  |  changes: {len(changes)} "
          f"({n_add} org-add, {n_remove} org-remove, {n_role} role, {n_limit} limit)\n")
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings:
        print()
    for c in changes:
        print(_render_change(c, label=c.subject, where=_where(org_index, c)))
    if not changes:
        print("  (no changes — everyone in the roster is already in their destination org)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path} --approved   # org adds/increases are gated")
    return plan


def _offboard_targets_from_file(file: str, actual: dict) -> tuple[list[str], str]:
    """Resolve a bulk-offboard roster file to the user_ids to offboard.

    The roster is the same CSV/.xlsx shape as onboard/reassign, but offboard only
    needs the EMAIL column — it removes each member from ALL orgs, so any
    group/organization column is ignored (with a warning). Emails are validated up
    front (well-formed, no duplicates) and every one must already be an enterprise
    user; unknown emails fail before anything changes (use the audit log to
    confirm anyone already removed). Returns (target user_ids, scope)."""
    # Offboard ignores orgs, so org-name validity is irrelevant here; a trivial
    # is_valid_org keeps header/email-column detection working off emails alone.
    try:
        roster = roster_mod.parse_roster(file, is_valid_org=lambda _name: False)
    except roster_mod.RosterError as e:
        raise SystemExit(f"ERROR: {e}")
    for w in roster.warnings:
        print(f"WARNING: {w}")
    if roster.has_org_column:
        print("WARNING: offboard removes members from ALL orgs — ignoring the "
              "group/organization column.")

    errors = _validate_roster_emails(roster)
    if errors:
        _fail_roster(errors)

    actual_by_email = _by_email(actual)
    unknown = [f"row {i}: {email} is not an enterprise user "
               "(nothing to offboard — it may already have been removed)"
               for i, (email, _org) in enumerate(roster.rows(), start=1)
               if email.strip().lower() not in actual_by_email]
    if unknown:
        _fail_roster(unknown)

    targets = [actual_by_email[email.strip().lower()][0] for email, _org in roster.rows()]
    return targets, f"file:{os.path.basename(file)}"


def offboard(cfg: Config, client, *, user_id: Optional[str] = None,
             org_dissolved: Optional[str] = None, file: Optional[str] = None):
    """Offboard: zero/reclaim the limit, remove the user from ALL orgs, then set
    the special leaver enterprise role (config.leaver). Use --user for one leaver,
    --file for a CSV/.xlsx roster of emails (bulk), or --org-dissolved to fan out
    across all members of a dissolved org. Every change is a revoke/downgrade, so
    the plan auto-applies (no approval); still diff-first via the apply gate."""
    if not user_id and not org_dissolved and not file:
        raise SystemExit("ERROR: offboard requires --user USER_ID, "
                         "--org-dissolved NAME, or --file PATH")

    actual = read_actual(client)
    org_index = read_org_index(client)
    leaver_role = cfg.leaver.get("enterprise_role_id")
    raw_limit = cfg.leaver.get("limit", 0)
    leaver_limit = (None if isinstance(raw_limit, str) and raw_limit.lower() in ("null", "none")
                    else int(raw_limit))

    if user_id:
        user_id = resolve_one(actual, user_id)
        targets, scope = [user_id], f"user:{user_id}"
    elif org_dissolved:
        oid = _org_id_by_name(org_index, org_dissolved)
        targets = [uid for uid, a in actual.items() if oid in a["org_ids"]]
        scope = f"org-dissolved:{org_dissolved}"
    else:
        targets, scope = _offboard_targets_from_file(file, actual)

    changes = []
    for uid in targets:
        a = actual[uid]
        reason = f"offboard ({scope})"
        # 1) zero/reclaim the limit
        if a.get("limit") != leaver_limit:
            changes.append(Change(uid, "limit_decrease", "limit", a.get("limit"), leaver_limit, reason))
        # 2) set the special enterprise role (before removing orgs, while clearly present)
        cur_ent = (a.get("enterprise_role") or {}).get("role_id")
        if leaver_role and cur_ent != leaver_role:
            changes.append(Change(uid, "role_downgrade", "enterprise_role", cur_ent, leaver_role, reason))
        # 3) remove from ALL orgs (includes any orphaned org refs)
        for oid_, r in a.get("org_roles", {}).items():
            changes.append(Change(uid, "org_remove", "org_membership", r.get("role_id"), None,
                                  reason, org_id=oid_))

    plan = Plan(workflow="offboard", triggered_by=f"offboard:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    email = _emailer(actual)

    print(f"=== offboard ({scope}) ===")
    print(f"Target users: {len(targets)}  |  changes: {len(changes)} "
          f"(all auto-apply: offboard = revokes/downgrades)\n")
    for uid in targets:
        ucs = [c for c in changes if c.user_id == uid]
        print(f"  {email(uid)}: {len(ucs)} change(s)")
        for c in ucs:
            print(f"     {c.kind:14} {c.field:16} {c.before} -> {c.after}"
                  f"{_where(org_index, c)}")
    if not changes:
        print("  (nothing to do — already offboarded)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path}   # all changes auto-apply")
    return plan


def reconcile(cfg: Config, client, *, user_id: Optional[str] = None,
              org: Optional[str] = None, limits_only: bool = False):
    """Report drift of actual vs desired (limits + roles) and save a plan.

    Read-only — it computes and saves a plan but does not apply it. By default it
    covers EVERYONE and both dimensions (limits + enterprise roles); narrow it
    with:

      - ``user_id`` (--user, an email or user_id) — just that one member;
      - ``org`` (--org NAME) — just that org's members;
      - ``limits_only`` (--limits-only) — only ACU-limit drift, leaving roles
        alone (the single-user form is the usage-driven upgrade).

    Honors overrides, admin-exemption, and the single-governed-org rule, and
    flags non-admins in >1 org. ``--user`` and ``--org`` are mutually exclusive."""
    if user_id and org:
        raise SystemExit("ERROR: reconcile takes at most one of --user / --org")

    actual, desired, org_index, pol = _resolve_population(cfg, client)

    if user_id:
        uid = resolve_one(actual, user_id)
        scope_uids, scope = {uid}, f"user:{uid}"
    elif org:
        oid = _org_id_by_name(org_index, org)
        scope_uids = {u for u, a in actual.items() if oid in a["org_ids"]}
        scope = f"org:{org}"
    else:
        scope_uids, scope = set(actual), "all"

    desired = {u: d for u, d in desired.items() if u in scope_uids}
    changes = diff(actual, desired)
    if limits_only:
        changes = [c for c in changes if c.field == "limit"]

    triggered = "reconcile" if scope == "all" else f"reconcile:{scope}"
    plan = Plan(workflow="reconcile", triggered_by=triggered, changes=changes)
    path = save_plan(cfg, plan)

    email = _emailer(actual)

    need = [c for c in changes if c.needs_approval]
    auto = [c for c in changes if not c.needs_approval]
    governed_names = sorted(set(pol.roles) | set(pol.limits))

    print("=== reconcile (read-only) ===")
    if scope != "all" or limits_only:
        bits = ([scope] if scope != "all" else []) + (["limits only"] if limits_only else [])
        print(f"Scope: {' | '.join(bits)}")
    print(f"Population: {len(scope_uids)} user(s)  |  Governed orgs: {', '.join(governed_names)}")
    print(f"Drift: {len(changes)} change(s) — {len(need)} need approval, {len(auto)} auto-apply\n")

    if changes:
        print("Drift detail:")
        for c in changes:
            print(_render_change(c, label=email(c.user_id), show_field=False,
                                 suffix=f"  ({c.reason})"))
        print()

    exempt = [uid for uid, d in desired.items() if d.source == "admin-exempt"]
    violations = [(uid, d) for uid, d in desired.items() if d.source == "violation"]
    no_org = [uid for uid, d in desired.items() if d.source == "no-governed-org"]
    orphans = {}
    for uid, a in actual.items():
        if uid not in scope_uids:
            continue
        unknown = [oid for oid in a["org_ids"] if oid not in org_index]
        if unknown:
            orphans[uid] = unknown

    print(f"Exempt (admins): {len(exempt)}")
    for uid in exempt:
        rn = (actual[uid].get("enterprise_role") or {}).get("role_name")
        print(f"  - {email(uid)} ({rn})")
    if violations:
        print(f"Violations (non-admin in multiple governed orgs): {len(violations)}")
        for uid, d in violations:
            print(f"  - {email(uid)}: {d.note}")
    if no_org:
        print(f"No governed org: {len(no_org)}")
        for uid in no_org:
            print(f"  - {email(uid)}")
    if orphans:
        print(f"Orphaned org refs (not in inventory): {len(orphans)}")
        for uid, ids in orphans.items():
            print(f"  - {email(uid)}: {ids}")

    print(f"\nPlan saved: {path}")
    print("Apply drift with:  python govern.py apply", path, "[--approved]")
    return plan


def _utilization_status(days: list, cap, *, near_cap_pct: float,
                        trend_window_days: int, products: list, now: int) -> dict:
    """Pure helper: summarize one user's daily consumption against their cap.

    Returns consumption (this cycle), pct of cap, recent vs prior window sums,
    a trend label, and whether the user is at/near the cap.
    """
    def total(after, before):
        acc = 0.0
        for d in days:
            ts = d.get("date", 0)
            if after is not None and ts < after:
                continue
            if before is not None and ts >= before:
                continue
            if products:
                by = d.get("acus_by_product") or {}
                acc += sum((by.get(p) or 0) for p in products)
            else:
                acc += d.get("acus") or 0
        return acc

    consumption = total(None, None)
    window = trend_window_days * 86400
    recent = total(now - window, None)
    prior = total(now - 2 * window, now - window)
    if recent > prior:
        trend = "up"
    elif recent < prior:
        trend = "down"
    else:
        trend = "flat"
    pct = (consumption / cap) if cap else None
    flagged = pct is not None and pct >= near_cap_pct
    return {"consumption": consumption, "cap": cap, "pct": pct,
            "recent": recent, "prior": prior, "trend": trend, "flagged": flagged}


def _export_format(path: str) -> str:
    """Map an export filename's extension to a writer format, mirroring
    ``roster.read_rows`` on the read side so the supported types line up:
    ``.csv``/``.txt`` -> ``"csv"``, ``.tsv`` -> ``"tsv"``, ``.xlsx`` -> ``"xlsx"``.
    Anything we can't write (legacy Excel, an unknown/missing extension) is a
    clean error rather than a silent wrong-format write."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt"):
        return "csv"
    if ext == ".tsv":
        return "tsv"
    if ext == ".xlsx":
        return "xlsx"
    if ext in (".xls", ".xlsm", ".xlsb"):
        raise SystemExit(
            f"ERROR: unsupported export format {ext!r}; save as .xlsx (or .csv)")
    raise SystemExit(
        f"ERROR: cannot infer an export format from "
        f"{ext or '(no extension)'!r}; use a .csv or .xlsx filename")


def _write_table(path: str, header: list, rows: list) -> None:
    """Write ``header`` + ``rows`` to ``path`` as CSV/TSV or Excel, choosing the
    format from the extension (see :func:`_export_format`). Excel goes through
    ``openpyxl`` (lazy import, same guidance as the roster reader) so CSV-only
    users never need the dependency. Parent directories are created as needed."""
    fmt = _export_format(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if fmt in ("csv", "tsv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t" if fmt == "tsv" else ",")
            w.writerow(header)
            w.writerows(rows)
        return
    try:
        from openpyxl import Workbook
    except ModuleNotFoundError as e:  # pragma: no cover - depends on install
        raise SystemExit(
            "ERROR: writing .xlsx requires openpyxl "
            "(pip install -r requirements.txt); alternatively export to .csv") from e
    wb = Workbook()
    ws = wb.active
    ws.title = "usage"
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def usage(cfg: Config, client, *, reverse: bool = False,
          user_id: Optional[str] = None, export: Optional[str] = None):
    """Flag users near/at their cap with a usage trend. Detection only:
    it emits candidates for the single-user `reconcile --limits-only` upgrade and
    never mutates.

    Rows are printed sorted by percent-of-cap, highest first; ``reverse`` (the
    --reverse flag) flips that to lowest first.

    ``user_id`` (the --user flag, an email or user_id) narrows the report to a
    single member — a spot-check that prints just that user's row. The read stays
    lean (like `lookup`): it resolves via the member list and fetches ONLY that
    user's limit, not the whole population's like read_actual. It never overwrites
    the shared state/usage-candidates.json (that stays the last FULL-population
    output, the upgrade worklist); if the member has no numeric cap there is
    nothing to evaluate against and it says so.

    ``export`` (the --export PATH flag) additionally writes the full usage table
    (every row shown above, not just the flagged candidates) to PATH. The file
    format is chosen from the extension — .csv/.tsv for delimited text, .xlsx for
    Excel (which needs openpyxl). It works in both the full and --user spot-check
    modes, and is independent of the state/usage-candidates.json worklist."""
    # Fail fast on an unwritable --export extension before any network reads.
    if export:
        _export_format(export)

    u = cfg.utilization
    near = float(u.get("near_cap_pct", 0.8))
    trend_window = int(u.get("trend_window_days", 14))
    cycle_days = int(u.get("cycle_days", 30))
    products = list(u.get("products", []) or [])

    now = int(time.time())
    after = now - cycle_days * 86400

    # A single-user spot-check stays lean (like `lookup`): resolve via the member
    # list (one call, no per-user limit reads) and fetch ONLY that user's limit,
    # rather than triggering read_actual's whole-population limit fan-out.
    single = user_id is not None
    if single:
        members = read_members(client)
        user_id = resolve_one(members, user_id)
        raw = client.get_user_limit(user_id) or {}
        local_agent = raw.get("local_agent") or {}
        actual = {user_id: {"email": members[user_id].get("email"),
                            "limit": local_agent.get("cycle_acu_limit"),
                            "limit_set": "local_agent" in raw}}
    else:
        actual = read_actual(client)

    print("=== usage / cap detection (detection only) ===")
    src = ", ".join(products) if products else "total_acus"
    print(f"cap: per-user limit | usage: {src} over {cycle_days}d | "
          f"near-cap >= {near:.0%} | trend window {trend_window}d\n")

    capped = [(uid, a) for uid, a in actual.items()
              if isinstance(a.get("limit"), (int, float)) and a["limit"] > 0]
    if not capped:
        if single:
            who = actual[user_id].get("email") or user_id
            print(f"{who} has no numeric per-user cap set — nothing to evaluate.")
            if export:
                print(f"(--export {export}: nothing written — no cap to report.)")
            return []
        print("No users have a numeric per-user cap set — nothing to evaluate.")

    # Fetch every capped user's utilization in parallel (network-latency bound,
    # like read_actual's limit reads); the per-user summary below is pure/local.
    util = read_utilizations(client, [uid for uid, _ in capped],
                             time_after=after, time_before=now)
    rows, candidates = [], []
    for uid, a in capped:
        data = util.get(uid) or {}
        st = _utilization_status(data.get("consumption_by_date", []), a["limit"],
                                 near_cap_pct=near, trend_window_days=trend_window,
                                 products=products, now=now)
        rows.append((uid, a, st))
        if st["flagged"]:
            candidates.append({"user_id": uid, "email": a.get("email"),
                               "consumption": st["consumption"], "cap": st["cap"],
                               "pct": st["pct"], "trend": st["trend"]})

    rows.sort(key=lambda r: r[2]["pct"] or 0, reverse=not reverse)
    for uid, a, st in rows:
        flag = "NEAR/AT CAP" if st["flagged"] else "ok"
        print(f"  [{flag:11}] {a.get('email') or uid:34} "
              f"{st['consumption']:.1f}/{st['cap']} ({(st['pct'] or 0):.0%}) "
              f"trend={st['trend']} (recent {st['recent']:.1f} vs prior {st['prior']:.1f})")

    # --export writes the FULL table (every row above), independent of the flagged
    # upgrade worklist below; format is picked from the extension (.csv/.xlsx).
    if export:
        header = ["email", "user_id", "status", "consumption", "cap",
                  "pct_of_cap", "trend", "recent_window_acus", "prior_window_acus"]
        table = [[a.get("email") or "", uid,
                  "NEAR/AT CAP" if st["flagged"] else "ok",
                  round(st["consumption"], 4), st["cap"],
                  round(st["pct"], 4) if st["pct"] is not None else "",
                  st["trend"], round(st["recent"], 4), round(st["prior"], 4)]
                 for uid, a, st in rows]
        _write_table(export, header, table)
        print(f"\nExported {len(table)} usage row(s) to: {export}")

    # A single-user spot-check never clobbers the shared full-population worklist;
    # it just prints the row above (+ an upgrade hint when flagged) and returns.
    if single:
        for c in candidates:
            print(f"\n  upgrade: python govern.py reconcile --user {c['user_id']} --limits-only"
                  f"   # {c['email']} at {c['pct']:.0%}")
        return candidates

    os.makedirs(cfg.path("state_dir"), exist_ok=True)
    out = os.path.join(cfg.path("state_dir"), "usage-candidates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, ensure_ascii=False)

    print(f"\n{len(candidates)} upgrade candidate(s). Written: {out}")
    for c in candidates:
        print(f"  upgrade: python govern.py reconcile --user {c['user_id']} --limits-only"
              f"   # {c['email']} at {c['pct']:.0%}")
    return candidates


def capacity(cfg: Config, client):
    """Total provisioned ACU: sum every member's per-user monthly Local Agent
    ACU limit into a single enterprise-wide figure (the answer to "if I took
    everyone and added up their monthly limit"). Read-only — it reads each
    user's current limit (like `usage`/`coverage`) and only prints; it writes no
    plan and mutates nothing.

    Only numeric per-user caps are summable, so members whose limit is
    *unlimited* (an explicit no-cap override) or *unset* (no override at all)
    can't be folded into the total — they are counted and reported separately
    so the headline figure isn't silently undercounting uncapped usage."""
    actual = read_actual(client)

    numeric = [a["limit"] for a in actual.values()
               if isinstance(a["limit"], (int, float))]
    unlimited = sum(1 for a in actual.values()
                    if a["limit_set"] and a["limit"] is None)
    unset = sum(1 for a in actual.values() if not a["limit_set"])
    total = sum(numeric)
    total_str = f"{int(total):,}" if float(total).is_integer() else f"{total:,.1f}"
    w = len(str(len(actual)))

    print("=== capacity (read-only) ===")
    print("Sum of every member's per-user monthly Local Agent ACU limit.\n")
    print(f"Population: {len(actual)} member(s)")
    print(f"  with a numeric monthly cap : {len(numeric):>{w}}")
    print(f"  unlimited (explicit no-cap): {unlimited:>{w}}")
    print(f"  unset (no override)        : {unset:>{w}}")
    print(f"\nTOTAL monthly ACU limit: {total_str}"
          f"   (sum of {len(numeric)} numeric per-user cap(s))")
    if unlimited:
        print(f"Note: {unlimited} uncapped (unlimited) member(s) are NOT in the "
              f"total — their usage has no ceiling.")
    return {"total": total, "numeric": len(numeric),
            "unlimited": unlimited, "unset": unset, "population": len(actual)}


def coverage(cfg: Config, client):
    """Per-org compliance report: for each governed org, show its intended limit
    and role and how many of its (non-admin) members already match them, listing
    any members that don't. Read-only — it prints a summary and writes no plan;
    use `reconcile` when you want that drift turned into an applyable plan."""
    actual, _desired, org_index, pol = _resolve_population(cfg, client)
    admin_subs = [s.lower() for s in cfg.governance.get("admin_role_name_contains", [])]
    orgs = sorted(org_index.items(), key=lambda kv: kv[1])  # (org_id, name)

    members_by_org: dict[str, list[str]] = {}
    for uid, a in actual.items():
        for oid in a["org_ids"]:
            members_by_org.setdefault(oid, []).append(uid)

    governed_names = set(pol.roles) | set(pol.limits)
    print("=== coverage (read-only) ===\n")
    for oid, name in orgs:
        if name not in governed_names:
            continue
        has_limit, has_role = name in pol.limits, name in pol.roles
        intended_limit, intended_role = pol.limits.get(name), pol.roles.get(name)
        members = members_by_org.get(oid, [])
        governed = [u for u in members if not _is_admin(actual[u], admin_subs)]
        admins = len(members) - len(governed)

        lim_ok = sum(1 for u in governed if actual[u]["limit"] == intended_limit)
        role_ok = sum(1 for u in governed
                      if (actual[u].get("enterprise_role") or {}).get("role_id") == intended_role)

        il = _fmt_limit(intended_limit) if has_limit else "(ungoverned)"
        print(f"Org: {name}")
        print(f"  intended: limit={il}  role={intended_role or '(ungoverned)'}")
        print(f"  members: {len(members)} (admins/exempt: {admins}, governed: {len(governed)})")
        if has_limit:
            print(f"  limit coverage: {lim_ok}/{len(governed)} governed member(s) at intended")
        if has_role:
            print(f"  role  coverage: {role_ok}/{len(governed)} governed member(s) at intended")

        mismatches = []
        for u in governed:
            problems = []
            if has_limit and actual[u]["limit"] != intended_limit:
                problems.append(f"limit {_fmt_limit(actual[u]['limit'], actual[u]['limit_set'])} "
                                f"(want {_fmt_limit(intended_limit)})")
            if has_role:
                cur = (actual[u].get("enterprise_role") or {}).get("role_id")
                if cur != intended_role:
                    problems.append(f"role {cur} (want {intended_role})")
            if problems:
                mismatches.append((u, problems))
        if mismatches:
            print("  mismatches:")
            for u, problems in mismatches:
                print(f"    - {actual[u].get('email') or u}: {'; '.join(problems)}")
        print()

    ungoverned = sorted(name for _oid, name in orgs if name not in governed_names)
    if ungoverned:
        print(f"Ungoverned orgs (no policy entry): {', '.join(ungoverned)}")


def _pct(n: int, d: int) -> str:
    """Format n/d as a whole-percent string (0% when there's nothing to divide)."""
    return f"{(n / d):.0%}" if d else "0%"


def logins(cfg: Config, client, dump_never: Optional[str] = None):
    """Login-activity report: of all enterprise members, how many have logged in
    at least once vs never, with a per-org breakdown.

    Read-only. It reads the member list plus the enterprise audit log
    (action=login, full history) and matches login events back to current
    members (by user_id, falling back to email); login events for people who are
    no longer members are ignored. Writes no plan and mutates nothing.

    ``dump_never`` (the --dump-never PATH flag) additionally writes the email
    addresses of members who have never logged in to PATH, one per line. That's
    an explicit, non-governed report artifact, so it's written even on a
    --dry-run (mirroring how ``usage`` always emits its candidates file)."""
    members = read_members(client)
    org_index = read_org_index(client)

    # Set of CURRENT members who have logged in at least once. Match each login
    # event on user_id first, then fall back to email (case-insensitive) for
    # events with no user_id; events for non-members are ignored.
    email_to_uid = {(m["email"] or "").lower(): uid
                    for uid, m in members.items() if m.get("email")}
    logged_in: set[str] = set()
    for ev in client.list_all_audit_logs(action="login"):
        uid = ev.get("user_id")
        if uid in members:
            logged_in.add(uid)
            continue
        em = (ev.get("user_email") or "").lower()
        if em in email_to_uid:
            logged_in.add(email_to_uid[em])

    total = len(members)
    n_in = len(logged_in)
    n_never = total - n_in

    print("=== logins (read-only) ===")
    print("Source: enterprise audit log, action=login (full history)\n")
    print(f"Enterprise members: {total}")
    print(f"  logged in >= once: {n_in} ({_pct(n_in, total)})")
    print(f"  never logged in:   {n_never} ({_pct(n_never, total)})\n")

    # Per-org breakdown. A member in multiple orgs is counted under each, so these
    # rows don't sum to the totals above; members in no org are bucketed last.
    members_by_org: dict[str, list[str]] = {}
    for uid, m in members.items():
        for oid in m["org_ids"]:
            members_by_org.setdefault(oid, []).append(uid)

    def row(label: str, uids: list[str]):
        ins = sum(1 for u in uids if u in logged_in)
        never = len(uids) - ins
        print(f"  {label}: {len(uids)} member(s) | logged in {ins} "
              f"({_pct(ins, len(uids))}) | never {never} ({_pct(never, len(uids))})")

    print("Per-org breakdown (members in multiple orgs count under each):")
    for oid in sorted(members_by_org, key=lambda o: org_index.get(o, f"<unknown:{o}>")):
        row(org_index.get(oid, f"<unknown:{oid}>"), members_by_org[oid])
    if not members_by_org:
        print("  (no org memberships)")

    no_org = [uid for uid, m in members.items() if not m["org_ids"]]
    if no_org:
        row("(no org)", no_org)

    if dump_never:
        # Emails of members who never logged in, sorted & de-duped. Members
        # without an email on file can't be dumped, so we count them separately
        # rather than emitting blank lines.
        never_emails = sorted({(m["email"] or "").lower()
                               for uid, m in members.items()
                               if uid not in logged_in and m.get("email")})
        missing = n_never - len(never_emails)
        d = os.path.dirname(dump_never)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dump_never, "w", encoding="utf-8") as f:
            f.write("".join(e + "\n" for e in never_emails))
        print(f"\nWrote {len(never_emails)} never-logged-in email(s) to: {dump_never}")
        if missing:
            print(f"  ({missing} never-logged-in member(s) had no email on file)")

    return {"total": total, "logged_in": n_in, "never": n_never}


def lookup(cfg: Config, client, *, user_id: Optional[str] = None):
    """Resolve a member by email (or user_id) and print their user_id(s) + ACU limit.

    The Devin API can hold MORE THAN ONE identity for the same person — e.g. a
    pending ``email|<hash>`` invite alongside the ``okta|<Org>|<id>`` (or
    ``user-<uuid>``) identity minted once they authenticate via SSO — so a single
    email can map to several user_ids. Unlike the strict resolver the action
    commands use (``resolve_one``, which fails on ambiguity so they never
    touch the wrong identity), lookup prints EVERY matching user_id, one per
    line, so the SSO identity (e.g. ``okta|Cognition|00u...``) is always
    surfaced. A value that is itself a known user_id is echoed back; an unknown
    value exits non-zero.

    Reads stay lean: one list_enterprise_members() call (via read_members) plus
    one get_user_limit() call per MATCHED identity (usually 1–2) — not the whole
    population like read_actual. Each identity's limit is its per-user monthly
    Local Agent ACU cap: a number, ``unlimited`` for an explicit no-cap
    override, or ``unset`` when no override exists.

    Output is ``<user_id><TAB><ACU limit>`` per line, so a pipeline can still
    grab just the id with ``cut -f1``, e.g.::

        UID=$(python govern.py lookup --user alice@example.com | cut -f1)

    Returns the ``[(user_id, acu_limit_str), ...]`` rows in user_id order."""
    if not user_id:
        raise SystemExit("ERROR: lookup requires --user EMAIL_OR_USER_ID")
    members = read_members(client)
    matches = resolve_identities(members, user_id)  # every matching identity
    rows = []
    for uid in matches:             # one get_user_limit per match (usually 1–2)
        raw = client.get_user_limit(uid) or {}
        local_agent = raw.get("local_agent") or {}
        limit = _fmt_limit(local_agent.get("cycle_acu_limit"), "local_agent" in raw)
        rows.append((uid, limit))
        print(f"{uid}\t{limit}")
    return rows
