"""Pure helpers in workflows.py: utilization math, resolvers, export, format."""
from __future__ import annotations

import csv

import pytest

from govern.plan import Change
from govern.workflows import (_export_format, _fmt_limit, _is_admin,
                              _org_id_by_name, _pct, _render_change, _tag,
                              _utilization_status, _where, _write_table)

NOW = 1_000_000
DAY = 86400


def _status(days, cap, *, near=0.8, window_days=1, products=()):
    return _utilization_status(days, cap, near_cap_pct=near,
                               trend_window_days=window_days,
                               products=list(products), now=NOW)


# --- _utilization_status ----------------------------------------------------
def test_consumption_sums_all_days():
    days = [{"date": NOW - 5 * DAY, "acus": 100},
            {"date": NOW - 1 * DAY + 1, "acus": 10},
            {"date": NOW - 2 * DAY + 1, "acus": 4}]
    st = _status(days, cap=1000)
    assert st["consumption"] == 114
    assert st["cap"] == 1000


def test_trend_up_down_flat():
    # window_days=1: recent = [NOW-DAY, NOW); prior = [NOW-2*DAY, NOW-DAY).
    recent_day = {"date": NOW - 1, "acus": 10}
    prior_day = {"date": NOW - DAY - 1, "acus": 4}
    assert _status([recent_day, prior_day], cap=100)["trend"] == "up"
    assert _status([{"date": NOW - 1, "acus": 4},
                    {"date": NOW - DAY - 1, "acus": 10}], cap=100)["trend"] == "down"
    # All consumption older than both windows -> recent == prior == 0 -> flat.
    assert _status([{"date": NOW - 10 * DAY, "acus": 5}], cap=100)["trend"] == "flat"


def test_flagged_at_or_above_threshold():
    days = [{"date": NOW - 1, "acus": 80}]
    assert _status(days, cap=100, near=0.8)["flagged"] is True   # exactly 80%
    assert _status([{"date": NOW - 1, "acus": 79}], cap=100, near=0.8)["flagged"] is False


def test_pct_none_when_no_cap():
    st = _status([{"date": NOW - 1, "acus": 10}], cap=None)
    assert st["pct"] is None
    assert st["flagged"] is False


def test_products_filter_restricts_consumption():
    days = [{"date": NOW - 1, "acus": 999,
             "acus_by_product": {"cascade": 6, "terminal": 4, "devin": 989}}]
    # With a product filter we count only those products, not the acus total.
    st = _status(days, cap=100, products=["cascade", "terminal"])
    assert st["consumption"] == 10


# --- _org_id_by_name --------------------------------------------------------
def test_org_id_by_name_case_insensitive():
    idx = {"o1": "IDE Standard", "o2": "CLI"}
    assert _org_id_by_name(idx, "ide standard") == "o1"


def test_org_id_unknown_exits():
    with pytest.raises(SystemExit, match="no org named"):
        _org_id_by_name({"o1": "IDE"}, "Nope")


def test_org_id_ambiguous_exits():
    with pytest.raises(SystemExit, match="multiple"):
        _org_id_by_name({"o1": "Dup", "o2": "Dup"}, "Dup")


# --- _export_format ---------------------------------------------------------
@pytest.mark.parametrize("name, fmt", [
    ("u.csv", "csv"), ("u.txt", "csv"), ("u.tsv", "tsv"), ("u.xlsx", "xlsx")])
def test_export_format_supported(name, fmt):
    assert _export_format(name) == fmt


@pytest.mark.parametrize("name", ["u.xls", "u.xlsm", "u.xlsb", "u", "u.json"])
def test_export_format_rejected(name):
    with pytest.raises(SystemExit):
        _export_format(name)


# --- _write_table -----------------------------------------------------------
def test_write_table_csv_roundtrip(tmp_path):
    path = str(tmp_path / "out.csv")
    _write_table(path, ["email", "n"], [["a@x.com", 1], ["b@x.com", 2]])
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows == [["email", "n"], ["a@x.com", "1"], ["b@x.com", "2"]]


