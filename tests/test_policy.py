"""Policy loading + desired-state precedence (the source-of-truth rules)."""
from __future__ import annotations

import pytest

from govern.policy import Policy, coerce_limit, resolve_desired


# --- coerce_limit -----------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    (None, None),
    ("null", None),
    ("NULL", None),
    ("none", None),
    ("  null  ", None),
    (5, 5),
    ("5", 5),
    (100, 100),
])
def test_coerce_limit_valid(value, expected):
    assert coerce_limit(value) == expected


@pytest.mark.parametrize("value", [0, -1, "0", "-5"])
def test_coerce_limit_rejects_non_positive(value):
    with pytest.raises(ValueError):
        coerce_limit(value)


def test_coerce_limit_rejects_garbage():
    with pytest.raises(ValueError):
        coerce_limit("not-a-number")


# --- resolve_desired precedence ---------------------------------------------
def _policy(**kw) -> Policy:
    return Policy(limits=kw.get("limits", {}), roles=kw.get("roles", {}),
                  overrides=kw.get("overrides", {}))


def test_override_wins_over_everything(cfg):
    pol = _policy(
        limits={"IDE": 100}, roles={"IDE": "role-ide"},
        overrides={"u1": {"reason": "pinned", "limit": 999, "enterprise_role": "role-x"}},
    )
    d = resolve_desired("u1", ["IDE"], is_admin=True, policy=pol, cfg=cfg)
    assert d.source == "override"
    assert d.limit == 999 and d.enterprise_role == "role-x"
    assert d.check_limit and d.check_role
    assert d.note == "pinned"


def test_override_only_specified_fields_are_checked(cfg):
    # An override that pins only the limit must NOT reconcile the role.
    pol = _policy(overrides={"u1": {"limit": 50}})
    d = resolve_desired("u1", [], is_admin=False, policy=pol, cfg=cfg)
    assert d.check_limit is True
    assert d.check_role is False
    assert d.limit == 50


def test_override_limit_is_coerced(cfg):
    pol = _policy(overrides={"u1": {"limit": "null"}})
    d = resolve_desired("u1", [], is_admin=False, policy=pol, cfg=cfg)
    assert d.check_limit is True
    assert d.limit is None  # "null" -> unlimited


def test_admin_is_exempt_and_ungoverned(cfg):
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE", "CLI"], is_admin=True, policy=pol, cfg=cfg)
    assert d.source == "admin-exempt"
    assert d.check_limit is False and d.check_role is False


def test_single_governed_org_sets_limit_and_role(cfg):
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE"], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "policy:IDE"
    assert d.limit == 100 and d.enterprise_role == "role-ide"
    assert d.check_limit and d.check_role


def test_single_org_checks_only_dimensions_with_policy(cfg):
    # IDE has a limit but no role entry -> only the limit is reconciled.
    pol = _policy(limits={"IDE": 100})
    d = resolve_desired("u1", ["IDE"], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "policy:IDE"
    assert d.check_limit is True
    assert d.check_role is False


def test_ungoverned_org_membership_is_no_governed_org(cfg):
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["Some Ungoverned Org"], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "no-governed-org"
    assert d.check_limit is False and d.check_role is False


def test_no_orgs_is_no_governed_org(cfg):
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", [], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "no-governed-org"


def test_multiple_governed_orgs_is_violation(cfg):
    pol = _policy(limits={"IDE": 100, "CLI": 50}, roles={"IDE": "r1", "CLI": "r2"})
    d = resolve_desired("u1", ["IDE", "CLI"], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "violation"
    assert d.check_limit is False and d.check_role is False
    assert "IDE" in d.note and "CLI" in d.note


def test_one_governed_plus_one_ungoverned_is_single(cfg):
    # Only governed orgs count toward the single-org rule.
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE", "Ungoverned"], is_admin=False, policy=pol, cfg=cfg)
    assert d.source == "policy:IDE"
    assert d.limit == 100
