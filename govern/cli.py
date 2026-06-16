"""Command-line interface: ``python govern.py <command> [--dry-run]``."""
from __future__ import annotations

import argparse
from typing import Optional

from .client import DevinClient
from .config import load_config
from . import workflows


def _client(cfg, dry_run: bool) -> DevinClient:
    return DevinClient(
        cfg.token, cfg.base_url, dry_run=dry_run,
        max_retries=int(cfg.api.get("max_retries", 5)),
        backoff=float(cfg.api.get("retry_backoff_seconds", 2.0)),
        sleep=float(cfg.api.get("rate_limit_sleep_seconds", 0.1)),
    )


def main(argv: Optional[list[str]] = None) -> int:
    # Shared flags accepted either before OR after the command. default=SUPPRESS
    # so an unset flag in one position never clobbers a value set in the other.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="plan only; never mutate")

    p = argparse.ArgumentParser(prog="govern", parents=[common],
                                description="Devin enterprise limit & role governance")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("onboard", parents=[common],
                        help="set a new member's limit + role from policy")
    sp.add_argument("--user", help="email or user_id of the joiner")
    sp.add_argument("--org", help="org name: whole-org, or to validate --user")

    sub.add_parser("move", parents=[common],
                   help="re-materialize members who changed orgs since last run")

    sp = sub.add_parser("update-limits", parents=[common],
                        help="re-materialize limits after editing limits.toml")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--org", help="org name whose members to re-materialize")
    g.add_argument("--user", help="a single email or user_id")

    sp = sub.add_parser("offboard", parents=[common],
                        help="remove a user from all orgs + zero limit + leaver role")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="email or user_id to offboard")
    g.add_argument("--org-dissolved", dest="org_dissolved",
                   help="offboard ALL members of this org")

    sub.add_parser("reconcile", parents=[common],
                   help="report drift of actual vs desired (+ save a plan)")
    sub.add_parser("usage", parents=[common],
                   help="flag users near/at their cap (detection only)")
    sub.add_parser("coverage", parents=[common],
                   help="per-org intended-vs-actual coverage report")

    sp = sub.add_parser("apply", parents=[common],
                        help="execute a saved plan (the approval gate)")
    sp.add_argument("plan", help="path to a plan json under state/plans/")
    sp.add_argument("--approved", action="store_true",
                    help="also apply held increases / new grants")

    args = p.parse_args(argv)
    cfg = load_config(getattr(args, "config", None))
    if not cfg.token:
        p.error("DEVIN_SERVICE_USER_TOKEN not set (see .env / .env.example)")
    client = _client(cfg, getattr(args, "dry_run", False))

    cmd = args.cmd
    if cmd == "onboard":
        workflows.onboard(cfg, client, user_id=getattr(args, "user", None),
                          org=getattr(args, "org", None))
    elif cmd == "move":
        workflows.move_members(cfg, client)
    elif cmd == "update-limits":
        workflows.update_limits(cfg, client, org=getattr(args, "org", None),
                                user_id=getattr(args, "user", None))
    elif cmd == "offboard":
        workflows.offboard(cfg, client, user_id=getattr(args, "user", None),
                           org_dissolved=getattr(args, "org_dissolved", None))
    elif cmd == "reconcile":
        workflows.reconcile(cfg, client)
    elif cmd == "usage":
        workflows.usage(cfg, client)
    elif cmd == "coverage":
        workflows.coverage(cfg, client)
    elif cmd == "apply":
        from .apply import apply_plan
        from .plan import load_plan
        apply_plan(cfg, client, load_plan(args.plan),
                   approved=getattr(args, "approved", False), plan_path=args.plan)
    return 0