def test_write_table_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "deep" / "out.tsv")
    _write_table(path, ["a"], [["x"]])
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter="\t"))
    assert rows == [["a"], ["x"]]


def test_write_table_xlsx_roundtrip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = str(tmp_path / "out.xlsx")
    _write_table(path, ["email", "n"], [["a@x.com", 1], ["b@x.com", 2]])
    wb = openpyxl.load_workbook(path)
    rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    assert rows == [["email", "n"], ["a@x.com", 1], ["b@x.com", 2]]


# --- small formatters -------------------------------------------------------
@pytest.mark.parametrize("value, is_set, out", [
    (None, True, "unlimited"),
    (None, False, "unset"),
    (100, True, "100"),
])
def test_fmt_limit(value, is_set, out):
    assert _fmt_limit(value, is_set) == out


def test_fmt_limit_default_is_set():
    assert _fmt_limit(50) == "50"


@pytest.mark.parametrize("n, d, out", [(8, 10, "80%"), (1, 3, "33%"), (0, 0, "0%"), (5, 0, "0%")])
def test_pct(n, d, out):
    assert _pct(n, d) == out


def test_is_admin_matches_role_name_substring_case_insensitive():
    user = {"enterprise_role": {"role_name": "Enterprise Admin"}}
    assert _is_admin(user, ["admin"]) is True
    assert _is_admin({"enterprise_role": {"role_name": "Member"}}, ["admin"]) is False
    assert _is_admin({"enterprise_role": None}, ["admin"]) is False


# --- change-line rendering (exact-format guards) -----------------------------
def test_tag():
    assert _tag(Change("u", "org_add", "org_membership", None, None, "r")) == "APPROVAL"
    assert _tag(Change("u", "limit_decrease", "limit", 2, 1, "r")) == "auto"


def test_where_uses_org_name_then_id_then_blank():
    c = Change("u", "org_remove", "org_membership", "r", None, "r", org_id="o1")
    assert _where({"o1": "IDE"}, c) == "  [IDE]"
    assert _where({}, c) == "  [o1]"                 # unknown org -> id
    assert _where({}, Change("u", "limit_decrease", "limit", 2, 1, "r")) == ""


def test_render_change_with_field_and_where_matches_onboard_format():
    c = Change("u1", "org_add", "org_membership", None, "role-org", "onboard:IDE",
               org_id="o1")
    where = "  [IDE Standard]"
    expected = (f"  [{'APPROVAL':8}] {c.kind:14} {c.field:16} {c.subject:34} "
                f"{c.before} -> {c.after}{where}")
    assert _render_change(c, label=c.subject, where=where) == expected


def test_render_change_with_field_email_label_matches_move_format():
    c = Change("u1", "limit_decrease", "limit", 100, 50, "policy:CLI")
    expected = (f"  [{'auto':8}] {c.kind:14} {c.field:16} {'a@x.com':34} "
                f"{c.before} -> {c.after}")
    assert _render_change(c, label="a@x.com") == expected


def test_render_change_no_field_format():
    c = Change("u1", "limit_increase", "limit", 50, 100, "policy:IDE")
    expected = f"  [{'APPROVAL':8}] {c.kind:14} {'a@x.com':34} {c.before} -> {c.after}"
    assert _render_change(c, label="a@x.com", show_field=False) == expected


def test_render_change_no_field_with_suffix_matches_reconcile_format():
    c = Change("u1", "role_change", "enterprise_role", "r1", "r2", "policy:IDE")
    suffix = f"  ({c.reason})"
    expected = (f"  [{'APPROVAL':8}] {c.kind:14} {'a@x.com':34} "
                f"{c.before} -> {c.after}  ({c.reason})")
    assert _render_change(c, label="a@x.com", show_field=False, suffix=suffix) == expected
