"""Roster intake: email validation, shape rules, column/header detection."""
from __future__ import annotations

import pytest

from govern import roster as r

# Governed orgs used by the header heuristic / org-column detection in tests.
ORGS = {"ide standard", "cli ide super"}
is_org = lambda name: (name or "").strip().lower() in ORGS  # noqa: E731


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- is_valid_email ---------------------------------------------------------
@pytest.mark.parametrize("email, ok", [
    ("jane@company.com", True),
    ("a.b+c@sub.example.co", True),
    ("nope", False),
    ("no@domain", False),         # no dotted domain
    ("two@@at.com", False),
    ("spaces in@x.com", False),
    ("", False),
    (None, False),
])
def test_is_valid_email(email, ok):
    assert r.is_valid_email(email) is ok


# --- two-column happy paths -------------------------------------------------
def test_two_column_email_then_group(tmp_path):
    path = _write(tmp_path, "roster.csv",
                  "email,group\njane@company.com,IDE Standard\nraj@company.com,CLI IDE Super\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.has_org_column is True
    assert roster.warnings == []
    assert roster.emails == ["jane@company.com", "raj@company.com"]
    assert roster.orgs == ["IDE Standard", "CLI IDE Super"]


def test_two_column_group_then_email_is_autodetected(tmp_path):
    # Columns reversed: the email column must still be detected (by content).
    path = _write(tmp_path, "roster.csv",
                  "group,email\nIDE Standard,jane@company.com\nCLI IDE Super,raj@company.com\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.has_org_column is True
    assert roster.emails == ["jane@company.com", "raj@company.com"]
    assert roster.orgs == ["IDE Standard", "CLI IDE Super"]


def test_single_column_has_no_org_column(tmp_path):
    path = _write(tmp_path, "roster.csv", "email\njane@company.com\nraj@company.com\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.has_org_column is False
    assert roster.emails == ["jane@company.com", "raj@company.com"]
    assert roster.orgs == [None, None]


# --- header heuristic -------------------------------------------------------
def test_first_row_that_looks_like_data_is_kept_with_warning(tmp_path):
    path = _write(tmp_path, "roster.csv",
                  "jane@company.com,IDE Standard\nraj@company.com,CLI IDE Super\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert any("looks like data" in w for w in roster.warnings)
    assert roster.emails == ["jane@company.com", "raj@company.com"]


def test_header_row_is_dropped(tmp_path):
    path = _write(tmp_path, "roster.csv", "email,group\njane@company.com,IDE Standard\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.warnings == []
    assert roster.emails == ["jane@company.com"]


# --- structural failures ----------------------------------------------------
def test_more_than_two_columns_is_error(tmp_path):
    path = _write(tmp_path, "roster.csv", "email,group,extra\na@x.com,IDE Standard,zzz\n")
    with pytest.raises(r.RosterError, match="columns"):
        r.parse_roster(path, is_valid_org=is_org)


def test_trailing_empty_column_does_not_count(tmp_path):
    # A stray trailing comma (empty 3rd column) must not trip the >2 rule.
    path = _write(tmp_path, "roster.csv", "email,group,\njane@company.com,IDE Standard,\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.emails == ["jane@company.com"]
    assert roster.orgs == ["IDE Standard"]


def test_missing_file_is_error(tmp_path):
    with pytest.raises(r.RosterError, match="not found"):
        r.parse_roster(str(tmp_path / "nope.csv"), is_valid_org=is_org)


def test_empty_file_is_error(tmp_path):
    path = _write(tmp_path, "roster.csv", "\n\n")
    with pytest.raises(r.RosterError, match="empty"):
        r.parse_roster(path, is_valid_org=is_org)


def test_header_only_no_data_is_error(tmp_path):
    path = _write(tmp_path, "roster.csv", "email,group\n")
    with pytest.raises(r.RosterError, match="no data"):
        r.parse_roster(path, is_valid_org=is_org)


def test_both_columns_look_like_emails_is_ambiguous(tmp_path):
    path = _write(tmp_path, "roster.csv", "a@x.com,b@x.com\nc@x.com,d@x.com\n")
    with pytest.raises(r.RosterError, match="auto-detect"):
        r.parse_roster(path, is_valid_org=is_org)


def test_no_email_column_is_error(tmp_path):
    # Header dropped, the remaining data row has no emails in either column.
    path = _write(tmp_path, "roster.csv", "colA,colB\nfoo,bar\n")
    with pytest.raises(r.RosterError, match="email"):
        r.parse_roster(path, is_valid_org=is_org)


def test_unsupported_extension_is_error(tmp_path):
    path = _write(tmp_path, "roster.json", "{}")
    with pytest.raises(r.RosterError, match="unsupported file type"):
        r.parse_roster(path, is_valid_org=is_org)


def test_legacy_xls_is_rejected_with_guidance(tmp_path):
    path = _write(tmp_path, "roster.xls", "anything")
    with pytest.raises(r.RosterError, match=r"save as \.xlsx"):
        r.parse_roster(path, is_valid_org=is_org)


# --- blank rows + TSV -------------------------------------------------------
def test_blank_rows_are_dropped(tmp_path):
    path = _write(tmp_path, "roster.csv",
                  "email,group\njane@company.com,IDE Standard\n\n\nraj@company.com,CLI IDE Super\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.emails == ["jane@company.com", "raj@company.com"]


def test_tsv_is_read_with_tab_delimiter(tmp_path):
    path = _write(tmp_path, "roster.tsv",
                  "email\tgroup\njane@company.com\tIDE Standard\n")
    roster = r.parse_roster(path, is_valid_org=is_org)
    assert roster.emails == ["jane@company.com"]
    assert roster.orgs == ["IDE Standard"]


# --- xlsx (openpyxl is available in this env) -------------------------------
def test_xlsx_roster_is_read(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "roster.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["email", "group"])
    ws.append(["jane@company.com", "IDE Standard"])
    ws.append(["raj@company.com", "CLI IDE Super"])
    wb.save(path)
    roster = r.parse_roster(str(path), is_valid_org=is_org)
    assert roster.has_org_column is True
    assert roster.emails == ["jane@company.com", "raj@company.com"]
    assert roster.orgs == ["IDE Standard", "CLI IDE Super"]
