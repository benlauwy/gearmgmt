"""Command-level tests for the read-only reports (reports.py).

Exercise usage/capacity/coverage/logins/lookup end-to-end against FakeClient +
tmp policy, anchoring their output and return values (and guarding the
ActualState-based field access they all share).
"""
from __future__ import annotations

import json
import os
import time

from conftest import FakeClient, ent_role, member, org_role, write_policy

from govern import reports

NOW = int(time.time())


# --- usage ------------------------------------------------------------------
def _usage_client():
    # u1 is at 90% of its cap (flagged); u2 sits at 5% (ok).
    return FakeClient(
        members=[
            member("u1", "near@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u2", "fine@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
        ],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}},
                "u2": {"local_agent": {"cycle_acu_limit": 200}}},
        utilizations={
            "u1": {"consumption_by_date": [{"date": NOW - 1, "acus": 90}]},
            "u2": {"consumption_by_date": [{"date": NOW - 1, "acus": 10}]},
        },
    )


def test_usage_flags_near_cap_and_writes_candidates(cfg, capsys):
    candidates = reports.usage(cfg, _usage_client())
    out = capsys.readouterr().out

    assert {c["user_id"] for c in candidates} == {"u1"}        # only u1 is near cap
    assert "NEAR/AT CAP" in out and "near@x.com" in out
    # The full-population run persists the shared worklist.
    worklist = os.path.join(cfg.path("state_dir"), "usage-candidates.json")
    with open(worklist) as f:
        assert json.load(f)[0]["user_id"] == "u1"


def test_usage_single_user_spot_check_does_not_touch_worklist(cfg, capsys):
    candidates = reports.usage(cfg, _usage_client(), user_id="near@x.com")
    out = capsys.readouterr().out

    assert [c["user_id"] for c in candidates] == ["u1"]
    assert "near@x.com" in out and "fine@x.com" not in out
    # A spot-check never writes the shared candidates file.
    assert not os.path.exists(os.path.join(cfg.path("state_dir"), "usage-candidates.json"))


# --- capacity ---------------------------------------------------------------
def test_capacity_sums_numeric_and_counts_uncapped(cfg, capsys):
    write_policy(cfg)  # capacity now reads overrides.toml (here: none)
    client = FakeClient(
        members=[member(f"u{i}", f"{i}@x.com", [ent_role("role-ide")]) for i in range(3)],
        limits={"u0": {"local_agent": {"cycle_acu_limit": 100}},   # numeric
                "u1": {"local_agent": {"cycle_acu_limit": None}},  # unlimited
                "u2": {}},                                          # unset
    )
    result = reports.capacity(cfg, client)
    out = capsys.readouterr().out

    assert result == {"total": 100, "numeric": 1, "unlimited": 1,
                      "unset": 1, "population": 3, "from_overrides": 0}
    assert "TOTAL monthly ACU limit: 100" in out


def test_capacity_folds_finite_overrides(cfg, capsys):
    # A finite overrides.toml limit is folded into the total (even over an unset
    # or differing live value); an unlimited ("null") override counts as unlimited.
    write_policy(cfg, overrides={
        "u0": {"reason": "pinned higher", "limit": 500},    # live 100 -> count 500
        "u2": {"reason": "pin a cap", "limit": 300},        # live unset -> count 300
        "u3": {"reason": "pin unlimited", "limit": "null"}, # live 100 -> unlimited
    })
    client = FakeClient(
        members=[member(f"u{i}", f"{i}@x.com", [ent_role("role-ide")]) for i in range(4)],
        limits={"u0": {"local_agent": {"cycle_acu_limit": 100}},
                "u1": {"local_agent": {"cycle_acu_limit": 200}},   # no override
                "u2": {},                                           # unset, pinned 300
                "u3": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    result = reports.capacity(cfg, client)
    out = capsys.readouterr().out

    # 500 (u0 override) + 200 (u1 live) + 300 (u2 override) = 1000; u3 -> unlimited
    assert result == {"total": 1000, "numeric": 3, "unlimited": 1,
                      "unset": 0, "population": 4, "from_overrides": 2}
    assert "TOTAL monthly ACU limit: 1,000" in out
    assert "2 pinned by overrides.toml" in out


# --- coverage ---------------------------------------------------------------
def test_coverage_reports_per_org_and_mismatches(cfg, capsys):
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[
            member("u1", "ok@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u2", "drift@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
        ],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}},
                "u2": {"local_agent": {"cycle_acu_limit": 50}}},   # wrong limit
    )
    reports.coverage(cfg, client)
    out = capsys.readouterr().out

    assert "=== coverage (read-only) ===" in out
    assert "Org: IDE Standard" in out
    assert "drift@x.com" in out and "want 100" in out


