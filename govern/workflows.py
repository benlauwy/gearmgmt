"""Workflow orchestration.

Each command builds a Plan (diff-first) and applies it through the apply gate:
  onboard · move · update_limits · offboard · reconcile · usage · coverage
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from .config import Config
from .plan import Change, Plan, diff, save_plan
from .policy import load_policy, resolve_desired
from .state import (diff_membership, load_snapshot, read_actual, save_snapshot,
                    snapshot_path)


def _is_admin(actual_user: dict, admin_subs: list[str]) -> bool:
    name = ((actual_user.get("enterprise_role") or {}).get("role_name") or "").lower()
    return any(s in name for s in admin_subs)


def _fmt_limit(value, is_set: bool = True) -> str:
    if value is None:
        return "unlimited" if is_set else "unset"
    return str(value)


def _org_id_by_name(org_index: dict, name: str) -> str:
    """Resolve an org name to its id (case-insensitive) or exit with a message."""
    matches = [oid for oid, n in org_index.items() if n.lower() == name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"ERROR: no org named {name!r}. Known: {sorted(org_index.values())}")
    raise SystemExit(f"ERROR: multiple orgs named {name!r}")


def _resolve_user_id(actual: dict, value: str) -> str:
    """Resolve a --user value (a user_id or an email) to a canonical user_id.

    Accepts the API user_id directly, or a case-insensitive email match. Exits
    with a clear message if the value matches no one (or, defensively, >1 user).
    """
    if value in actual:
        return value
    matches = [uid for uid, a in actual.items()
               if (a.get("email") or "").lower() == value.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"ERROR: no user matching {value!r} "
                         f"(give an email or the user_id)")
    raise SystemExit(f"ERROR: email {value!r} matches multiple users: {matches}")


def _resolve_population(cfg: Config, client):
    """Read actual state + org index and resolve desired state for every user.

    Returns (actual, desired_map, org_index, policy).
    """
    pol = load_policy(cfg)
    org_index = {o["org_id"]: o["name"] for o in client.list_organizations()}
    admin_subs = [s.lower() for s in cfg.governance.get("admin_role_name_contains", [])]
    actual = read_actual(client)
    desired = {}
    for uid, a in actual.items():
        names = [org_index.get(oid, f"<unknown:{oid}>") for oid in a["org_ids"]]
        desired[uid] = resolve_desired(uid, names, is_admin=_is_admin(a, admin_subs),
                                       policy=pol, cfg=cfg)
    return actual, desired, org_index, pol


def onboard(cfg: Config, client, *, user_id: Optional[str] = None,
            org: Optional[str] = None):
    """Onboard a member: materialize their desired limit AND enterprise role from
    policy. Use --user for a single joiner or --org for a whole-org
    bulk; pass both to scope a single user within an org. Diff-first — writes a
    plan; apply it with ``govern.py apply <plan>``."""
    if not user_id and not org:
        raise SystemExit("ERROR: onboard requires --user USER_ID and/or --org NAME")

    actual, desired, org_index, _pol = _resolve_population(cfg, client)
    if user_id:
        user_id = _resolve_user_id(actual, user_id)
        targets, scope = {user_id}, f"user:{user_id}"
        if org:
            oid = _org_id_by_name(org_index, org)
            if oid not in actual[user_id]["org_ids"]:
                raise SystemExit(f"ERROR: user {user_id!r} is not a member of {org!r}")
            scope = f"user:{user_id}@{org}"
    else:
        oid = _org_id_by_name(org_index, org)
        targets = {uid for uid, a in actual.items() if oid in a["org_ids"]}
        scope = f"org:{org}"

    subset = {uid: d for uid, d in desired.items() if uid in targets}
    changes = diff(actual, subset)  # both limit and enterprise_role
    plan = Plan(workflow="onboard", triggered_by=f"onboard:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    def email(uid):
        return actual.get(uid, {}).get("email") or uid

    n_limit = sum(c.field == "limit" for c in changes)
    n_role = sum(c.field == "enterprise_role" for c in changes)
    print(f"=== onboard ({scope}) ===")
    print(f"Target members: {len(targets)}  |  changes: {len(changes)} "
          f"({n_limit} limit, {n_role} role)\n")
    for c in changes:
        tag = "APPROVAL" if c.needs_approval else "auto"
        print(f"  [{tag:8}] {c.kind:14} {c.field:16} {email(c.user_id):34} {c.before} -> {c.after}")
    if not changes:
        print("  (no changes — targets already match policy, or are exempt/overridden)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path} [--approved]")
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

    def email(uid):
        return actual.get(uid, {}).get("email") or uid

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
            tag = "APPROVAL" if c.needs_approval else "auto"
            print(f"  [{tag:8}] {c.kind:14} {c.field:16} {email(c.user_id):34} {c.before} -> {c.after}")

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


def update_limits(cfg: Config, client, *, org: Optional[str] = None,
                  user_id: Optional[str] = None):
    """Re-materialize limits after a limits.toml change; the --user
    variant is the single-user upgrade fed by `usage`. Limit-only and diff-first:
    computes each target's desired limit (honoring overrides / admin-exemption /
    single-org rules) and writes a plan. Apply with ``govern.py apply <plan>``."""
    if not org and not user_id:
        raise SystemExit("ERROR: update-limits requires --org NAME or --user USER_ID")

    actual, desired, org_index, _pol = _resolve_population(cfg, client)
    if user_id:
        user_id = _resolve_user_id(actual, user_id)
        targets, scope = {user_id}, f"user:{user_id}"
    else:
        oid = _org_id_by_name(org_index, org)
        targets = {uid for uid, a in actual.items() if oid in a["org_ids"]}
        scope = f"org:{org}"

    subset = {uid: d for uid, d in desired.items() if uid in targets}
    changes = [c for c in diff(actual, subset) if c.field == "limit"]
    plan = Plan(workflow="update-limits", triggered_by=f"update-limits:{scope}", changes=changes)
    path = save_plan(cfg, plan)

    def email(uid):
        return actual.get(uid, {}).get("email") or uid

    print(f"=== update-limits ({scope}) ===")
    print(f"Target members: {len(targets)}  |  limit changes: {len(changes)}\n")
    for c in changes:
        tag = "APPROVAL" if c.needs_approval else "auto"
        print(f"  [{tag:8}] {c.kind:14} {email(c.user_id):34} {c.before} -> {c.after}")
    if not changes:
        print("  (no drift — every target is already at its desired limit)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path}")
    print(f"             python govern.py apply {path} --approved   # include increases")
    return plan


def offboard(cfg: Config, client, *, user_id: Optional[str] = None,
             org_dissolved: Optional[str] = None):
    """Offboard: zero/reclaim the limit, remove the user from ALL orgs, then set
    the special leaver enterprise role (config.leaver). Use --user for
    one leaver or --org-dissolved to fan out across all members of a dissolved org.
    Every change is a revoke/downgrade, so the plan auto-applies (no approval);
    still diff-first via the apply gate."""
    if not user_id and not org_dissolved:
        raise SystemExit("ERROR: offboard requires --user USER_ID or --org-dissolved NAME")

    actual = read_actual(client)
    org_index = {o["org_id"]: o["name"] for o in client.list_organizations()}
    leaver_role = cfg.leaver.get("enterprise_role_id")
    raw_limit = cfg.leaver.get("limit", 0)
    leaver_limit = (None if isinstance(raw_limit, str) and raw_limit.lower() in ("null", "none")
                    else int(raw_limit))

    if user_id:
        user_id = _resolve_user_id(actual, user_id)
        targets, scope = [user_id], f"user:{user_id}"
    else:
        oid = _org_id_by_name(org_index, org_dissolved)
        targets = [uid for uid, a in actual.items() if oid in a["org_ids"]]
        scope = f"org-dissolved:{org_dissolved}"

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

    def email(uid):
        return actual.get(uid, {}).get("email") or uid

    print(f"=== offboard ({scope}) ===")
    print(f"Target users: {len(targets)}  |  changes: {len(changes)} "
          f"(all auto-apply: offboard = revokes/downgrades)\n")
    for uid in targets:
        ucs = [c for c in changes if c.user_id == uid]
        print(f"  {email(uid)}: {len(ucs)} change(s)")
        for c in ucs:
            where = f"  [{org_index.get(c.org_id, c.org_id)}]" if c.org_id else ""
            print(f"     {c.kind:14} {c.field:16} {c.before} -> {c.after}{where}")
    if not changes:
        print("  (nothing to do — already offboarded)")
    print(f"\nPlan saved: {path}")
    print(f"Apply with:  python govern.py apply {path}   # all changes auto-apply")
    return plan


def reconcile(cfg: Config, client, *, auto_correct: bool = False):
    """Report drift of actual vs desired (limits + roles) across the population;
    honors overrides and flags non-admins in >1 org. Read-only — it
    computes and saves a plan but does not apply it."""
    actual, desired, org_index, pol = _resolve_population(cfg, client)
    changes = diff(actual, desired)
    plan = Plan(workflow="reconcile", triggered_by="reconcile", changes=changes)
    path = save_plan(cfg, plan)

    def email(uid):
        return actual.get(uid, {}).get("email") or uid

    need = [c for c in changes if c.needs_approval]
    auto = [c for c in changes if not c.needs_approval]
    governed_names = sorted(set(pol.roles) | set(pol.limits))

    print("=== reconcile (read-only) ===")
    print(f"Population: {len(actual)} user(s)  |  Governed orgs: {', '.join(governed_names)}")
    print(f"Drift: {len(changes)} change(s) — {len(need)} need approval, {len(auto)} auto-apply\n")

    if changes:
        print("Drift detail:")
        for c in changes:
            tag = "APPROVAL" if c.needs_approval else "auto"
            print(f"  [{tag:8}] {c.kind:14} {email(c.user_id):34} {c.before} -> {c.after}  ({c.source})")
        print()

    exempt = [uid for uid, d in desired.items() if d.source == "admin-exempt"]
    violations = [(uid, d) for uid, d in desired.items() if d.source == "violation"]
    no_org = [uid for uid, d in desired.items() if d.source == "no-governed-org"]
    orphans = {}
    for uid, a in actual.items():
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


def usage(cfg: Config, client):
    """Flag users near/at their cap with a usage trend. Detection only:
    it emits candidates for the single-user `update-limits` upgrade and never
    mutates."""
    u = cfg.utilization
    near = float(u.get("near_cap_pct", 0.8))
    trend_window = int(u.get("trend_window_days", 14))
    cycle_days = int(u.get("cycle_days", 30))
    products = list(u.get("products", []) or [])

    now = int(time.time())
    after = now - cycle_days * 86400
    actual = read_actual(client)

    print("=== usage / cap detection (detection only) ===")
    src = ", ".join(products) if products else "total_acus"
    print(f"cap: per-user limit | usage: {src} over {cycle_days}d | "
          f"near-cap >= {near:.0%} | trend window {trend_window}d\n")

    capped = [(uid, a) for uid, a in actual.items()
              if isinstance(a.get("limit"), (int, float)) and a["limit"] > 0]
    if not capped:
        print("No users have a numeric per-user cap set — nothing to evaluate.")

    rows, candidates = [], []
    for uid, a in capped:
        data = client.get_user_utilization(uid, time_after=after, time_before=now)
        st = _utilization_status(data.get("consumption_by_date", []), a["limit"],
                                 near_cap_pct=near, trend_window_days=trend_window,
                                 products=products, now=now)
        rows.append((uid, a, st))
        if st["flagged"]:
            candidates.append({"user_id": uid, "email": a.get("email"),
                               "consumption": st["consumption"], "cap": st["cap"],
                               "pct": st["pct"], "trend": st["trend"]})

    rows.sort(key=lambda r: r[2]["pct"] or 0, reverse=True)
    for uid, a, st in rows:
        flag = "NEAR/AT CAP" if st["flagged"] else "ok"
        print(f"  [{flag:11}] {a.get('email') or uid:34} "
              f"{st['consumption']:.1f}/{st['cap']} ({(st['pct'] or 0):.0%}) "
              f"trend={st['trend']} (recent {st['recent']:.1f} vs prior {st['prior']:.1f})")

    os.makedirs(cfg.path("state_dir"), exist_ok=True)
    out = os.path.join(cfg.path("state_dir"), "usage-candidates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, ensure_ascii=False)

    print(f"\n{len(candidates)} upgrade candidate(s). Written: {out}")
    for c in candidates:
        print(f"  upgrade: python govern.py update-limits --user {c['user_id']}"
              f"   # {c['email']} at {c['pct']:.0%}")
    return candidates


def coverage(cfg: Config, client):
    """Per-org intended-vs-actual limit & role coverage report; doubles
    as reconciliation input. Read-only."""
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
