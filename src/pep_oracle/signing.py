"""Pluggable OAuth signing-key backend (Phase 2b2).

Resolves the HS256 secret used to sign/verify access-token JWTs. Two backends
behind one ``resolve_signing_key()`` entry point, selected by config:
  - "local" (default): env ``PEP_ORACLE_OAUTH_SIGNING_KEY`` -> ``$DATA_DIR/oauth_signing_key``
    -> a freshly generated key written 0600. Unchanged OptiPlex/dev behavior.
  - "ssm": a KMS-encrypted SecureString from SSM Parameter Store (the Lambda path).
    Fail-closed -- a missing/empty parameter raises rather than silently generating
    a key that would mismatch every previously issued token.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pep_oracle import config

logger = logging.getLogger(__name__)


def resolve_signing_key() -> str:
    backend = config.OAUTH_SIGNING_BACKEND
    if backend == "ssm":
        return _resolve_ssm(config.OAUTH_SIGNING_SSM_PARAM, config.OAUTH_SIGNING_SSM_REGION)
    if backend == "local":
        return _resolve_local()
    raise ValueError(f"unknown PEP_ORACLE_OAUTH_SIGNING_BACKEND: {backend!r}")


def _resolve_local() -> str:
    """env PEP_ORACLE_OAUTH_SIGNING_KEY -> $DATA_DIR/oauth_signing_key -> generated 0600."""
    env_key = os.environ.get("PEP_ORACLE_OAUTH_SIGNING_KEY", "").strip()
    if env_key:
        return env_key
    data_dir = Path(
        os.environ.get("PEP_ORACLE_DATA_DIR") or (Path.home() / ".pep-oracle")
    ).expanduser()
    key_path = data_dir / "oauth_signing_key"
    if key_path.exists():
        existing = key_path.read_text().strip()
        if existing:
            return existing
    data_dir.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_urlsafe(32)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key.encode("ascii"))
    finally:
        os.close(fd)
    logger.info("Generated new OAuth signing key at %s (mode 0600)", key_path)
    return new_key


def _resolve_ssm(param_name: str, region: str) -> str:
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    value = resp["Parameter"]["Value"].strip()
    if not value:
        raise RuntimeError(f"SSM signing-key parameter {param_name!r} is empty")
    logger.info("Loaded OAuth signing key from SSM parameter %s", param_name)
    return value
