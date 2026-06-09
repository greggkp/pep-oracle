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
