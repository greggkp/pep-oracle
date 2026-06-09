"""Text embedding via AWS Bedrock.

`embed_texts()` embeds a list of strings using the configured Bedrock model
(default: amazon.titan-embed-text-v2:0, 1024-d). One InvokeModel call per text.
Query and corpus vectors must come from the same model — see corpus manifest
`embed_model`.
"""

from __future__ import annotations

import json
import time

from pep_oracle import config
from pep_oracle.timing import timed

_bedrock = None      # boto3 bedrock-runtime singleton

_MAX_RETRIES = 6
_BASE_BACKOFF = 0.5  # seconds; doubled each retry


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        # One-time, cold-only: boto3 import + client construction (credential +
        # endpoint resolution). Timed to separate it from the embed round-trip.
        with timed("embed.client_init"):
            import boto3

            _bedrock = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)
    return _bedrock


class _ThrottlingError(Exception):
    """Internal marker so the retry loop is testable without importing botocore."""


def _is_throttling(exc: Exception) -> bool:
    if isinstance(exc, _ThrottlingError):
        return True
    name = exc.__class__.__name__
    # ModelTimeoutException is a transient failure we also retry, not strictly throttling.
    return name in {"ThrottlingException", "TooManyRequestsException", "ModelTimeoutException"}


def _embed_one_bedrock(text: str) -> list[float]:
    body = json.dumps(
        {"inputText": text, "dimensions": config.EMBED_DIMS, "normalize": True}
    )
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _bedrock_client().invoke_model(modelId=config.EMBED_MODEL, body=body)
            return json.loads(resp["body"].read())["embedding"]
        except Exception as exc:  # noqa: BLE001 — retry only throttling, re-raise the rest
            if _is_throttling(exc) and attempt < _MAX_RETRIES - 1:
                time.sleep(_BASE_BACKOFF * (2 ** attempt))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [_embed_one_bedrock(t) for t in texts]
