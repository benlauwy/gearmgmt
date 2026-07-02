"""Policy loading + desired-state precedence (the source-of-truth rules)."""
from __future__ import annotations

import pytest

from govern.policy import Policy, coerce_limit, match_org_role_ids, resolve_desired


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


@pytest.mark.parametrize("value, expected", [(0, 0), ("0", 0), (5, 5), ("null", None)])
def test_coerce_limit_allow_zero(value, expected):
    # The offboard leaver limit zeroes a cap, so 0 must be accepted there.
    assert coerce_limit(value, allow_zero=True) == expected


def test_coerce_limit_allow_zero_still_rejects_negative():
    with pytest.raises(ValueError):
        coerce_limit(-1, allow_zero=True)


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


def test_admin_limit_governed_by_admin_org_role_exempt(cfg):
    # Admins are limit-governed from the Admin Org (overrides aside), may be
    # multi-org (no violation), and keep their role (check_role False).
    pol = _policy(limits={"Admin Org": 1000, "IDE": 100},
                  roles={"Admin Org": "role-admin", "IDE": "role-ide"})
    d = resolve_desired("u1", ["Admin Org", "IDE", "CLI"], is_admin=True,
                        policy=pol, cfg=cfg)
    assert d.source == "admin"
    assert d.limit == 1000
    assert d.enterprise_role is None
    assert d.check_limit is True and d.check_role is False


def test_admin_not_in_admin_org_is_flagged_but_still_limited(cfg):
    # An admin who is not a member of the Admin Org still gets its limit, but is
    # flagged via the "admin-no-admin-org" source + a note.
    pol = _policy(limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    d = resolve_desired("u1", ["IDE"], is_admin=True, policy=pol, cfg=cfg)
    assert d.source == "admin-no-admin-org"
    assert d.limit == 1000 and d.check_limit is True
    assert d.check_role is False
    assert "Admin Org" in d.note


def test_admin_with_no_admin_org_limit_policy_is_not_limit_checked(cfg):
    # If the Admin Org has no limits.toml entry there is nothing to apply, so the
    # limit is left ungoverned (check_limit False) rather than forced to unlimited.
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE"], is_admin=True, policy=pol, cfg=cfg)
    assert d.source == "admin-no-admin-org"
    assert d.check_limit is False and d.check_role is False


def test_admin_org_name_is_configurable(cfg):
    # The Admin Org name comes from cfg.governance.admin_org_name.
    cfg.governance["admin_org_name"] = "Ops Admins"
    pol = _policy(limits={"Ops Admins": 500}, roles={"Ops Admins": "role-admin"})
    d = resolve_desired("u1", ["Ops Admins"], is_admin=True, policy=pol, cfg=cfg)
    assert d.source == "admin"
    assert d.limit == 500 and d.check_limit is True


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


# --- match_org_role_ids -----------------------------------------------------
def test_match_org_role_ids_prefers_id_over_name():
    # An explicit id wins and needs no roles list to resolve.
    assert match_org_role_ids("role-x", "Ignored", []) == ["role-x"]


def test_match_org_role_ids_resolves_name_case_insensitive():
    roles = [{"role_id": "r1", "role_name": "Organization User"},
             {"role_id": "r2", "role_name": "Org Admin"}]
    assert match_org_role_ids(None, "organization user", roles) == ["r1"]


def test_match_org_role_ids_none_configured_is_empty():
    assert match_org_role_ids(None, None, [{"role_id": "r1", "role_name": "X"}]) == []


def test_match_org_role_ids_reports_no_or_ambiguous_match():
    roles = [{"role_id": "r1", "role_name": "Dup"}, {"role_id": "r2", "role_name": "Dup"}]
    assert match_org_role_ids(None, "Dup", roles) == ["r1", "r2"]  # ambiguous
    assert match_org_role_ids(None, "Nope", roles) == []           # no match


# --- resolve_desired: org-role dimension (as {org_id: role_id}) -------------
def test_org_role_governed_for_single_org_non_admin(cfg):
    # A governed non-admin's role in their single org -> the member org_role_id,
    # keyed by that org's id.
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE"], is_admin=False, policy=pol, cfg=cfg,
                        org_role_id="role-org", name_to_org_id={"IDE": "o1"})
    assert d.org_role_by_oid == {"o1": "role-org"}


