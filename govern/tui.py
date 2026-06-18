"""Tiny terminal UI helpers (standard-library ``curses`` only).

Used by ``onboard`` when the roster has a single (email) column: the operator is
shown the list of governed organizations and picks one with the arrow keys.
"""
from __future__ import annotations

import sys
from typing import Optional


class MenuUnavailable(RuntimeError):
    """Raised when an interactive menu can't be shown (e.g. not a TTY)."""


def select_from_list(title: str, options: list[str]) -> Optional[str]:
    """Show an arrow-key menu and return the chosen option (or None if cancelled).

    Up/Down (or k/j) move, Enter selects, q/Esc cancels. Requires an interactive
    terminal; raises :class:`MenuUnavailable` otherwise so the caller can print a
    helpful message instead of crashing.
    """
    if not options:
        raise MenuUnavailable("no options to choose from")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise MenuUnavailable("an interactive terminal is required to pick a group")

    import curses

    def _run(stdscr) -> Optional[str]:
        curses.curs_set(0)
        idx = 0
        top = 0
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            header = title[: max(0, width - 1)]
            stdscr.addstr(0, 0, header, curses.A_BOLD)
            hint = "(↑/↓ to move · Enter to select · q to cancel)"
            stdscr.addstr(1, 0, hint[: max(0, width - 1)])

            list_top = 3
            visible = max(1, height - list_top - 1)
            if idx < top:
                top = idx
            elif idx >= top + visible:
                top = idx - visible + 1

            for row, opt in enumerate(options[top:top + visible]):
                i = top + row
                marker = "> " if i == idx else "  "
                line = f"{marker}{opt}"[: max(0, width - 1)]
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addstr(list_top + row, 0, line, attr)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(options)
            elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
                return options[idx]
            elif key in (ord("q"), 27):  # q or Esc
                return None

    return curses.wrapper(_run)
