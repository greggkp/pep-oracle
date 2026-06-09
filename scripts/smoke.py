"""Auth-free post-deploy smoke test for the live pep-oracle endpoint (Phase 4).

Run:  python scripts/smoke.py
Env:
  PEP_ORACLE_SMOKE_URL  base URL (default https://pep-oracle.iicapn.com)
  EXPECT_SHA            if set, /version code_git_sha must equal it
  EXPECT_SEMVER         if set, /version code_semver must equal it
Exits non-zero on any failed check (fails the CI release). Retries ~2min so a
cold start after deploy has time to serve the new image — a network/timeout
error is treated as a retryable failure (status 0), not a crash."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError:
        # Timeout / connection error (e.g. a Lambda cold start exceeding the read
        # timeout) — return a retryable sentinel so the loop retries, not crashes.
        # (TimeoutError, socket.timeout, urllib URLError all subclass OSError.)
        return 0, b""


def _post_no_token(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def check(base: str, expect_sha: str = "", expect_semver: str = "") -> list[str]:
    """Return a list of failure messages (empty list = all checks passed)."""
    base = base.rstrip("/")
    failures: list[str] = []

    status, _ = _get(f"{base}/health")
    if status != 200:
        failures.append(f"/health -> {status} (want 200)")

    status, body = _get(f"{base}/version")
    if status != 200:
        failures.append(f"/version -> {status} (want 200)")
    else:
        try:
            data = json.loads(body or b"{}")
        except (json.JSONDecodeError, ValueError):
            data = None
        if data is None:
            failures.append("/version returned a non-JSON body")
        else:
            if expect_sha and data.get("code_git_sha") != expect_sha:
                failures.append(f"/version code_git_sha={data.get('code_git_sha')} (want {expect_sha})")
            if expect_semver and data.get("code_semver") != expect_semver:
                failures.append(f"/version code_semver={data.get('code_semver')} (want {expect_semver})")
            if not data.get("corpus_version"):
                failures.append("/version missing corpus_version")

    status, _ = _get(f"{base}/.well-known/oauth-authorization-server")
    if status != 200:
        failures.append(f"/.well-known/oauth-authorization-server -> {status} (want 200)")

    status = _post_no_token(f"{base}/mcp")
    if status != 401:
        failures.append(f"/mcp no-token -> {status} (want 401)")

    return failures


def main() -> int:
    base = os.getenv("PEP_ORACLE_SMOKE_URL", "https://pep-oracle.iicapn.com")
    expect_sha = os.getenv("EXPECT_SHA", "")
    expect_semver = os.getenv("EXPECT_SEMVER", "")
    waited, deadline = 0.0, 120.0
    while True:
        failures = check(base, expect_sha, expect_semver)
        if not failures:
            print(f"smoke OK: {base}")
            return 0
        if waited >= deadline:
            print(f"smoke FAILED for {base}:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            return 1
        time.sleep(10.0)
        waited += 10.0


if __name__ == "__main__":
    raise SystemExit(main())
