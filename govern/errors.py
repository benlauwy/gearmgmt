"""Engine-level error types.

The engine raises :class:`GovernError` (rather than ``SystemExit``) for expected,
user-facing problems — an unknown user/org, an unconfigured role, a malformed
roster, a cancelled prompt. Keeping these as ordinary exceptions means the
``govern`` package never terminates the process itself: the CLI entrypoints
(``cli.main`` / ``report.main``) catch ``GovernError`` and convert it into a
clean, non-zero exit, while embedding code can catch and handle it however it
likes.
"""
from __future__ import annotations


class GovernError(Exception):
    """An expected, user-facing error. CLI entrypoints render it and exit non-zero."""