def test_org_role_ungoverned_when_no_member_org_role(cfg):
    # No member org role -> org roles left ungoverned (backward compatible).
    pol = _policy(limits={"IDE": 100}, roles={"IDE": "role-ide"})
    d = resolve_desired("u1", ["IDE"], is_admin=False, policy=pol, cfg=cfg,
                        org_role_id=None, name_to_org_id={"IDE": "o1"})
    assert d.org_role_by_oid == {}


def test_org_role_exempt_for_override_users(cfg):
    # overrides.toml users are full exceptions -> org role not reconciled (even
    # with both the member and admin org roles configured).
    pol = _policy(overrides={"u1": {"limit": 50}})
    d = resolve_desired("u1", ["IDE"], is_admin=False, policy=pol, cfg=cfg,
                        org_role_id="role-org", admin_org_role_id="role-admin-org",
                        name_to_org_id={"IDE": "o1"})
    assert d.source == "override"
    assert d.org_role_by_oid == {}


def test_org_role_exempt_for_violation_and_no_org(cfg):
    # A non-admin in >1 governed org (violation) or 0 (no-governed-org) has no
    # single org to govern the role in.
    pol = _policy(limits={"IDE": 100, "CLI": 50}, roles={"IDE": "r1", "CLI": "r2"})
    viol = resolve_desired("u1", ["IDE", "CLI"], is_admin=False, policy=pol, cfg=cfg,
                           org_role_id="role-org", name_to_org_id={"IDE": "o1", "CLI": "o2"})
    assert viol.source == "violation" and viol.org_role_by_oid == {}
    none = resolve_desired("u2", [], is_admin=False, policy=pol, cfg=cfg,
                           org_role_id="role-org", name_to_org_id={})
    assert none.source == "no-governed-org" and none.org_role_by_oid == {}


# --- admin org-role governance (every org the admin belongs to) -------------
def test_admin_org_role_governed_on_every_org(cfg):
    # governance.admin_org_role_id governs an admin's role on ALL their orgs; the
    # member [invite] role is NOT applied to admins.
    pol = _policy(limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    d = resolve_desired("u1", ["Admin Org", "IDE"], is_admin=True, policy=pol, cfg=cfg,
                        org_role_id="role-member", admin_org_role_id="role-admin-org",
                        name_to_org_id={"Admin Org": "oa", "IDE": "o1"})
    assert d.source == "admin"
    assert d.org_role_by_oid == {"oa": "role-admin-org", "o1": "role-admin-org"}


def test_admin_org_role_ungoverned_when_unset(cfg):
    # No admin_org_role_id -> admins keep their org roles (exempt, as before).
    pol = _policy(limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    d = resolve_desired("u1", ["Admin Org", "IDE"], is_admin=True, policy=pol, cfg=cfg,
                        org_role_id="role-member", admin_org_role_id=None,
                        name_to_org_id={"Admin Org": "oa", "IDE": "o1"})
    assert d.org_role_by_oid == {}


def test_admin_org_role_governed_even_outside_admin_org(cfg):
    # An admin who is not in the Admin Org is still flagged, but its org role is
    # governed on the orgs it IS in.
    pol = _policy(limits={"Admin Org": 1000}, roles={"Admin Org": "role-admin"})
    d = resolve_desired("u1", ["IDE"], is_admin=True, policy=pol, cfg=cfg,
                        admin_org_role_id="role-admin-org", name_to_org_id={"IDE": "o1"})
    assert d.source == "admin-no-admin-org"
    assert d.org_role_by_oid == {"o1": "role-admin-org"}
