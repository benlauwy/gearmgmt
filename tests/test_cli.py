"""CLI boundary: GovernError is rendered as a clean message + non-zero exit."""
from __future__ import annotations

from conftest import make_cfg

from govern import cli
from govern.errors import GovernError


def _stub_setup(monkeypatch, tmp_path):
    """Make cli.main reach a command handler without real config/network."""
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: make_cfg(tmp_path))
    monkeypatch.setattr(cli.DevinClient, "from_config",
                        staticmethod(lambda cfg, **_k: object()))


def test_main_converts_govern_error_to_exit_1(monkeypatch, capsys, tmp_path):
    _stub_setup(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise GovernError("no user matching 'x'")
    monkeypatch.setattr("govern.reports.lookup", boom)

    rc = cli.main(["lookup", "--user", "x"])
    assert rc == 1
    assert "ERROR: no user matching 'x'" in capsys.readouterr().err


def test_main_returns_zero_on_success(monkeypatch, capsys, tmp_path):
    _stub_setup(monkeypatch, tmp_path)
    monkeypatch.setattr("govern.reports.coverage", lambda *_a, **_k: None)

    assert cli.main(["coverage"]) == 0
