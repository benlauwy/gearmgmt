# Devin Enterprise Governance

A small CLI that keeps every enterprise user's **Local Agent ACU limit** and
**enterprise role** in line with policy files — with a diff-first plan/apply
workflow, an approval gate, and a full audit log.

Pure standard library (Python 3.11+), no dependencies to install.

---

## 1. Setup

```bash
cp .env.example .env                      # service-user token + API host
cp config.toml.example config.toml        # operational config (tenant role IDs)
cp roles.toml.example roles.toml          # per-org desired enterprise role
cp overrides.toml.example overrides.toml  # per-user exceptions (optional)
```

These copies hold tenant-specific IDs / PII and are git-ignored; edit them with
your real values. `limits.toml` is committed, so edit it in place.

`.env` needs a service-user token and (for dedicated deployments) the API host:

```
DEVIN_SERVICE_USER_TOKEN=cog_xxx
DEVIN_API_BASE_URL=https://<company>.devinenterprise.com/api
```

The token's service user needs these enterprise permissions:
`ViewAccountMembership` (reads), `ManageBilling` (limits),
`ManageAccountMembership` (roles + org add/remove).

Run everything via the entrypoint:

```bash
python govern.py <command> [--dry-run]
python govern.py --help
```

---

## 2. Usage — a member's lifecycle

Every **action** command is diff-first: it writes a plan to `state/plans/` and
changes nothing until you `apply` it. **Read-only** commands (`reconcile`,
`coverage`, `usage`) just report. Add `--dry-run` to simulate any command, and
pass `--user` as **either an email or the raw user_id** (for `onboard`,
`update-limits`, and `offboard`). See *How it works* (next section) for the
approval gate.

**Check the current state (any time, read-only):**
```bash
python govern.py reconcile      # drift: actual vs desired, across everyone
python govern.py coverage       # per-org limit & role coverage
```

### A new person joins
`onboard` does **not** add anyone to an org. The person must already exist in
Devin and already be a member of their org (placed there by your IDP/SSO group
sync or in the admin UI). `onboard` reads the org they're in and sets the
matching limit + enterprise role from policy. New grants need `--approved`:
```bash
python govern.py onboard --user "jane@company.com"
python govern.py apply state/plans/onboard-<ts>.json --approved
```
Standing up a whole new org/tier? Onboard everyone already in it at once:
```bash
python govern.py onboard --org "IDE Standard"
python govern.py apply state/plans/onboard-<ts>.json --approved
```

### A person is hitting their cap
`usage` flags anyone near/at their limit and prints the exact upgrade command:
```bash
python govern.py usage
```
After bumping their tier (move them to a higher org, or edit policy),
re-materialize just that user — increases need `--approved`:
```bash
python govern.py update-limits --user "jane@company.com"
python govern.py apply state/plans/update-limits-<ts>.json --approved
```

### A whole tier's limit changes
Edit `limits.toml`, then re-materialize the affected org. Decreases apply
immediately; increases are held for approval:
```bash
python govern.py update-limits --org "IDE Light" --dry-run   # preview
python govern.py update-limits --org "IDE Light"
python govern.py apply state/plans/update-limits-<ts>.json             # decreases now
python govern.py apply state/plans/update-limits-<ts>.json --approved  # ...increases too
```

### A person moves to a different org
`move` diffs membership against the last snapshot and re-resolves movers' limit +
role from their new org. The first run just records a baseline:
```bash
python govern.py move           # first run = baseline
# ...after the move happens in Devin...
python govern.py move
python govern.py apply state/plans/move-<ts>.json [--approved]
```

### A person leaves
`offboard` zeros their limit, removes them from **every** org, and sets the
leaver role. Every change is a revoke/downgrade, so it all auto-applies (no
`--approved` needed):
```bash
python govern.py offboard --user "jane@company.com"
python govern.py apply state/plans/offboard-<ts>.json
```
Dissolving an entire org? Offboard all of its members at once:
```bash
python govern.py offboard --org-dissolved "Old Team"
python govern.py apply state/plans/offboard-<ts>.json
```

---

## 3. How it works

The engine governs two per-user dimensions:

