"""DevinClient.from_config: the single shared client factory (CLI + report)."""
from __future__ import annotations

from conftest import make_cfg

from govern.client import DevinClient


def test_from_config_reads_api_settings(tmp_path):
    cfg = make_cfg(tmp_path, api={
        "max_retries": 9, "retry_backoff_seconds": 1.5,
        "rate_limit_sleep_seconds": 0.25, "read_concurrency": 4,
        "apply_concurrency": 3,
    })
    c = DevinClient.from_config(cfg)
    assert c.token == cfg.token
    assert c.base_url == "https://example.test/api"
    assert c.dry_run is False
    assert c.max_retries == 9
    assert c.backoff == 1.5
    assert c.sleep == 0.25
    assert c.read_concurrency == 4
    assert c.apply_concurrency == 3


def test_from_config_uses_defaults_when_api_empty(tmp_path):
    c = DevinClient.from_config(make_cfg(tmp_path, api={}))
    assert (c.max_retries, c.backoff, c.sleep) == (5, 2.0, 0.1)
    assert (c.read_concurrency, c.apply_concurrency) == (8, 8)


def test_from_config_dry_run_flag(tmp_path):
    assert DevinClient.from_config(make_cfg(tmp_path), dry_run=True).dry_run is True


def test_from_config_strips_trailing_slash(tmp_path):
    c = DevinClient.from_config(make_cfg(tmp_path, base_url="https://h.test/api/"))
    assert c.base_url == "https://h.test/api"
