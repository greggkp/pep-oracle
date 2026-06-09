# Portable Dev Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a fresh `git clone` of pep-oracle self-sufficient to develop in (devcontainer + `bootstrap.sh` + a committed commit-gate hook + the CDK CLI pin + `SETUP.md`), verified by a clean-room check.

**Architecture:** Repo-only changes. Un-gitignore the bits a fresh clone needs (the shared `.claude/settings.json` + `pre-commit.sh`, `infra/package.json` + lock), add a `.devcontainer/` whose `postCreateCommand` runs `scripts/bootstrap.sh` (which mirrors `ci.yml`'s install steps), document the (now-small) secrets list in `SETUP.md`, and dedup the CDK-CLI version between `ci.yml` and `infra/package.json`.

**Tech Stack:** git, `uv` (Python 3.12), Node 20 + `aws-cdk` CLI (pinned), Docker, `gh`, `jq`, dev-container features.

**Spec:** `docs/superpowers/specs/2026-06-09-portable-dev-env-design.md`

---

## Commit gate (read before executing)

Every `git commit` triggers a Claude Code PreToolUse hook (`.claude/hooks/pre-commit.sh`) that blocks unless **(1)** `uv run pytest -x -q` passes and **(2)** `.claude/.md-reviewed` exists and is newer than `CLAUDE.md`. No task here changes `CLAUDE.md`, so **`touch .claude/.md-reviewed` immediately before each commit** (the hook consumes it on success). Do not use `--no-verify` (it matches `git commit*` regardless).

**Task 1 specifically rewires this hook** — read its hazard note before running it.

## File structure (what each deliverable owns)

| File | Responsibility |
|---|---|
| `.gitignore` | Stop ignoring the shared `.claude` bits + the CDK pin; keep personal/build files ignored |
| `.claude/settings.json` (new, tracked) | The *shared* config: only the `PreToolUse` pre-commit hook wiring |
| `.claude/settings.local.json` (local, untracked) | Personal config: permissions only — hook wiring removed (moved to settings.json) |
| `.claude/hooks/pre-commit.sh` (now tracked) | The pytest + CLAUDE.md-review gate (unchanged content) |
| `infra/package.json` + `infra/package-lock.json` (now tracked) | The single source of the CDK CLI pin (`2.1126.0`) |
| `scripts/bootstrap.sh` (new) | Idempotent project provisioning, mirrors `ci.yml` |
| `.devcontainer/devcontainer.json` (new) | Toolchain via features + `postCreateCommand` → `bootstrap.sh` |
| `SETUP.md` (new) | Quickstart, the small secrets list, AWS-dev-access note, memory-path caveat |
| `.github/workflows/ci.yml` | Install the CDK CLI from `infra/package.json` (kill the inline version literal) |

---

## Task 1: Commit the shared commit-gate hook (gitignore surgery + de-dup the local hook)

Makes the commit gate reproducible on a fresh clone by tracking `.claude/settings.json` + `.claude/hooks/pre-commit.sh`, while keeping the personal `settings.local.json` ignored. Drops the OptiPlex-only `restart-server.sh`.

**HAZARD (read first):** the pre-commit gate is currently wired ONLY in `.claude/settings.local.json` (gitignored). After this task, `.claude/settings.json` (committed) wires it instead. If BOTH files wire the same `PreToolUse` Bash hook at once, it fires TWICE per `git commit`; the first run consumes `.claude/.md-reviewed`, the second finds it gone and FAILS the commit. So this task also REMOVES the hook block from `settings.local.json` (leaving its `permissions`). The settings reload may lag within a running session — if the Task 1 commit double-fires and fails on the flag, just `touch .claude/.md-reviewed` again and re-run the commit.

**Files:**
- Modify: `.gitignore`
- Create: `.claude/settings.json`
- Modify (local, NOT committed — it's gitignored): `.claude/settings.local.json`
- Delete: `.claude/hooks/restart-server.sh`
- Track (already on disk): `.claude/hooks/pre-commit.sh`

- [ ] **Step 1: Flip `.gitignore` from blanket `.claude/` to ignore-personal-only**

In `.gitignore`, replace the single line `.claude/` with:
```gitignore
.claude/*
!.claude/settings.json
!.claude/hooks/
.claude/settings.local.json
.claude/.md-reviewed
.claude/scheduled_tasks.lock
```
(Keep every other `.gitignore` line unchanged. The last two lines keep the personal flag-file + lock ignored even though `.claude/*` is now negated for settings.json/hooks.)

- [ ] **Step 2: Create the shared `.claude/settings.json`**

Create `.claude/settings.json` with ONLY the pre-commit hook (no permissions, no PostToolUse):
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

- [ ] **Step 3: Remove the hook block from the personal `settings.local.json`**

Edit `.claude/settings.local.json` (a local, gitignored file) to DELETE its top-level `"hooks"` key entirely (both the `PreToolUse` pre-commit entry — now in `settings.json` — and the `PostToolUse` restart-server entry — being deleted). KEEP the `"permissions"` key untouched. Use this to verify only `permissions` remains:
```bash
python3 -c "import json; d=json.load(open('.claude/settings.local.json')); print(sorted(d.keys()))"
```
Expected: `['permissions']`

- [ ] **Step 4: Delete the OptiPlex-only restart hook**

```bash
rm -f .claude/hooks/restart-server.sh
```

- [ ] **Step 5: Verify the tracking flips correctly**

```bash
git check-ignore .claude/settings.json .claude/hooks/pre-commit.sh ; echo "rc=$?"
```
Expected: NO output and `rc=1` (neither is ignored anymore).
```bash
git check-ignore .claude/settings.local.json .claude/.md-reviewed ; echo "rc=$?"
```
Expected: both paths printed and `rc=0` (still ignored).
Validate the JSON:
```bash
python3 -c "import json; json.load(open('.claude/settings.json')); print('settings.json OK')"
```
Expected: `settings.json OK`. Confirm `.claude/hooks/pre-commit.sh` is executable: `test -x .claude/hooks/pre-commit.sh && echo EXEC`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS (no code changed; sanity only).

- [ ] **Step 7: Commit (mind the hazard)**

```bash
touch .claude/.md-reviewed
git add .gitignore .claude/settings.json .claude/hooks/pre-commit.sh
git rm -q --cached --ignore-unmatch .claude/hooks/restart-server.sh
git status   # confirm: .gitignore, settings.json, pre-commit.sh staged; settings.local.json NOT staged
git commit -m "build: track shared .claude commit-gate hook for fresh-clone reproducibility"
```
If the commit fails complaining `.claude/.md-reviewed` is missing (a transient double-fire), re-run `touch .claude/.md-reviewed && git commit -m "..."`.

---

## Task 2: Un-gitignore the CDK CLI pin + make `ci.yml` use it

Tracks `infra/package.json` + lock (the `aws-cdk@2.1126.0` pin) so `cd infra && npm ci` reproduces the CLI, and switches CI to install from that pin instead of a duplicated inline literal.

**Files:**
- Modify: `.gitignore`
- Track (already on disk): `infra/package.json`, `infra/package-lock.json`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Stop ignoring the CDK pin files**

In `.gitignore`, DELETE these two lines (keep `infra/node_modules/`, `infra/.venv/`, `infra/cdk.out/`, `infra/cdk.context.json` ignored):
```gitignore
infra/package.json
infra/package-lock.json
```

- [ ] **Step 2: Confirm they're now trackable and contain the pin**

```bash
git check-ignore infra/package.json infra/package-lock.json ; echo "rc=$?"
```
Expected: no output, `rc=1`.
```bash
cat infra/package.json
```
Expected: contains `"aws-cdk": "^2.1126.0"`.

- [ ] **Step 3: Switch `ci.yml`'s CDK install to the pin**

In `.github/workflows/ci.yml`, find the "CDK synth (all stacks)" step:
```yaml
      - name: CDK synth (all stacks)
        run: |
          npm install -g aws-cdk@2.1126.0
          cd infra && cdk synth '*' -c allowed_email=ci@example.com > /dev/null
```
Replace its `run:` block with:
```yaml
      - name: CDK synth (all stacks)
        run: |
          cd infra && npm ci && npx cdk synth '*' -c allowed_email=ci@example.com > /dev/null
```

- [ ] **Step 4: Verify the pinned CLI synthesizes locally**

Run: `cd infra && npm ci && npx cdk synth '*' -c allowed_email=ci@example.com > /dev/null && echo "SYNTH OK"; cd ..`
Expected: `SYNTH OK` (npm installs `aws-cdk` into `infra/node_modules`; `npx cdk` resolves it). If `npm ci` complains the lock is out of sync, run `cd infra && npm install` once to refresh the lock, then re-run `npm ci`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
touch .claude/.md-reviewed
git add .gitignore infra/package.json infra/package-lock.json .github/workflows/ci.yml
git commit -m "build: track CDK CLI pin (infra/package.json) and install it in CI"
```

---

## Task 3: `scripts/bootstrap.sh`

Idempotent provisioning that mirrors `ci.yml`, runnable both by the devcontainer's `postCreateCommand` and by hand on a laptop.

**Files:**
- Create: `scripts/bootstrap.sh`

- [ ] **Step 1: Write the script**

Create `scripts/bootstrap.sh`:
```bash
#!/usr/bin/env bash
# Bootstrap the pep-oracle dev environment after a fresh clone. Idempotent and
# re-runnable. Mirrors .github/workflows/ci.yml's install steps. The devcontainer
# features provide python3.12/node20/docker/gh/jq; run by hand on a laptop those
# must already be present (this script checks and warns).
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

echo "==> Checking system toolchain"
missing=0
for tool in python3 node npm git jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "  MISSING: $tool"; missing=1; }
done
command -v docker >/dev/null 2>&1 || echo "  NOTE: docker not found (needed only for the 'docker build' checks)"

# jq is required by the commit-gate hook; try to install it if we can, else fail.
if ! command -v jq >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    echo "  installing jq via apt-get"; sudo apt-get update -qq && sudo apt-get install -y -qq jq && missing=0
  fi
fi
[ "$missing" -eq 1 ] && { echo "Install the missing tools (or use the devcontainer) and re-run."; exit 1; }

echo "==> Ensuring uv is installed"
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user --upgrade pip uv
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> Installing the root project env (uv)"
uv sync --extra server --extra aws --extra dev

echo "==> Installing the infra (CDK) Python env"
[ -d infra/.venv ] || python3 -m venv infra/.venv
infra/.venv/bin/python -m pip install --quiet --upgrade pip
infra/.venv/bin/python -m pip install --quiet -r infra/requirements.txt

echo "==> Installing the pinned CDK CLI"
( cd infra && npm ci )

cat <<'DONE'
==> Bootstrap complete. Verify with:
    uv run ruff check . && uv run pytest
    cd infra && .venv/bin/python -m pytest && npx cdk synth '*' -c allowed_email=ci@example.com >/dev/null && cd ..
DONE
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/bootstrap.sh
```

- [ ] **Step 3: Run it (idempotency + green)**

Run: `scripts/bootstrap.sh`
Expected: completes without error, prints "Bootstrap complete". Run it a SECOND time:
Run: `scripts/bootstrap.sh`
Expected: completes again without error (idempotent — `uv sync`/`npm ci` are no-ops when satisfied).

- [ ] **Step 4: Confirm the post-bootstrap checks pass**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
touch .claude/.md-reviewed
git add scripts/bootstrap.sh
git commit -m "build: add scripts/bootstrap.sh (mirrors CI install; idempotent)"
```

---

## Task 4: `.devcontainer/devcontainer.json`

A container-based dev env (read by Codespaces and most cloud dev environments, incl. the Claude cloud env) that provisions the toolchain via features and runs `bootstrap.sh`.

**Files:**
- Create: `.devcontainer/devcontainer.json`

- [ ] **Step 1: Write the devcontainer**

Create `.devcontainer/devcontainer.json`:
```json
{
  "name": "pep-oracle",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
  "features": {
    "ghcr.io/devcontainers/features/python:1": { "version": "3.12" },
    "ghcr.io/devcontainers/features/node:1": { "version": "20" },
    "ghcr.io/devcontainers/features/docker-in-docker:2": {},
    "ghcr.io/devcontainers/features/github-cli:1": {}
  },
  "postCreateCommand": "bash scripts/bootstrap.sh",
  "remoteUser": "vscode"
}
```
(`bootstrap.sh` apt-installs `jq` itself when missing — the base image has `sudo` + `apt-get`.)

- [ ] **Step 2: Validate the JSON**

Run: `python3 -c "import json; json.load(open('.devcontainer/devcontainer.json')); print('devcontainer OK')"`
Expected: `devcontainer OK`.

- [ ] **Step 3: Commit**

```bash
touch .claude/.md-reviewed
git add .devcontainer/devcontainer.json
git commit -m "build: add devcontainer (py3.12/node20/docker/gh) running bootstrap.sh"
```

*(Note: the container itself can only be fully exercised in a container-capable environment; the clean-room verification (Task 6) confirms `bootstrap.sh` + the checks from a fresh clone, and the devcontainer is validated end-to-end when the user opens it in the target env.)*

---

## Task 5: `SETUP.md` + verify `.env.example`

**Files:**
- Create: `SETUP.md`
- Verify (likely no change): `.env.example`

- [ ] **Step 1: Confirm `.env.example` already matches the slimmed secrets**

Run: `cat .env.example`
Expected: lists `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`, `PEP_ORACLE_OAUTH_SIGNING_KEY`, `PEP_ORACLE_PUBLIC_URL`, `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH`, and commented Cognito/signing-backend/AWS notes; and does NOT contain `ANTHROPIC_API_KEY`, `PEP_ORACLE_BACKUP_REMOTE`, `OPENAI_API_KEY`, `PEP_ORACLE_EMBED_BACKEND`, or `PEP_ORACLE_SERVE_FROM_ARTIFACT`. If any stale key is present, delete that line. (The CLI+GUI removal already trimmed it, so expect no change.)

- [ ] **Step 2: Write `SETUP.md`**

Create `SETUP.md`:
```markdown
# Developing pep-oracle

pep-oracle is AWS-only: an MCP serving Lambda (corpus artifact + Bedrock + OAuth)
plus a Fargate ingestion job. "Dev" is just edit → test → tag; deploys happen via
CI on a `v*` tag, not from your machine.

## Quickstart

**In a devcontainer / cloud dev env** (Codespaces, the Claude cloud env, etc.):
open the repo — `.devcontainer/devcontainer.json` provisions Python 3.12, Node 20,
Docker, `gh`, and `jq`, then runs `scripts/bootstrap.sh`.

**By hand** (laptop with Python 3.12, Node 20, Docker, `gh`, `jq` already installed):

    git clone git@github.com:greggkp/pep-oracle.git
    cd pep-oracle
    scripts/bootstrap.sh

## Verify the setup

    uv run ruff check . && uv run pytest          # lint + unit tests
    cd infra && .venv/bin/python -m pytest         # infra (CDK) tests
    npx cdk synth '*' -c allowed_email=ci@example.com > /dev/null   # CDK synth (in infra/)
    cd .. && docker build -f Dockerfile . && docker build -f Dockerfile.ingest .

The commit-gate hook (`.claude/hooks/pre-commit.sh`, wired by `.claude/settings.json`)
runs `pytest` + a CLAUDE.md-review check on every `git commit` in a Claude Code session.

## Secrets (none needed for the core dev loop)

The edit → `pytest` → `cdk synth` → tag loop needs NO secrets — tests mock every
external API. Set these only to run a subsystem by hand from your dev box (inject
via the platform's secret store as env vars, or `cp .env.example .env`):

| Var | Needed for |
|---|---|
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | running `pep-oracle ingest-artifact` by hand (Modal transcribe/diarize). Modal also reads `~/.modal.toml`. Prod ingestion is Fargate (pulls these from SSM). |
| `PEP_ORACLE_PUBLIC_URL` + OAuth/signing block (`PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH` **or** `PEP_ORACLE_AUTHORIZE_GATE=cognito`, `PEP_ORACLE_OAUTH_SIGNING_*`) | running the local MCP server (`pep-oracle-server`) with `/mcp` mounted for hand-testing. |

`HF_TOKEN` is **not** a dev var — diarization's Hugging Face token lives in the
Modal Secret `huggingface-token`, read only inside `cloud/diarize_modal.py`.

## AWS access for dev

Dev needs read/inspect + occasional gated ops, **not** `cdk deploy` (CI deploys via
GitHub OIDC). Use a short-lived, scoped credential — do **not** copy the long-lived
`optiplex-cli` keys. (Minting that scoped principal and rotating the optiplex-cli
keys is a separate gated AWS op.)

## Claude Code memory

Claude Code's project memory is path-keyed to `-opt-pep-oracle-app`. Clone to
`/opt/pep-oracle/app` (or copy the memory dir) to keep memory attached; otherwise
expect fresh context per session.
```

- [ ] **Step 3: Commit**

```bash
touch .claude/.md-reviewed
git add SETUP.md
# include .env.example only if Step 1 required a trim:
git add .env.example 2>/dev/null || true
git status   # confirm what's staged
git commit -m "docs: add SETUP.md (bootstrap, slimmed secrets, AWS-dev + memory notes)"
```

---

## Task 6: Clean-room verification

Prove a fresh clone bootstraps and passes the checks. Run from OUTSIDE the working tree (a temp clone) so nothing leaks from the dev box's existing state.

**Files:** none (verification only).

- [ ] **Step 1: Fresh clone of the current branch into a temp dir**

```bash
BR=$(git -C /opt/pep-oracle/app branch --show-current)
rm -rf /tmp/peporacle-cleanroom
git clone --branch "$BR" /opt/pep-oracle/app /tmp/peporacle-cleanroom
cd /tmp/peporacle-cleanroom
```

- [ ] **Step 2: Bootstrap from clean**

Run: `scripts/bootstrap.sh`
Expected: completes, prints "Bootstrap complete". (It re-resolves `uv`/infra/npm in the temp clone.)

- [ ] **Step 3: Run the runnable clean-room checks (1–5)**

```bash
uv run ruff check .
uv run pytest -q
( cd infra && .venv/bin/python -m pytest -q )
( cd infra && npx cdk synth '*' -c allowed_email=ci@example.com > /dev/null && echo "SYNTH OK" )
docker build -f Dockerfile -t peporacle-cleanroom:serve .
docker build -f Dockerfile.ingest -t peporacle-cleanroom:ingest .
```
Expected: ruff clean; pytest all pass; infra pytest pass; `SYNTH OK`; both docker images build.

- [ ] **Step 4: Statically verify the commit gate (check 6)**

The gate is a Claude Code hook, NOT a git hook, so a plain `git commit` here can't fire it. Verify instead that a fresh clone HAS what a Claude Code session needs to load it:
```bash
git ls-files .claude/settings.json .claude/hooks/pre-commit.sh   # both must be tracked
python3 -c "import json; h=json.load(open('.claude/settings.json'))['hooks']['PreToolUse']; assert h[0]['hooks'][0]['command'].endswith('pre-commit.sh'); print('hook wired')"
test -x .claude/hooks/pre-commit.sh && echo "pre-commit.sh executable"
git ls-files .claude/settings.local.json   # must print NOTHING (personal, untracked)
```
Expected: both files listed by `git ls-files`; `hook wired`; `pre-commit.sh executable`; `settings.local.json` prints nothing.

- [ ] **Step 5: Clean up the temp clone**

```bash
cd /opt/pep-oracle/app
rm -rf /tmp/peporacle-cleanroom
docker image rm peporacle-cleanroom:serve peporacle-cleanroom:ingest 2>/dev/null || true
```

- [ ] **Step 6: Push the branch and open the PR**

```bash
git push -u origin "$(git branch --show-current)"
gh pr create --base main --title "Portable dev environment (bootstrap-clean repo)" \
  --body "Makes a fresh clone self-sufficient: devcontainer + scripts/bootstrap.sh, committed .claude commit-gate hook/settings, tracked CDK CLI pin, SETUP.md, CI CDK-install dedup. Clean-room verified (bootstrap + ruff/pytest/infra-pytest/cdk-synth/docker from a temp clone). Spec + plan in docs/superpowers/. Out of scope: OptiPlex decommission + scoped-dev-credential minting."
```

CI (`ci.yml`) is the authoritative remote clean-room (ruff + pytest + infra pytest + the now-pinned `cdk synth` + docker builds, no AWS access).

---

## Self-review notes (coverage vs spec)

- Deliverables table (spec) → Tasks 1–5 implement every row: `.devcontainer/` (T4), `bootstrap.sh` (T3), `.claude/settings.json` + `pre-commit.sh` track + `restart-server.sh` delete (T1), `.gitignore` surgery (T1 for `.claude`, T2 for the CDK pin), `infra/package.json`+lock track (T2), `SETUP.md` (T5), `.env.example` verify (T5), `ci.yml` dedup (T2).
- The `.claude/` ignore-personal-only pattern (spec) → T1 Step 1, plus the double-fire hazard handled by T1 Step 3.
- Bootstrap contract (spec: uv sync / infra venv / npm ci) → T3 Step 1.
- Success criteria's 6 checks (spec) → T6 (checks 1–5 runnable; check 6 static, because the gate is a Claude Code hook not a git hook — noted in the spec's success criteria as "dev-session ergonomics CI does not exercise").
- "No tag push" in clean-room (spec) → T6 verifies without tagging; the PR's CI is the remote gate.
- Out-of-scope items (spec) → not implemented here (OptiPlex decommission, dev-credential minting).