- **Limit** — the per-user Local Agent ACU cycle limit.
- **Enterprise role** — the single, global product-access role (e.g. *IDE only*,
  *CLI and IDE*).

**Desired state** for each user is resolved with this precedence:

1. **`overrides.toml`** — if the user is listed, their pinned values win and they
   are excluded from correction.
2. **Admin-exempt** — if the user's *actual* enterprise role name contains an
   admin keyword (`config.toml [governance].admin_role_name_contains`), they are
   left alone and may belong to many orgs.
3. **Policy** — otherwise the user's single governed org determines limit + role.
4. **Flags** — a non-admin in **0** governed orgs → `no-governed-org`; in **>1** →
   `violation` (the single-org rule).

> An org is **managed** only if it appears in `limits.toml` **or** `roles.toml`.
> To stop managing an org entirely, remove it from **both**.

### Plan → apply (the safety model)

No command mutates directly. Read-only commands report; action commands write a
**plan** to `state/plans/`. You then run `apply`:

- **`apply <plan>`** runs the auto-applicable changes (limit *decreases*, role
  *revokes/downgrades*, org removals).
- **`apply <plan> --approved`** also runs the gated changes (limit *increases*,
  new role grants).
- The gate is **atomic per user**: if any of a user's changes needs approval,
  *none* of that user's changes apply until approved — so a move never lands
  half-done.
- Every applied mutation is appended to `audit.jsonl`; plans double as a resume
  ledger (per-change status), and rate-limited calls are retried.

Add `--dry-run` to any command to simulate without writing anything.

---

## 4. Commands

| Command | What it does |
|---|---|
| `reconcile` | Report drift (actual vs desired) across everyone; save a plan |
| `coverage` | Per-org intended-vs-actual limit & role coverage |
| `usage` | Flag users near/at their cap; emit upgrade candidates |
| `onboard --user USER \| --org NAME` | Set a joiner's limit + role from policy → plan |
| `update-limits --org NAME \| --user USER` | Re-materialize limits after editing `limits.toml` → plan |
| `move` | Detect users who changed orgs since last run → plan |
| `offboard --user USER \| --org-dissolved NAME` | Zero limit + remove from all orgs + leaver role → plan |
| `apply PLAN [--approved]` | Execute a saved plan (gated, audited, resumable) |

`--user` accepts an email or the raw user_id. Global flags (accepted before or
after the command): `--dry-run`, `--config PATH`.

---

## 5. Files

**Policy (source of truth — edit these):**
- `limits.toml` — per-org ACU limit (positive int, or `"null"` for unlimited). *(committed)*
- `roles.toml` — per-org desired enterprise `role_id`. *(git-ignored — copy from `roles.toml.example`)*
- `overrides.toml` — per-user exceptions, keyed by `user_id` (honored, excluded
  from correction; this is where admins are pinned). *(git-ignored — copy from `overrides.toml.example`)*
- `config.toml` — admin detection, leaver role/limit, near-cap thresholds, retry. *(git-ignored — copy from `config.toml.example`)*

> `config.toml`, `roles.toml`, and `overrides.toml` hold tenant-specific IDs / PII,
> so they are git-ignored and committed only as `*.example` templates. Keep your
> real values local.

**Runtime (git-ignored, written by the engine):**
- `audit.jsonl` — append-only audit log (who/what/when/why/triggered-by).
- `state/plans/*.json` — saved plans + resume status.
- `state/membership.json` — last membership snapshot (for `move`).
- `state/usage-candidates.json` — last `usage` output.

---

## 6. Notes & caveats

- **Role changes are conservatively gated.** A real→real enterprise-role change is
  treated as needing approval (we don't yet rank roles, so we can't prove it's a
  downgrade). Offboarding is exempt (always auto). To let genuine downgrades
  auto-apply, add a role rank.
- **Enterprise roles can't be cleared to "none"** via the API — they can only be
  *set* to another role. The normal workflows never try to clear one.
- **IDP-group-derived memberships** can't be removed by `offboard`/`apply`
  (direct org-role assignments only) — manage those via IDP configuration.
- **`move` is reactive** via snapshot diffing; run it on a schedule. The first run
  just records a baseline.
