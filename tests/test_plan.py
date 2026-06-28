"""Change/Plan model: limit classification, diff, approval gate, (de)serialize."""
from __future__ import annotations

import pytest

from govern.plan import (AUTO_APPLY, NEEDS_APPROVAL, Change, Plan, _limit_kind,
                         diff)
from govern.policy import DesiredState


# --- _limit_kind (None == unlimited, ranks highest) -------------------------
@pytest.mark.parametrize("cur, tgt, kind", [
    (50, 100, "limit_increase"),
    (100, 50, "limit_decrease"),
    (50, None, "limit_increase"),    # number -> unlimited is an increase
    (None, 50, "limit_decrease"),    # unlimited -> number is a decrease
])
def test_limit_kind(cur, tgt, kind):
    assert _limit_kind(cur, tgt) == kind


# --- diff: only checked dimensions, set-to-desired --------------------------
def _desired(uid, *, limit=None, role=None, check_limit=False, check_role=False,
             source="policy:IDE"):
    return DesiredState(uid, limit, role, source,
                        check_limit=check_limit, check_role=check_role)


def test_diff_emits_limit_increase_when_checked():
    actual = {"u1": {"limit": 50, "enterprise_role": {"role_id": "r1"}}}
    desired = {"u1": _desired("u1", limit=100, check_limit=True)}
    changes = diff(actual, desired)
    assert len(changes) == 1
    c = changes[0]
    assert c.field == "limit" and c.kind == "limit_increase"
    assert c.before == 50 and c.after == 100


def test_diff_skips_unchecked_dimensions():
    # Role differs, but check_role is False -> no change emitted.
    actual = {"u1": {"limit": 100, "enterprise_role": {"role_id": "r1"}}}
    desired = {"u1": _desired("u1", limit=100, role="r2",
                              check_limit=True, check_role=False)}
    assert diff(actual, desired) == []


def test_diff_no_change_when_already_at_desired():
    actual = {"u1": {"limit": 100, "enterprise_role": {"role_id": "r1"}}}
    desired = {"u1": _desired("u1", limit=100, role="r1",
                              check_limit=True, check_role=True)}
    assert diff(actual, desired) == []


def test_diff_role_grant_when_actual_none():
    actual = {"u1": {"limit": 100, "enterprise_role": None}}
    desired = {"u1": _desired("u1", role="r1", check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_grant" and c.before is None and c.after == "r1"


def test_diff_role_revoke_when_desired_none():
    actual = {"u1": {"limit": 100, "enterprise_role": {"role_id": "r1"}}}
    desired = {"u1": _desired("u1", role=None, check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_revoke" and c.after is None


def test_diff_role_change_when_both_real():
    actual = {"u1": {"limit": 100, "enterprise_role": {"role_id": "r1"}}}
    desired = {"u1": _desired("u1", role="r2", check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_change" and c.before == "r1" and c.after == "r2"


def test_diff_missing_actual_user_classifies_limit_as_decrease():
    # Characterization of a subtle rule: a user absent from `actual` reads back
    # limit=None, and since None ranks as *unlimited* (highest), setting any
    # numeric cap is a DECREASE (auto-apply), not an increase. This is the same
    # rule _onboard_row_changes leans on so a new user's cap auto-applies. (In
    # practice every diff() caller builds `desired` from `actual`, so a truly
    # missing user is only a theoretical input here.)
    actual = {}
    desired = {"u1": _desired("u1", limit=100, role="r1",
                              check_limit=True, check_role=True)}
    kinds = {c.kind for c in diff(actual, desired)}
    assert kinds == {"limit_decrease", "role_grant"}


# --- approval classification ------------------------------------------------
@pytest.mark.parametrize("kind", sorted(NEEDS_APPROVAL))
def test_needs_approval_kinds(kind):
    assert Change("u", kind, "limit", 1, 2, "r").needs_approval is True


@pytest.mark.parametrize("kind", sorted(AUTO_APPLY))
def test_auto_apply_kinds(kind):
    assert Change("u", kind, "limit", 2, 1, "r").needs_approval is False


def test_approval_and_auto_sets_are_disjoint():
    assert NEEDS_APPROVAL.isdisjoint(AUTO_APPLY)


def test_every_kind_diff_emits_is_classified():
    # Guard against a new change kind that no gate covers.
    emitted = {"limit_increase", "limit_decrease", "role_grant", "role_revoke",
               "role_change"}
    assert emitted <= (NEEDS_APPROVAL | AUTO_APPLY)


# --- Change.subject ---------------------------------------------------------
def test_subject_prefers_user_id_then_email():
    assert Change("u1", "limit_increase", "limit", 1, 2, "r").subject == "u1"
    assert Change("", "user_invite", "enterprise_role", None, "r", "x",
                  email="a@b.com").subject == "a@b.com"
    assert Change("", "limit_increase", "limit", 1, 2, "r").subject == "<unknown>"


# --- Plan round-trip --------------------------------------------------------
def test_plan_to_from_dict_roundtrip():
    plan = Plan(workflow="reconcile", triggered_by="t", changes=[
        Change("u1", "limit_increase", "limit", 50, 100, "policy:IDE"),
        Change("", "user_invite", "enterprise_role", None, "r", "onboard",
               email="a@b.com"),
    ])
    restored = Plan.from_dict(plan.to_dict())
    assert restored.workflow == "reconcile"
    assert restored.triggered_by == "t"
    assert len(restored.changes) == 2
    assert restored.changes[0].kind == "limit_increase"
    assert restored.changes[1].email == "a@b.com"
    assert restored.changes[0].status == "pending"
