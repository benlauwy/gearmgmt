"""Devin enterprise governance engine.

Materializes per-user limits and enterprise roles from policy files
(limits.toml, roles.toml) with a manual-override exception (overrides.toml),
and provides reconciliation/reporting (reconcile, usage, coverage).

The engine is diff-first: every action command builds a plan that is then
executed through the apply gate (auto-applies revokes/downgrades; holds
increases/grants for approval), with an append-only audit log.
"""

__version__ = "0.0.1"
