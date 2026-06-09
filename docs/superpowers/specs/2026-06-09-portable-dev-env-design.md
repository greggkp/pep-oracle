# Portable dev environment — design spec

**Date:** 2026-06-09 (reworked after the CLI+GUI removal landed)
**Status:** Reworked against the AWS-only tree; ready for implementation plan
**Related:** `project_portable_dev_env` memory, `2026-06-09-cli-gui-removal-design.md`, `2026-06-02-aws-mcp-migration-design.md`

## Goal

Make a fresh `git clone` of `greggkp/pep-oracle` fully self-sufficient to develop
in, with no OptiPlex-local files required. After the AWS MCP migration **and the
CLI+GUI removal** (the tree is now AWS-only — MCP serving Lambda + Fargate
ingestion; `v1.1.0` live), the OptiPlex's only remaining roles are (1) the dev
box where Claude Code runs and (2) a passive DNS-rollback fallback. Deploys now
happen via CI/GitHub-OIDC on a `v*` tag, not from this box — so "dev" is just
edit → test → tag, which is portable to any container-based environment (the
Claude cloud dev environment, GitHub Codespaces, or a fresh laptop).

This spec makes the repo bootstrap-clean. It is **repo-changes only** — no live
AWS mutation. The OptiPlex decommission and the scoped-dev-credential minting are
sequenced *after* this work and tracked as separate gated tasks.

**Rework delta (post-excision):** the bootstrap-clean *design* is unchanged from
the originally-approved version; the AWS-only slim-down only shrinks the edges —
the secrets list is much smaller (no `ANTHROPIC_API_KEY`/`HF_TOKEN`/backup-remote),
`.env.example` was already trimmed by the removal, the clean-room test suite no
longer includes web/Playwright tests (so the devcontainer needs no browser
tooling), and the current release is `v1.1.0`.

## Success criteria

The work is done when a **clean-room verification** passes: a fresh `git clone`
into a clean container, then `scripts/bootstrap.sh`, then all of the following
green:

1. `uv run ruff check .`
2. `uv run pytest`
3. `cd infra && python -m pytest`
4. `cd infra && npx cdk synth '*' -c allowed_email=ci@example.com`
5. `docker build -f Dockerfile .` and `docker build -f Dockerfile.ingest .`
6. The commit-gate hook fires correctly on a test `git commit` (blocks on test
   failure / unreviewed CLAUDE.md; passes otherwise).

These mirror `ci.yml` (which is already a working clean-room for checks 1–5 from
a fresh checkout with no AWS access) plus the dev-session ergonomics (check 6)
that CI does not exercise.

**No tag push.** The handoff mentioned verifying with "a throwaway tag," but
pushing a real `v*` tag triggers `deploy.yml` → a real prod deploy. The tag/
release path is already proven by the live `v1.0.0` release, so it is excluded
from clean-room verification.

## Background: why these specific files

The blockers to a clean clone are files that are currently gitignored or
OptiPlex-local:

- **The entire `.claude/` directory is gitignored** — so the commit-gate hook
  (`pre-commit.sh`), the OptiPlex-only `restart-server.sh`, and the
  `settings.local.json` that wires them are all local-only. A fresh session has
  no commit gate.
- **`infra/package.json` + lock are gitignored** — they pin the CDK CLI
  (`2.1126.0`). Without them `cd infra && npm install` can't reproduce the CLI.
- **No `SETUP.md`, no committed `.claude/settings.json`** — a newcomer (human or
  fresh Claude session) has no documented bootstrap path or secrets list.
- **`.env` is an OptiPlex symlink** → `/opt/pep-oracle/.env`. On a fresh clone it
  is absent; that is fine (`python-dotenv`'s `load_dotenv()` no-ops when the file
  is missing, and platform-injected env vars take precedence), but it must be
  documented.

CI already proves the test + synth + docker path works from a clean checkout with
no AWS creds and **no `cdk.context.json`** (it's gitignored and absent in CI, yet
`cdk synth` passes). Therefore `cdk.context.json` stays gitignored — synth only
needs `-c allowed_email=...` because `cdk.json` ships a `REPLACE_ME` placeholder.

## Deliverables

| File | Action | Purpose |
|---|---|---|
| `.devcontainer/devcontainer.json` | new | Base image + features (python 3.12, node 20, docker-in-docker, gh, jq); `postCreateCommand` → `scripts/bootstrap.sh` |
| `scripts/bootstrap.sh` | new | Project provisioning, mirrors `ci.yml` (see Bootstrap contract) |
| `.claude/settings.json` | new (tracked) | Only the `PreToolUse` pre-commit-hook wiring — no permissions, no `PostToolUse` |
| `.claude/hooks/pre-commit.sh` | track existing | The pytest + claude-md-review gate (unchanged) |
| `.claude/hooks/restart-server.sh` | delete | OptiPlex-only (restarts local systemd service) — not for a dev box |
| `.gitignore` | edit | Un-ignore the tracked `.claude` bits + `infra/package.json` + lock |
| `infra/package.json` + `infra/package-lock.json` | track existing | CDK CLI pin `2.1126.0` as single source of truth |
| `SETUP.md` | new | Bootstrap steps, secrets/env-var list, AWS-dev-access note, memory-path caveat |
| `.env.example` | verify/trim | Already trimmed by the CLI+GUI removal (Task 9); just confirm it matches the slimmed SETUP secrets list |
| `.github/workflows/ci.yml` | edit (small) | Install the CDK CLI from `infra/package.json` (`cd infra && npm ci && npx cdk synth`) instead of the inline `npm install -g aws-cdk@2.1126.0` literal, killing the version duplication |

