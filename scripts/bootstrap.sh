#!/usr/bin/env bash
# Bootstrap the pep-oracle dev environment after a fresh clone. Idempotent and
# re-runnable. Mirrors .github/workflows/ci.yml's install steps. The devcontainer
# features provide python3.12/node20/docker/gh/jq; run by hand on a laptop those
# must already be present (this script checks and warns).
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

echo "==> Checking system toolchain"
missing=0
for tool in python3 node npm git; do
  command -v "$tool" >/dev/null 2>&1 || { echo "  MISSING: $tool"; missing=1; }
done
command -v docker >/dev/null 2>&1 || echo "  NOTE: docker not found (needed only for the 'docker build' checks)"

# jq is used by project scripts; try to install it if we can, else fail. Handled
# outside the loop above because it is the one tool we self-heal: the verdict has
# to come from re-checking jq AFTER the install, and a blanket `missing=0` here
# would clear the flag for every other missing tool too (installing jq would mask
# an absent npm, turning a clean preflight message into a confusing `npm ci`).
if ! command -v jq >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    echo "  installing jq via apt-get"; sudo apt-get update -qq && sudo apt-get install -y -qq jq
  fi
  command -v jq >/dev/null 2>&1 || { echo "  MISSING: jq"; missing=1; }
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
# Validate, don't just test for the directory: an interrupted or --without-pip
# `python3 -m venv` leaves a venv with no pip behind, and a `[ -d ... ]` guard
# then skips recreation forever, so every later run dies on "No module named pip".
if ! infra/.venv/bin/python -m pip --version >/dev/null 2>&1; then
  [ -d infra/.venv ] && echo "  infra/.venv is unusable (no working pip) — recreating"
  python3 -m venv --clear infra/.venv
fi
infra/.venv/bin/python -m pip install --quiet --upgrade pip
infra/.venv/bin/python -m pip install --quiet -r infra/requirements.txt

echo "==> Installing the pinned CDK CLI"
( cd infra && npm ci )

# Point git at the tracked hook dir so the pre-push gate (scripts/ci-local.sh)
# is version-controlled rather than a per-clone .git/hooks copy that silently
# rots. Respect an existing custom hooksPath instead of stomping it.
echo "==> Wiring git hooks (.githooks)"
current_hooks="$(git config --local core.hooksPath || true)"
if [ -z "$current_hooks" ]; then
  git config --local core.hooksPath .githooks
  echo "  core.hooksPath -> .githooks (pre-push runs scripts/ci-local.sh)"
elif [ "$current_hooks" = ".githooks" ]; then
  echo "  core.hooksPath already .githooks"
else
  echo "  NOTE: core.hooksPath is '$current_hooks' — leaving it; .githooks/pre-push not active"
fi

cat <<'DONE'
==> Bootstrap complete. Verify with:
    uv run ruff check . && uv run pytest
    ( cd infra && .venv/bin/python -m pytest && PATH="$PWD/.venv/bin:$PATH" npx cdk synth '*' -c allowed_email=ci@example.com >/dev/null )

    ...or the whole CI gate in one command (needs gitleaks + trivy — see
    scripts/install-scanners.sh):
    bash scripts/ci-local.sh

    That gate also runs automatically on `git push` (.githooks/pre-push).
    Bypass a single push with: git push --no-verify
DONE
