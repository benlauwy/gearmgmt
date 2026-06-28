"""Actual-state reads, role-assignment splitting, identity resolution."""
from __future__ import annotations

import pytest
from conftest import FakeClient

from govern.errors import GovernError
from govern.state import (
    ActualState,
    _split_role_assignments,
    parse_limit_payload,
    read_actual,
    read_members,
    resolve_identities,
    resolve_one,
)


def _member(uid, email, assignments, name=None):
    return {"user_id": uid, "email": email, "name": name or uid,
            "role_assignments": assignments}


def _ent(role_id, name="Ent"):
    return {"role": {"role_id": role_id, "role_name": name, "role_type": "enterprise"}}


def _org(org_id, role_id, name="Org User"):
    return {"org_id": org_id,
            "role": {"role_id": role_id, "role_name": name, "role_type": "org"}}


# --- _split_role_assignments ------------------------------------------------
def test_split_separates_enterprise_and_org_roles():
    member = _member("u1", "a@x.com", [_ent("ent1"), _org("o1", "r1"), _org("o2", "r2")])
    ent, orgs = _split_role_assignments(member)
    assert ent == {"role_id": "ent1", "role_name": "Ent"}
    assert set(orgs) == {"o1", "o2"}
    assert orgs["o1"]["role_id"] == "r1"


def test_split_treats_null_org_id_as_enterprise():
    # An assignment with no org_id is the enterprise role even without the type.
    member = _member("u1", "a@x.com",
                     [{"org_id": None, "role": {"role_id": "ent9", "role_name": "X"}}])
    ent, orgs = _split_role_assignments(member)
    assert ent["role_id"] == "ent9"
    assert orgs == {}


def test_split_handles_no_assignments():
    ent, orgs = _split_role_assignments(_member("u1", "a@x.com", []))
    assert ent is None and orgs == {}


# --- read_members (lean: no per-user limit calls) ---------------------------
def test_read_members_returns_identity_and_sorted_org_ids():
    client = FakeClient(members=[
        _member("u1", "a@x.com", [_ent("e1"), _org("o2", "r2"), _org("o1", "r1")]),
        _member("u2", "b@x.com", [_ent("e1")]),
    ])
    members = read_members(client)
    assert members["u1"].email == "a@x.com"
    assert members["u1"].org_ids == ["o1", "o2"]   # sorted
    assert members["u2"].org_ids == []
    assert client.calls == []                          # no mutations / limit reads


# --- parse_limit_payload ----------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ({"local_agent": {"cycle_acu_limit": 100}}, (100, True)),   # numeric cap
    ({"local_agent": {"cycle_acu_limit": None}}, (None, True)),  # explicit unlimited
    ({}, (None, False)),                                         # no override
    (None, (None, False)),                                       # missing payload
])
def test_parse_limit_payload(raw, expected):
    assert parse_limit_payload(raw) == expected


# --- read_actual (members + per-user limit) ---------------------------------
def test_read_actual_merges_limits_and_flags_set():
    client = FakeClient(
        members=[
            _member("u1", "a@x.com", [_ent("e1"), _org("o1", "r1")]),
            _member("u2", "b@x.com", [_ent("e1")]),
            _member("u3", "c@x.com", [_ent("e1")]),
        ],
        limits={
            "u1": {"local_agent": {"cycle_acu_limit": 100}},  # numeric cap
            "u2": {"local_agent": {"cycle_acu_limit": None}},  # explicit unlimited
            "u3": {},                                          # no override
        },
    )
    actual = read_actual(client, workers=1, progress=False)

    assert actual["u1"].limit == 100 and actual["u1"].limit_set is True
    assert actual["u1"].org_ids == ["o1"]
    assert actual["u1"].enterprise_role["role_id"] == "e1"

    assert actual["u2"].limit is None and actual["u2"].limit_set is True   # unlimited
    assert actual["u3"].limit is None and actual["u3"].limit_set is False  # unset


# --- identity resolvers -----------------------------------------------------
def _index():
    return {"user-1": ActualState("user-1", email="a@x.com"),
            "user-2": ActualState("user-2", email="b@x.com")}


def test_resolve_one_passthrough_user_id():
    assert resolve_one(_index(), "user-1") == "user-1"


def test_resolve_one_by_email_case_insensitive():
    assert resolve_one(_index(), "A@X.COM") == "user-1"


def test_resolve_one_unknown_exits():
    with pytest.raises(GovernError):
        resolve_one(_index(), "missing@x.com")


def test_resolve_one_ambiguous_email_exits():
    dup = {"u1": ActualState("u1", email="dup@x.com"),
           "u2": ActualState("u2", email="dup@x.com")}
    with pytest.raises(GovernError):
        resolve_one(dup, "dup@x.com")


def test_resolve_identities_returns_all_matches_sorted():
    dup = {"u2": ActualState("u2", email="dup@x.com"),
           "u1": ActualState("u1", email="dup@x.com"),
           "u3": ActualState("u3", email="other@x.com")}
    assert resolve_identities(dup, "dup@x.com") == ["u1", "u2"]   # sorted, both ids


def test_resolve_identities_user_id_passthrough():
    assert resolve_identities(_index(), "user-2") == ["user-2"]


def test_resolve_identities_unknown_exits():
    with pytest.raises(GovernError):
        resolve_identities(_index(), "nobody@x.com")
