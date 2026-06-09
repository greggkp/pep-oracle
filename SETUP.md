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
