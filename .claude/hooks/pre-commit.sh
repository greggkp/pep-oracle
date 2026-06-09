#!/bin/bash
# Pre-commit hook: runs tests and checks CLAUDE.md review status.
#
# Only fires on `git commit` commands. Blocks the commit if:
# 1. Tests fail
# 2. CLAUDE.md hasn't been reviewed via /claude-md-improver
#
# Workflow: run /claude-md-improver → touch .claude/.md-reviewed →
# stage CLAUDE.md changes → commit (everything in one commit).

set -euo pipefail

# Ensure uv is on PATH (installed to ~/.local/bin by default)
export PATH="$HOME/.local/bin:$PATH"

# Read JSON input from stdin
INPUT=$(cat)

# Only act on git commit commands
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
case "$COMMAND" in
  git\ commit*) ;;
  *) exit 0 ;;
esac

cd "$CLAUDE_PROJECT_DIR"

# --- 1. Run tests ---
TEST_OUTPUT=$(uv run pytest -x -q 2>&1) || {
  echo "Tests failed — fix before committing:" >&2
  echo "$TEST_OUTPUT" >&2
  exit 2
}

# --- 2. Check CLAUDE.md review flag ---
FLAG=".claude/.md-reviewed"
CLAUDE_MD="CLAUDE.md"

if [ ! -f "$FLAG" ]; then
  echo "CLAUDE.md has not been reviewed." >&2
  echo "Run /claude-md-improver, stage any changes, then: touch .claude/.md-reviewed" >&2
  exit 2
fi

if [ "$CLAUDE_MD" -nt "$FLAG" ]; then
  echo "CLAUDE.md was modified after the last review." >&2
  echo "Run /claude-md-improver again, then: touch .claude/.md-reviewed" >&2
  exit 2
fi

# All checks passed — consume the flag so next commit requires a fresh review
rm -f "$FLAG"
exit 0