def test_coverage_folds_admins_into_admin_org_limit(cfg, capsys):
    # Admins' limit is governed by the Admin Org, so they count toward ITS limit
    # coverage (and show as mismatches there) but never toward role coverage. A
    # pinned (overrides.toml) admin is an exception, excluded from coverage.
    write_policy(cfg,
                 limits={"Admin Org": 1000, "IDE Standard": 100},
                 roles={"Admin Org": "role-admin", "IDE Standard": "role-ide"},
                 overrides={"a3": {"reason": "pinned", "limit": "null"}})
    client = FakeClient(
        orgs={"oa": "Admin Org", "o1": "IDE Standard"},
        members=[
            member("a1", "admin-ok@x.com",
                   [ent_role("role-x", "Admin"), org_role("oa", "roa")]),
            member("a2", "admin-bad@x.com",
                   [ent_role("role-x", "Admin"), org_role("oa", "roa")]),
            member("a3", "admin-pinned@x.com",
                   [ent_role("role-x", "Admin"), org_role("oa", "roa")]),
            member("u1", "ide@x.com",
                   [ent_role("role-ide"), org_role("o1", "ro1")]),
        ],
        limits={"a1": {"local_agent": {"cycle_acu_limit": 1000}},  # matches Admin Org
                "a2": {"local_agent": {"cycle_acu_limit": 500}},   # wrong
                "a3": {"local_agent": {"cycle_acu_limit": 7}},     # wrong, but pinned
                "u1": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    reports.coverage(cfg, client)
    out = capsys.readouterr().out

    # Admin Org folds in the 2 un-pinned admins (1 ok) and flags the bad one.
    assert "Org: Admin Org" in out
    assert "limit coverage: 1/2 member(s) at intended incl. admins" in out
    assert "admin-bad@x.com" in out and "want 1000" in out
    # The pinned admin is excluded entirely (not a mismatch despite limit 7).
    assert "admin-pinned@x.com" not in out
    assert "pinned/override: 1" in out
    # Admins are never counted toward role coverage (role exempt).
    assert "role  coverage: 0/0 non-admin member(s) at intended" in out


# --- logins -----------------------------------------------------------------
def test_logins_counts_logged_in_vs_never(cfg, capsys):
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[
            member("u1", "in@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u2", "never@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
        ],
        audit_logs=[{"action": "login", "user_id": "u1"}],
    )
    result = reports.logins(cfg, client)
    out = capsys.readouterr().out

    assert result == {"total": 2, "logged_in": 1, "never": 1}
    assert "logged in >= once: 1" in out


def test_logins_dump_never_writes_emails(cfg, tmp_path, capsys):
    client = FakeClient(
        members=[member("u1", "in@x.com", [ent_role("role-ide")]),
                 member("u2", "never@x.com", [ent_role("role-ide")])],
        audit_logs=[{"action": "login", "user_id": "u1"}],
    )
    path = str(tmp_path / "never.txt")
    reports.logins(cfg, client, dump_never=path)
    capsys.readouterr()
    with open(path) as f:
        assert f.read().splitlines() == ["never@x.com"]


# --- lookup -----------------------------------------------------------------
def test_lookup_prints_every_identity_with_limit(cfg, capsys):
    client = FakeClient(
        members=[member("u1", "a@x.com", [ent_role("role-ide")]),
                 member("email|hash", "a@x.com", [ent_role("role-ide")])],  # pending invite
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}},
                "email|hash": {}},                                          # unset
    )
    rows = reports.lookup(cfg, client, user_id="a@x.com")
    out = capsys.readouterr().out

    assert dict(rows) == {"u1": "100", "email|hash": "unset"}
    assert "u1\t100" in out and "email|hash\tunset" in out
