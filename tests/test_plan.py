"""Change/Plan model: limit classification, diff, approval gate, (de)serialize."""
from __future__ import annotations

import pytest

from govern.plan import AUTO_APPLY, NEEDS_APPROVAL, Change, Plan, diff, limit_kind
from govern.policy import DesiredState
from govern.state import ActualState


# --- limit_kind (None == unlimited, ranks highest) --------------------------
@pytest.mark.parametrize("cur, tgt, kind", [
    (50, 100, "limit_increase"),
    (100, 50, "limit_decrease"),
    (50, None, "limit_increase"),    # number -> unlimited is an increase
    (None, 50, "limit_decrease"),    # unlimited -> number is a decrease
])
def test_limit_kind(cur, tgt, kind):
    assert limit_kind(cur, tgt) == kind


# --- diff: only checked dimensions, set-to-desired --------------------------
def _desired(uid, *, limit=None, role=None, check_limit=False, check_role=False,
             source="policy:IDE"):
    return DesiredState(uid, limit, role, source,
                        check_limit=check_limit, check_role=check_role)


def test_diff_emits_limit_increase_when_checked():
    actual = {"u1": ActualState("u1", limit=50, enterprise_role={"role_id": "r1"})}
    desired = {"u1": _desired("u1", limit=100, check_limit=True)}
    changes = diff(actual, desired)
    assert len(changes) == 1
    c = changes[0]
    assert c.field == "limit" and c.kind == "limit_increase"
    assert c.before == 50 and c.after == 100


def test_diff_skips_unchecked_dimensions():
    # Role differs, but check_role is False -> no change emitted.
    actual = {"u1": ActualState("u1", limit=100, enterprise_role={"role_id": "r1"})}
    desired = {"u1": _desired("u1", limit=100, role="r2",
                              check_limit=True, check_role=False)}
    assert diff(actual, desired) == []


def test_diff_no_change_when_already_at_desired():
    actual = {"u1": ActualState("u1", limit=100, enterprise_role={"role_id": "r1"})}
    desired = {"u1": _desired("u1", limit=100, role="r1",
                              check_limit=True, check_role=True)}
    assert diff(actual, desired) == []


def test_diff_role_grant_when_actual_none():
    actual = {"u1": ActualState("u1", limit=100, enterprise_role=None)}
    desired = {"u1": _desired("u1", role="r1", check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_grant" and c.before is None and c.after == "r1"


def test_diff_role_revoke_when_desired_none():
    actual = {"u1": ActualState("u1", limit=100, enterprise_role={"role_id": "r1"})}
    desired = {"u1": _desired("u1", role=None, check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_revoke" and c.after is None


def test_diff_role_change_when_both_real():
    actual = {"u1": ActualState("u1", limit=100, enterprise_role={"role_id": "r1"})}
    desired = {"u1": _desired("u1", role="r2", check_role=True)}
    (c,) = diff(actual, desired)
    assert c.kind == "role_change" and c.before == "r1" and c.after == "r2"


# --- diff: org-role dimension ({org_id: role_id}, one Change per org) --------
def _desired_org(uid, *, org_role_by_oid, source="policy:IDE"):
    return DesiredState(uid, None, None, source, org_role_by_oid=dict(org_role_by_oid))


def test_diff_org_role_change_is_gated_and_org_stamped():
    actual = {"u1": ActualState("u1", org_roles={"o1": {"role_id": "ro1"}})}
    desired = {"u1": _desired_org("u1", org_role_by_oid={"o1": "ro2"})}
    (c,) = diff(actual, desired)
    assert c.field == "org_role" and c.kind == "role_change"
    assert c.before == "ro1" and c.after == "ro2"
    assert c.org_id == "o1"          # targets the specific membership
    assert c.needs_approval is True  # gated, like an enterprise role_change


def test_diff_org_role_no_change_when_matching():
    actual = {"u1": ActualState("u1", org_roles={"o1": {"role_id": "ro1"}})}
    desired = {"u1": _desired_org("u1", org_role_by_oid={"o1": "ro1"})}
    assert diff(actual, desired) == []


def test_diff_org_role_grant_when_absent_in_that_org():
    # No org role recorded for the governed org -> a (still gated) grant.
    actual = {"u1": ActualState("u1", org_roles={})}
    desired = {"u1": _desired_org("u1", org_role_by_oid={"o1": "ro2"})}
    (c,) = diff(actual, desired)
    assert c.kind == "role_grant" and c.before is None and c.after == "ro2"


def test_diff_org_role_empty_map_is_no_op():
    actual = {"u1": ActualState("u1", org_roles={"o1": {"role_id": "ro1"}})}
    desired = {"u1": _desired_org("u1", org_role_by_oid={})}
    assert diff(actual, desired) == []


def test_diff_admin_org_role_across_multiple_orgs():
    # An admin's org role governed on several orgs -> one Change per DRIFTING org.
    actual = {"u1": ActualState("u1", org_roles={
        "o1": {"role_id": "ro1"}, "o2": {"role_id": "admin-org"}})}
    desired = {"u1": _desired_org("u1", source="admin",
                                  org_role_by_oid={"o1": "admin-org", "o2": "admin-org"})}
    changes = diff(actual, desired)
    assert [(c.field, c.org_id, c.after) for c in changes] == \
        [("org_role", "o1", "admin-org")]  # o2 already matches


def test_diff_enterprise_and_org_role_are_independent_changes():
    # Both role dimensions drift -> two Changes, distinguished by field + org_id.
    actual = {"u1": ActualState("u1", enterprise_role={"role_id": "e1"},
                                org_roles={"o1": {"role_id": "ro1"}})}
    desired = {"u1": DesiredState("u1", None, "e2", "policy:IDE", check_role=True,
                                  org_role_by_oid={"o1": "ro2"})}
    fields = {(c.field, c.org_id) for c in diff(actual, desired)}
    assert fields == {("enterprise_role", None), ("org_role", "o1")}


def test_diff_missing_actual_user_classifies_limit_as_decrease():
    # Characterization of a subtle rule: a user absent from `actual` reads back
    # limit=None, and since None ranks as *unlimited* (highest), setting any
    # numeric cap is a DECREASE (auto-apply), not an increase. This is the same
    # rule intake.onboard_row_changes leans on so a new user's cap auto-applies. (In
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
