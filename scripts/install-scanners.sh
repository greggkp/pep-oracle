#!/usr/bin/env bash
# Install the two CI scanners that .github/workflows/ci.yml runs as marketplace
# actions (gitleaks, trivy) so scripts/ci-local.sh can run the full gate locally.
# Idempotent: re-running with the pinned version already installed is a no-op.
#
# Versions are PINNED here rather than floating, for the same reason ci.yml pins
# its action SHAs: ci-local.sh exists to mirror CI, and a local scanner that
# drifts produces findings CI won't reproduce (or misses ones it will). Bumping
# is a one-line reviewable diff — see "Updating" below.
#
# Distro packages are deliberately not used: Debian stable freezes gitleaks for
# the release's life (trixie ships 8.16.0, from early 2023, which predates the
# `gitleaks git` subcommand), and Aqua's own apt repo trails upstream Trivy by
# months. Both would look maintained while sitting still.
#
# Updating: bump the version, run with --print-checksums to fetch the upstream
# sha256s for the new release, paste them in, commit. Trivy's findings come from
# a vulnerability DB it re-downloads per scan, so its binary can lag safely until
# a DB schema bump; gitleaks compiles its rules in, so keep that one current.
set -euo pipefail

GITLEAKS_VERSION=8.30.1
TRIVY_VERSION=0.74.0

# sha256 of the release tarball, per arch. From each project's published
# <name>_<version>_checksums.txt.
GITLEAKS_SHA256_amd64=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
GITLEAKS_SHA256_arm64=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
TRIVY_SHA256_amd64=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
TRIVY_SHA256_arm64=b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5

BINDIR="${BINDIR:-$HOME/.local/bin}"
FORCE=0

usage() {
  cat <<'USAGE'
Usage: scripts/install-scanners.sh [--force] [--print-checksums]

Installs pinned gitleaks + trivy into $BINDIR (default ~/.local/bin).

  --force             Reinstall even if the pinned version is already present.
  --print-checksums   Fetch and print the upstream sha256s for the pinned
                      versions, in this script's variable format, then exit.
                      Use when bumping a version.

Env:
  BINDIR   install directory (default ~/.local/bin)
USAGE
}

PRINT_CHECKSUMS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force)            FORCE=1 ;;
    --print-checksums)  PRINT_CHECKSUMS=1 ;;
    -h|--help)          usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m) (expected x86_64 or aarch64)" >&2; exit 1 ;;
esac
[ "$(uname -s)" = "Linux" ] || { echo "this script installs the Linux builds; got $(uname -s)" >&2; exit 1; }

# Upstream names the same arch differently per project, hence two mappings.
case "$ARCH" in
  amd64) GITLEAKS_ARCH=x64;   TRIVY_ARCH=64bit ;;
  arm64) GITLEAKS_ARCH=arm64; TRIVY_ARCH=ARM64 ;;
esac

GITLEAKS_URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GITLEAKS_ARCH}.tar.gz"
TRIVY_URL="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TRIVY_ARCH}.tar.gz"

if [ "$PRINT_CHECKSUMS" -eq 1 ]; then
  echo "# gitleaks ${GITLEAKS_VERSION}"
  curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt" \
    | awk '/linux_x64\.tar\.gz$/ {print "GITLEAKS_SHA256_amd64=" $1} /linux_arm64\.tar\.gz$/ {print "GITLEAKS_SHA256_arm64=" $1}'
  echo "# trivy ${TRIVY_VERSION}"
  curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" \
    | awk '/Linux-64bit\.tar\.gz$/ {print "TRIVY_SHA256_amd64=" $1} /Linux-ARM64\.tar\.gz$/ {print "TRIVY_SHA256_arm64=" $1}'
  exit 0
fi

for tool in curl tar sha256sum install; do
  command -v "$tool" >/dev/null 2>&1 || { echo "MISSING: $tool" >&2; exit 1; }
done

mkdir -p "$BINDIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# $1=name $2=url $3=expected sha256 $4=currently-installed version $5=wanted version
fetch_verify_install() {
  local name="$1" url="$2" want="$3" have="$4" want_version="$5" archive got
  if [ "$FORCE" -eq 0 ] && [ "$have" = "$want_version" ]; then
    echo "  $name $want_version already installed — skipping"
    return
  fi
  archive="$tmp/$name.tar.gz"
  echo "  downloading $name $want_version"
  curl -fsSL --retry 3 -o "$archive" "$url"
  got="$(sha256sum "$archive" | cut -d' ' -f1)"
  if [ "$got" != "$want" ]; then
    echo "  CHECKSUM MISMATCH for $name" >&2
    echo "    expected $want" >&2
    echo "    got      $got" >&2
    echo "  Refusing to install. If you just bumped the version, refresh the" >&2
    echo "  pins with: scripts/install-scanners.sh --print-checksums" >&2
    exit 1
  fi
  tar xzf "$archive" -C "$tmp" "$name"
  install -m755 "$tmp/$name" "$BINDIR/$name"
  echo "  installed $name $want_version -> $BINDIR/$name"
}

# Both print their version with different spellings; normalise to a bare semver.
installed_gitleaks="$(gitleaks version 2>/dev/null | tr -d 'v' | head -1 || true)"
installed_trivy="$(trivy --version 2>/dev/null | awk '/^Version:/ {print $2; exit}' || true)"

echo "==> Installing CI scanners into $BINDIR (linux/$ARCH)"
eval "gitleaks_want=\$GITLEAKS_SHA256_$ARCH"
eval "trivy_want=\$TRIVY_SHA256_$ARCH"
fetch_verify_install gitleaks "$GITLEAKS_URL" "$gitleaks_want" "$installed_gitleaks" "$GITLEAKS_VERSION"
fetch_verify_install trivy    "$TRIVY_URL"    "$trivy_want"    "$installed_trivy"    "$TRIVY_VERSION"

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo; echo "  NOTE: $BINDIR is not on your PATH — add it in your shell profile:"
     echo "        export PATH=\"$BINDIR:\$PATH\"" ;;
esac

echo
echo "==> Done. Run the full gate with: bash scripts/ci-local.sh"
