"""Command-line interface: ``python govern.py <command> [--dry-run]``."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import reports, workflows
from .client import DevinClient
from .config import load_config
from .errors import GovernError

# Shown as the top-level description AND appended to every subcommand's
# ``--help`` so a reader always sees what the tool itself is.
_ENGINE = (
    "govern is the Devin enterprise governance engine: it manages members' "
    "organization membership, enterprise/org roles, and per-user ACU limits. "
    "It is diff-first: every command computes the changes and writes a plan "
    "that you then apply through an approval gate (govern.py apply)."
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
    sp.set_defaults(func=_run_onboard)

    sp = add("reassign", "bulk-move members from a CSV/.xlsx roster to a new org "
                         "(add to destination + set its role/limit, remove from old org)")
    sp.add_argument("--file", required=True,
                    help="path to a CSV or .xlsx roster: an email column and an "
                         "optional destination group/org-name column (with a header row)")
    sp.set_defaults(func=_run_reassign)

    sp = add("offboard", "remove user(s) from all orgs + zero limit + leaver role")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="email or user_id to offboard")
    g.add_argument("--org-dissolved", dest="org_dissolved",
                   help="offboard ALL members of this org")
    g.add_argument("--file", help="path to a CSV/.xlsx roster of emails to "
                                  "offboard in bulk (an email column with a header "
                                  "row; any group/org column is ignored)")
    sp.set_defaults(func=_run_offboard)

    sp = add("reconcile", "report drift of actual vs desired (+ save a plan); "
                          "optionally scoped to a --user/--org and/or --limits-only")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--user", help="restrict to a single member (an email or "
                                  "user_id); the --limits-only form is the "
                                  "usage-driven single-user upgrade")
    g.add_argument("--org", help="restrict to one org's members (by org name)")
    sp.add_argument("--limits-only", action="store_true", dest="limits_only",
                    help="reconcile only ACU limits, leaving enterprise roles alone")
    sp.set_defaults(func=_run_reconcile)

    sp = add("usage", "flag users near/at their cap (detection only)")
    sp.add_argument("--reverse", action="store_true",
                    help="reverse the sort order (lowest usage first instead of "
                         "the default highest-first)")
    sp.add_argument("--user", help="restrict the report to a single member "
                                   "(an email or user_id); prints just that "
                                   "user's row and never overwrites the shared "
                                   "state/usage-candidates.json")
    sp.add_argument("--export", metavar="PATH",
                    help="also write the full usage table to PATH; the file "
                         "format is chosen from the extension — .csv/.tsv for "
                         "delimited text or .xlsx for Excel (.xlsx needs "
                         "openpyxl). Works with --user too")
    sp.set_defaults(func=_run_usage)

    add("coverage",
        "per-org report of how many members already match their org's intended "
        "limit & role (read-only; lists any that don't)").set_defaults(func=_run_coverage)
    add("capacity",
        "sum every member's per-user monthly ACU limit into one enterprise-wide "
        "total (read-only; counts unlimited/unset members separately)"
        ).set_defaults(func=_run_capacity)

    sp = add("logins",
             "report how many enterprise members have logged in at least once vs "
             "never, with a per-org breakdown (read-only; uses the audit log)")
    sp.add_argument("--dump-never", dest="dump_never", metavar="PATH",
                    help="also write the email addresses of members who have "
                         "never logged in to PATH, one per line")
    sp.set_defaults(func=_run_logins)

    sp = add("lookup", "print the user_id(s) + ACU limit for a member by "
                       "email/user_id (read-only; lists every matching identity)")
    sp.add_argument("--user", required=True,
                    help="an email or user_id to resolve; prints every matching "
                         "user_id with its ACU limit (one 'user_id<TAB>limit' per "
                         "line), e.g. the okta|Org|... SSO identity plus any "
                         "pending email|... invite")
    sp.set_defaults(func=_run_lookup)

    sp = add("apply", "execute a saved plan (the approval gate); with no plan, "
                      "apply all outstanding plans after a y/N confirm")
    sp.add_argument("plan", nargs="?",
                    help="path to a plan json under state/plans/; omit to apply "
                         "every outstanding plan (asks y/N first)")
    sp.add_argument("--approved", action="store_true",
                    help="also apply held increases / new grants")
    sp.set_defaults(func=_run_apply)

    args = p.parse_args(argv)
    cfg = load_config(getattr(args, "config", None))
    if not cfg.token:
        p.error("DEVIN_SERVICE_USER_TOKEN not set (see .env / .env.example)")
    client = DevinClient.from_config(cfg, dry_run=getattr(args, "dry_run", False))

    # Each subparser sets ``func`` to its handler (set_defaults), so dispatch is
    # a single call — no command if/elif chain. Expected, user-facing problems
    # surface as GovernError; render them as a clean message + non-zero exit.
    try:
        args.func(args, cfg, client)
    except GovernError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


# Per-command handlers. Each reads only its own subparser's args (guaranteed
# present), so there are no defensive getattr lookups for command-specific flags.
def _run_onboard(args, cfg, client):
    workflows.onboard(cfg, client, file=args.file)


def _run_reassign(args, cfg, client):
    workflows.reassign(cfg, client, file=args.file)


def _run_offboard(args, cfg, client):
    workflows.offboard(cfg, client, user_id=args.user,
                       org_dissolved=args.org_dissolved, file=args.file)


def _run_reconcile(args, cfg, client):
    workflows.reconcile(cfg, client, user_id=args.user, org=args.org,
                        limits_only=args.limits_only)


def _run_usage(args, cfg, client):
    reports.usage(cfg, client, reverse=args.reverse, user_id=args.user,
                  export=args.export)


def _run_coverage(args, cfg, client):
    reports.coverage(cfg, client)


def _run_capacity(args, cfg, client):
    reports.capacity(cfg, client)


def _run_logins(args, cfg, client):
    reports.logins(cfg, client, dump_never=args.dump_never)


def _run_lookup(args, cfg, client):
    reports.lookup(cfg, client, user_id=args.user)


def _run_apply(args, cfg, client):
    from .apply import apply_outstanding, apply_plan
    from .plan import load_plan
    if args.plan:
        apply_plan(cfg, client, load_plan(args.plan),
                   approved=args.approved, plan_path=args.plan)
    else:
        apply_outstanding(cfg, client, approved=args.approved)
