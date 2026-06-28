#!/usr/bin/env python3
"""Daily ACU consumption report for a single enterprise member.

Usage:
    DEVIN_SERVICE_USER_TOKEN=<cog_...> python report.py --user <email_or_user_id>

Given a single user, list their daily Local Agent ACU consumption over a date
range. With no range flags it reports the CURRENT MONTH (the 1st through today).

Range selection (all dates are UTC days, matching the API's daily buckets):
    (no flags)            current month: the 1st .. today
    --month YYYY-MM       a whole calendar month (capped at today for this month)
    --from YYYY-MM-DD     range start (default: the 1st of the current month)
    --to   YYYY-MM-DD     range end   (default: today)

--month is mutually exclusive with --from/--to. Every day in the range is listed
even when it has no consumption (zero-filled) so the daily series is complete and
the total is unambiguous. --by-product adds the per-product breakdown
(devin / cascade / terminal / review); --json emits machine-readable output.

Read-only: one member-list call to resolve the user, then one daily-consumption
call per matched identity (an email can map to several user_ids — e.g. a pending
invite plus an SSO identity — and their daily series are summed). Token + host
come from .env, operational config from config.toml, exactly like govern.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from govern.client import DevinClient
from govern.config import load_config
from govern.constants import SECONDS_PER_DAY as DAY
from govern.errors import GovernError
from govern.state import read_members, resolve_identities

# The daily endpoint splits ACUs into these products; keep a stable column order
# and append any unknown ones (defensively) after them.
PRODUCT_ORDER = ["devin", "cascade", "terminal", "review"]


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"ERROR: invalid date {s!r} (expected YYYY-MM-DD)") from None


def _month_bounds(s: str, today: date) -> tuple[date, date]:
    """First/last day of month ``YYYY-MM``; the end is capped at today so the
    current month never lists future days."""
    try:
        dt = datetime.strptime(s, "%Y-%m")
    except ValueError:
        raise SystemExit(f"ERROR: invalid month {s!r} (expected YYYY-MM)") from None
    start = date(dt.year, dt.month, 1)
    nxt = date(dt.year + 1, 1, 1) if dt.month == 12 else date(dt.year, dt.month + 1, 1)
    return start, min(nxt - timedelta(days=1), today)


def _resolve_range(args, today: date) -> tuple[date, date, str]:
    """Turn the range flags into an inclusive [start, end] plus a human label."""
    if args.month:
        if args.frm or args.to:
            raise SystemExit("ERROR: --month cannot be combined with --from/--to")
        start, end = _month_bounds(args.month, today)
        label = f"month {args.month}"
    elif args.frm or args.to:
        start = _parse_date(args.frm) if args.frm else today.replace(day=1)
        end = _parse_date(args.to) if args.to else today
        label = "custom range"
    else:
        start, end, label = today.replace(day=1), today, "current month"
    if start > end:
        raise SystemExit(f"ERROR: range start {start} is after end {end}")
    return start, end, label


def _day_start_utc(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _bucket_date(ts: int) -> date:
    """The calendar day a daily bucket belongs to. Buckets start at 08:00 UTC
    (midnight UTC-8), whose UTC date is exactly that consumption day."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _collect(client, ids: list[str], start: date, end: date):
    """Sum each matched identity's daily series into {date: total} +
    {date: {product: acus}}, keeping only days within [start, end]."""
    time_after = _day_start_utc(start) - DAY            # pad the request window
    time_before = _day_start_utc(end) + 2 * DAY         # then filter precisely below
    totals: dict[date, float] = defaultdict(float)
    per_product: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    products_seen: set[str] = set()
    for uid in ids:
        data = client.get_user_utilization(
            uid, time_after=time_after, time_before=time_before) or {}
        for d in data.get("consumption_by_date") or []:
            bd = _bucket_date(d.get("date", 0))
            if bd < start or bd > end:
                continue
            totals[bd] += d.get("acus") or 0.0
            for prod, val in (d.get("acus_by_product") or {}).items():
                per_product[bd][prod] += val or 0.0
                products_seen.add(prod)
    cols = [p for p in PRODUCT_ORDER if p in products_seen]
    cols += sorted(products_seen - set(cols))
    return totals, per_product, cols


