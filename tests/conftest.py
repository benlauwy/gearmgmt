"""Shared test fixtures: a tmp-backed Config and a recording FakeClient.

The engine talks to the API through a small duck-typed ``client`` and writes its
state (plans, audit log, snapshots) under ``cfg.path(...)``. These fixtures give
tests a Config rooted in ``tmp_path`` (so nothing touches the real repo) and a
FakeClient that records every mutation and mirrors DevinClient's dry-run
sentinel, so apply/workflow logic can be exercised without a network.
"""
from __future__ import annotations

import json

import pytest

from govern.config import Config


def make_cfg(tmp_path, **overrides) -> Config:
    """A Config with state_dir/audit_log under ``tmp_path`` (absolute, so
    Config.path() uses them verbatim instead of anchoring at the repo root)."""
    paths = {
        "state_dir": str(tmp_path / "state"),
        "audit_log": str(tmp_path / "audit.jsonl"),
        "limits_policy": str(tmp_path / "limits.toml"),
        "roles_policy": str(tmp_path / "roles.toml"),
        "overrides": str(tmp_path / "overrides.toml"),
    }
    kwargs = dict(
        paths=paths,
        governance={"admin_role_name_contains": ["Admin"]},
        leaver={"enterprise_role_id": "role-leaver", "limit": 0},
        utilization={},
        api={},
        invite={},
        token="cog_test",
        base_url="https://example.test/api",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_cfg(tmp_path)


class FakeClient:
    """A recording stand-in for DevinClient.

    Every mutation appends ``(method, *args)`` to ``self.calls``. In dry-run it
    returns DevinClient's sentinel dict (and invite returns it too, so the
    applier resolves no user_id) without recording — matching the real client's
    ``_mutate``. ``fail_on`` maps a method name to an Exception to raise, for
    exercising the resumable failure path.
    """

    def __init__(self, *, dry_run=False, apply_concurrency=1, read_concurrency=1,
                 sleep=0, invite_uid="user-new", fail_on=None, members=None,
                 limits=None, utilizations=None):
        self.dry_run = dry_run
        self.apply_concurrency = apply_concurrency
        self.read_concurrency = read_concurrency
        self.sleep = sleep
        self.invite_uid = invite_uid
        self.fail_on = dict(fail_on or {})
        self._members = members or []
        self._limits = limits or {}
        self._utilizations = utilizations or {}
        self.calls: list[tuple] = []

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise self.fail_on[name]

    def _sentinel(self, method, path=None, body=None):
        return {"dry_run": True, "method": method, "path": path, "body": body}

    # ---- writes ----
    def invite_users(self, emails, enterprise_role_id):
        if self.dry_run:
            return self._sentinel("POST")
        self._maybe_fail("invite_users")
        self.calls.append(("invite_users", tuple(emails), enterprise_role_id))
        return [{"email": e, "user_id": self.invite_uid} for e in emails]

    def set_user_limit(self, user_id, acu_limit):
        if self.dry_run:
            return self._sentinel("PATCH")
        self._maybe_fail("set_user_limit")
        self.calls.append(("set_user_limit", user_id, acu_limit))

    def set_enterprise_role(self, user_id, role_id):
        if self.dry_run:
            return self._sentinel("PATCH")
        self._maybe_fail("set_enterprise_role")
        self.calls.append(("set_enterprise_role", user_id, role_id))

    def add_user_to_org(self, org_id, user_id, role_id):
        if self.dry_run:
            return self._sentinel("POST")
        self._maybe_fail("add_user_to_org")
        self.calls.append(("add_user_to_org", org_id, user_id, role_id))

    def remove_user_from_org(self, org_id, user_id):
        if self.dry_run:
            return self._sentinel("DELETE")
        self._maybe_fail("remove_user_from_org")
        self.calls.append(("remove_user_from_org", org_id, user_id))

    # ---- reads (for workflow-level tests that need them) ----
    def list_enterprise_members(self):
        return list(self._members)

    def get_user_limit(self, user_id):
        return dict(self._limits.get(user_id, {}))

    def get_user_utilization(self, user_id, time_after=None, time_before=None):
        return dict(self._utilizations.get(user_id, {}))


def read_audit(cfg) -> list[dict]:
    """Parse the audit.jsonl written under ``cfg`` (empty list if none)."""
    import os
    path = cfg.path("audit_log")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
