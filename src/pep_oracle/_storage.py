"""Tiny storage dispatch: a URI is either a local filesystem path or s3://bucket/key.

Keeps corpus read/write code agnostic to where the artifact lives (local dev dir
vs S3 in prod). The S3 client is lazy so non-AWS installs never import boto3.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pep_oracle import config

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3", region_name=config.BEDROCK_REGION)
    return _s3_client


def is_s3(uri: str) -> bool:
    return str(uri).startswith("s3://")


def _split_s3(uri: str) -> tuple[str, str]:
    parts = urlparse(str(uri))
    return parts.netloc, parts.path.lstrip("/")


def put_bytes(uri: str, data: bytes) -> None:
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        _s3().put_object(Bucket=bucket, Key=key, Body=data)
    else:
        p = Path(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def get_bytes(uri: str) -> bytes:
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return Path(uri).read_bytes()


def put_text(uri: str, text: str) -> None:
    put_bytes(uri, text.encode("utf-8"))


def get_text(uri: str) -> str:
    return get_bytes(uri).decode("utf-8")
