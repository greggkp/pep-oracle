#!/usr/bin/env bash
# Run the full CI gate (.github/workflows/ci.yml) on a laptop, in the same order.
# Mirrors every step including the two that CI runs as marketplace actions
# (gitleaks, trivy) — install those CLIs with scripts/install-scanners.sh.
#
# Unlike CI this does NOT stop at the first failure: it runs every step and
# prints a summary, so one `ruff format` nit doesn't hide a Trivy finding three
# steps later. Pass --fail-fast for CI's stop-on-first-failure behaviour.
#
# Assumes scripts/bootstrap.sh has already been run (root .venv, infra/.venv,
# infra/node_modules). Exits non-zero if any step failed.
#
# NOTE Trivy takes its verdict from a live vulnerability database, so a local
# pass can legitimately disagree with a CI run from a different day. With the
# repository decommissioned there is no scheduled CI; run this gate or dispatch
# ci.yml manually when a current vulnerability verdict is needed.
set -uo pipefail   # deliberately not -e: run_step handles each step's exit code

cd "$(dirname "$0")/.."   # repo root

FAIL_FAST=0
RUN_DOCKER=1

usage() {
  cat <<'USAGE'
Usage: scripts/ci-local.sh [--fail-fast] [--no-docker]

  --fail-fast   Stop at the first failing step (matches CI).
  --no-docker   Skip the docker builds and both Trivy scans (the slow half).
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --fail-fast) FAIL_FAST=1 ;;
    --no-docker) RUN_DOCKER=0 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

echo "==> Preflight"
missing=0
tools=(uv node npm gitleaks)
[ "$RUN_DOCKER" -eq 1 ] && tools+=(docker trivy)
for tool in "${tools[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "  MISSING: $tool"; missing=1; }
done

for path in .venv infra/.venv infra/node_modules; do
  [ -e "$path" ] || { echo "  MISSING: $path (run scripts/bootstrap.sh)"; missing=1; }
done

if [ "$RUN_DOCKER" -eq 1 ] && command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 || {
    echo "  Docker daemon unreachable — is the service up, and is \$USER in the 'docker' group?"
    missing=1
  }
fi

if [ "$missing" -eq 1 ]; then
  echo "Install the missing pieces and re-run:"
  echo "    scripts/bootstrap.sh          # venvs + node_modules"
  echo "    scripts/install-scanners.sh   # pinned gitleaks + trivy"
  exit 1
fi

# Soft check only: the project floor is >=3.11, so uv will happily build a venv on
# a newer interpreter than the 3.12 CI pins — and ruff/mypy verdicts can differ by
# Python version. Warn rather than fail; it isn't wrong, just not what CI ran.
pyver="$(uv run python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
[ "$pyver" = "3.12" ] || echo "  NOTE: project venv is Python ${pyver:-unknown}; CI uses 3.12 (uv python install 3.12 && uv sync -p 3.12 …)"

names=(); states=(); durations=(); failed=0

run_step() {
  local name="$1" fn="$2" start elapsed state
  if [ "$failed" -ne 0 ] && [ "$FAIL_FAST" -eq 1 ]; then
    names+=("$name"); states+=("SKIP"); durations+=("-")
    return
  fi
  printf '\n==> %s\n' "$name"
  start=$SECONDS
  if "$fn"; then state="PASS"; else state="FAIL"; failed=1; fi
  elapsed=$(( SECONDS - start ))
  names+=("$name"); states+=("$state"); durations+=("${elapsed}s")
}

step_gitleaks() {
  # CI runs gitleaks-action, which scans committed history. `gitleaks git` is the
  # v8.19+ spelling of the old `detect`; use whichever this install understands.
  # (For uncommitted work in the worktree, run `gitleaks dir .` yourself.)
  if gitleaks git --help >/dev/null 2>&1; then
    gitleaks git . --redact
  else
    gitleaks detect --source . --redact
  fi
}

step_ruff_check()  { uv run ruff check .; }
step_ruff_format() { uv run ruff format --check .; }
step_mypy()        { uv run mypy src; }
step_pip_audit()   { uv run pip-audit; }
step_pytest_root() { uv run pytest -q; }   # -m 'not live' --ignore=infra come from pyproject addopts

# CI installs infra deps into the runner's system python; locally bootstrap.sh
# isolates them in infra/.venv (which is also what AGENTS.md documents).
step_pytest_infra() { ( cd infra && .venv/bin/python -m pytest -q ); }
# cdk.json runs `python app.py`. CI gets a bare `python` (with aws-cdk-lib on it)
# from setup-python; locally the deps live only in infra/.venv and a Debian box
# has just `python3` — so put the venv first on PATH and let cdk find its own
# interpreter there. Without this the synth dies with `python: not found` (127).
step_cdk_synth() {
  ( cd infra && PATH="$PWD/.venv/bin:$PATH" npx cdk synth '*' -c allowed_email=ci@example.com >/dev/null )
}

step_docker_build() {
  docker build -f Dockerfile -t pep-oracle:ci . \
    && docker build -f Dockerfile.ingest -t pep-oracle-ingest:ci .
}

# Same knobs trivy-action is given in ci.yml. Everything else stays at Trivy's
# defaults so the local scanner set matches the action's. --vuln-type was renamed
# --pkg-types in newer Trivy; pick whichever this build exposes.
trivy_scan() {
  local pkg_flag=--vuln-type
  trivy image --help 2>&1 | grep -q -- '--pkg-types' && pkg_flag=--pkg-types
  trivy image \
    --format table \
    --exit-code 1 \
    --ignore-unfixed \
    "$pkg_flag" os,library \
    --severity CRITICAL,HIGH \
    "$1"
}

step_trivy_serving() { trivy_scan pep-oracle:ci; }
step_trivy_ingest()  { trivy_scan pep-oracle-ingest:ci; }

run_step "Secret Scan (Gitleaks)"     step_gitleaks
run_step "Ruff Check"                 step_ruff_check
run_step "Ruff Format Check"          step_ruff_format
run_step "Mypy"                       step_mypy
run_step "Python dependency audit"    step_pip_audit
run_step "Pytest (root)"              step_pytest_root
run_step "Pytest (infra)"             step_pytest_infra
run_step "CDK synth (all stacks)"     step_cdk_synth
if [ "$RUN_DOCKER" -eq 1 ]; then
  run_step "Docker build (both images)" step_docker_build
  run_step "Trivy (serving image)"      step_trivy_serving
  run_step "Trivy (ingest image)"       step_trivy_ingest
fi

echo
echo "==> Summary"
for i in "${!names[@]}"; do
  printf '  %-6s %-28s %s\n' "${states[$i]}" "${names[$i]}" "${durations[$i]}"
done

if [ "$failed" -ne 0 ]; then
  echo
  echo "CI gate FAILED."
  exit 1
fi

echo
if [ "$RUN_DOCKER" -eq 1 ]; then
  echo "CI gate passed."
else
  echo "CI gate passed (docker builds + Trivy skipped — CI still runs them)."
fi
