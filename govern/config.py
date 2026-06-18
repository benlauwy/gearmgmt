"""Configuration + environment loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

# Repo root = parent of the govern/ package directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: Optional[str] = None, override: bool = False) -> None:
    """Minimal .env loader (KEY=VALUE, optional surrounding quotes / `export`).

    Real environment variables win unless ``override`` is set. (A fuller variant
    of this loader also exists in the archived old/ scripts.)
    """
    if path is None:
        for cand in (os.path.join(os.getcwd(), ".env"), os.path.join(REPO_ROOT, ".env")):
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
                val = val[1:-1]
            if override or key not in os.environ:
                os.environ[key] = val


def load_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Config:
    """Parsed config.toml plus environment-derived values."""

    paths: dict[str, str]
    governance: dict[str, Any]
    leaver: dict[str, Any]
    utilization: dict[str, Any]
    api: dict[str, Any]
    invite: dict[str, Any]
    token: str = ""
    base_url: str = "https://api.devin.ai"

    def path(self, key: str) -> str:
        """Resolve a configured path (relative entries are anchored at repo root)."""
        p = self.paths[key]
        return p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)


def load_config(path: Optional[str] = None) -> Config:
    load_dotenv()
    cfg_path = path or os.path.join(REPO_ROOT, "config.toml")
    data = load_toml(cfg_path)
    return Config(
        paths=data.get("paths", {}),
        governance=data.get("governance", {}),
        leaver=data.get("leaver", {}),
        utilization=data.get("utilization", {}),
        api=data.get("api", {}),
        invite=data.get("invite", {}),
        token=os.environ.get("DEVIN_SERVICE_USER_TOKEN", ""),
        base_url=os.environ.get("DEVIN_API_BASE_URL", "https://api.devin.ai").rstrip("/"),
    )
