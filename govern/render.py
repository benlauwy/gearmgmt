"""Console-rendering helpers for plans and reports.

Pure presentation: every function here only formats strings (the change-line
columns, limit labels, percentages, the saved-plan footer). Keeping them out of
the command modules lets the orchestration compute a plan/result and the output
stay consistent and independently testable.
"""
from __future__ import annotations

from collections import Counter

from .plan import Change


def fmt_limit(value, is_set: bool = True) -> str:
    """Render an ACU limit: a number, ``unlimited`` (set, no cap), or ``unset``."""
    if value is None:
        return "unlimited" if is_set else "unset"
    return str(value)


def pct(n: int, d: int) -> str:
    """Format n/d as a whole-percent string (0% when there's nothing to divide)."""
    return f"{(n / d):.0%}" if d else "0%"


def emailer(actual: dict):
    """Return a uid -> display-email lookup (falling back to the uid itself).

    ``actual`` maps user_id -> ActualState (or None for an unknown uid)."""
    def email(uid):
        a = actual.get(uid)
        return (a.email if a else None) or uid
    return email


def tag(c: Change) -> str:
    """The approval-gate label shown at the start of a change line."""
    return "APPROVAL" if c.needs_approval else "auto"


def where(org_index: dict, c: Change) -> str:
    """A ``  [Org Name]`` suffix for org-scoped changes (empty when no org)."""
    return f"  [{org_index.get(c.org_id, c.org_id)}]" if c.org_id else ""


def render_change(c: Change, *, label: str, show_field: bool = True,
                  where: str = "", suffix: str = "") -> str:
    """One formatted change line for a plan listing.

    Unifies the columns shared by the action commands: the approval tag, the
    change kind, an optional field column, a left-justified ``label`` (the
    subject — user_id/email), the ``before -> after``, and optional ``where``
    (org) / ``suffix`` (e.g. the drift reason)."""
    field = f"{c.field:16} " if show_field else ""
    return (f"  [{tag(c):8}] {c.kind:14} {field}{label:34} "
            f"{c.before} -> {c.after}{where}{suffix}")


def plan_footer(path: str, apply_hint: str) -> None:
    """Print the standard action-command footer: a blank line, the saved-plan
    path, and the command-specific ``Apply with``/``Apply drift with`` hint."""
    print(f"\nPlan saved: {path}")
    print(apply_hint)


def change_counts(changes: list[Change]) -> tuple[Counter, Counter]:
    """Tally a change list by ``kind`` and by ``field`` for summary lines.

    Returns ``(kinds, fields)`` Counters so callers read e.g. ``kinds["org_add"]``
    or ``fields["limit"]`` without re-scanning the list once per category.
    """
    return (Counter(c.kind for c in changes),
            Counter(c.field for c in changes))
