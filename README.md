# Devin Enterprise Governance

A small CLI that keeps every enterprise user's **Local Agent ACU limit** and
**enterprise role** in line with policy files — with a diff-first plan/apply
workflow, an approval gate, and a full audit log.

Standard library only (Python 3.11+) — the single optional dependency is
`openpyxl`, needed only to read Excel (`.xlsx`) onboarding rosters (CSV rosters
need nothing extra).

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

Onboarding from an **Excel** roster needs `openpyxl` (CSV needs nothing):

```bash
pip install -r requirements.txt   # only required for .xlsx rosters
```

`.env` needs a service-user token and (for dedicated deployments) the API host:

```
DEVIN_SERVICE_USER_TOKEN=cog_xxx
DEVIN_API_BASE_URL=https://<company>.devinenterprise.com/api
```

The token's service user needs these enterprise permissions:
`ViewAccountMembership` (reads), `ManageBilling` (limits),
`ManageAccountMembership` (invites + roles + org add/remove).

Run everything via the entrypoint:

```bash
python govern.py <command> [--dry-run]
python govern.py --help
```

---

## 2. Usage — a member's lifecycle

Every **action** command is diff-first: it writes a plan to `state/plans/` and
changes nothing until you `apply` it — so each step below pairs a command with
the `apply` that materializes it. **Read-only** commands just *report*
(`reconcile` also writes a plan you can apply). Add `--dry-run` to simulate any
command, and pass `--user` as **either an email or the raw user_id**. See *How it
works* (next section) for the approval gate.

The four commands below cover the everyday lifecycle — **onboard → reconcile →
usage → offboard**. See *Commands* (section 4) for the full set, including
`coverage`, `update-limits`, `move`, and `reassign`.

### New people join → `onboard`
`onboard` **invites** users from a roster file (CSV or `.xlsx`) and materializes
each one from policy: it creates the user with their org's enterprise role
(`roles.toml`), adds them to that org, and sets the org's ACU limit
(`limits.toml`). Anyone who already exists is reconciled, not re-invited.

The roster has a header row and one or two columns: an **email** column
(required) plus a **group / organization name** column (optional). With two
columns the email and org columns are auto-detected; with one column you pick a
single target org from an arrow-key menu. Anything malformed — more than two
columns, an invalid email, an unknown/ungoverned org — fails up front, before
anyone is invited. (If the first row already looks like data rather than labels,
it's kept as a data row with a warning.)

Invites and grants are gated, so `apply` needs `--approved`:
```bash
python govern.py onboard --file roster.csv
python govern.py apply state/plans/onboard-<ts>.json --approved
```

Example two-column roster:
```csv
email,group
jane@company.com,IDE Standard
raj@company.com,CLI IDE Super
```
New members receive a per-org role on join — set `[invite].org_role_id` (or
`org_role_name`) in `config.toml` (run `onboard` once to see the available org
roles if unsure).

### Move people to a new org → `reassign`
`reassign` is onboard's sibling for **existing** members: it bulk-**moves** people
to a new org from the same kind of roster. For each row it adds the member to the
destination org, sets that org's enterprise role (`roles.toml`) and ACU limit
(`limits.toml`), then removes them from their other governed orgs (ungoverned
memberships are left alone). Every email must already be an enterprise user —
unknown emails fail up front (use `onboard` to invite new ones). This is the
proactive, file-driven counterpart to `move`, which instead *detects* org changes
that already happened.

The roster is the same shape as onboard's: two columns (email + **destination**
group), or one column (email only) where you pick a single destination from an
arrow-key menu. The org add and any role/limit increase are gated, so `apply`
needs `--approved`; the org removals and limit decreases auto-apply:
```bash
python govern.py reassign --file moves.csv
python govern.py apply state/plans/reassign-<ts>.json --approved
```
Example two-column roster:
```csv
email,group
jane@company.com,IDE Super
raj@company.com,CLI IDE Standard
```

### Check & fix drift → `reconcile`
`reconcile` reports drift (actual vs desired) across **everyone** and writes a
plan; `apply` it to bring the population back in line. Decreases/revokes apply
immediately; increases/grants are held for `--approved`:
```bash
python govern.py reconcile                                         # report drift + write a plan
python govern.py apply state/plans/reconcile-<ts>.json             # decreases/revokes now
python govern.py apply state/plans/reconcile-<ts>.json --approved  # ...increases/grants too
```

