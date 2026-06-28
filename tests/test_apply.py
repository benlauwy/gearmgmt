"""The apply gate: atomic-per-user approval, resume, dry-run, invites, archive.

These are the engine's crown jewels — the rules that decide what mutates and when
— so they get the most coverage. Plans are written with save_plan and applied
through a recording FakeClient rooted in tmp_path (see conftest).
"""
from __future__ import annotations

import os

from govern.apply import (apply_outstanding, apply_plan, list_outstanding_plans)
from govern.plan import Change, Plan, save_plan
from conftest import FakeClient, read_audit


def _change(uid, kind, *, field="limit", before=None, after=None, reason="r",
            org_id=None, email=None, status="pending"):
    return Change(uid, kind, field, before, after, reason,
                  org_id=org_id, email=email, status=status)


def _archive_path(cfg, plan_path):
    return os.path.join(cfg.path("state_dir"), "plans", "archive",
                        os.path.basename(plan_path))


# --- auto-apply (revokes/downgrades/decreases need no approval) -------------
def test_auto_change_applies_without_approval(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50)])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=False, plan_path=path)

    assert client.calls == [("set_user_limit", "u1", 50)]
    assert plan.changes[0].status == "applied"
    audit = read_audit(cfg)
    assert len(audit) == 1 and audit[0]["action"] == "limit_decrease"
    # Fully applied -> archived out of the outstanding dir.
    assert not os.path.exists(path)
    assert os.path.exists(_archive_path(cfg, path))


# --- approval gate ----------------------------------------------------------
def test_increase_is_held_without_approval(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_increase", before=50, after=100)])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=False, plan_path=path)

    assert client.calls == []
    assert plan.changes[0].status == "pending"   # held, not applied
    assert read_audit(cfg) == []
    assert os.path.exists(path)                   # NOT archived (still outstanding)


def test_increase_applies_with_approval(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_increase", before=50, after=100)])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=True, plan_path=path)

    assert client.calls == [("set_user_limit", "u1", 100)]
    assert plan.changes[0].status == "applied"
    assert os.path.exists(_archive_path(cfg, path))


# --- atomicity per user -----------------------------------------------------
def test_user_held_atomically_when_any_change_needs_approval(cfg):
    # u1 has one auto change AND one approval change: without --approved NEITHER
    # may land (so a move never lands half-done).
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50),
        _change("u1", "role_grant", field="enterprise_role", before=None, after="r1"),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=False, plan_path=path)

    assert client.calls == []                       # nothing for u1 applied
    assert {c.status for c in plan.changes} == {"pending"}
    assert os.path.exists(path)


def test_atomic_user_applies_all_with_approval(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50),
        _change("u1", "role_grant", field="enterprise_role", before=None, after="r1"),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=True, plan_path=path)

    assert ("set_user_limit", "u1", 50) in client.calls
    assert ("set_enterprise_role", "u1", "r1") in client.calls
    assert {c.status for c in plan.changes} == {"applied"}


def test_independent_users_one_auto_one_held(cfg):
    # The hold is per-user: u1's auto change lands even though u2 is held.
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50),
        _change("u2", "limit_increase", before=50, after=100),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=False, plan_path=path)

    assert client.calls == [("set_user_limit", "u1", 50)]
    statuses = {c.user_id: c.status for c in plan.changes}
    assert statuses == {"u1": "applied", "u2": "pending"}
    assert os.path.exists(path)                       # not fully applied -> stays


def test_resume_applies_remaining_then_archives(cfg):
    # Continuation of the scenario above: re-run with --approved to finish u2.
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50, status="applied"),
        _change("u2", "limit_increase", before=50, after=100),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=True, plan_path=path)

    # u1 already applied -> not re-called; only u2 mutates now.
    assert client.calls == [("set_user_limit", "u2", 100)]
    assert {c.status for c in plan.changes} == {"applied"}
    assert os.path.exists(_archive_path(cfg, path))


