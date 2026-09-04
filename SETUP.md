# Developing pep-oracle

pep-oracle is an MCP server and ingestion pipeline designed for AWS and Modal.
The hosted product was fully decommissioned on 2026-09-04; development is now
edit → test, with no active release or deployment path.

## Quickstart

**In a devcontainer / cloud development environment** (Codespaces, coding-agent environments, etc.):
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

> The two `docker build` lines are a CI/deploy check, **not** part of the dev loop:
> they pull the AWS Lambda base image from `public.ecr.aws` (plus Docker Hub), so
> they may fail in network-restricted dev environments. That's fine — CI builds both
> images on every PR. The edit → test → `cdk synth` loop needs no Docker.

Project guidance lives once in `AGENTS.md`; Codex reads it directly and `CLAUDE.md`
imports it for Claude Code.

## Secrets (none needed for the core dev loop)

The edit → `pytest` → `cdk synth` loop needs NO secrets — tests mock every
external API. Set these only to run a subsystem by hand from your dev box (inject
via the platform's secret store as env vars, or `cp .env.example .env`):

| Var | Needed for |
|---|---|
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | running `pep-oracle ingest-artifact` after deliberately recreating the stopped Modal apps and their Hugging Face secret |
| `PEP_ORACLE_PUBLIC_URL` + OAuth/signing block (`PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH` **or** `PEP_ORACLE_AUTHORIZE_GATE=cognito`, `PEP_ORACLE_OAUTH_SIGNING_*`) | running the local MCP server (`pep-oracle-server`) with `/mcp` mounted for hand-testing |

`HF_TOKEN` is **not** a dev var. A restored diarization deployment needs it in a
new Modal Secret named `huggingface-token`, read only inside
`cloud/diarize_modal.py`.

## AWS access for dev

The normal dev loop needs no AWS access. `infra/` is a restoration/design
reference; any future provisioning should use a short-lived, scoped credential
and follow a newly validated runbook rather than the retired deployment workflow.
