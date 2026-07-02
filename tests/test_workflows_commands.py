"""Command-level characterization tests.

These exercise the full workflow commands end-to-end (against FakeClient + tmp
policy files), capturing stdout and the returned Plan. They anchor the console
output and change-set of the commands whose shared scaffolding is about to be
de-duplicated, so a refactor that changes behaviour is caught.
"""
from __future__ import annotations

import pytest
from conftest import FakeClient, ent_role, member, org_role, write_policy

from govern import workflows
from govern.errors import GovernError


# --- reconcile --------------------------------------------------------------
def test_reconcile_reports_drift_violation_and_tags(cfg, capsys):
    write_policy(cfg, limits={"IDE Standard": 100, "CLI": 50},
                 roles={"IDE Standard": "role-ide", "CLI": "role-cli"})
    client = FakeClient(
        orgs={"o1": "IDE Standard", "o2": "CLI"},
        members=[
            member("u1", "match@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u2", "drift@x.com", [ent_role("role-old"), org_role("o1", "ro1")]),
            member("u4", "high@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u3", "multi@x.com",
                   [ent_role("role-ide"), org_role("o1", "ro1"), org_role("o2", "ro2")]),
        ],
        limits={
            "u1": {"local_agent": {"cycle_acu_limit": 100}},
            "u2": {"local_agent": {"cycle_acu_limit": 50}},
            "u4": {"local_agent": {"cycle_acu_limit": 200}},
            "u3": {"local_agent": {"cycle_acu_limit": 100}},
        },
    )
    plan = workflows.reconcile(cfg, client)
    out = capsys.readouterr().out

    kinds = {(c.user_id, c.kind) for c in plan.changes}
    assert ("u2", "role_change") in kinds       # real -> real, needs approval
    assert ("u2", "limit_increase") in kinds
    assert ("u4", "limit_decrease") in kinds     # auto
    assert not any(c.user_id == "u1" for c in plan.changes)   # already matches
    assert not any(c.user_id == "u3" for c in plan.changes)   # violation, ungoverned

    assert "=== reconcile (read-only) ===" in out
    assert "APPROVAL" in out and "auto" in out
    assert "drift@x.com" in out and "high@x.com" in out
    assert "Violations" in out and "multi@x.com" in out


def test_reconcile_governs_admin_limits_via_admin_org(cfg, capsys):
    # Admins are limit-governed from the Admin Org limit (role left alone). An
    # admin outside the Admin Org still gets that limit but is flagged.
    write_policy(cfg, limits={"Admin Org": 1000, "IDE Standard": 100},
                 roles={"Admin Org": "role-admin", "IDE Standard": "role-ide"})
    client = FakeClient(
        orgs={"oa": "Admin Org", "o1": "IDE Standard"},
        members=[
            member("a1", "admin1@x.com",
                   [ent_role("role-adm", "Admin"), org_role("oa", "roa")]),
            member("a2", "admin2@x.com",   # admin, but NOT in the Admin Org
                   [ent_role("role-adm", "Admin no Devin"), org_role("o1", "ro1")]),
            member("a3", "admin3@x.com",   # already at the Admin Org limit
                   [ent_role("role-adm", "Admin"), org_role("oa", "roa")]),
        ],
        limits={"a3": {"local_agent": {"cycle_acu_limit": 1000}}},
    )
    plan = workflows.reconcile(cfg, client)
    out = capsys.readouterr().out

    by_user = {c.user_id: c for c in plan.changes}
    # a1: admin in the Admin Org, unset -> its 1000 limit, reason "admin".
    # (unset ranks as unlimited, so applying a cap classifies as a decrease.)
    assert by_user["a1"].field == "limit" and by_user["a1"].after == 1000
    assert by_user["a1"].kind == "limit_decrease" and by_user["a1"].reason == "admin"
    # a2: admin NOT in the Admin Org -> still 1000, but flagged source.
    assert by_user["a2"].after == 1000
    assert by_user["a2"].reason == "admin-no-admin-org"
    assert "a3" not in by_user                       # already compliant
    assert all(c.field == "limit" for c in plan.changes)  # roles never touched

    assert "Admins (limit via Admin Org" in out
    assert "WARNING:" in out and "admin2@x.com" in out