# --- dry-run ----------------------------------------------------------------
def test_dry_run_mutates_nothing_and_does_not_archive(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_increase", before=50, after=100)])
    path = save_plan(cfg, plan)
    client = FakeClient(dry_run=True)

    apply_plan(cfg, client, plan, approved=True, plan_path=path)

    assert client.calls == []
    assert plan.changes[0].status == "pending"   # dry-run never marks applied
    assert read_audit(cfg) == []
    assert os.path.exists(path)                   # never archived on dry-run


# --- failure is recorded and resumable --------------------------------------
def test_failure_is_recorded_and_resumable(cfg):
    plan = Plan(workflow="reconcile", changes=[
        _change("u1", "limit_decrease", before=100, after=50)])
    path = save_plan(cfg, plan)
    boom = FakeClient(fail_on={"set_user_limit": RuntimeError("boom")})

    apply_plan(cfg, boom, plan, approved=False, plan_path=path)
    assert plan.changes[0].status == "failed"
    assert "boom" in (plan.changes[0].error or "")
    assert read_audit(cfg) == []
    assert os.path.exists(path)                   # failed -> stays for resume

    # Resume with a healthy client: the same change now lands and archives.
    ok = FakeClient()
    apply_plan(cfg, ok, plan, approved=False, plan_path=path)
    assert ok.calls == [("set_user_limit", "u1", 50)]
    assert plan.changes[0].status == "applied"
    assert os.path.exists(_archive_path(cfg, path))


# --- invite threading -------------------------------------------------------
def test_invite_threads_new_user_id_into_followups(cfg, monkeypatch):
    # The SSO-toggle prompt fires on a real, approved, invite-bearing run; stub
    # it so the test never blocks on stdin.
    monkeypatch.setattr("govern.apply.confirm_yes", lambda *a, **k: None)
    plan = Plan(workflow="onboard", changes=[
        _change("", "user_invite", field="enterprise_role", before=None,
                after="role-ent", email="new@x.com"),
        _change("", "org_add", field="org_membership", before=None,
                after="role-org", org_id="org1", email="new@x.com"),
        _change("", "limit_decrease", before=None, after=100, email="new@x.com"),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient(invite_uid="user-123")

    apply_plan(cfg, client, plan, approved=True, plan_path=path)

    # The id the invite returned must flow into the org-add and limit calls.
    assert client.calls == [
        ("invite_users", ("new@x.com",), "role-ent"),
        ("add_user_to_org", "org1", "user-123", "role-org"),
        ("set_user_limit", "user-123", 100),
    ]
    assert {c.user_id for c in plan.changes} == {"user-123"}
    assert {c.status for c in plan.changes} == {"applied"}


def test_invite_bearing_plan_held_without_approval(cfg):
    # user_invite always needs approval, so the whole invitee group is held.
    plan = Plan(workflow="onboard", changes=[
        _change("", "user_invite", field="enterprise_role", before=None,
                after="role-ent", email="new@x.com"),
        _change("", "org_add", field="org_membership", before=None,
                after="role-org", org_id="org1", email="new@x.com"),
    ])
    path = save_plan(cfg, plan)
    client = FakeClient()

    apply_plan(cfg, client, plan, approved=False, plan_path=path)
    assert client.calls == []
    assert {c.status for c in plan.changes} == {"pending"}


# --- apply_outstanding ------------------------------------------------------
def test_list_outstanding_excludes_archive(cfg):
    p1 = save_plan(cfg, Plan(workflow="reconcile", created_at=1000, changes=[
        _change("u1", "limit_decrease", before=100, after=50)]))
    p2 = save_plan(cfg, Plan(workflow="offboard", created_at=2000, changes=[
        _change("u2", "limit_decrease", before=100, after=0)]))
    outstanding = list_outstanding_plans(cfg)
    assert set(outstanding) == {p1, p2}


def test_apply_outstanding_applies_all_when_confirmed(cfg, monkeypatch):
    monkeypatch.setattr("govern.apply.confirm", lambda *a, **k: True)
    p1 = save_plan(cfg, Plan(workflow="reconcile", created_at=1000, changes=[
        _change("u1", "limit_decrease", before=100, after=50)]))
    p2 = save_plan(cfg, Plan(workflow="offboard", created_at=2000, changes=[
        _change("u2", "limit_decrease", before=100, after=0)]))
    client = FakeClient()

    apply_outstanding(cfg, client, approved=False)

    assert ("set_user_limit", "u1", 50) in client.calls
    assert ("set_user_limit", "u2", 0) in client.calls
    assert list_outstanding_plans(cfg) == []          # both archived
    assert os.path.exists(_archive_path(cfg, p1))
    assert os.path.exists(_archive_path(cfg, p2))


def test_apply_outstanding_aborts_when_declined(cfg, monkeypatch):
    monkeypatch.setattr("govern.apply.confirm", lambda *a, **k: False)
    p1 = save_plan(cfg, Plan(workflow="reconcile", created_at=1000, changes=[
        _change("u1", "limit_decrease", before=100, after=50)]))
    client = FakeClient()

    apply_outstanding(cfg, client, approved=False)

    assert client.calls == []
    assert list_outstanding_plans(cfg) == [p1]        # untouched


# --- parallel apply (apply_concurrency > 1) ---------------------------------
def test_parallel_apply_applies_all_independent_users(cfg):
    # Exercise the ThreadPoolExecutor fan-out: many independent users, all auto.
    # (Calls arrive in nondeterministic order under threads, so compare as a set.)
    changes = [_change(f"u{i}", "limit_decrease", before=100, after=i) for i in range(5)]
    plan = Plan(workflow="reconcile", changes=changes)
    path = save_plan(cfg, plan)
    client = FakeClient(apply_concurrency=8)

    apply_plan(cfg, client, plan, approved=False, plan_path=path)

    assert set(client.calls) == {("set_user_limit", f"u{i}", i) for i in range(5)}
    assert {c.status for c in plan.changes} == {"applied"}
    assert len(read_audit(cfg)) == 5
    assert os.path.exists(_archive_path(cfg, path))
