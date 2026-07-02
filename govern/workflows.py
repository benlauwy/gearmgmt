"""Workflow orchestration: the diff-first action commands that build a Plan.

  onboard · reassign · offboard · reconcile

Each computes a change set and writes a plan that ``apply`` later executes
through the approval gate. Read-only reports live in :mod:`govern.reports`;
plan execution in :mod:`govern.apply`.
"""
from __future__ import annotations

import os
from typing import Optional

from . import roster as roster_mod
from .config import Config
from .errors import GovernError
from .intake import (
    by_email,
    fail_roster,
    load_and_validate_roster,
    onboard_row_changes,
    reassign_row_changes,
    resolve_single_column_org,
    validate_roster_emails,
)
from .plan import Change, Plan, diff, save_plan
from .policy import coerce_limit, match_org_role_ids
from .population import org_id_by_name, resolve_population
from .render import change_counts, emailer, plan_footer, render_change, where
from .state import read_actual, read_org_index, resolve_one


def _resolve_org_role_id(cfg: Config, client) -> str:
    """Resolve the per-org role granted to invitees when added to their org.

    Prefers ``[invite].org_role_id``; else resolves ``[invite].org_role_name``
    against the live org-type roles. Exits with the available org roles listed if
    neither is configured (or the name doesn't match) so the operator can fix it
    BEFORE anything is mutated. (reconcile uses the tolerant
    population.configured_org_role_ids for the same config — it never raises.)"""
    inv = cfg.invite or {}
    rid = inv.get("org_role_id")
    if rid:
        return rid
    org_roles = [r for r in client.list_roles() if r.get("role_type") == "org"]
    ids = match_org_role_ids(inv.get("org_role_id"), inv.get("org_role_name"), org_roles)
    if len(ids) == 1:
        return ids[0]
    available = "; ".join(f"{r.get('role_name')} = {r.get('role_id')}"
                          for r in org_roles) or "(none found)"
    name = inv.get("org_role_name")
    if name and not ids:
        raise GovernError(f"no org role named {name!r}. "
                          f"Available org roles: {available}")
    if name:
        raise GovernError(f"multiple org roles named {name!r}")
    raise GovernError(
        "onboarding needs an org-level role for new members. Set "
        "[invite].org_role_id or [invite].org_role_name in config.toml. "
        f"Available org roles: {available}")


