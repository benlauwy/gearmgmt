"""Actual-state reads, role-assignment splitting, membership snapshot diffing."""
from __future__ import annotations

from govern.state import (_split_role_assignments, diff_membership,
                          load_snapshot, read_actual, read_members,
                          save_snapshot)
from conftest import FakeClient


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
    assert members["u1"]["email"] == "a@x.com"
    assert members["u1"]["org_ids"] == ["o1", "o2"]   # sorted
    assert members["u2"]["org_ids"] == []
    assert client.calls == []                          # no mutations / limit reads


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

    assert actual["u1"]["limit"] == 100 and actual["u1"]["limit_set"] is True
    assert actual["u1"]["org_ids"] == ["o1"]
    assert actual["u1"]["enterprise_role"]["role_id"] == "e1"

    assert actual["u2"]["limit"] is None and actual["u2"]["limit_set"] is True   # unlimited
    assert actual["u3"]["limit"] is None and actual["u3"]["limit_set"] is False  # unset


# --- diff_membership --------------------------------------------------------
def test_diff_membership_classifies_joiner_mover_leaver():
    prev = {"u1": ["o1"], "u2": ["o1", "o2"], "u3": ["o1"]}
    curr = {"u1": ["o1"], "u2": ["o2"], "u4": ["o1"]}
    delta = diff_membership(prev, curr)

    assert delta["joiners"] == ["u4"]
    assert delta["leavers"] == ["u3"]
    assert delta["movers"] == [("u2", ["o1", "o2"], ["o2"])]


def test_diff_membership_no_change_is_empty():
    same = {"u1": ["o1", "o2"]}
    delta = diff_membership(same, dict(same))
    assert delta == {"joiners": [], "movers": [], "leavers": []}


# --- snapshot round-trip ----------------------------------------------------
def test_snapshot_save_then_load(cfg):
    assert load_snapshot(cfg) == {}            # nothing written yet
    snap = {"u1": ["o1"], "u2": ["o1", "o2"]}
    save_snapshot(cfg, snap)
    assert load_snapshot(cfg) == snap
