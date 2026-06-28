# Repo notes for agents

Governance CLI (`govern.py`) + daily report CLI (`report.py`). Core engine lives
in the `govern/` package. See `README.md` for product behaviour.

## Tests

Run the suite from the repo root:

```bash
python -m pytest          # config in pyproject.toml: pythonpath=["."], testpaths=["tests"]
```

Conventions:
- Tests cover the **pure / safety-critical** logic: `policy` (desired-state
  precedence), `plan` (diff + approval classification), `roster` (parsing),
  `apply` (the atomic-per-user gate, resume, dry-run, archiving), `state`
  (snapshot diff, role splitting), `workflows` pure helpers, and `report` dates.
- `tests/conftest.py` provides a tmp-backed `cfg` fixture (so tests never touch
  the real repo state/audit) and a recording `FakeClient` (mirrors DevinClient's
  dry-run sentinel; set `apply_concurrency`/`fail_on`/`limits` as needed).
- No network: everything runs against `FakeClient`. Keep it that way.
- All roster/export fixtures use `tmp_path` (the repo `.gitignore` excludes
  `*.csv`/`*.xlsx`, so don't add fixtures under the repo tree).
