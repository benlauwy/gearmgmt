#!/usr/bin/env python3
"""Entrypoint for the Devin enterprise governance engine.

Usage:
    DEVIN_SERVICE_USER_TOKEN=<cog_...> python govern.py <command> [--dry-run]

Commands: onboard, sync-moves, reassign, offboard, reconcile, usage, coverage, capacity, logins, lookup, apply

See README.md for usage.
"""
import sys

from govern.cli import main

if __name__ == "__main__":
    sys.exit(main())