def test_reconcile_checks_org_roles_via_invite_role(cfg, capsys):
    # With [invite].org_role_id set, reconcile reconciles each governed non-admin's
    # role INSIDE their org to that global value (gated, like enterprise roles).
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    cfg.invite["org_role_id"] = "role-org"
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[
            member("u1", "match@x.com", [ent_role("role-ide"), org_role("o1", "role-org")]),
            member("u2", "orgdrift@x.com", [ent_role("role-ide"), org_role("o1", "role-old")]),
        ],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}},
                "u2": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    plan = workflows.reconcile(cfg, client)
    out = capsys.readouterr().out

    org_changes = [c for c in plan.changes if c.field == "org_role"]
    assert len(org_changes) == 1
    c = org_changes[0]
    assert c.user_id == "u2" and c.before == "role-old" and c.after == "role-org"
    assert c.kind == "role_change" and c.org_id == "o1" and c.needs_approval
    assert not any(c.user_id == "u1" for c in plan.changes)  # matches on every dim
    assert "[IDE Standard]" in out                            # org tag on the drift line


def test_reconcile_resolves_org_role_by_name(cfg, capsys):
    # org_role_name is resolved against the live org-type roles (like onboard).
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    cfg.invite["org_role_name"] = "Organization User"
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        roles=[{"role_id": "role-org", "role_name": "Organization User", "role_type": "org"}],
        members=[member("u2", "orgdrift@x.com",
                        [ent_role("role-ide"), org_role("o1", "role-old")])],
        limits={"u2": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    plan = workflows.reconcile(cfg, client)
    capsys.readouterr()

    org_changes = [c for c in plan.changes if c.field == "org_role"]
    assert len(org_changes) == 1 and org_changes[0].after == "role-org"


def test_reconcile_limits_only_excludes_org_roles(cfg, capsys):
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    cfg.invite["org_role_id"] = "role-org"
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[member("u2", "orgdrift@x.com",
                        [ent_role("role-ide"), org_role("o1", "role-old")])],
        limits={"u2": {"local_agent": {"cycle_acu_limit": 50}}},   # limit also drifts
    )
    plan = workflows.reconcile(cfg, client, limits_only=True)
    capsys.readouterr()
    assert [c.field for c in plan.changes] == ["limit"]  # org-role drift excluded


