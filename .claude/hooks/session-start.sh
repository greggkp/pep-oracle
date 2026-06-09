#!/bin/bash
# SessionStart hook: install dependencies so tests and linters work in
# Claude Code on the web sessions. Mirrors the root install in .github/workflows/ci.yml.
set -euo pipefail

# Only run in the remote (web) environment; local sessions manage their own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# uv manages the project venv. Install the root project with the server, aws,
# and dev extras (ruff) plus the default dev dependency-group (pytest, moto, …).
# `uv sync` is idempotent and benefits from the cached container state.
if ! command -v uv >/dev/null 2>&1; then
  python -m pip install --upgrade pip uv
fi

uv sync --extra server --extra aws --extra dev