### Someone's hitting their cap → `usage`
`usage` flags anyone near/at their limit (detection only — it writes no plan). To
raise someone, bump their tier (move them to a higher org, or edit `limits.toml`
/ `overrides.toml`), then `reconcile` to materialize the increase:
```bash
python govern.py usage                                             # flag near/at-cap users
# ...raise their org/limit in policy, then:
python govern.py reconcile
python govern.py apply state/plans/reconcile-<ts>.json --approved  # the increase needs --approved
```

### A person leaves → `offboard`
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
- **`apply` (no plan)** applies *every* outstanding plan in `state/plans/`. It
  lists them and asks once (`[y/N]` — any non-`y` answer aborts) before running;
  `--approved` still applies to all of them.
- The gate is **atomic per user**: if any of a user's changes needs approval,
  *none* of that user's changes apply until approved — so a move never lands
  half-done.
- Every applied mutation is appended to `audit.jsonl`; plans double as a resume
  ledger (per-change status), and rate-limited calls are retried.
- A plan is **archived to `state/plans/archive/`** once *every* change has landed
  (so it no longer counts as outstanding). A plan with held/failed changes stays
  put so you can resume it — e.g. run `apply <plan>` for the decreases, then
  `apply <plan> --approved` for the increases; it's archived after the second
  run. `--dry-run` never archives.
- When an `--approved` run will actually **invite** members, `apply` brackets the
  run with two mandatory prompts: first to **uncheck** *Settings > Enterprise >
  General > "Require SSO for member access"* (invites only work while it's off),
  then to **re-check** it afterwards. Each waits for a `y`/`Y` keypress — any
  other key is ignored and the prompt stays put. Plans with no invites (and
  `--dry-run`) skip this.

Add `--dry-run` to any command to simulate without writing anything.

---

## 4. Commands

| Command | What it does |
|---|---|
| `reconcile` | Report drift (actual vs desired) across everyone; save a plan |
| `coverage` | Per-org intended-vs-actual limit & role coverage |
| `usage` | Flag users near/at their cap; emit upgrade candidates |
| `onboard --file PATH` | Invite users from a CSV/`.xlsx` roster; add to org + set role + limit → plan |
| `update-limits --org NAME \| --user USER` | Re-materialize limits after editing `limits.toml` → plan |
| `move` | Detect users who changed orgs since last run → plan |
| `reassign --file PATH` | Bulk-move existing members to a new org from a CSV/`.xlsx` roster: add to destination + set role/limit, remove from old governed org → plan |
| `offboard --user USER \| --org-dissolved NAME` | Zero limit + remove from all orgs + leaver role → plan |
| `apply [PLAN] [--approved]` | Execute a saved plan (gated, audited, resumable); with no `PLAN`, apply all outstanding plans after a y/N confirm |

`--user` accepts an email or the raw user_id. `onboard --file` and
`reassign --file` accept a `.csv` or `.xlsx` roster. Global flags (accepted before
or after the command): `--dry-run`, `--config PATH`.

---

## 5. Files

**Policy (source of truth — edit these):**
- `limits.toml` — per-org ACU limit (positive int, or `"null"` for unlimited). *(committed)*
- `roles.toml` — per-org desired enterprise `role_id`. *(git-ignored — copy from `roles.toml.example`)*
- `overrides.toml` — per-user exceptions, keyed by `user_id` (honored, excluded
  from correction; this is where admins are pinned). *(git-ignored — copy from `overrides.toml.example`)*
- `config.toml` — admin detection, leaver role/limit, near-cap thresholds, retry,
  invite org-role (`[invite]`). *(git-ignored — copy from `config.toml.example`)*

> `config.toml`, `roles.toml`, and `overrides.toml` hold tenant-specific IDs / PII,
> so they are git-ignored and committed only as `*.example` templates. Keep your
> real values local.

**Runtime (git-ignored, written by the engine):**
- `audit.jsonl` — append-only audit log (who/what/when/why/triggered-by).
- `state/plans/*.json` — saved plans + resume status (outstanding plans).
- `state/plans/archive/*.json` — plans retired here once fully applied.
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