def test_reconcile_org_roles_ungoverned_when_invite_unset(cfg, capsys):
    # No [invite] org role configured -> org roles are left alone (safe default).
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[member("u2", "orgdrift@x.com",
                        [ent_role("role-ide"), org_role("o1", "role-old")])],
        limits={"u2": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    plan = workflows.reconcile(cfg, client)
    capsys.readouterr()
    assert not any(c.field == "org_role" for c in plan.changes)


def test_reconcile_admin_org_role_untouched_when_admin_role_unset(cfg, capsys):
    # The member [invite] role must NOT be applied to admins; with no admin org
    # role configured, an admin's elevated org role is left alone.
    write_policy(cfg, limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    cfg.invite["org_role_id"] = "role-org"
    client = FakeClient(
        orgs={"oa": "Admin Org"},
        members=[member("a1", "admin@x.com",
                        [ent_role("role-adm", "Admin"), org_role("oa", "role-org-admin")])],
        limits={"a1": {"local_agent": {"cycle_acu_limit": 1000}}},
    )
    plan = workflows.reconcile(cfg, client)
    capsys.readouterr()
    assert not any(c.field == "org_role" for c in plan.changes)


def test_reconcile_governs_admin_org_role_on_every_org(cfg, capsys):
    # [governance].admin_org_role_id governs an admin's role on EVERY org they're
    # in (gated), independent of the member [invite] role.
    write_policy(cfg, limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    cfg.governance["admin_org_role_id"] = "role-admin-org"
    cfg.invite["org_role_id"] = "role-member"   # must NOT be applied to admins
    client = FakeClient(
        orgs={"oa": "Admin Org", "o1": "IDE Standard"},
        members=[member("a1", "admin@x.com",
                        [ent_role("role-adm", "Admin"),
                         org_role("oa", "role-admin-org"),   # already correct
                         org_role("o1", "role-old")])],       # drifts
        limits={"a1": {"local_agent": {"cycle_acu_limit": 1000}}},
    )
    plan = workflows.reconcile(cfg, client)
    out = capsys.readouterr().out

    org_changes = [c for c in plan.changes if c.field == "org_role"]
    assert len(org_changes) == 1                      # only the drifting org (o1)
    c = org_changes[0]
    assert c.user_id == "a1" and c.org_id == "o1"
    assert c.before == "role-old" and c.after == "role-admin-org"
    assert c.kind == "role_change" and c.needs_approval
    assert "[IDE Standard]" in out


# --- onboard ----------------------------------------------------------------
def test_onboard_two_column_invite_plus_existing(cfg, tmp_path, capsys):
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    cfg.invite["org_role_id"] = "role-org"
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[member("u1", "exists@x.com", [ent_role("role-ide")])],  # exists, no org
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    roster = tmp_path / "roster.csv"
    roster.write_text("email,group\nnew@x.com,IDE Standard\nexists@x.com,IDE Standard\n",
                      encoding="utf-8")

    plan = workflows.onboard(cfg, client, file=str(roster))
    out = capsys.readouterr().out

    kinds = [c.kind for c in plan.changes]
    assert kinds.count("user_invite") == 1
    assert kinds.count("org_add") == 2
    assert any(c.kind == "limit_decrease" and c.email == "new@x.com" for c in plan.changes)
    # org-add role is filled in from [invite].org_role_id
    assert all(c.after == "role-org" for c in plan.changes if c.kind == "org_add")

    assert "=== onboard (file: roster.csv) ===" in out
    assert "[IDE Standard]" in out               # the org `where` suffix on org-add lines


def test_onboard_single_column_without_tty_errors(cfg, tmp_path):
    # The single-column path needs an interactive menu; under pytest stdin isn't
    # a TTY, so it must fail cleanly (exercises the MenuUnavailable branch).
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    client = FakeClient(orgs={"o1": "IDE Standard"}, members=[])
    roster = tmp_path / "roster.csv"
    roster.write_text("email\nnew@x.com\n", encoding="utf-8")
    with pytest.raises(GovernError):
        workflows.onboard(cfg, client, file=str(roster))


# --- reassign ---------------------------------------------------------------
def test_reassign_moves_existing_member(cfg, tmp_path, capsys):
    write_policy(cfg, limits={"IDE Standard": 100, "CLI": 50},
                 roles={"IDE Standard": "role-ide", "CLI": "role-cli"})
    cfg.invite["org_role_id"] = "role-org"
    client = FakeClient(
        orgs={"o1": "IDE Standard", "o2": "CLI"},
        members=[member("u1", "mover@x.com", [ent_role("role-ide"), org_role("o1", "ro1")])],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    roster = tmp_path / "moves.csv"
    roster.write_text("email,group\nmover@x.com,CLI\n", encoding="utf-8")

    plan = workflows.reassign(cfg, client, file=str(roster))
    out = capsys.readouterr().out

    kinds = {c.kind for c in plan.changes}
    assert "org_add" in kinds          # added to CLI
    assert "org_remove" in kinds       # removed from IDE Standard
    assert "limit_decrease" in kinds   # 100 -> 50
    assert "role_change" in kinds      # role-ide -> role-cli
    assert "=== reassign (file: moves.csv) ===" in out


# --- reconcile scoping (the merged-in update-limits behaviour) --------------
def _drifting_client():
    # u1 drifts on BOTH dimensions: limit 50->100 and role role-old->role-ide.
    return FakeClient(
        orgs={"o1": "IDE Standard", "o2": "CLI"},
        members=[
            member("u1", "low@x.com", [ent_role("role-old"), org_role("o1", "ro1")]),
            member("u2", "other@x.com", [ent_role("role-cli"), org_role("o2", "ro2")]),
        ],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 50}},
                "u2": {"local_agent": {"cycle_acu_limit": 999}}},
    )


def test_reconcile_user_scope_limits_only(cfg, capsys):
    # Replaces `update-limits --user`: scoped to one user, limits only.
    write_policy(cfg, limits={"IDE Standard": 100, "CLI": 50},
                 roles={"IDE Standard": "role-ide", "CLI": "role-cli"})
    plan = workflows.reconcile(cfg, _drifting_client(), user_id="low@x.com",
                               limits_only=True)
    out = capsys.readouterr().out

    assert [c.kind for c in plan.changes] == ["limit_increase"]   # role drift excluded
    assert all(c.user_id == "u1" for c in plan.changes)            # only the scoped user
    assert "Scope: user:u1 | limits only" in out
    assert "low@x.com" in out and "other@x.com" not in out


def test_reconcile_user_scope_includes_roles_by_default(cfg, capsys):
    write_policy(cfg, limits={"IDE Standard": 100, "CLI": 50},
                 roles={"IDE Standard": "role-ide", "CLI": "role-cli"})
    plan = workflows.reconcile(cfg, _drifting_client(), user_id="low@x.com")
    capsys.readouterr()

    kinds = {c.kind for c in plan.changes}
    assert kinds == {"limit_increase", "role_change"}             # both dimensions
    assert all(c.user_id == "u1" for c in plan.changes)


def test_reconcile_org_scope(cfg, capsys):
    write_policy(cfg, limits={"IDE Standard": 100, "CLI": 50},
                 roles={"IDE Standard": "role-ide", "CLI": "role-cli"})
    plan = workflows.reconcile(cfg, _drifting_client(), org="IDE Standard")
    out = capsys.readouterr().out

    assert {c.user_id for c in plan.changes} == {"u1"}            # only IDE Standard members
    assert "Scope: org:IDE Standard" in out


def test_reconcile_user_and_org_are_mutually_exclusive(cfg):
    write_policy(cfg, limits={"IDE Standard": 100}, roles={"IDE Standard": "role-ide"})
    with pytest.raises(GovernError, match="at most one"):
        workflows.reconcile(cfg, _drifting_client(), user_id="low@x.com",
                            org="IDE Standard")


# --- offboard ---------------------------------------------------------------
def test_offboard_user_zeroes_and_removes(cfg, capsys):
    client = FakeClient(
        orgs={"o1": "IDE Standard", "o2": "CLI"},
        members=[member("u1", "leaver@x.com",
                        [ent_role("role-ide"), org_role("o1", "ro1"), org_role("o2", "ro2")])],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    plan = workflows.offboard(cfg, client, user_id="leaver@x.com")
    out = capsys.readouterr().out

    assert sorted(c.kind for c in plan.changes) == \
        ["limit_decrease", "org_remove", "org_remove", "role_downgrade"]
    assert "=== offboard (user:u1) ===" in out
    assert "leaver@x.com" in out
    assert "[IDE Standard]" in out and "[CLI]" in out


def test_offboard_from_file_resolves_emails(cfg, tmp_path, capsys):
    client = FakeClient(
        orgs={"o1": "IDE Standard"},
        members=[
            member("u1", "a@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
            member("u2", "b@x.com", [ent_role("role-ide"), org_role("o1", "ro1")]),
        ],
        limits={"u1": {"local_agent": {"cycle_acu_limit": 100}},
                "u2": {"local_agent": {"cycle_acu_limit": 100}}},
    )
    roster = tmp_path / "leavers.csv"
    roster.write_text("email\na@x.com\nb@x.com\n", encoding="utf-8")

    plan = workflows.offboard(cfg, client, file=str(roster))
    out = capsys.readouterr().out

    assert {c.user_id for c in plan.changes} == {"u1", "u2"}
    assert sum(c.kind == "org_remove" for c in plan.changes) == 2
    assert "=== offboard (file:leavers.csv) ===" in out
