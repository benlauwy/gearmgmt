"""Read-only reports: usage/cap detection, capacity, coverage, logins, lookup.

None of these mutate or write a plan (usage writes only its candidates worklist
and an optional export). They read actual state and print a summary; turn drift
into an applyable plan with ``reconcile`` instead.
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import Optional

from .config import Config
from .constants import SECONDS_PER_DAY
from .errors import GovernError
from .population import is_admin, resolve_population
from .render import fmt_limit, pct
from .state import (
    ActualState,
    parse_limit_payload,
    read_actual,
    read_members,
    read_org_index,
    read_utilizations,
    resolve_identities,
    resolve_one,
)


def utilization_status(days: list, cap, *, near_cap_pct: float,
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
    window = trend_window_days * SECONDS_PER_DAY
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


def export_format(path: str) -> str:
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
        raise GovernError(
            f"unsupported export format {ext!r}; save as .xlsx (or .csv)")
    raise GovernError(
        f"cannot infer an export format from "
        f"{ext or '(no extension)'!r}; use a .csv or .xlsx filename")


def write_table(path: str, header: list, rows: list) -> None:
    """Write ``header`` + ``rows`` to ``path`` as CSV/TSV or Excel, choosing the
    format from the extension (see :func:`export_format`). Excel goes through
    ``openpyxl`` (lazy import, same guidance as the roster reader) so CSV-only
    users never need the dependency. Parent directories are created as needed."""
    fmt = export_format(path)
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
        raise GovernError(
            "writing .xlsx requires openpyxl "
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
        export_format(export)

    u = cfg.utilization
    near = float(u.get("near_cap_pct", 0.8))
    trend_window = int(u.get("trend_window_days", 14))
    cycle_days = int(u.get("cycle_days", 30))
    products = list(u.get("products", []) or [])

    now = int(time.time())
    after = now - cycle_days * SECONDS_PER_DAY

    # A single-user spot-check stays lean (like `lookup`): resolve via the member
    # list (one call, no per-user limit reads) and fetch ONLY that user's limit,
    # rather than triggering read_actual's whole-population limit fan-out.
    single = user_id is not None
    if single:
        members = read_members(client)
        user_id = resolve_one(members, user_id)
        limit, limit_set = parse_limit_payload(client.get_user_limit(user_id))
        actual = {user_id: ActualState(user_id=user_id,
                                       email=members[user_id].email,
                                       limit=limit, limit_set=limit_set)}
    else:
        actual = read_actual(client)

    print("=== usage / cap detection (detection only) ===")
    src = ", ".join(products) if products else "total_acus"
    print(f"cap: per-user limit | usage: {src} over {cycle_days}d | "
          f"near-cap >= {near:.0%} | trend window {trend_window}d\n")

    capped = [(uid, a) for uid, a in actual.items()
              if isinstance(a.limit, (int, float)) and a.limit > 0]
    if not capped:
        if single:
            who = actual[user_id].email or user_id
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
        st = utilization_status(data.get("consumption_by_date", []), a.limit,
                                 near_cap_pct=near, trend_window_days=trend_window,
                                 products=products, now=now)
        rows.append((uid, a, st))
        if st["flagged"]:
            candidates.append({"user_id": uid, "email": a.email,
                               "consumption": st["consumption"], "cap": st["cap"],
                               "pct": st["pct"], "trend": st["trend"]})

    rows.sort(key=lambda r: r[2]["pct"] or 0, reverse=not reverse)
    for uid, a, st in rows:
        flag = "NEAR/AT CAP" if st["flagged"] else "ok"
        print(f"  [{flag:11}] {a.email or uid:34} "
              f"{st['consumption']:.1f}/{st['cap']} ({(st['pct'] or 0):.0%}) "
              f"trend={st['trend']} (recent {st['recent']:.1f} vs prior {st['prior']:.1f})")

    # --export writes the FULL table (every row above), independent of the flagged
    # upgrade worklist below; format is picked from the extension (.csv/.xlsx).
    if export:
        header = ["email", "user_id", "status", "consumption", "cap",
                  "pct_of_cap", "trend", "recent_window_acus", "prior_window_acus"]
        table = [[a.email or "", uid,
                  "NEAR/AT CAP" if st["flagged"] else "ok",
                  round(st["consumption"], 4), st["cap"],
                  round(st["pct"], 4) if st["pct"] is not None else "",
                  st["trend"], round(st["recent"], 4), round(st["prior"], 4)]
                 for uid, a, st in rows]
        write_table(export, header, table)
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

    numeric = [a.limit for a in actual.values()
               if isinstance(a.limit, (int, float))]
    unlimited = sum(1 for a in actual.values()
                    if a.limit_set and a.limit is None)
    unset = sum(1 for a in actual.values() if not a.limit_set)
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
    actual, _desired, org_index, pol = resolve_population(cfg, client)
    admin_subs = [s.lower() for s in cfg.governance.get("admin_role_name_contains", [])]
    orgs = sorted(org_index.items(), key=lambda kv: kv[1])  # (org_id, name)

    members_by_org: dict[str, list[str]] = {}
    for uid, a in actual.items():
        for oid in a.org_ids:
            members_by_org.setdefault(oid, []).append(uid)

    governed_names = set(pol.roles) | set(pol.limits)
    print("=== coverage (read-only) ===\n")
    for oid, name in orgs:
        if name not in governed_names:
            continue
        has_limit, has_role = name in pol.limits, name in pol.roles
        intended_limit, intended_role = pol.limits.get(name), pol.roles.get(name)
        members = members_by_org.get(oid, [])
        governed = [u for u in members if not is_admin(actual[u], admin_subs)]
        admins = len(members) - len(governed)

        lim_ok = sum(1 for u in governed if actual[u].limit == intended_limit)
        role_ok = sum(1 for u in governed
                      if (actual[u].enterprise_role or {}).get("role_id") == intended_role)

        il = fmt_limit(intended_limit) if has_limit else "(ungoverned)"
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
            if has_limit and actual[u].limit != intended_limit:
                problems.append(f"limit {fmt_limit(actual[u].limit, actual[u].limit_set)} "
                                f"(want {fmt_limit(intended_limit)})")
            if has_role:
                cur = (actual[u].enterprise_role or {}).get("role_id")
                if cur != intended_role:
                    problems.append(f"role {cur} (want {intended_role})")
            if problems:
                mismatches.append((u, problems))
        if mismatches:
            print("  mismatches:")
            for u, problems in mismatches:
                print(f"    - {actual[u].email or u}: {'; '.join(problems)}")
        print()

    ungoverned = sorted(name for _oid, name in orgs if name not in governed_names)
    if ungoverned:
        print(f"Ungoverned orgs (no policy entry): {', '.join(ungoverned)}")


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
    email_to_uid = {(m.email or "").lower(): uid
                    for uid, m in members.items() if m.email}
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
    print(f"  logged in >= once: {n_in} ({pct(n_in, total)})")
    print(f"  never logged in:   {n_never} ({pct(n_never, total)})\n")

    # Per-org breakdown. A member in multiple orgs is counted under each, so these
    # rows don't sum to the totals above; members in no org are bucketed last.
    members_by_org: dict[str, list[str]] = {}
    for uid, m in members.items():
        for oid in m.org_ids:
            members_by_org.setdefault(oid, []).append(uid)

    def row(label: str, uids: list[str]):
        ins = sum(1 for u in uids if u in logged_in)
        never = len(uids) - ins
        print(f"  {label}: {len(uids)} member(s) | logged in {ins} "
              f"({pct(ins, len(uids))}) | never {never} ({pct(never, len(uids))})")

    print("Per-org breakdown (members in multiple orgs count under each):")
    for oid in sorted(members_by_org, key=lambda o: org_index.get(o, f"<unknown:{o}>")):
        row(org_index.get(oid, f"<unknown:{oid}>"), members_by_org[oid])
    if not members_by_org:
        print("  (no org memberships)")

    no_org = [uid for uid, m in members.items() if not m.org_ids]
    if no_org:
        row("(no org)", no_org)

    if dump_never:
        # Emails of members who never logged in, sorted & de-duped. Members
        # without an email on file can't be dumped, so we count them separately
        # rather than emitting blank lines.
        never_emails = sorted({(m.email or "").lower()
                               for uid, m in members.items()
                               if uid not in logged_in and m.email})
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
        raise GovernError("lookup requires --user EMAIL_OR_USER_ID")
    members = read_members(client)
    matches = resolve_identities(members, user_id)  # every matching identity
    rows = []
    for uid in matches:             # one get_user_limit per match (usually 1–2)
        limit = fmt_limit(*parse_limit_payload(client.get_user_limit(uid)))
        rows.append((uid, limit))
        print(f"{uid}\t{limit}")
    return rows
