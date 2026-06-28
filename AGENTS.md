# Repo notes for agents

Governance CLI (`govern.py`) + daily report CLI (`report.py`). Core engine lives
in the `govern/` package. See `README.md` for product behaviour.

## Module layout (`govern/`)

- `cli.py` — argparse wiring; each subcommand `set_defaults(func=...)` to a tiny
  handler. Catches `errors.GovernError` and turns it into a clean non-zero exit.
- `errors.py` — `GovernError`, the engine's user-facing exception (never raise
  `SystemExit` inside the package; let the CLI/`report.py` boundary convert it).
- `config.py` / `policy.py` — config + `.env` loading; policy load + desired-state
  precedence (`resolve_desired`, `coerce_limit`).
- `state.py` — actual-state reads → `ActualState` dataclass, identity resolvers,
  audit log, the generic `_parallel_map`.
- `population.py` — `resolve_population` (actual+policy → desired), `is_admin`,
  `org_id_by_name`.
- `plan.py` — `Change`/`Plan` model, `diff`, classification, (de)serialize.
- `intake.py` — roster value-validation + per-row change builders (onboard/reassign).
- `render.py` — pure console formatting (change lines, limits, summaries).
- `workflows.py` — the action commands (onboard/reassign/offboard/reconcile).
- `reports.py` — read-only reports (usage/coverage/capacity/logins/lookup).
- `apply.py` — the approval gate / resumable executor; `roster.py` — file parsing;
  `tui.py` — prompts/menu; `constants.py` — shared constants.

## Tests

Run the suite from the repo root:

```bash
python -m pytest          # config in pyproject.toml: pythonpath=["."], testpaths=["tests"]
```

Conventions:
- Tests cover the **pure / safety-critical** logic: `policy` (desired-state
  precedence), `plan` (diff + approval classification), `roster` (parsing),
  `apply` (the atomic-per-user gate, resume, dry-run, archiving), `state`
  (role splitting, identity resolution), the pure helpers now in `render` /
  `reports` / `population`, the `workflows` commands end-to-end, the `cli`
  GovernError→exit boundary, and `report` dates.
- `tests/conftest.py` provides a tmp-backed `cfg` fixture (so tests never touch
  the real repo state/audit) and a recording `FakeClient` (mirrors DevinClient's
  dry-run sentinel; set `apply_concurrency`/`fail_on`/`limits` as needed).
- No network: everything runs against `FakeClient`. Keep it that way.
- All roster/export fixtures use `tmp_path` (the repo `.gitignore` excludes
  `*.csv`/`*.xlsx`, so don't add fixtures under the repo tree).
