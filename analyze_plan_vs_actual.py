#!/usr/bin/env python3
"""Planned vs actual Local Agent ACU consumption — month-to-date, active members.

A read-only companion to ``govern.py`` / ``report.py`` that answers one question:
*how are we tracking on PLANNED ACU consumption vs ACTUAL?* It is deliberately
opinionated about the four wrinkles that make a naive comparison misleading:

  1. **Not everyone has logged in.** We aggregate to a *person* (by email, so an
     ``email|...`` invite and an ``okta|...`` SSO id count once and their usage is
     summed) and restrict the headline to ACTIVE people (>=1 login, from the
     audit log). Never-logged-in seats are reported separately as idle capacity,
     never folded into the utilization ratio.
  2. **Planned limits changed mid-month.** "Planned" = each person's *current*
     per-user monthly limit (the corrected tiers). We also keep each person's
     *original* onboarding plan (the corrected Light tier of their IDE/CLI track,
     since everyone was onboarded into Light) so upgrades are visible.
  3. **Only a few days of data, but a whole-month question.** We bridge both
     ways: prorate the monthly plan DOWN to each person's active days, and
     extrapolate actual UP to a full-month run-rate. Confidence is flagged.
  4. **The limits "mistake" was the LOW initial tiers (18/36/72).** Those are
     treated as the error; the corrected tiers (from the live limit + current
     limits.toml) are the plan. People moved to a higher org or pinned in
     overrides.toml show up as the "exceeded original plan" cohort — the usage
     that drove the upgrades.

Read-only: it lists members + orgs + the login audit (to find who's active), then
fetches the per-user limit and daily consumption ONLY for active people. Token +
host come from .env, operational config from config.toml — exactly like the rest
of the toolkit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from govern.client import DevinClient
from govern.config import load_config
from govern.constants import SECONDS_PER_DAY as DAY
from govern.policy import load_policy
from govern.state import (
    _parallel_map,
    parse_limit_payload,
    read_members,
    read_org_index,
    read_utilizations,
)

DAYS_IN_CYCLE = 30  # the per-user monthly Local Agent ACU cap is a 30-day cycle


# ----------------------------------------------------------------------------
# date / range helpers (UTC days, matching the API's daily buckets)
# ----------------------------------------------------------------------------
def _day_start_utc(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _bucket_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _month_bounds(s: str, today: date) -> tuple[date, date]:
    dt = datetime.strptime(s, "%Y-%m")
    start = date(dt.year, dt.month, 1)
    nxt = date(dt.year + 1, 1, 1) if dt.month == 12 else date(dt.year, dt.month + 1, 1)
    return start, min(nxt - timedelta(days=1), today)


def _resolve_range(month: Optional[str], today: date) -> tuple[date, date, str]:
    if month:
        start, end = _month_bounds(month, today)
        return start, end, f"month {month}"
    return today.replace(day=1), today, "current month"


# ----------------------------------------------------------------------------
# local-file inputs: onboarding tier (audit.jsonl) + IDE/CLI track (ocbc CSVs)
# ----------------------------------------------------------------------------
def _onboard_tier_by_uid(audit_path: str) -> dict[str, str]:
    """{onboard_user_id: tier_name} from the FIRST 'onboard:<Tier>' audit record.

    The onboard records key the (often ``email|...``) id assigned at invite time;
    we reconcile that to a live person by shared identity later. Best-effort: the
    fallback is the person's IDE/CLI-track Light tier.
    """
    out: dict[str, str] = {}
    if not os.path.isfile(audit_path):
        return out
    with open(audit_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            reason = r.get("reason") or ""
            if not reason.startswith("onboard:"):
                continue
            uid = r.get("user_id")
            if uid and uid not in out:
                out[uid] = reason.split(":", 1)[1]
    return out


def _emails_from_csv(path: str) -> set[str]:
    """Lower-cased email set from an ocbc access CSV (first column = email)."""
    out: set[str] = set()
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        next(f, None)  # header
        for line in f:
            cell = line.split(",", 1)[0].strip().strip('"').lower()
            if cell and "@" in cell:
                out.add(cell)
    return out


# ----------------------------------------------------------------------------
# person model: collapse an email's identities into one unit
# ----------------------------------------------------------------------------
class Person:
    __slots__ = (
        "active",
        "active_days_with_use",
        "actual",
        "email",
        "first_login",
        "key",
        "limit",
        "limit_set",
        "name",
        "org_ids",
        "original",
        "track",
        "uids",
    )

    def __init__(self, key):
        self.key = key
        self.email = None
        self.name = None
        self.uids: list[str] = []
        self.org_ids: set[str] = set()
        self.active = False
        self.first_login: Optional[int] = None
        self.limit: Optional[int] = None      # current plan (numeric/None=unlimited)
        self.limit_set = False
        self.track = "IDE"                     # "IDE" | "CLI"
        self.original: Optional[int] = None    # corrected onboarding (Light) plan
        self.actual = 0.0                      # summed ACUs in window
        self.active_days_with_use = 0

    @property
    def numeric(self) -> bool:
        return isinstance(self.limit, (int, float))


def _group_people(members) -> tuple[dict, dict[str, str]]:
    """Group ActualState members into Person units keyed by lower-cased email
    (falling back to the user_id when an entry has no email). Returns
    (people_by_key, uid->key)."""
    people: dict[str, Person] = {}
    uid_to_key: dict[str, str] = {}
    for uid, m in members.items():
        key = (m.email or "").lower() or uid
        p = people.get(key)
        if p is None:
            p = people[key] = Person(key)
        p.uids.append(uid)
        uid_to_key[uid] = key
        if m.email and not p.email:
            p.email = m.email
        if m.name and not p.name:
            p.name = m.name
        p.org_ids.update(m.org_ids)
    return people, uid_to_key


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_plan_vs_actual",
        description="Planned vs actual Local Agent ACU consumption (active "
                    "members, month-to-date), with run-rate projection.")
    p.add_argument("--month", help="analyze a whole calendar month YYYY-MM "
                                    "(default: the current month)")
    p.add_argument("--top", type=int, default=15,
                   help="rows to show in each ranked list (default 15)")
    p.add_argument("--include-never", action="store_true",
                   help="also fetch never-logged-in members' limits for an exact "
                        "(not estimated) idle-capacity figure")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--config", help="path to config.toml")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.token:
        p.error("DEVIN_SERVICE_USER_TOKEN not set (see .env / .env.example)")
    client = DevinClient.from_config(cfg)
    pol = load_policy(cfg)

    today = datetime.now(timezone.utc).date()
    start, end, range_label = _resolve_range(args.month, today)

    # Corrected tier plans for the IDE/CLI Light onboarding baseline (#2/#4).
    ide_light = pol.limits.get("IDE Light")
    cli_light = pol.limits.get("CLI IDE Light")

    # --- 1. members + orgs + login audit (cheap) ---------------------------
    members = read_members(client)
    org_index = read_org_index(client)
    people, uid_to_key = _group_people(members)

    email_to_key = {p.email.lower(): p.key for p in people.values() if p.email}
    for ev in client.list_all_audit_logs(action="login"):
        uid = ev.get("user_id")
        key = uid_to_key.get(uid)
        if key is None:
            key = email_to_key.get((ev.get("user_email") or "").lower())
        if key is None:
            continue
        person = people[key]
        person.active = True
        # The live audit API timestamps events with `created_at` (epoch seconds);
        # fall back to the local audit log's `ts` for offline/test inputs.
        ts = ev.get("created_at") or ev.get("ts")
        if isinstance(ts, int) and (person.first_login is None or ts < person.first_login):
            person.first_login = ts

    # IDE/CLI track + corrected original (Light) plan per person.
    cli_emails = _emails_from_csv("ocbc_cli_access_users.csv")
    onboard_tier = _onboard_tier_by_uid(cfg.path("audit_log"))
    for person in people.values():
        em = (person.email or "").lower()
        names = [org_index.get(o, "") for o in person.org_ids]
        is_cli = em in cli_emails or any(n.startswith("CLI") for n in names)
        person.track = "CLI" if is_cli else "IDE"
        tier = next((onboard_tier[u] for u in person.uids if u in onboard_tier), None)
        person.original = (pol.limits.get(tier) if tier in pol.limits else None)
        if person.original is None:
            person.original = cli_light if is_cli else ide_light

    active = [p for p in people.values() if p.active]
    never = [p for p in people.values() if not p.active]

    # --- 2. per-user limit + utilization for ACTIVE people only ------------
    active_uids = [u for p in active for u in p.uids]
    limits = _parallel_map(client.get_user_limit, active_uids,
                           workers=getattr(client, "read_concurrency", 8))
    for p_ in active:
        lim, lim_set = None, False
        for u in p_.uids:
            lv, s = parse_limit_payload(limits.get(u))
            if s and lv is None:          # explicit unlimited dominates
                lim, lim_set = None, True
                break
            if isinstance(lv, (int, float)):
                lim = lv if lim is None else max(lim, lv)
                lim_set = True
            elif s:
                lim_set = True
        p_.limit, p_.limit_set = lim, lim_set

    time_after = _day_start_utc(start) - DAY
    time_before = _day_start_utc(end) + 2 * DAY
    util = read_utilizations(client, active_uids,
                             time_after=time_after, time_before=time_before)
    for p_ in active:
        days_used = set()
        for u in p_.uids:
            for d in (util.get(u) or {}).get("consumption_by_date") or []:
                bd = _bucket_date(d.get("date", 0))
                if bd < start or bd > end:
                    continue
                acus = d.get("acus") or 0.0
                p_.actual += acus
                if acus > 0:
                    days_used.add(bd)
        p_.active_days_with_use = len(days_used)

    # --- 3. derive per-person metrics --------------------------------------
    elapsed_month = (end - start).days + 1

    def days_active(p_):
        # Days the person has had access within the window (since first login,
        # clamped to the window). Floored at the number of days they actually
        # consumed, so a login event that post-dates earlier usage (or is
        # missing) can never yield a denominator smaller than the numerator.
        if p_.first_login is None:
            base = elapsed_month
        else:
            fl = max(_bucket_date(p_.first_login), start)
            base = (end - fl).days + 1
        return max(1, p_.active_days_with_use, base)

    def projected(p_):
        return p_.actual / days_active(p_) * DAYS_IN_CYCLE

    def plan_to_date(p_):  # monthly cap prorated to the person's active days
        return (p_.limit or 0) * days_active(p_) / DAYS_IN_CYCLE

    capped = [p_ for p_ in active if p_.numeric and p_.limit > 0]
    uncapped = [p_ for p_ in active if not (p_.numeric and p_.limit > 0)]

    sum_plan_month = sum(p_.limit for p_ in capped)
    sum_plan_to_date = sum(plan_to_date(p_) for p_ in capped)
    sum_actual = sum(p_.actual for p_ in capped)
    sum_projected = sum(projected(p_) for p_ in capped)

    # upgrade story (#2 nuance): current plan raised above the original (Light)
    upgraded = [p_ for p_ in capped if p_.original and p_.limit > p_.original]
    exceeded_original = [p_ for p_ in capped if p_.original and
                         (p_.actual > p_.original or projected(p_) > p_.original)]
    over_current = [p_ for p_ in capped if projected(p_) > p_.limit]
    under_used = [p_ for p_ in capped if projected(p_) < 0.25 * p_.limit]

    # idle capacity from never-logged-in seats (#1). Estimate from the Light
    # track unless --include-never fetches the exact provisioned limits.
    if args.include_never and never:
        never_uids = [u for p_ in never for u in p_.uids]
        nlimits = _parallel_map(client.get_user_limit, never_uids,
                                workers=getattr(client, "read_concurrency", 8))
        idle = 0
        for p_ in never:
            best = 0
            for u in p_.uids:
                lv, _s = parse_limit_payload(nlimits.get(u))
                if isinstance(lv, (int, float)):
                    best = max(best, lv)
            idle += best
        idle_label = "exact"
    else:
        idle = sum((cli_light if p_.track == "CLI" else ide_light) or 0
                   for p_ in never)
        idle_label = "estimated (Light-tier)"

    def ratio(a, b):
        return (a / b) if b else None

    # --- 4. output ---------------------------------------------------------
    if args.json:
        def row(p_):
            return {"email": p_.email or p_.key, "track": p_.track,
                    "current_plan": p_.limit, "original_plan": p_.original,
                    "actual_mtd": round(p_.actual, 3),
                    "days_active": days_active(p_),
                    "active_days_with_use": p_.active_days_with_use,
                    "projected_month": round(projected(p_), 3),
                    "projected_pct_of_current": ratio(projected(p_), p_.limit),
                    "projected_pct_of_original": ratio(projected(p_), p_.original)}
        out = {
            "range": {"from": start.isoformat(), "to": end.isoformat(),
                      "label": range_label, "elapsed_days": elapsed_month},
            "population": {"people": len(people), "active": len(active),
                           "never_logged_in": len(never),
                           "active_capped": len(capped),
                           "active_uncapped": len(uncapped)},
            "active_capped_rows": [row(x) for x in capped],
            "active_capped_totals": {
                "planned_month": sum_plan_month,
                "planned_to_date": round(sum_plan_to_date, 3),
                "actual_to_date": round(sum_actual, 3),
                "projected_month": round(sum_projected, 3),
                "on_track_pct": ratio(sum_actual, sum_plan_to_date),
                "projected_utilization_pct": ratio(sum_projected, sum_plan_month)},
            "idle_capacity": {"never_logged_in": len(never),
                              "provisioned_acu": idle, "basis": idle_label},
            "upgraded": [row(x) for x in upgraded],
            "exceeded_original_plan": [row(x) for x in sorted(
                exceeded_original, key=projected, reverse=True)],
            "over_current_plan": [row(x) for x in sorted(
                over_current, key=lambda z: ratio(projected(z), z.limit) or 0,
                reverse=True)],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    def fmt(n):
        return f"{n:,.0f}" if abs(n) >= 100 else f"{n:,.1f}"

    print("=== planned vs actual ACU consumption (read-only) ===")
    print(f"window: {start} .. {end}  ({range_label}, UTC days; "
          f"{elapsed_month}d elapsed of a {DAYS_IN_CYCLE}d cycle)")
    print("plan = current per-user monthly limit | actual = summed daily ACUs | "
          "active = logged in >= once\n")

    print(f"Population: {len(people)} people  | active {len(active)} | "
          f"never logged in {len(never)}")
    print(f"  active with a numeric cap : {len(capped)}")
    print(f"  active uncapped/unset     : {len(uncapped)} "
          f"(excluded from the ratios below)\n")

    print("--- ACTIVE, capped — planned vs actual ---")
    print(f"  planned (full month)   : {fmt(sum_plan_month):>14}  ACU "
          f"(sum of {len(capped)} current caps)")
    print(f"  planned (to-date)      : {fmt(sum_plan_to_date):>14}  ACU "
          f"(caps prorated to each person's active days)")
    print(f"  actual  (to-date)      : {fmt(sum_actual):>14}  ACU")
    ot = ratio(sum_actual, sum_plan_to_date)
    pu = ratio(sum_projected, sum_plan_month)
    print(f"  on-track vs to-date    : {(ot or 0):>13.0%}  "
          f"(actual-to-date / planned-to-date)")
    print(f"  projected (run-rate)   : {fmt(sum_projected):>14}  ACU  "
          f"-> {(pu or 0):.0%} of the full-month plan\n")

    print(f"--- idle capacity (never-logged-in, {idle_label}) ---")
    print(f"  {len(never)} people holding ~{fmt(idle)} ACU of monthly plan, "
          f"unused so far (not in the ratios above)\n")

    print("--- upgrade story (the limits were raised because usage outgrew them) ---")
    up_orig = sum(x.original for x in upgraded)
    up_cur = sum(x.limit for x in upgraded)
    print(f"  {len(upgraded)} people upgraded above their original (Light) plan: "
          f"{fmt(up_orig)} -> {fmt(up_cur)} ACU planned")
    print(f"  {len(exceeded_original)} people have ACTUAL or projected usage "
          f"above their ORIGINAL plan (the upgrade-drivers)\n")

    def table(title, rows, key):
        if not rows:
            print(f"{title}: none\n")
            return
        print(title)
        print(f"  {'person':32} {'track':5} {'orig':>6} {'plan':>6} "
              f"{'actual':>9} {'proj':>9} {'%cur':>6} {'%orig':>6} {'days':>5}")
        for x in sorted(rows, key=key, reverse=True)[:args.top]:
            who = (x.email or x.key)[:32]
            pc = ratio(projected(x), x.limit)
            po = ratio(projected(x), x.original)
            print(f"  {who:32} {x.track:5} {fmt(x.original or 0):>6} "
                  f"{fmt(x.limit):>6} {fmt(x.actual):>9} {fmt(projected(x)):>9} "
                  f"{(pc or 0):>5.0%} {(po or 0):>5.0%} "
                  f"{x.active_days_with_use:>2}/{days_active(x):<2}")
        print()

    table("--- exceeded ORIGINAL plan (justified the upgrade) ---",
          exceeded_original, projected)
    table("--- projected to exceed CURRENT plan (watchlist) ---",
          over_current, lambda z: ratio(projected(z), z.limit) or 0)
    table("--- under-using current plan (< 25% projected) ---",
          under_used, lambda z: -(ratio(projected(z), z.limit) or 0))

    maxd = max((days_active(x) for x in capped), default=0)
    print("Caveats: run-rate is extrapolated from at most "
          f"{maxd} active day(s) of data — early, weekend-skewed and ramping, so "
          "projections are directional, not precise. Re-run as more days land.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