def _days(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="report",
        description="List a single member's daily Local Agent ACU consumption "
                    "(defaults to the current month).")
    p.add_argument("--user", required=True,
                   help="the member: an email or a raw user_id")
    p.add_argument("--month", help="report a whole calendar month, YYYY-MM "
                                   "(mutually exclusive with --from/--to)")
    p.add_argument("--from", dest="frm", metavar="YYYY-MM-DD",
                   help="range start (default: the 1st of the current month)")
    p.add_argument("--to", metavar="YYYY-MM-DD",
                   help="range end (default: today)")
    p.add_argument("--by-product", action="store_true", dest="by_product",
                   help="break each day down by product "
                        "(devin / cascade / terminal / review)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of the text table")
    p.add_argument("--config", help="path to config.toml")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.token:
        p.error("DEVIN_SERVICE_USER_TOKEN not set (see .env / .env.example)")
    client = DevinClient.from_config(cfg)

    today = datetime.now(timezone.utc).date()
    start, end, range_label = _resolve_range(args, today)

    members = read_members(client)
    try:
        ids = resolve_identities(members, args.user)
    except GovernError as e:
        raise SystemExit(f"ERROR: {e}") from e
    email = next((members[i].email for i in ids if members[i].email), None)

    totals, per_product, product_cols = _collect(client, ids, start, end)
    days = _days(start, end)
    grand_total = sum(totals.values())
    active = sum(1 for d in days if totals.get(d, 0.0) > 0)

    if args.json:
        out = {
            "user": args.user,
            "email": email,
            "user_ids": ids,
            "range": {"from": start.isoformat(), "to": end.isoformat(),
                      "label": range_label},
            "days": [
                {"date": d.isoformat(), "acus": round(totals.get(d, 0.0), 6),
                 **({"by_product": {p: round(per_product[d].get(p, 0.0), 6)
                                    for p in product_cols}} if args.by_product else {})}
                for d in days
            ],
            "total_acus": round(grand_total, 6),
            "active_days": active,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print("=== daily ACU consumption ===")
    print(f"user:  {email or args.user}")
    if len(ids) == 1:
        print(f"id:    {ids[0]}")
    else:
        print(f"ids:   {len(ids)} identities combined: {', '.join(ids)}")
    print(f"range: {start} .. {end}  ({range_label}, UTC days)\n")

    if args.by_product:
        head = f"  {'date':10}  {'acus':>12}  " + "  ".join(f"{p:>10}" for p in product_cols)
        rule = "  " + "-" * (len(head) - 2)
        print(head)
        print(rule)
        for d in days:
            cols = "  ".join(f"{per_product[d].get(p, 0.0):>10.4f}" for p in product_cols)
            print(f"  {d.isoformat():10}  {totals.get(d, 0.0):>12.4f}  {cols}")
        print(rule)
        tot_cols = "  ".join(
            f"{sum(per_product[d].get(p, 0.0) for d in days):>10.4f}" for p in product_cols)
        print(f"  {'total':10}  {grand_total:>12.4f}  {tot_cols}")
    else:
        print(f"  {'date':10}  {'acus':>12}")
        print(f"  {'-'*10}  {'-'*12}")
        for d in days:
            print(f"  {d.isoformat():10}  {totals.get(d, 0.0):>12.4f}")
        print(f"  {'-'*10}  {'-'*12}")
        print(f"  {'total':10}  {grand_total:>12.4f}")

    print(f"\n{grand_total:.4f} ACU over {len(days)} day(s) "
          f"({active} with consumption).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
