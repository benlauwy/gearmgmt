"""Command-line interface: ``python govern.py <command> [--dry-run]``."""
from __future__ import annotations

import argparse
from typing import Optional

from .client import DevinClient
from .config import load_config
from . import workflows


# Shown as the top-level description AND appended to every subcommand's
# ``--help`` so a reader always sees what the tool itself is.
_ENGINE = (
    "govern is the Devin enterprise governance engine: it manages members' "
    "organization membership, enterprise/org roles, and per-user ACU limits. "
    "It is diff-first: every command computes the changes and writes a plan "
    "that you then apply through an approval gate (govern.py apply)."
)


def _client(cfg, dry_run: bool) -> DevinClient:
    return DevinClient(
        cfg.token, cfg.base_url, dry_run=dry_run,
        max_retries=int(cfg.api.get("max_retries", 5)),
        backoff=float(cfg.api.get("retry_backoff_seconds", 2.0)),
        sleep=float(cfg.api.get("rate_limit_sleep_seconds", 0.1)),
        read_concurrency=int(cfg.api.get("read_concurrency", 8)),
        apply_concurrency=int(cfg.api.get("apply_concurrency", 8)),
    )


def main(argv: Optional[list[str]] = None) -> int:
    # Shared flags accepted either before OR after the command. default=SUPPRESS
    # so an unset flag in one position never clobbers a value set in the other.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to config.toml")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="plan only; never mutate")

    p = argparse.ArgumentParser(prog="govern", parents=[common],
                                description=_ENGINE)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, summary: str, **kwargs):
        """Register a subcommand. ``summary`` is the one-line help shown in the
        top-level command list; combined with the engine overview it also
        becomes the description printed by ``govern.py <command> --help``."""
        return sub.add_parser(name, parents=[common], help=summary,
                              description=f"{summary}. {_ENGINE}", **kwargs)

    sp = add("onboard", "invite users from a CSV/.xlsx roster + set org, role, limit")
    sp.add_argument("--file", required=True,
                    help="path to a CSV or .xlsx roster: an email column and an "
                         "optional group/org-name column (with a header row)")

    add("move", "re-materialize members who changed orgs since last run")

    sp = add("reassign", "bulk-move members from a CSV/.xlsx roster to a new org "
                         "(add to destination + set its role/limit, remove from old org)")
    sp.add_argument("--file", required=True,
                    help="path to a CSV or .xlsx roster: an email column and an "
                         "optional destination group/org-name column (with a header row)")

    sp = add("update-limits", "re-materialize limits after editing limits.toml / overrides.toml")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--org", help="org name whose members to re-materialize")
    g.add_argument("--user", help="a single email or user_id")

    sp = add("offboard", "remove user(s) from all orgs + zero limit + leaver role")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="email or user_id to offboard")
    g.add_argument("--org-dissolved", dest="org_dissolved",
                   help="offboard ALL members of this org")
    g.add_argument("--file", help="path to a CSV/.xlsx roster of emails to "
                                  "offboard in bulk (an email column with a header "
                                  "row; any group/org column is ignored)")

    add("reconcile", "report drift of actual vs desired (+ save a plan)")
    sp = add("usage", "flag users near/at their cap (detection only)")
    sp.add_argument("--reverse", action="store_true",
                    help="reverse the sort order (lowest usage first instead of "
                         "the default highest-first)")
    add("coverage",
        "per-org report of how many members already match their org's intended "
        "limit & role (read-only; lists any that don't)")
    add("capacity",
        "sum every member's per-user monthly ACU limit into one enterprise-wide "
        "total (read-only; counts unlimited/unset members separately)")
    sp = add("logins",
             "report how many enterprise members have logged in at least once vs "
             "never, with a per-org breakdown (read-only; uses the audit log)")
    sp.add_argument("--dump-never", dest="dump_never", metavar="PATH",
                    help="also write the email addresses of members who have "
                         "never logged in to PATH, one per line")

    sp = add("lookup", "print the user_id(s) + ACU limit for a member by "
                       "email/user_id (read-only; lists every matching identity)")
    sp.add_argument("--user", required=True,
                    help="an email or user_id to resolve; prints every matching "
                         "user_id with its ACU limit (one 'user_id<TAB>limit' per "
                         "line), e.g. the okta|Org|... SSO identity plus any "
                         "pending email|... invite")

    sp = add("apply", "execute a saved plan (the approval gate); with no plan, "
                      "apply all outstanding plans after a y/N confirm")
    sp.add_argument("plan", nargs="?",
                    help="path to a plan json under state/plans/; omit to apply "
                         "every outstanding plan (asks y/N first)")
    sp.add_argument("--approved", action="store_true",
                    help="also apply held increases / new grants")

    args = p.parse_args(argv)
    cfg = load_config(getattr(args, "config", None))
    if not cfg.token:
        p.error("DEVIN_SERVICE_USER_TOKEN not set (see .env / .env.example)")
    client = _client(cfg, getattr(args, "dry_run", False))

    cmd = args.cmd
    if cmd == "onboard":
        workflows.onboard(cfg, client, file=getattr(args, "file", None))
    elif cmd == "move":
        workflows.move_members(cfg, client)
    elif cmd == "reassign":
        workflows.reassign(cfg, client, file=getattr(args, "file", None))
    elif cmd == "update-limits":
        workflows.update_limits(cfg, client, org=getattr(args, "org", None),
                                user_id=getattr(args, "user", None))
    elif cmd == "offboard":
        workflows.offboard(cfg, client, user_id=getattr(args, "user", None),
                           org_dissolved=getattr(args, "org_dissolved", None),
                           file=getattr(args, "file", None))
    elif cmd == "reconcile":
        workflows.reconcile(cfg, client)
    elif cmd == "usage":
        workflows.usage(cfg, client, reverse=getattr(args, "reverse", False))
    elif cmd == "coverage":
        workflows.coverage(cfg, client)
    elif cmd == "capacity":
        workflows.capacity(cfg, client)
    elif cmd == "logins":
        workflows.logins(cfg, client, dump_never=getattr(args, "dump_never", None))
    elif cmd == "lookup":
        workflows.lookup(cfg, client, user_id=getattr(args, "user", None))
    elif cmd == "apply":
        from .apply import apply_outstanding, apply_plan
        from .plan import load_plan
        approved = getattr(args, "approved", False)
        if args.plan:
            apply_plan(cfg, client, load_plan(args.plan),
                       approved=approved, plan_path=args.plan)
        else:
            apply_outstanding(cfg, client, approved=approved)
    return 0
