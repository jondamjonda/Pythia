"""Thin boto3 wrapper for S3-compatible object storage (Hetzner Object Storage).

Used for:

* presigned PUT URLs so browsers upload documents (such as ČNB PDFs) directly to the bucket,
  and the server never holds the file in memory;
* parquet snapshots of embedding batch results, under ``intermediate/embeddings/``;
* the daily gzipped ``pg_dump`` backup, under ``backups/pgdump/``.

Configuration (environment):

* ``S3_ENDPOINT_URL``: for example ``https://fsn1.your-objectstorage.com``
* ``S3_ACCESS_KEY`` and ``S3_SECRET_KEY``: bucket credentials
* ``S3_BUCKET``: bucket name (default ``regsuper-mvp``)
* ``S3_REGION``: region code (default ``fsn1``)

`is_configured()` returns False when the endpoint or either credential is unset. Call it before
constructing a client so development environments without object storage still work.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from regsuper.config import get_settings


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str


def is_configured() -> bool:
    s = get_settings()
    return bool(s.s3_endpoint_url and s.s3_access_key and s.s3_secret_key)


def _settings() -> S3Settings:
    s = get_settings()
    if not is_configured():
        raise RuntimeError(
            "S3 not configured — set S3_ENDPOINT_URL / S3_ACCESS_KEY / "
            "S3_SECRET_KEY (and optionally S3_BUCKET, S3_REGION) in secrets.env"
        )
    return S3Settings(
        endpoint_url=s.s3_endpoint_url or "",
        access_key=s.s3_access_key or "",
        secret_key=s.s3_secret_key or "",
        bucket=s.s3_bucket,
        region=s.s3_region,
    )


def client() -> Any:
    """Return a boto3 client bound to the configured endpoint.

    Uses S3v4 signing because Hetzner Object Storage rejects v2.
    """
    cfg = _settings()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region,
        config=Config(signature_version="s3v4"),
    )


# ---------------------------------------------------------------------------
# Convenience operations
# ---------------------------------------------------------------------------
def put_bytes(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
    cfg = _settings()
    c = client()
    c.put_object(Bucket=cfg.bucket, Key=key, Body=data, ContentType=content_type)
    return f"s3://{cfg.bucket}/{key}"


def put_stream(
    key: str, fileobj: io.IOBase, *, content_type: str = "application/octet-stream"
) -> str:
    cfg = _settings()
    c = client()
    c.upload_fileobj(fileobj, cfg.bucket, key, ExtraArgs={"ContentType": content_type})
    return f"s3://{cfg.bucket}/{key}"


def get_bytes(key: str) -> bytes:
    cfg = _settings()
    c = client()
    resp = c.get_object(Bucket=cfg.bucket, Key=key)
    return resp["Body"].read()


def head(key: str) -> dict[str, Any] | None:
    """Return object metadata, or None if the key doesn't exist."""
    cfg = _settings()
    c = client()
    try:
        return c.head_object(Bucket=cfg.bucket, Key=key)
    except (ClientError, BotoCoreError):
        return None


def presign_put(
    key: str, *, content_type: str = "application/octet-stream", expires_seconds: int = 600
) -> str:
    """Return a presigned PUT URL for direct browser uploads."""
    cfg = _settings()
    c = client()
    return c.generate_presigned_url(
        "put_object",
        Params={"Bucket": cfg.bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_seconds,
        HttpMethod="PUT",
    )


def presign_get(key: str, *, expires_seconds: int = 600) -> str:
    cfg = _settings()
    c = client()
    return c.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg.bucket, "Key": key},
        ExpiresIn=expires_seconds,
        HttpMethod="GET",
    )


def ensure_bucket() -> str:
    """Create the bucket if it doesn't already exist. Idempotent."""
    cfg = _settings()
    c = client()
    existing = c.list_buckets()
    names = {b["Name"] for b in existing.get("Buckets", [])}
    if cfg.bucket not in names:
        c.create_bucket(Bucket=cfg.bucket)
    return cfg.bucket