def _fill_org_add_roles(cfg: Config, client, changes: list[Change]) -> None:
    """Fill in the org-level role for every org-add (resolved once, lazily)."""
    org_adds = [c for c in changes if c.kind == "org_add"]
    if org_adds:
        org_role_id = _resolve_org_role_id(cfg, client)
        for c in org_adds:
            c.after = org_role_id


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
        raise GovernError("onboard requires --file PATH (a CSV or .xlsx roster)")

    ctx = load_and_validate_roster(cfg, client, file)
    pol, org_index, org_by_lower = ctx.pol, ctx.org_index, ctx.org_by_lower
    governed, policy_name_by_lower, roster = (
        ctx.governed, ctx.policy_name_by_lower, ctx.roster)

    # --- single-column file: choose ONE org interactively for everyone (only
    #     orgs that can accept invites — i.e. have an enterprise role — are shown) ---
    resolve_single_column_org(
        roster, pol, org_by_lower,
        no_orgs_msg="no organizations available to invite into "
                    "(roles.toml has no org that exists in the enterprise).",
        prompt="Select the organization to add these users to:",
        selected_label="Selected organization")

    # --- read existing members (to split new vs existing) ---
    actual = read_actual(client)
    actual_by_email = by_email(actual)

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
        fail_roster(role_errors)

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
            other_governed = [org_index.get(o) for o in existing[1].org_ids
                              if org_index.get(o) in governed and org_index.get(o) != canonical]
            if other_governed:
                warnings.append(f"{email}: already in governed org(s) {other_governed}; "
                                f"adding to {canonical!r} creates a multi-org situation")
        else:
            n_new += 1
        changes.extend(onboard_row_changes(email, canonical, org_id, pol, existing,
                                            f"onboard:{canonical}"))

    _fill_org_add_roles(cfg, client, changes)

    scope = os.path.basename(file)
    plan = Plan(workflow="onboard", triggered_by=f"onboard:file:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    kinds, fields = change_counts(changes)
    n_invite, n_org = kinds["user_invite"], kinds["org_add"]
    n_limit = fields["limit"]
    n_role = fields["enterprise_role"] - n_invite  # invites carry the role grant
    print(f"=== onboard (file: {scope}) ===")
    print(f"Roster rows: {len(roster.emails)} ({n_new} new invite(s), "
          f"{n_existing} existing)  |  changes: {len(changes)} "
          f"({n_invite} invite, {n_org} org-add, {n_role} role, {n_limit} limit)\n")
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings:
        print()
    for c in changes:
        print(render_change(c, label=c.subject, where=where(org_index, c)))
    if not changes:
        print("  (no changes — everyone in the roster already matches policy)")
    plan_footer(path, f"Apply with:  python govern.py apply {path} --approved   "
                       "# invites/grants are gated")
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
        raise GovernError("reassign requires --file PATH (a CSV or .xlsx roster)")

    ctx = load_and_validate_roster(cfg, client, file)
    pol, org_index, org_by_lower = ctx.pol, ctx.org_index, ctx.org_by_lower
    governed, policy_name_by_lower, roster = (
        ctx.governed, ctx.policy_name_by_lower, ctx.roster)

    # --- read existing members. reassign MOVES existing users; it never invites,
    #     so every roster email must already exist. Check before any prompt so an
    #     unknown email fails up front rather than after picking a destination. ---
    actual = read_actual(client)
    actual_by_email = by_email(actual)
    unknown = [f"row {i}: {email} is not an enterprise user "
               "(reassign moves existing users; use onboard to invite new ones)"
               for i, (email, _org) in enumerate(roster.rows(), start=1)
               if email.strip().lower() not in actual_by_email]
    if unknown:
        fail_roster(unknown)

    # --- single-column file: choose ONE destination org for everyone (only orgs
    #     with an enterprise role that exist in the enterprise are shown) ---
    resolve_single_column_org(
        roster, pol, org_by_lower,
        no_orgs_msg="no organizations available to move into "
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
        row_changes = reassign_row_changes(email, canonical, org_id, pol, existing,
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

    kinds, fields = change_counts(changes)
    n_remove, n_add = kinds["org_remove"], kinds["org_add"]
    n_limit, n_role = fields["limit"], fields["enterprise_role"]
    print(f"=== reassign (file: {scope}) ===")
    print(f"Roster rows: {len(roster.emails)} ({n_moved} to move, {n_noop} already in place)"
          f"  |  changes: {len(changes)} "
          f"({n_add} org-add, {n_remove} org-remove, {n_role} role, {n_limit} limit)\n")
    for w in warnings:
        print(f"WARNING: {w}")
    if warnings:
        print()
    for c in changes:
        print(render_change(c, label=c.subject, where=where(org_index, c)))
    if not changes:
        print("  (no changes — everyone in the roster is already in their destination org)")
    plan_footer(path, f"Apply with:  python govern.py apply {path} --approved   "
                       "# org adds/increases are gated")
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
    # parse_roster raises RosterError (a GovernError); let it reach the CLI.
    roster = roster_mod.parse_roster(file, is_valid_org=lambda _name: False)
    for w in roster.warnings:
        print(f"WARNING: {w}")
    if roster.has_org_column:
        print("WARNING: offboard removes members from ALL orgs — ignoring the "
              "group/organization column.")

    errors = validate_roster_emails(roster)
    if errors:
        fail_roster(errors)

    actual_by_email = by_email(actual)
    unknown = [f"row {i}: {email} is not an enterprise user "
               "(nothing to offboard — it may already have been removed)"
               for i, (email, _org) in enumerate(roster.rows(), start=1)
               if email.strip().lower() not in actual_by_email]
    if unknown:
        fail_roster(unknown)

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
        raise GovernError("offboard requires --user USER_ID, "
                          "--org-dissolved NAME, or --file PATH")

    actual = read_actual(client)
    org_index = read_org_index(client)
    leaver_role = cfg.leaver.get("enterprise_role_id")
    leaver_limit = coerce_limit(cfg.leaver.get("limit", 0), allow_zero=True)

    if user_id:
        user_id = resolve_one(actual, user_id)
        targets, scope = [user_id], f"user:{user_id}"
    elif org_dissolved:
        oid = org_id_by_name(org_index, org_dissolved)
        targets = [uid for uid, a in actual.items() if oid in a.org_ids]
        scope = f"org-dissolved:{org_dissolved}"
    else:
        targets, scope = _offboard_targets_from_file(file, actual)

    changes = []
    for uid in targets:
        a = actual[uid]
        reason = f"offboard ({scope})"
        # 1) zero/reclaim the limit
        if a.limit != leaver_limit:
            changes.append(Change(uid, "limit_decrease", "limit", a.limit, leaver_limit, reason))
        # 2) set the special enterprise role (before removing orgs, while clearly present)
        cur_ent = (a.enterprise_role or {}).get("role_id")
        if leaver_role and cur_ent != leaver_role:
            changes.append(Change(uid, "role_downgrade", "enterprise_role", cur_ent, leaver_role, reason))
        # 3) remove from ALL orgs (includes any orphaned org refs)
        for oid_, r in a.org_roles.items():
            changes.append(Change(uid, "org_remove", "org_membership", r.get("role_id"), None,
                                  reason, org_id=oid_))

    plan = Plan(workflow="offboard", triggered_by=f"offboard:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    email = emailer(actual)

    print(f"=== offboard ({scope}) ===")
    print(f"Target users: {len(targets)}  |  changes: {len(changes)} "
          f"(all auto-apply: offboard = revokes/downgrades)\n")
    for uid in targets:
        ucs = [c for c in changes if c.user_id == uid]
        print(f"  {email(uid)}: {len(ucs)} change(s)")
        for c in ucs:
            print(f"     {c.kind:14} {c.field:16} {c.before} -> {c.after}"
                  f"{where(org_index, c)}")
    if not changes:
        print("  (nothing to do — already offboarded)")
    plan_footer(path, f"Apply with:  python govern.py apply {path}   "
                       "# all changes auto-apply")
    return plan


def reconcile(cfg: Config, client, *, user_id: Optional[str] = None,
              org: Optional[str] = None, limits_only: bool = False):
    """Report drift of actual vs desired (limits + roles) and save a plan.

    Read-only — it computes and saves a plan but does not apply it. By default it
    covers EVERYONE and every dimension: ACU limit, enterprise role, and — for
    governed non-admins — the per-org member role (reconciled to the single global
    ``[invite]`` org role; ungoverned if that isn't configured). Narrow it with:

      - ``user_id`` (--user, an email or user_id) — just that one member;
      - ``org`` (--org NAME) — just that org's members;
      - ``limits_only`` (--limits-only) — only ACU-limit drift, leaving both the
        enterprise and org roles alone (the single-user form is the usage-driven
        upgrade).

    Honors overrides, admin governance (limit via the Admin Org, with enterprise
    AND org roles plus the single-org rule left exempt), and the single-governed-
    org rule, and flags non-admins in >1 org. ``--user`` and ``--org`` are
    mutually exclusive."""
    if user_id and org:
        raise GovernError("reconcile takes at most one of --user / --org")

    actual, desired, org_index, pol = resolve_population(cfg, client)

    if user_id:
        uid = resolve_one(actual, user_id)
        scope_uids, scope = {uid}, f"user:{uid}"
    elif org:
        oid = org_id_by_name(org_index, org)
        scope_uids = {u for u, a in actual.items() if oid in a.org_ids}
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

    email = emailer(actual)

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
            # `where` stamps the org on org-role lines so an org-role change
            # (field=org_role) is distinguishable from an enterprise-role one.
            print(render_change(c, label=email(c.user_id), show_field=False,
                                 where=where(org_index, c), suffix=f"  ({c.reason})"))
        print()

    admins = [uid for uid, d in desired.items()
              if d.source in ("admin", "admin-no-admin-org")]
    admin_no_org = [uid for uid, d in desired.items()
                    if d.source == "admin-no-admin-org"]
    violations = [(uid, d) for uid, d in desired.items() if d.source == "violation"]
    no_org = [uid for uid, d in desired.items() if d.source == "no-governed-org"]
    orphans = {}
    for uid, a in actual.items():
        if uid not in scope_uids:
            continue
        unknown = [oid for oid in a.org_ids if oid not in org_index]
        if unknown:
            orphans[uid] = unknown

    print(f"Admins (limit via Admin Org, org role when configured; "
          f"enterprise-role & single-org exempt): {len(admins)}")
    for uid in admins:
        rn = (actual[uid].enterprise_role or {}).get("role_name")
        print(f"  - {email(uid)} ({rn})")
    if admin_no_org:
        print(f"WARNING: {len(admin_no_org)} admin(s) not in the Admin Org "
              "(its limit is applied anyway):")
        for uid in admin_no_org:
            print(f"  - {email(uid)}")
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

    plan_footer(path, f"Apply drift with:  python govern.py apply {path} [--approved]")
    return plan
