"""Shared constants for the governance engine."""
from __future__ import annotations

# Seconds in a UTC day. The consumption API buckets usage per day, so both the
# usage/cap detection (govern.reports) and the daily report (report.py) window
# their queries in whole-day increments.
SECONDS_PER_DAY = 86400