## The `.claude/` split (gitignore surgery)

`.gitignore` changes from a blanket `.claude/` ignore to ignore-personal-only:

```gitignore
.claude/*
!.claude/settings.json
!.claude/hooks/
.claude/settings.local.json
```

Result: `.claude/settings.json` (shared hook wiring) and `.claude/hooks/`
(`pre-commit.sh`) become tracked; `.claude/settings.local.json` (the personal
permission allowlist) stays personal/untracked. This is the idiomatic Claude Code
split — `settings.json` is meant to be committed and shared; `settings.local.json`
is per-user.

The committed `.claude/settings.json` contains **only** the `PreToolUse` hook:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/pre-commit.sh",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

No `PostToolUse` restart hook (OptiPlex-only) and no `permissions` block (those
remain in each user's `settings.local.json`).

## Bootstrap contract (`scripts/bootstrap.sh`)

Idempotent and re-runnable. Assumes the devcontainer features already provided the
system toolchain (python 3.12, node 20, docker, gh, jq); it **checks** for each
and warns rather than assuming `sudo`, so it also works run-by-hand on a laptop
where those are already present. Then it does the project-specific install,
mirroring `ci.yml`:

1. `python -m pip install --upgrade pip uv` (if `uv` absent)
2. `uv sync --extra server --extra aws --extra dev` — root env (the
   `[dependency-groups].dev` test deps are included by `uv sync` automatically)
3. `infra/`: create `infra/.venv` and `pip install -r infra/requirements.txt`
   (matches the local pattern `cd infra && .venv/bin/python -m pytest`)
4. `cd infra && npm ci` (or `npm install`) — installs the CDK CLI locally into
   `infra/node_modules` from the now-tracked `infra/package.json` + lock pin;
   invoke it as `npx cdk` (so `package.json` is the genuine single source — a
   global `npm install -g aws-cdk@<version>` would ignore it)

System dependencies the hook needs at runtime: `jq` and `uv` on `PATH` (the hook
exports `~/.local/bin` already).

## `SETUP.md` contents

- **Quickstart:** clone → open in the devcontainer (or run `scripts/bootstrap.sh`
  by hand) → run the six verification checks.
- **Secrets / env-var table.** The core dev loop (edit → `uv run pytest` →
  `cdk synth` → tag) needs **none** of these — tests mock all external APIs. They
  are only for running a given subsystem by hand from the dev box:
  - `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — only to run `pep-oracle
    ingest-artifact` by hand (Modal transcribe/diarize). Modal also reads
    `~/.modal.toml`, so these may be unset there; prod ingestion is Fargate (pulls
    them from SSM). `HF_TOKEN` is **not** a dev var — diarization's HF token lives
    in the Modal Secret `huggingface-token`, read only inside `cloud/diarize_modal.py`.
  - `PEP_ORACLE_PUBLIC_URL` + the OAuth/signing block
    (`PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH` **or** `PEP_ORACLE_AUTHORIZE_GATE=cognito`
    + `PEP_ORACLE_OAUTH_SIGNING_*`) — only to run the local MCP server
    (`pep-oracle-server`) with `/mcp` mounted for hand-testing.
  - Inject via the platform's secret store as env vars (no `.env` file required —
    `load_dotenv()` no-ops when absent and injected env vars take precedence), or
    `cp .env.example .env` for a laptop.
  - **Dropped since the original spec** (the removal deleted their consumers):
    `ANTHROPIC_API_KEY` (the `/ask` Claude path), `PEP_ORACLE_BACKUP_REMOTE`
    (`backup`), `OPENAI_API_KEY` (embeddings are Bedrock-only). `.env.example`
    already reflects this, so its deliverable below is now near-trivial.
- **AWS dev access (documented, not minted):** dev needs read/inspect + occasional
  gated ops, **not** `cdk deploy` (CI deploys via OIDC). Obtain a short-lived,
  scoped credential; do **not** copy the long-lived `optiplex-cli` keys. Minting
  the scoped principal and rotating the `optiplex-cli` keys is a deferred gated
  AWS op, out of scope here.
- **Memory-path caveat:** Claude Code memory is path-keyed to `-opt-pep-oracle-app`.
  Clone to `/opt/pep-oracle/app` (or copy the memory dir) to keep memory attached;
  otherwise expect fresh context per session.

## Out of scope (sequenced after)

- **OptiPlex decommission** — stop/disable `pep-oracle-api.service` + cloudflared,
  drop local `~/.pep-oracle` ChromaDB. Separate gated task; only after portable
  dev is verified working elsewhere, so a working box remains until then.
- **Minting the scoped dev AWS credential** + rotating `optiplex-cli` keys —
  deferred gated AWS op.
- **Unrelated migration items** — Phase 5 KMS asymmetric JWT signing; corpus gap
  179–216 + EXTRAs backfill.

## Risks / things to confirm during implementation

- **Devcontainer feature availability in the Claude cloud env.** The
  `devcontainer.json` is authored to the de-facto standard (Codespaces-compatible
  features), but the Claude cloud environment's exact handling of `.devcontainer/`
  can't be introspected from a session. `scripts/bootstrap.sh` is the fallback —
  it runs by hand regardless of whether the platform auto-reads the devcontainer.
- **`ci.yml` install-source change.** Switching CI from `npm install -g
  aws-cdk@2.1126.0` to `cd infra && npm ci && npx cdk synth ...` must stay
  CI-green; confirm `npx` resolves the locally-installed CLI and the synth output
  is unchanged.
- **`docker build` in the devcontainer** requires docker-in-docker (a feature) or
  a mounted socket; clean-room verification must confirm it works in the target
  environment.
