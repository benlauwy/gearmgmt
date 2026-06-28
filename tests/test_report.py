"""report.py date-range logic (the off-by-one-prone pure bits)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from report import (_bucket_date, _day_start_utc, _days, _month_bounds,
                    _parse_date, _resolve_range)


def _args(month=None, frm=None, to=None):
    return SimpleNamespace(month=month, frm=frm, to=to)


# --- _parse_date ------------------------------------------------------------
def test_parse_date_ok():
    assert _parse_date("2026-06-01") == date(2026, 6, 1)


def test_parse_date_bad_exits():
    with pytest.raises(SystemExit):
        _parse_date("06/01/2026")


# --- _month_bounds ----------------------------------------------------------
def test_month_bounds_whole_past_month():
    start, end = _month_bounds("2026-05", date(2026, 6, 15))
    assert (start, end) == (date(2026, 5, 1), date(2026, 5, 31))


def test_month_bounds_current_month_capped_at_today():
    start, end = _month_bounds("2026-06", date(2026, 6, 10))
    assert (start, end) == (date(2026, 6, 1), date(2026, 6, 10))


def test_month_bounds_december_rollover():
    start, end = _month_bounds("2026-12", date(2027, 1, 5))
    assert (start, end) == (date(2026, 12, 1), date(2026, 12, 31))


def test_month_bounds_bad_exits():
    with pytest.raises(SystemExit):
        _month_bounds("2026-13", date(2026, 6, 1))


# --- _resolve_range ---------------------------------------------------------
def test_resolve_range_defaults_to_current_month():
    start, end, label = _resolve_range(_args(), date(2026, 6, 15))
    assert (start, end) == (date(2026, 6, 1), date(2026, 6, 15))
    assert label == "current month"


def test_resolve_range_month():
    start, end, label = _resolve_range(_args(month="2026-05"), date(2026, 6, 15))
    assert (start, end) == (date(2026, 5, 1), date(2026, 5, 31))
    assert "2026-05" in label


def test_resolve_range_from_only_defaults_end_to_today():
    start, end, _ = _resolve_range(_args(frm="2026-06-03"), date(2026, 6, 15))
    assert (start, end) == (date(2026, 6, 3), date(2026, 6, 15))


def test_resolve_range_to_only_defaults_start_to_first():
    start, end, _ = _resolve_range(_args(to="2026-06-09"), date(2026, 6, 15))
    assert (start, end) == (date(2026, 6, 1), date(2026, 6, 9))


def test_resolve_range_month_with_from_exits():
    with pytest.raises(SystemExit, match="cannot be combined"):
        _resolve_range(_args(month="2026-05", frm="2026-05-02"), date(2026, 6, 15))


def test_resolve_range_start_after_end_exits():
    with pytest.raises(SystemExit, match="after end"):
        _resolve_range(_args(frm="2026-06-20", to="2026-06-10"), date(2026, 6, 15))


# --- _days / bucket round-trip ----------------------------------------------
def test_days_inclusive():
    assert _days(date(2026, 6, 1), date(2026, 6, 3)) == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]


def test_days_single():
    assert _days(date(2026, 6, 1), date(2026, 6, 1)) == [date(2026, 6, 1)]


def test_bucket_date_roundtrips_day_start():
    d = date(2026, 6, 1)
    assert _bucket_date(_day_start_utc(d)) == d
